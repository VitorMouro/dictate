#!/usr/bin/env sh
# Started by XDG autostart, from inside the desktop session, so it has the session's
# environment. Neither unit can be autostarted by systemd alone: the user manager does
# not inherit DISPLAY on its own, and Cinnamon never activates graphical-session.target,
# so a unit wanted by it simply never runs.
set -e
systemctl --user import-environment \
    DISPLAY WAYLAND_DISPLAY XAUTHORITY XDG_SESSION_TYPE XDG_CURRENT_DESKTOP
exec systemctl --user restart dictate.service dictate-tray.service
