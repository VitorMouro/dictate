#!/usr/bin/env python3
"""End-to-end test of dictate on Linux Mint, in a QEMU VM with a working microphone.

Mint has no cloud image and preseeding its installer is fragile, so the base image is
installed by hand once (--install). Every automated run then boots a throwaway overlay
on top of it, which is what makes a run start from the same clean state every time.

Speech is injected rather than spoken. A null sink on the host is fed a WAV file, and
the VM's emulated microphone is that sink's monitor, so the run is deterministic and
needs nobody at the keyboard.

    ./mint-vm.py --install    # one time: boot the ISO and install Mint by hand
    ./mint-vm.py --setup      # one time: authorise the harness ssh key in the VM
    ./mint-vm.py --run        # automated end-to-end run on a fresh overlay
    ./mint-vm.py --gui        # boot the overlay in a window to poke at it yourself
    ./mint-vm.py --clean      # drop the overlay, keep the base image

Requires on the host: qemu, espeak-ng, ImageMagick, pipewire-pulse.
"""

import argparse
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

VMDIR = Path(os.environ.get("MINT_VMDIR", Path.home() / "mint-vm-test"))
ISO = Path(os.environ.get("MINT_ISO", VMDIR / "linuxmint-22.3-cinnamon-64bit.iso"))
BASE = VMDIR / "mint-base.qcow2"
OVERLAY = VMDIR / "mint-run.qcow2"
QMP_SOCK = VMDIR / "qmp.sock"
KEY = VMDIR / "id_vm"
SHOTS = VMDIR / "shots"
SAMPLE = VMDIR / "sample.wav"

SSH_PORT = int(os.environ.get("MINT_SSH_PORT", "2222"))
WEB_PORT = int(os.environ.get("MINT_WEB_PORT", "8123"))
GUEST_USER = os.environ.get("MINT_USER", "vitor")
REPO = os.environ.get("DICTATE_REPO", "https://github.com/VitorMouro/dictate")
SINK = "dictatest"
DISK_SIZE, MEM, SMP = "40G", "6144", "4"

SENTENCE = "the quick brown fox jumps over the lazy dog"
# systemctl --user and the X tools need a session context that ssh does not set up.
GUEST_ENV = ("export XDG_RUNTIME_DIR=/run/user/$(id -u); "
             "export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus; "
             "export DISPLAY=:0; "
             # The astral installer puts uv here and edits shell rc files, which a
             # non-interactive ssh command never sources.
             'export PATH="$HOME/.local/bin:$PATH"; ')

vm = None
sink_module = None


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg):
    log(f"FATAL: {msg}")
    sys.exit(1)


# ------------------------------------------------------------------ host audio ---

def host(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def load_sink():
    """A null sink whose monitor becomes the VM's microphone. Loaded per run and
    unloaded on the way out, so it never lingers in the user's session."""
    global sink_module
    existing = host(["pactl", "list", "short", "sinks"]).stdout
    if SINK in existing:
        log(f"null sink {SINK} already loaded, reusing")
        return
    r = host(["pactl", "load-module", "module-null-sink", f"sink_name={SINK}",
              f"sink_properties=device.description={SINK}"])
    if r.returncode:
        die(f"could not load null sink: {r.stderr.strip()}")
    sink_module = r.stdout.strip()
    log(f"loaded null sink {SINK} (module {sink_module})")


def unload_sink():
    global sink_module
    if sink_module:
        host(["pactl", "unload-module", sink_module])
        log(f"unloaded null sink module {sink_module}")
        sink_module = None


def make_sample():
    """espeak-ng is robotic but parakeet transcribes it fine, which keeps the run from
    depending on a recording of anyone's voice.

    The padding is not cosmetic: a null sink stops delivering frames once playback
    ends, so without trailing silence the VAD never sees the gap that closes an
    utterance and the speech is left buffered and never decoded."""
    if SAMPLE.exists():
        return
    raw = VMDIR / "sample-raw.wav"
    host(["espeak-ng", "-s", "140", "-g", "4", "-w", str(raw), SENTENCE])
    with wave.open(str(raw)) as w:
        params, frames = w.getparams(), w.readframes(w.getnframes())
    pad = b"\x00" * (params.sampwidth * params.nchannels * int(params.framerate * 1.5))
    with wave.open(str(SAMPLE), "wb") as o:
        o.setparams(params)
        o.writeframes(pad + frames + pad)
    raw.unlink()
    log(f"generated {SAMPLE.name}")


def inject():
    log("injecting speech into the VM microphone")
    host(["paplay", f"--device={SINK}", str(SAMPLE)])


# ------------------------------------------------------------------------ qemu ---

def qemu_args(disk, display, cdrom=None):
    args = [
        "qemu-system-x86_64",
        "-machine", "q35,accel=kvm", "-cpu", "host", "-smp", SMP, "-m", MEM,
        "-drive", f"file={disk},if=virtio,format=qcow2",
        "-netdev", f"user,id=n0,hostfwd=tcp::{SSH_PORT}-:22",
        "-device", "virtio-net-pci,netdev=n0",
        # Deliberately NOT virtio-gpu-gl. Under virgl the framebuffer lives as a host
        # GL texture and QMP screendump reads the emulated scanout, which stays empty -
        # every screenshot comes back blank. Cinnamon renders fine on plain virtio-gpu.
        "-vga", "none", "-device", "virtio-gpu-pci",
        "-audiodev", f"pipewire,id=snd0,in.name={SINK}.monitor",
        "-device", "ich9-intel-hda", "-device", "hda-micro,audiodev=snd0",
        "-qmp", f"unix:{QMP_SOCK},server,nowait",
        "-display", display,
    ]
    if cdrom:
        args += ["-cdrom", str(cdrom), "-boot", "d"]
    return args


def launch(disk, display, cdrom=None):
    global vm
    QMP_SOCK.unlink(missing_ok=True)
    args = qemu_args(disk, display, cdrom)
    log("qemu " + " ".join(args[1:]))
    vm = subprocess.Popen(args)
    return vm


def shutdown():
    global vm
    if vm and vm.poll() is None:
        vm.terminate()
        try:
            vm.wait(timeout=20)
        except subprocess.TimeoutExpired:
            vm.kill()
    vm = None


def qmp(command):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(QMP_SOCK))
        f = s.makefile("rwb")
        f.readline()
        for msg in ({"execute": "qmp_capabilities"}, command):
            f.write((json.dumps(msg) + "\n").encode())
            f.flush()
            while True:
                line = json.loads(f.readline())
                if "return" in line or "error" in line:
                    break
        return line


def screenshot(name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    ppm = SHOTS / f"{name}.ppm"
    png = SHOTS / f"{name}.png"
    try:
        qmp({"execute": "screendump", "arguments": {"filename": str(ppm)}})
    except OSError as e:
        log(f"  screenshot {name}: qmp failed ({e})")
        return None
    for _ in range(20):
        if ppm.exists() and ppm.stat().st_size:
            break
        time.sleep(0.25)
    magick = shutil.which("magick") or shutil.which("convert")
    if magick and ppm.exists():
        subprocess.run([magick, str(ppm), str(png)], capture_output=True)
        ppm.unlink(missing_ok=True)
        log(f"  screenshot -> {png}")
        return png
    return ppm


# ------------------------------------------------------------------------- ssh ---

SSH_BASE = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5"]


def guest(cmd, timeout=120, check=False):
    full = ["ssh", *SSH_BASE, "-i", str(KEY), "-p", str(SSH_PORT),
            f"{GUEST_USER}@127.0.0.1", GUEST_ENV + cmd]
    r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        die(f"guest command failed: {cmd}\n{r.stdout}\n{r.stderr}")
    return r


def wait_for_ssh(timeout=300):
    log("waiting for ssh")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if vm and vm.poll() is not None:
            die("qemu exited while waiting for ssh")
        if guest("true", timeout=10).returncode == 0:
            log("  ssh up")
            return True
        time.sleep(3)
    die("ssh never came up")


def wait_for_cinnamon(timeout=180):
    log("waiting for the Cinnamon session")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if guest("pgrep -x cinnamon >/dev/null && echo yes").stdout.strip() == "yes":
            log("  cinnamon running")
            return True
        time.sleep(3)
    log("  WARNING: no cinnamon process; the desktop may not have started")
    return False


def ensure_key():
    if not KEY.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(KEY),
                        "-C", "dictate-mint-vm"], check=True, capture_output=True)
        log(f"generated {KEY}")
    return (KEY.with_suffix(".pub")).read_text().strip()


def state():
    out = guest("cat $XDG_RUNTIME_DIR/dictate/status.json 2>/dev/null").stdout.strip()
    try:
        return json.loads(out).get("alt")
    except ValueError:
        return None


def wait_state(want, timeout=30):
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        s = state()
        if s and (not seen or seen[-1] != s):
            seen.append(s)
        if s == want:
            return True, seen
        time.sleep(0.4)
    return False, seen


def serve_vmdir():
    """Hands the public key to the guest, which reaches the host loopback at 10.0.2.2
    under user-mode networking. Typing an ed25519 key into a QEMU window by hand is
    not a reasonable ask. Bound to loopback so it is not exposed to the network."""
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(VMDIR), **kw)
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", WEB_PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"serving {VMDIR} on 127.0.0.1:{WEB_PORT} (guest sees 10.0.2.2:{WEB_PORT})")
    return httpd


# ----------------------------------------------------------------------- modes ---

SETUP_SH = """#!/usr/bin/env bash
# One-time setup so the harness can drive this VM over ssh.
set -e
sudo apt-get update
sudo apt-get install -y openssh-server
mkdir -p ~/.ssh && chmod 700 ~/.ssh
curl -fsS http://10.0.2.2:{port}/id_vm.pub >> ~/.ssh/authorized_keys
sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
echo '{user} ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/99-vmtest >/dev/null
sudo chmod 440 /etc/sudoers.d/99-vmtest
sudo systemctl enable --now ssh
echo
echo "SETUP COMPLETE - close this window."
"""


def write_setup_script():
    path = VMDIR / "setup.sh"
    path.write_text(SETUP_SH.format(port=WEB_PORT, user=GUEST_USER))
    return path


def do_setup():
    """Boot the installed base image so the one-time ssh setup can be done by hand.

    The base image, not an overlay: this has to persist. apt needs the account
    password, which the harness does not have, so a human types it once."""
    if not BASE.exists():
        die(f"no base image at {BASE}; run --install first")
    ensure_key()
    write_setup_script()
    print(f"""
─────────────────────────────────────────────────────────────────────────────
  A QEMU window is opening on the installed Mint system.

  1. Press Ctrl+Alt+T to open a terminal.
  2. Run this one line:

     curl -so /tmp/s.sh http://10.0.2.2:{WEB_PORT}/setup.sh && bash /tmp/s.sh

     It installs openssh-server, authorises the harness key and adds a
     passwordless-sudo drop-in. It will ask for your VM account password once.

  3. When it prints SETUP COMPLETE, close the QEMU window.

  Then: ./mint-vm.py --run
─────────────────────────────────────────────────────────────────────────────
""")
    httpd = serve_vmdir()
    launch(BASE, "gtk").wait()
    httpd.shutdown()
    log("setup window closed")


def do_install():
    VMDIR.mkdir(parents=True, exist_ok=True)
    if not ISO.exists():
        die(f"ISO not found at {ISO}; set MINT_ISO or download it there")
    ensure_key()
    if BASE.exists():
        log(f"{BASE} already exists; delete it first to reinstall")
    else:
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(BASE), DISK_SIZE],
                       check=True, capture_output=True)
        log(f"created {BASE} ({DISK_SIZE})")

    print(f"""
─────────────────────────────────────────────────────────────────────────────
  Installing Mint by hand, once. A QEMU window is opening.

  1. Run the installer on the desktop. Any defaults are fine EXCEPT:
       - Username MUST be: {GUEST_USER}
       - Tick "Log in automatically". Without it the VM sits at the LightDM
         greeter, there is no session on :0, and the automated run cannot
         drive the desktop at all.
  2. Reboot into the installed system when it asks, and log in.
  3. Open a terminal in the VM and run:

     sudo apt update && sudo apt install -y openssh-server
     mkdir -p ~/.ssh && chmod 700 ~/.ssh
     curl -s http://10.0.2.2:{WEB_PORT}/id_vm.pub >> ~/.ssh/authorized_keys
     chmod 600 ~/.ssh/authorized_keys
     echo '{GUEST_USER} ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/99-vmtest
     sudo chmod 440 /etc/sudoers.d/99-vmtest

     The sudoers line matters: the run installs packages over ssh, and without it
     apt blocks forever on a password prompt. This is a throwaway test VM.

  4. Close the QEMU window.

  Then: ./mint-vm.py --run
─────────────────────────────────────────────────────────────────────────────
""")
    httpd = serve_vmdir()
    load_sink()
    launch(BASE, "gtk", cdrom=ISO).wait()
    httpd.shutdown()
    log("installer window closed")


def fresh_overlay():
    if not BASE.exists():
        die(f"no base image at {BASE}; run --install first")
    OVERLAY.unlink(missing_ok=True)
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-b", str(BASE),
                    "-F", "qcow2", str(OVERLAY)], check=True, capture_output=True)
    log(f"fresh overlay on {BASE.name}")


def do_run(keep=False):
    VMDIR.mkdir(parents=True, exist_ok=True)
    make_sample()
    fresh_overlay()
    load_sink()
    launch(OVERLAY, "none")
    wait_for_ssh()
    wait_for_cinnamon()

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        log(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")

    log("installing dependencies in the guest")
    guest("sudo apt-get update -qq && sudo apt-get install -y -qq "
          "pulseaudio-utils xdotool sound-theme-freedesktop gir1.2-xapp-1.0 "
          "python3-gi git curl bzip2 xterm", timeout=900)
    guest("curl -LsSf https://astral.sh/uv/install.sh | sh", timeout=300)

    log("cloning and installing dictate")
    guest(f"rm -rf ~/dictate && git clone -q {REPO} ~/dictate", timeout=300)
    r = guest("~/dictate/install.sh", timeout=1800)
    check("install.sh exits clean", r.returncode == 0,
          (r.stderr or r.stdout).strip().splitlines()[-1] if r.returncode else "")

    check("daemon active",
          guest("systemctl --user is-active dictate.service").stdout.strip() == "active")
    check("tray active",
          guest("systemctl --user is-active dictate-tray.service").stdout.strip() == "active")

    backend = guest("journalctl --user -u dictate.service -n 50 --no-pager -o cat "
                    "| grep -m1 '^typing:'").stdout.strip()
    check("typing backend is xdotool", backend == "typing: xdotool", backend)

    check("status file published", state() == "off", f"state={state()}")
    screenshot("01-idle")

    # The shipped default is 5s, which is fine in use but races the test: arming,
    # focusing a window and starting playback has to fit inside it. Widen it here so a
    # slow VM does not read as a failure. The default itself is asserted separately.
    shipped = guest("grep -c '^idle_timeout = 5.0' ~/dictate/settings.toml").stdout.strip()
    check("shipped idle_timeout is 5s", shipped == "1", f"grep count={shipped}")
    guest("sed -i 's/^idle_timeout = .*/idle_timeout = 30.0/' ~/.config/dictate/settings.toml")

    log("opening a window to catch the typed text")
    guest("rm -f /tmp/typed.txt")
    guest("nohup xterm -e sh -c 'cat > /tmp/typed.txt' >/dev/null 2>&1 &")
    time.sleep(3)
    focused = guest("xdotool search --class XTerm windowactivate --sync "
                    "&& xdotool getactivewindow getwindowname")
    check("capture window focused", focused.returncode == 0, focused.stdout.strip())

    log("toggling on via the control FIFO")
    guest("sh -c 'F=\"$XDG_RUNTIME_DIR/dictate/control\"; test -p \"$F\" && echo > \"$F\"'")
    ok, seen = wait_state("listening", 20)
    check("toggles to listening", ok, f"seen={seen}")
    screenshot("02-listening")

    inject()
    ok, seen = wait_state("transcribing", 25)
    check("reaches transcribing", ok, f"seen={seen}")
    screenshot("03-transcribing")

    time.sleep(6)
    typed = guest("cat /tmp/typed.txt 2>/dev/null").stdout.strip()
    words = set(SENTENCE.split())
    hit = len(words & set(typed.lower().replace(".", "").split()))
    check("typed text matches speech", hit >= len(words) - 3,
          f"{hit}/{len(words)} words, got {typed!r}")

    screenshot("04-after")
    guest("systemctl --user stop dictate.service")
    check("status file removed on exit", state() is None)

    log("")
    log("=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        log(f"{'PASS' if ok else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")
    log(f"{passed}/{len(results)} checks passed")
    log(f"screenshots in {SHOTS}")
    log("NOTE: the tray mouse click is not covered here; run --gui and click it.")
    log("=" * 60)

    if not keep:
        shutdown()
    return all(ok for _, ok, _ in results)


def do_gui():
    if not OVERLAY.exists():
        fresh_overlay()
    make_sample()
    load_sink()
    proc = launch(OVERLAY, "gtk")
    log("VM window open. Click the microphone icon in the panel to test the mouse "
        "toggle. Close the window when done.")
    log(f"To feed it speech from here: paplay --device={SINK} {SAMPLE}")
    proc.wait()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true", help="one-time manual Mint install")
    g.add_argument("--run", action="store_true", help="automated end-to-end run")
    g.add_argument("--setup", action="store_true",
                   help="boot the base image to do the one-time ssh setup")
    g.add_argument("--gui", action="store_true", help="boot the overlay in a window")
    g.add_argument("--clean", action="store_true", help="drop the overlay")
    ap.add_argument("--keep", action="store_true", help="leave the VM up after --run")
    args = ap.parse_args()

    VMDIR.mkdir(parents=True, exist_ok=True)
    try:
        if args.install:
            do_install()
        elif args.setup:
            do_setup()
        elif args.gui:
            do_gui()
        elif args.clean:
            OVERLAY.unlink(missing_ok=True)
            log("overlay removed")
        else:
            sys.exit(0 if do_run(keep=args.keep) else 1)
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        shutdown()
        unload_sink()
        QMP_SOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
