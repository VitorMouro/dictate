#!/usr/bin/env python3
"""Tray indicator for the dictation daemon. Reads the status file the daemon publishes
and toggles it through the same control FIFO the keyboard shortcut writes to, so the
mouse and the keybind are the same code path on the daemon side.

Run under the system python3, not uv: this needs the distro's PyGObject and XApp
typelibs, which an isolated venv cannot see."""

import html
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("XApp", "1.0")
from gi.repository import Gio, Gtk, XApp  # noqa: E402

RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "dictate"
STATUS = RUNTIME / "status.json"
CONTROL = RUNTIME / "control"

ICONS = {
    "off": "microphone-disabled-symbolic",
    "listening": "microphone-sensitivity-low-symbolic",
    "speech": "microphone-sensitivity-high-symbolic",
    "transcribing": "emblem-synchronizing-symbolic",
}
DOWN = "microphone-disabled-symbolic"
TAGS = re.compile(r"<[^>]+>")


def toggle(*_):
    """Same contract as the keybind: one line into the FIFO, and a no-op rather than a
    blocking open when the daemon is down. O_NONBLOCK on a FIFO with no reader fails
    with ENXIO, which is exactly the "daemon is down" case."""
    try:
        fd = os.open(CONTROL, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        os.write(fd, b"\n")
    except OSError:
        pass
    finally:
        os.close(fd)


def restart_daemon(*_):
    subprocess.Popen(["systemctl", "--user", "restart", "dictate.service"])


def read_status():
    """The daemon writes waybar's payload: `alt` is the state, `tooltip` is Pango
    markup. Strip the markup, since a tray tooltip is plain text."""
    try:
        data = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return None, "Dictation daemon is not running"
    tooltip = html.unescape(TAGS.sub("", data.get("tooltip", ""))).strip()
    return data.get("alt"), tooltip or "Dictation"


class Tray:
    def __init__(self):
        self.icon = XApp.StatusIcon()
        # Must be a valid 4-part name prefixed org.x.StatusIcon, or XApp ignores it and
        # falls back to "org.x.StatusIcon.<prgname>". For a python script that resolves
        # to a name Mint's own python-based tray icons already own, and since XApp asks
        # for it with DO_NOT_QUEUE the loser simply never appears.
        self.icon.set_name("org.x.StatusIcon.dictate")
        self.icon.connect("activate", toggle)
        # Only a secondary menu. XApp skips "activate" for whichever button has a menu
        # attached, so setting a primary one here would silently kill left-click.
        self.icon.set_secondary_menu(self.build_menu())

        RUNTIME.mkdir(parents=True, exist_ok=True)
        # Watch the directory, not the file. The daemon publishes with an atomic
        # rename and unlinks the file on exit, so a file monitor would be left holding
        # an inode that no longer has a name.
        self.monitor = Gio.File.new_for_path(str(RUNTIME)).monitor_directory(
            Gio.FileMonitorFlags.NONE, None)
        self.monitor.connect("changed", self.on_change)
        self.refresh()

    def build_menu(self):
        menu = Gtk.Menu()
        for label, handler in (("Toggle dictation", toggle),
                               ("Restart daemon", restart_daemon),
                               ("Quit tray", lambda *_: Gtk.main_quit())):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", handler)
            menu.append(item)
        menu.show_all()
        return menu

    def on_change(self, _monitor, gfile, _other, _event):
        if gfile.get_basename() == STATUS.name:
            self.refresh()

    def refresh(self):
        state, tooltip = read_status()
        self.icon.set_icon_name(ICONS.get(state, DOWN))
        self.icon.set_tooltip_text(tooltip)


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        # Exit 0, not an error: Restart=on-failure must not spin here.
        print("no display; not starting the tray", file=sys.stderr, flush=True)
        return
    # Autostart can beat the display server to it by a moment. Retry rather than exit
    # non-zero, which would otherwise log a failure on every login before recovering.
    for attempt in range(15):
        if Gtk.init_check()[0]:
            break
        time.sleep(1)
    else:
        print("no usable display after 15s; not starting the tray",
              file=sys.stderr, flush=True)
        return
    Tray()
    Gtk.main()


if __name__ == "__main__":
    main()
