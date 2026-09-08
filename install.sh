#!/usr/bin/env bash
# Checks dependencies, fetches the speech models, links config and units into place,
# and enables the services. Idempotent - safe to re-run after a git pull.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS="${XDG_DATA_HOME:-$HOME/.local/share}/dictate/models"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/dictate"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
ASR=https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models
PARAKEET=sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- dependencies ---
missing=()
need() { command -v "$1" >/dev/null 2>&1 || missing+=("$1 - $2"); }

need uv       "curl -LsSf https://astral.sh/uv/install.sh | sh"
need parecord "apt install pulseaudio-utils"
need paplay   "apt install pulseaudio-utils"
need pactl    "apt install pulseaudio-utils"
need curl     "apt install curl"
need bzip2    "apt install bzip2"

# The daemon picks its typing tool the same way, testing Wayland first: DISPLAY is set
# under Xwayland too, so keying on it would pick xdotool on a Wayland session.
if [ -n "${WAYLAND_DISPLAY:-}" ] || [ "${XDG_SESSION_TYPE:-}" = wayland ]; then
    need wtype "apt install wtype"
else
    need xdotool "apt install xdotool"
fi

[ -f /usr/share/sounds/freedesktop/stereo/device-added.oga ] \
    || missing+=("freedesktop sound theme - apt install sound-theme-freedesktop")

python3 -c 'import gi; gi.require_version("XApp", "1.0"); from gi.repository import XApp' \
    2>/dev/null || missing+=("XApp python bindings - apt install gir1.2-xapp-1.0 python3-gi")

if [ ${#missing[@]} -gt 0 ]; then
    printf '\033[1;31mmissing dependencies:\033[0m\n' >&2
    printf '  %s\n' "${missing[@]}" >&2
    exit 1
fi
say "dependencies ok"

# ---------------------------------------------------------------------- models ---
# ~640MB. Guarded on the unpacked files so a re-run does not re-download.
mkdir -p "$MODELS"
if [ ! -f "$MODELS/silero_vad.onnx" ]; then
    say "fetching the Silero VAD model"
    curl -fL --progress-bar -o "$MODELS/silero_vad.onnx" "$ASR/silero_vad.onnx"
fi
if [ ! -f "$MODELS/$PARAKEET/encoder.int8.onnx" ]; then
    say "fetching parakeet-tdt (~640MB, one time)"
    curl -fL --progress-bar "$ASR/$PARAKEET.tar.bz2" | tar -xj -C "$MODELS"
fi
say "models in $MODELS"

# ------------------------------------------------------------ config and units ---
mkdir -p "$CONF" "$UNITS"
ln -sfn "$REPO/dictate.py" "$CONF/dictate.py"
ln -sfn "$REPO/tray.py"    "$CONF/tray.py"

# Copied, not linked: this is the file you edit, and a symlink would turn every tweak
# into a dirty working tree.
[ -f "$CONF/settings.toml" ] || cp "$REPO/settings.toml" "$CONF/settings.toml"

ln -sfn "$REPO/dictate.service"      "$UNITS/dictate.service"
ln -sfn "$REPO/dictate-tray.service" "$UNITS/dictate-tray.service"

systemctl --user daemon-reload
systemctl --user enable --now dictate.service
systemctl --user enable --now dictate-tray.service
say "services enabled"

cat <<'DONE'

Installed. The mic is off until you toggle it.

  Mouse:    left-click the microphone icon in the panel.
  Keyboard: bind a shortcut to this command -
            sh -c 'F="$XDG_RUNTIME_DIR/dictate/control"; test -p "$F" && echo > "$F"'

  On Cinnamon: System Settings -> Keyboard -> Shortcuts -> Custom Shortcuts -> Add.

  Tune it in ~/.config/dictate/settings.toml. It is re-read on every toggle, so no
  restart is needed - the speech model stays loaded.

  Logs: journalctl --user -u dictate.service -f
DONE
