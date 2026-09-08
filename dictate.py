#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["sherpa-onnx==1.13.6", "numpy"]
# ///
"""Dictation daemon. Writing a line to the control FIFO toggles listening; while
listening, Silero VAD carves the stream into utterances, parakeet-tdt transcribes each
one when it ends, and the session's typing tool types the result.
Requires: uv, parecord/paplay/pactl (pulseaudio-utils), models under MODELS,
sound-theme-freedesktop, and wtype on Wayland or xdotool on X11. swayosd-client is
optional and skipped when absent."""

import html
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import numpy as np
import sherpa_onnx

MODELS = Path.home() / ".local/share/dictate/models"
PARAKEET = MODELS / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
SILERO = MODELS / "silero_vad.onnx"
SOUNDS = Path("/usr/share/sounds/freedesktop/stereo")
SETTINGS = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dictate/settings.toml"

DEFAULTS = {
    "vad_threshold": 0.7,
    "min_silence_duration": 0.35,
    "min_speech_duration": 0.25,
    "max_speech_duration": 15.0,
    "idle_timeout": 10.0,
    "sound_on": "device-added",
    "sound_off": "device-removed",
}

SAMPLE_RATE = 16000
CHUNK = 512                 # VAD window, in samples (32ms) — also the state-change latency
NUM_THREADS = min(4, os.cpu_count() or 4)   # leave room for the rest of the session
MAX_LISTEN = 300.0          # hard ceiling: any room noise re-arms idle_timeout, so that
                            # alone does not guarantee the mic ever closes. This does.

RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "dictate"
STATUS = RUNTIME / "status.json"
CONTROL = RUNTIME / "control"
WAYBAR_SIGNAL = 8

HEADLINE = {
    "off": "Click the tray icon or press your shortcut to start",
    "listening": "Waiting for speech",
    "speech": "Capturing speech",
    "transcribing": "Transcribing",
}

published_state = None
published_payload = None
waybar_pid = None
type_cmd = None
osd_cmd = None
mic_name = ""
settings = dict(DEFAULTS)
session = {"utterances": 0, "words": 0, "text": "", "decode": 0.0, "audio": 0.0}


def load_settings():
    values = dict(DEFAULTS)
    try:
        with open(SETTINGS, "rb") as f:
            values.update({k: v for k, v in tomllib.load(f).items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"settings: {e}; using defaults", file=sys.stderr, flush=True)
    return values


def run(cmd, timeout=5):
    """Nothing external is allowed to wedge the loop: the OSD and the bar are
    cosmetic, and a wtype that never returns would freeze dictation outright."""
    try:
        subprocess.run(cmd, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"{cmd[0]}: {e}", file=sys.stderr, flush=True)


def resolve_type_cmd():
    """Wayland is tested first because DISPLAY is set under Xwayland too - keying on it
    would pick xdotool on a Wayland session and type into nothing."""
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        cmd = ["wtype", "--"]
    elif os.environ.get("DISPLAY"):
        # xdotool types at 12ms/key by default, slow enough to watch on a long sentence.
        cmd = ["xdotool", "type", "--clearmodifiers", "--delay", "1", "--"]
    else:
        print("no WAYLAND_DISPLAY or DISPLAY: transcribing but not typing",
              file=sys.stderr, flush=True)
        return None
    if not shutil.which(cmd[0]):
        print(f"{cmd[0]} not found: transcribing but not typing",
              file=sys.stderr, flush=True)
        return None
    return cmd


def find_waybar():
    """`pkill -RTMIN+8 waybar` kills a waybar that has not installed its handlers yet,
    and waybar restarts on resume here — so a toggle at the wrong moment would take the
    bar down. Only ever signal a process whose SigCgt proves it will catch it."""
    caught = 1 << (signal.SIGRTMIN + WAYBAR_SIGNAL - 1)
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if (proc / "comm").read_text().strip() != "waybar":
                continue
            line = next(l for l in (proc / "status").read_text().splitlines()
                        if l.startswith("SigCgt:"))
            if int(line.split()[1], 16) & caught:
                return int(proc.name)
        except (OSError, ValueError, StopIteration):
            continue
    return None


def notify_waybar():
    global waybar_pid
    for attempt in (waybar_pid, None):
        pid = attempt if attempt is not None else find_waybar()
        if pid is None:
            continue
        try:
            os.kill(pid, signal.SIGRTMIN + WAYBAR_SIGNAL)
            waybar_pid = pid
            return
        except OSError:
            waybar_pid = None


def tooltip(state, countdown):
    head = HEADLINE[state]
    if state == "listening" and countdown is not None:
        head += f" · auto-off in {countdown}s"
    lines = [f"<b>Dictation — {state}</b>", head]
    if session["utterances"]:
        text = session["text"]
        if len(text) > 64:
            text = text[:63] + "…"
        lines += [
            "",
            f"Last: <i>{html.escape(text)}</i>",
            f"Session: {session['utterances']} utterances, {session['words']} words",
            f"Decode: {session['decode']:.2f}s for {session['audio']:.1f}s of audio",
        ]
    lines += ["", f"VAD threshold: {settings['vad_threshold']:.2f}"]
    if mic_name:
        lines.append(f"Mic: {html.escape(mic_name)}")
    return "\n".join(lines)


def publish(state, countdown=None):
    """Called every 32ms frame. Rewrite only when the rendered payload actually changes,
    and wake the bar only on a state change — the countdown ticking is picked up by the
    module's own 2s poll, which does not need a signal per second."""
    global published_state, published_payload
    payload = json.dumps({"text": "", "alt": state, "class": state,
                          "tooltip": tooltip(state, countdown)})
    if payload == published_payload:
        return
    published_payload = payload
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(payload)
    tmp.replace(STATUS)
    if state != published_state:
        published_state = state
        notify_waybar()


def osd(message, icon):
    if osd_cmd:
        run([osd_cmd, "--custom-message", message, "--custom-icon", icon], timeout=2)


def sound(name):
    """Blocking on purpose: the on-cue has to finish before parecord opens, or the VAD
    latches onto the chime and transcribes it. The off-cue plays once the mic is shut,
    for the same reason. Empty name disables the cue."""
    if not name:
        return
    run(["paplay", str(SOUNDS / f"{name}.oga")], timeout=3)


def default_source():
    """The device parecord must be told to open. Passed explicitly because pulse's
    module-stream-restore otherwise pins the stream to whatever device an application
    called "parecord" used last - which can be a monitor of the speakers, silently
    transcribing system audio instead of the mic. pw-record had no such layer."""
    try:
        return subprocess.run(["pactl", "get-default-source"], capture_output=True,
                              text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def default_source_name(name=None):
    """The readable description of the source, so the tooltip says which mic is live."""
    name = name or default_source()
    if not name:
        return ""
    try:
        raw = subprocess.run(["pactl", "--format=json", "list", "sources"],
                             capture_output=True, text=True, timeout=2).stdout
        return next((s.get("description", name) for s in json.loads(raw)
                     if s.get("name") == name), name)
    except (OSError, ValueError, subprocess.TimeoutExpired, StopIteration):
        return name


def open_control():
    """A FIFO, not SIGUSR1: `systemctl kill` signals the whole cgroup, so a toggle also
    hit parecord. That killed the mic, listen() exited down the "capture died" path
    without consuming the toggle, and main() restarted listening immediately — the
    keybind looked like it did nothing. The second open keeps a writer alive so select
    never sees a permanent EOF once a client disconnects."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    CONTROL.unlink(missing_ok=True)
    os.mkfifo(CONTROL, 0o600)
    fd = os.open(CONTROL, os.O_RDONLY | os.O_NONBLOCK)
    os.open(CONTROL, os.O_WRONLY)
    return fd


def build_recognizer():
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(PARAKEET / "encoder.int8.onnx"),
        decoder=str(PARAKEET / "decoder.int8.onnx"),
        joiner=str(PARAKEET / "joiner.int8.onnx"),
        tokens=str(PARAKEET / "tokens.txt"),
        model_type="nemo_transducer",
        num_threads=NUM_THREADS,
        sample_rate=SAMPLE_RATE,
        feature_dim=80,
        decoding_method="greedy_search",
    )


def build_vad():
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(SILERO)
    config.silero_vad.threshold = settings["vad_threshold"]
    config.silero_vad.min_silence_duration = settings["min_silence_duration"]
    config.silero_vad.min_speech_duration = settings["min_speech_duration"]
    config.silero_vad.max_speech_duration = settings["max_speech_duration"]
    config.sample_rate = SAMPLE_RATE
    return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)


def transcribe(recognizer, samples):
    stream = recognizer.create_stream()
    stream.accept_waveform(SAMPLE_RATE, samples)
    recognizer.decode_stream(stream)
    return stream.result.text.strip()


class Typist:
    """Types each finished utterance, spaced off the one before it."""

    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.started = False

    def emit(self, words):
        if not words:
            return
        text = " ".join(words)
        if self.dry_run:
            print(f"  + {text}", file=sys.stderr, flush=True)
        elif type_cmd:
            run(type_cmd + [text if not self.started else " " + text])
        self.started = True


def listen(recognizer, control, dry_run):
    global mic_name, settings
    settings = load_settings()
    session.update(utterances=0, words=0, text="", decode=0.0, audio=0.0)
    source = default_source()
    mic_name = default_source_name(source)
    vad = build_vad()
    typist = Typist(dry_run)
    publish("listening")
    osd("Dictation on", "audio-input-microphone-symbolic")
    sound(settings["sound_on"])
    # No trailing "-": parecord takes that as a literal filename and writes a file
    # called "-" instead of feeding stdout. Omitting the arg is what selects stdout,
    # unlike pw-record.
    capture = ["parecord", "--raw", f"--rate={SAMPLE_RATE}", "--channels=1",
               "--format=s16le", "--latency-msec=20"]
    if source:
        capture.append(f"--device={source}")
    mic = subprocess.Popen(capture, stdout=subprocess.PIPE, bufsize=0)
    mic_fd = mic.stdout.fileno()
    pending = bytearray()
    started_at = last_speech = time.monotonic()
    try:
        while True:
            # Timed wait, so idle_timeout and MAX_LISTEN are still evaluated if the
            # capture stalls without reaching EOF — an unplugged mic would otherwise
            # leave the daemon parked in "listening" until the next toggle.
            ready, _, _ = select.select([mic_fd, control], [], [], 0.25)
            if control in ready:
                os.read(control, 4096)
                break

            if mic_fd in ready:
                data = os.read(mic_fd, 8192)
                if not data:
                    print("parecord ended", file=sys.stderr, flush=True)
                    break
                pending += data

            while len(pending) >= CHUNK * 2:
                frame = np.frombuffer(pending[:CHUNK * 2], dtype=np.int16)
                del pending[:CHUNK * 2]
                vad.accept_waveform(frame.astype(np.float32) / 32768)

                while not vad.empty():
                    samples = vad.front.samples
                    vad.pop()
                    publish("transcribing")
                    started = time.monotonic()
                    text = transcribe(recognizer, samples)
                    elapsed = time.monotonic() - started
                    words = text.split()
                    if words:
                        typist.emit(words)
                        session["utterances"] += 1
                        session["words"] += len(words)
                        session["text"] = text
                        session["decode"] = elapsed
                        session["audio"] = len(samples) / SAMPLE_RATE
                    # Decoding is blocking and can run into seconds; without this the
                    # idle countdown would have been ticking throughout it.
                    last_speech = time.monotonic()

                now = time.monotonic()
                speaking = vad.is_speech_detected()
                if speaking:
                    last_speech = now
                publish("speech" if speaking else "listening",
                        max(0, int(settings["idle_timeout"] - (now - last_speech))))

            now = time.monotonic()
            if now - last_speech > settings["idle_timeout"] or now - started_at > MAX_LISTEN:
                break
    finally:
        mic.terminate()
        mic.wait()
        osd("Dictation off", "microphone-sensitivity-muted-symbolic")
        sound(settings["sound_off"])


def main():
    global mic_name, settings, type_cmd, osd_cmd
    dry_run = "--dry-run" in sys.argv
    type_cmd = resolve_type_cmd()
    osd_cmd = shutil.which("swayosd-client")
    print(f"typing: {type_cmd[0] if type_cmd else 'disabled'}", file=sys.stderr, flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    control = open_control()
    settings = load_settings()
    mic_name = default_source_name()   # so the idle tooltip already names the mic

    recognizer = build_recognizer()
    print("ready", file=sys.stderr, flush=True)

    while True:
        publish("off")
        select.select([control], [], [])
        os.read(control, 4096)
        listen(recognizer, control, dry_run)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        STATUS.unlink(missing_ok=True)
        CONTROL.unlink(missing_ok=True)
        notify_waybar()
