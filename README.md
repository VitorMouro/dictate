# dictate

Offline push-to-talk dictation for Linux. Toggle it on, talk, and the words are typed
into whatever window has focus. Nothing leaves the machine.

Voice activity detection (Silero) splits the mic stream into utterances and
parakeet-tdt-0.6b-v3 transcribes each one as it ends, so text lands sentence by
sentence while you are still talking rather than in one dump at the end. The model
covers 25 languages with punctuation and casing, auto-detected. It runs int8 on the
CPU at roughly 0.07 RTF, so no GPU is needed.

The daemon stays resident with the model loaded. Toggling only opens the microphone,
which is what makes the shortcut feel instant.

## Requirements

- `uv` - not in the Mint or Debian repos: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `pulseaudio-utils` - `parecord`, `paplay`, `pactl`. Works on PulseAudio and on PipeWire.
- `xdotool` on X11, or `wtype` on Wayland
- `sound-theme-freedesktop` - the toggle chimes
- `gir1.2-xapp-1.0` and `python3-gi` - the tray icon

On Linux Mint:

```sh
sudo apt install pulseaudio-utils xdotool sound-theme-freedesktop gir1.2-xapp-1.0 python3-gi
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Install

```sh
git clone https://github.com/VitorMouro/dictate ~/dictate
~/dictate/install.sh
```

It checks the dependencies, downloads the speech models (~640MB, once), links the
config and the systemd user units into place, and starts the services. Re-running it
after a `git pull` is safe and will not re-download anything.

## Use

The microphone is off until you toggle it.

- **Mouse**: left-click the microphone icon in the panel. Right-click for a menu.
- **Keyboard**: bind a shortcut to

  ```sh
  sh -c 'F="$XDG_RUNTIME_DIR/dictate/control"; test -p "$F" && echo > "$F"'
  ```

  On Cinnamon: System Settings -> Keyboard -> Shortcuts -> Custom Shortcuts -> Add.
  Super+Space is a reasonable choice.

The tray icon tracks four states, and a chime plays when the mic opens and closes:

| icon | meaning |
|---|---|
| muted mic | off |
| low mic | listening, no speech heard yet |
| high mic | speech detected, capturing |
| sync arrows | decoding |

Listening stops on its own after `idle_timeout` seconds of silence, so a forgotten
toggle does not leave the mic open.

## Settings

`~/.config/dictate/settings.toml`, re-read on every toggle. Edit it and press the
shortcut again to apply; no restart, the model stays loaded.

| key | what it does |
|---|---|
| `vad_threshold` | Probability above which a 32ms frame counts as speech. Lower is more sensitive. 0.5 is the Silero default. |
| `min_silence_duration` | Silence that ends an utterance. Raise it if sentences get chopped while you think. |
| `min_speech_duration` | Utterances shorter than this are dropped. |
| `max_speech_duration` | Longest single utterance before it is force-ended and transcribed. |
| `idle_timeout` | Listening turns itself off after this long with no speech. |
| `sound_on` / `sound_off` | Toggle chimes, named from the freedesktop sound theme. Set to `""` for a silent toggle. |

## Other desktops

The daemon picks its typing tool from the session, so it works on Wayland with `wtype`
as well as on X11. The tray is XApp, which is native on Cinnamon and works anywhere
that shows a status icon.

If you run waybar, skip the tray unit and read the status file directly. The daemon
writes waybar's own JSON payload to `$XDG_RUNTIME_DIR/dictate/status.json` and sends
`SIGRTMIN+8` on every state change:

```jsonc
"custom/dictation": {
    "exec": "cat $XDG_RUNTIME_DIR/dictate/status.json",
    "exec-if": "test -f $XDG_RUNTIME_DIR/dictate/status.json",
    "return-type": "json",
    "interval": 2,
    "signal": 8,
    "format": "{icon}",
    "format-icons": { "off": " ", "listening": " ", "speech": " ", "transcribing": " " },
    "on-click": "sh -c 'F=\"$XDG_RUNTIME_DIR/dictate/control\"; test -p \"$F\" && echo > \"$F\"'"
}
```

Hyprland bind:

```
bind = SUPER, SPACE, exec, sh -c 'F="$XDG_RUNTIME_DIR/dictate/control"; test -p "$F" && echo > "$F"'
```

`test -p` makes the key a no-op when the daemon is down, instead of blocking the shell
on a FIFO nobody is reading.

## Troubleshooting

```sh
journalctl --user -u dictate.service -f
```

The daemon logs its typing backend at startup (`typing: xdotool`). If it says
`typing: disabled`, the tool for your session type is not installed and it will
transcribe without typing.

To check the speech pipeline without typing anything:

```sh
systemctl --user stop dictate.service
~/dictate/dictate.py --dry-run
```

Stop the service first. The control FIFO lives at a fixed path, so a second instance
takes it over and the running one stops responding to the shortcut.
