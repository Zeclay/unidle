"""Unidle — keeps Microsoft Teams (or any app) from going idle.

Sends an invisible F15 keypress on an interval to reset the OS idle timer.
Lives entirely in the system tray/menu bar. No credentials, no network,
no interaction with Teams itself.
"""

import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

from PIL import Image, ImageDraw
import pystray
from pynput.keyboard import Controller, Key

# ---------------------------------------------------------------------------
# Config — edit these to change defaults. Everything here is a plain
# constant so it is easy to find and tweak in one place.
# ---------------------------------------------------------------------------

APP_NAME = "Unidle"

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".unidle.json")
LOG_PATH = os.path.join(os.path.expanduser("~"), ".unidle_log.jsonl")
LAST_KEYPRESS_PATH = os.path.join(os.path.expanduser("~"), ".unidle_last.json")
LOG_MAX_LINES = 500
LOG_TRUNCATE_CHECK_INTERVAL = 100  # only check/truncate every N appends; cheap
SINGLE_INSTANCE_PORT = 58731  # localhost-only mutex; OS frees it if we crash

DEFAULT_INTERVAL = 60  # seconds between keypresses
INTERVAL_CHOICES = [30, 60, 120, 300]  # 30s / 1min / 2min / 5min

RANDOMIZE_DEFAULT = True
RANDOMIZE_JITTER = 0.25  # +/- 25% of the chosen interval

WORKING_HOURS_ENABLED_DEFAULT = False
WORKING_HOURS_START = "09:00"
WORKING_HOURS_END = "18:00"
WORKING_DAYS = {0, 1, 2, 3, 4}  # Monday=0 .. Sunday=6

AUTO_STOP_CHOICES = {"Off": 0, "1h": 3600, "4h": 4 * 3600, "8h": 8 * 3600}
AUTO_STOP_DEFAULT = "Off"

NOTIFICATIONS_DEFAULT = False

TICK_SECONDS = 1  # worker loop resolution; lets toggles/interval react fast
ICON_SIZE = 64  # rendered large then downsampled for crisp HiDPI icons

SETTINGS_KEYS = (
    "running",
    "interval",
    "randomize",
    "working_hours_enabled",
    "working_hours_start",
    "working_hours_end",
    "working_days",
    "auto_stop_choice",
    "notifications_enabled",
)


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

def default_settings():
    return {
        "running": True,
        "interval": DEFAULT_INTERVAL,
        "randomize": RANDOMIZE_DEFAULT,
        "working_hours_enabled": WORKING_HOURS_ENABLED_DEFAULT,
        "working_hours_start": WORKING_HOURS_START,
        "working_hours_end": WORKING_HOURS_END,
        "working_days": sorted(WORKING_DAYS),
        "auto_stop_choice": AUTO_STOP_DEFAULT,
        "notifications_enabled": NOTIFICATIONS_DEFAULT,
    }


def _is_valid_time_str(value):
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value, "%H:%M")
        return True
    except ValueError:
        return False


def _is_valid_days(value):
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(isinstance(d, int) and 0 <= d <= 6 for d in value)
    )


def load_settings():
    """Load settings from disk. Missing/corrupt file -> silent defaults."""
    settings = default_settings()
    first_run = not os.path.exists(CONFIG_PATH)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in SETTINGS_KEYS:
            if key in data:
                settings[key] = data[key]
        if settings["interval"] not in INTERVAL_CHOICES:
            settings["interval"] = DEFAULT_INTERVAL
        if settings["auto_stop_choice"] not in AUTO_STOP_CHOICES:
            settings["auto_stop_choice"] = AUTO_STOP_DEFAULT
        if not _is_valid_time_str(settings["working_hours_start"]):
            settings["working_hours_start"] = WORKING_HOURS_START
        if not _is_valid_time_str(settings["working_hours_end"]):
            settings["working_hours_end"] = WORKING_HOURS_END
        if not _is_valid_days(settings["working_days"]):
            settings["working_days"] = sorted(WORKING_DAYS)
    except Exception:
        pass
    return settings, first_run


def save_settings(data):
    """Atomic write: write to a temp file then rename over the real one."""
    try:
        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        pass


def _config_mtime():
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# One-time migration from the pre-rename "Always Online" file names. Runs
# before anything else touches CONFIG_PATH/LOG_PATH/LAST_KEYPRESS_PATH so
# existing users keep their settings and activity history across the
# rename. Best-effort and silent: if a rename fails for any reason, we just
# fall back to defaults/empty like any other missing file — never crash
# startup over this.
# ---------------------------------------------------------------------------

def migrate_legacy_files():
    home = os.path.expanduser("~")
    legacy_pairs = (
        (os.path.join(home, ".always_online.json"), CONFIG_PATH),
        (os.path.join(home, ".always_online_log.jsonl"), LOG_PATH),
        (os.path.join(home, ".always_online_last.json"), LAST_KEYPRESS_PATH),
    )
    for old_path, new_path in legacy_pairs:
        try:
            if os.path.exists(old_path) and not os.path.exists(new_path):
                os.replace(old_path, new_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Activity log — append-only, capped, read by the Settings window's
# Activity card. Format is a shared contract with settings_ui.py: one JSON
# object per line, {"ts": ISO8601 w/ local offset, "event": <code>[, "detail": ...]}
# ---------------------------------------------------------------------------

_log_append_count = 0


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_event(event, detail=None):
    global _log_append_count
    entry = {"ts": _now_iso(), "event": event}
    if detail is not None:
        entry["detail"] = detail
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        _log_append_count += 1
        if _log_append_count >= LOG_TRUNCATE_CHECK_INTERVAL:
            _log_append_count = 0
            _truncate_log_if_needed()
    except Exception:
        pass


def _truncate_log_if_needed():
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > LOG_MAX_LINES:
            lines = lines[-LOG_MAX_LINES:]
            tmp_path = LOG_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp_path, LOG_PATH)
    except Exception:
        pass


def write_last_keypress():
    """Cheap single-file atomic write; polled by the Settings window every ~5s."""
    try:
        tmp_path = LAST_KEYPRESS_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"ts": _now_iso()}, f)
        os.replace(tmp_path, LAST_KEYPRESS_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Single-instance guard
#
# Binding a localhost TCP port doubles as a mutex: only one process can
# hold it at a time, and the OS releases it automatically on crash/exit,
# so there is no stale-lock state to clean up.
# ---------------------------------------------------------------------------

_instance_socket = None


def acquire_single_instance():
    global _instance_socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _instance_socket = s  # kept alive for the process lifetime
    return True


# ---------------------------------------------------------------------------
# Working hours
# ---------------------------------------------------------------------------

def is_within_working_hours(start, end, days, now=None):
    now = now or datetime.now()
    if now.weekday() not in days:
        return False
    start_t = datetime.strptime(start, "%H:%M").time()
    end_t = datetime.strptime(end, "%H:%M").time()
    return start_t <= now.time() <= end_t


# ---------------------------------------------------------------------------
# Icon drawing
#
# Three distinct states, drawn as simple presence-style dots so they read
# at 16-22px: ON = solid green, OFF = hollow gray ring, outside working
# hours = gray dot with a small clock badge. A light outline keeps them
# visible on both light and dark tray/menu-bar backgrounds.
# ---------------------------------------------------------------------------

def _dot(draw, color, outline, fill=True, width=4):
    cx = cy = ICON_SIZE / 2
    r = ICON_SIZE * 0.32
    bbox = [cx - r, cy - r, cx + r, cy + r]
    if fill:
        draw.ellipse(bbox, fill=color, outline=outline, width=3)
    else:
        draw.ellipse(bbox, outline=outline, width=width)


def build_icon_image(status):
    """status: 'on' | 'off' | 'outside_hours'"""
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    soft_outline = (255, 255, 255, 190)

    if status == "on":
        _dot(draw, (46, 204, 113, 255), soft_outline)
    elif status == "outside_hours":
        _dot(draw, (149, 165, 166, 255), soft_outline)
        bx = by = ICON_SIZE * 0.70
        br = ICON_SIZE * 0.22
        draw.ellipse(
            [bx - br, by - br, bx + br, by + br],
            fill=(52, 73, 94, 255),
            outline=(255, 255, 255, 220),
            width=2,
        )
        draw.line([bx, by, bx, by - br * 0.55], fill=(255, 255, 255, 230), width=2)
        draw.line([bx, by, bx + br * 0.45, by], fill=(255, 255, 255, 230), width=2)
    else:  # off
        _dot(draw, None, (189, 195, 199, 255), fill=False, width=4)

    return img


# ---------------------------------------------------------------------------
# macOS: double-click the status bar icon to open Settings
#
# There is no public pystray API for this, so this pokes at pystray's
# private Cocoa internals (_status_item, _delegate, _update_menu,
# _menu_handle — see pystray/_darwin.py). Every step is feature-detected
# and the whole thing is wrapped so that a pystray internals change makes
# this silently do nothing instead of crashing the app: the fallback is
# pystray's normal single-click-opens-menu behavior, which already has
# "Settings…" as the top item.
# ---------------------------------------------------------------------------

def setup_macos_double_click(icon, app):
    try:
        import AppKit
        import objc
    except ImportError:
        return

    try:
        status_item = icon._status_item
        button = status_item.button()
        delegate = icon._delegate
        nsapp = icon._app
        original_update_menu = icon._update_menu
        if not callable(original_update_menu):
            return
    except AttributeError:
        return

    state = {"timer": None}

    def _show_menu_now():
        state["timer"] = None
        try:
            menu_handle = icon._menu_handle
            if not menu_handle:
                return
            status_item.popUpStatusItemMenu_(menu_handle[0])
        except Exception:
            pass

    def _on_click(_self, _sender):
        try:
            click_count = nsapp.currentEvent().clickCount()
        except Exception:
            click_count = 1
        existing_timer = state["timer"]
        if existing_timer is not None:
            existing_timer.cancel()
            state["timer"] = None
        if click_count >= 2:
            try:
                app.open_settings()
            except Exception:
                pass
            return
        timer = threading.Timer(0.25, _show_menu_now)
        timer.daemon = True
        state["timer"] = timer
        timer.start()

    try:
        selector_name = b"unidleStatusClick:"
        action = objc.selector(_on_click, selector=selector_name, signature=b"v@:@")
        objc.classAddMethods(type(delegate), [action])
        button.setTarget_(delegate)
        button.setAction_(selector_name)
        button.sendActionOn_(AppKit.NSEventMaskLeftMouseUp)

        def _patched_update_menu():
            original_update_menu()
            try:
                status_item.setMenu_(None)
            except Exception:
                pass

        icon._update_menu = _patched_update_menu
        status_item.setMenu_(None)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Worker thread — sends the keepalive keypress
# ---------------------------------------------------------------------------

class Worker(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self._stop_event = threading.Event()
        self._next_press_at = 0.0
        self._keyboard = Controller()
        self.reschedule()

    def run(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(TICK_SECONDS)
            if self._stop_event.is_set():
                break
            self.app.check_config_reload()
            self.app.check_auto_stop()
            self.app.check_working_hours_transition()
            if not self._should_send():
                continue
            if time.time() >= self._next_press_at:
                self._send_keypress()
                self.reschedule()

    def _should_send(self):
        state = self.app.state
        with state.lock:
            if not state.running:
                return False
            if state.working_hours_enabled and not is_within_working_hours(
                state.working_hours_start, state.working_hours_end, state.working_days
            ):
                return False
        return True

    def reschedule(self):
        state = self.app.state
        with state.lock:
            interval = state.interval
            randomize = state.randomize
        if randomize:
            jitter = interval * RANDOMIZE_JITTER
            interval = random.uniform(max(1.0, interval - jitter), interval + jitter)
        self._next_press_at = time.time() + max(1.0, interval)

    def _send_keypress(self):
        try:
            self._keyboard.press(Key.f15)
            self._keyboard.release(Key.f15)
        except Exception:
            return
        with self.app.state.lock:
            first_since_resume = self.app.state.last_keypress_time is None
            self.app.state.last_keypress_time = time.time()
        write_last_keypress()
        if first_since_resume:
            log_event("keypress")

    def stop(self):
        self._stop_event.set()


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self, settings):
        self.lock = threading.RLock()
        self.running = settings["running"]
        self.interval = settings["interval"]
        self.randomize = settings["randomize"]
        self.working_hours_enabled = settings["working_hours_enabled"]
        self.working_hours_start = settings["working_hours_start"]
        self.working_hours_end = settings["working_hours_end"]
        self.working_days = settings["working_days"]
        self.auto_stop_choice = settings["auto_stop_choice"]
        self.notifications_enabled = settings["notifications_enabled"]

        self.start_time = time.time() if self.running else None
        self.last_keypress_time = None
        self.auto_stop_deadline = None
        self._refresh_auto_stop_deadline()

    def _refresh_auto_stop_deadline(self):
        secs = AUTO_STOP_CHOICES.get(self.auto_stop_choice, 0)
        self.auto_stop_deadline = time.time() + secs if (self.running and secs) else None

    def as_dict(self):
        return {
            "running": self.running,
            "interval": self.interval,
            "randomize": self.randomize,
            "working_hours_enabled": self.working_hours_enabled,
            "working_hours_start": self.working_hours_start,
            "working_hours_end": self.working_hours_end,
            "working_days": self.working_days,
            "auto_stop_choice": self.auto_stop_choice,
            "notifications_enabled": self.notifications_enabled,
        }


# ---------------------------------------------------------------------------
# Main application: menu, icon, and glue between state / worker / tray
# ---------------------------------------------------------------------------

class UnidleApp:
    def __init__(self, settings, first_run):
        self.state = AppState(settings)
        self.first_run = first_run
        self.worker = Worker(self)
        self.icon = pystray.Icon(APP_NAME, menu=self.build_menu())
        self.icon.icon = self._current_icon_image()
        self.icon.title = APP_NAME
        self._settings_process = None
        self._last_config_mtime = _config_mtime()
        self._last_wh_status = None

    # -- persistence -------------------------------------------------
    def save(self):
        save_settings(self.state.as_dict())
        self._last_config_mtime = _config_mtime()

    # -- settings window / hot-reload -------------------------------------------------
    def open_settings(self, icon=None, item=None):
        if self._settings_process is not None and self._settings_process.poll() is None:
            return
        if getattr(sys, "frozen", False):
            args = [sys.executable, "--settings"]
        else:
            args = [sys.executable, os.path.abspath(__file__), "--settings"]
        try:
            self._settings_process = subprocess.Popen(args)
        except Exception:
            pass

    def check_config_reload(self):
        mtime = _config_mtime()
        if mtime is None or mtime == self._last_config_mtime:
            return
        self._last_config_mtime = mtime
        settings, _ = load_settings()
        with self.state.lock:
            running_changed = settings["running"] != self.state.running
            if running_changed:
                self.state.start_time = time.time() if settings["running"] else None
                if settings["running"]:
                    self.state.last_keypress_time = None
            self.state.running = settings["running"]
            self.state.interval = settings["interval"]
            self.state.randomize = settings["randomize"]
            self.state.working_hours_enabled = settings["working_hours_enabled"]
            self.state.working_hours_start = settings["working_hours_start"]
            self.state.working_hours_end = settings["working_hours_end"]
            self.state.working_days = settings["working_days"]
            self.state.auto_stop_choice = settings["auto_stop_choice"]
            self.state.notifications_enabled = settings["notifications_enabled"]
            self.state._refresh_auto_stop_deadline()
        self.worker.reschedule()
        self.refresh_icon()
        try:
            self.icon.update_menu()
        except Exception:
            pass
        if running_changed:
            log_event("toggle_on" if settings["running"] else "toggle_off")

    def check_working_hours_transition(self):
        with self.state.lock:
            running = self.state.running
            enabled = self.state.working_hours_enabled
            if not running or not enabled:
                self._last_wh_status = None
                return
            inside = is_within_working_hours(
                self.state.working_hours_start,
                self.state.working_hours_end,
                self.state.working_days,
            )
        if self._last_wh_status is None:
            self._last_wh_status = inside
            return
        if inside != self._last_wh_status:
            log_event("wh_enter" if inside else "wh_exit")
            self._last_wh_status = inside

    # -- icon / status -------------------------------------------------
    def current_status(self):
        with self.state.lock:
            if not self.state.running:
                return "off"
            if self.state.working_hours_enabled and not is_within_working_hours(
                self.state.working_hours_start,
                self.state.working_hours_end,
                self.state.working_days,
            ):
                return "outside_hours"
            return "on"

    def _current_icon_image(self):
        return build_icon_image(self.current_status())

    def refresh_icon(self):
        status = self.current_status()
        self.icon.icon = build_icon_image(status)
        labels = {
            "on": "ON",
            "off": "OFF",
            "outside_hours": "outside working hours",
        }
        with self.state.lock:
            interval = self.state.interval
        self.icon.title = f"{APP_NAME} — {labels[status]} · every ~{interval}s"

    # -- notifications -------------------------------------------------
    def notify(self, message, title=APP_NAME):
        with self.state.lock:
            enabled = self.state.notifications_enabled
        if not enabled:
            return
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    # -- auto-stop -------------------------------------------------
    def check_auto_stop(self):
        with self.state.lock:
            deadline = self.state.auto_stop_deadline
            running = self.state.running
        if running and deadline and time.time() >= deadline:
            self.set_running(False, reason="auto-stop")

    # -- actions -------------------------------------------------
    def set_running(self, value, reason="manual"):
        with self.state.lock:
            if self.state.running == value:
                return
            self.state.running = value
            self.state.start_time = time.time() if value else None
            self.state.last_keypress_time = None if value else self.state.last_keypress_time
            self.state._refresh_auto_stop_deadline()
        self.worker.reschedule()
        self.refresh_icon()
        self.save()
        if reason == "auto-stop":
            log_event("auto_stop", detail=self.state.auto_stop_choice)
            self.notify("Auto-stop reached — keep-online turned off.")
        else:
            log_event("toggle_on" if value else "toggle_off")
            self.notify("Keeping you online." if value else "Keep-online turned off.")

    def toggle_running(self, icon=None, item=None):
        with self.state.lock:
            new_value = not self.state.running
        self.set_running(new_value)

    def set_interval(self, seconds):
        with self.state.lock:
            self.state.interval = seconds
        self.worker.reschedule()
        self.refresh_icon()
        self.save()

    def set_randomize(self, icon=None, item=None):
        with self.state.lock:
            self.state.randomize = not self.state.randomize
        self.worker.reschedule()
        self.save()

    def set_working_hours_enabled(self, icon=None, item=None):
        with self.state.lock:
            self.state.working_hours_enabled = not self.state.working_hours_enabled
        self.refresh_icon()
        self.save()

    def set_auto_stop(self, choice):
        with self.state.lock:
            self.state.auto_stop_choice = choice
            self.state._refresh_auto_stop_deadline()
        self.save()

    def set_notifications_enabled(self, icon=None, item=None):
        with self.state.lock:
            self.state.notifications_enabled = not self.state.notifications_enabled
        self.save()

    def quit(self, icon=None, item=None):
        log_event("app_quit")
        self.worker.stop()
        self.save()
        self.icon.stop()

    # -- menu text/state helpers -------------------------------------------------
    def _status_text(self, item):
        status = self.current_status()
        return {
            "on": "🟢  Keeping online",
            "off": "⚪  Keep-online is off",
            "outside_hours": "🌙  Outside working hours",
        }[status]

    def _runtime_text(self, item):
        with self.state.lock:
            running = self.state.running
            start = self.state.start_time
            last_press = self.state.last_keypress_time
        if not running or start is None:
            return "Active for: —"
        elapsed = int(time.time() - start)
        h, rem = divmod(elapsed, 3600)
        m, _ = divmod(rem, 60)
        duration = f"{h}h {m}m" if h else f"{m}m"
        if last_press:
            ago = int(time.time() - last_press)
            return f"Active for {duration} · last keypress {ago}s ago"
        return f"Active for {duration} · no keypress yet"

    def _interval_menu_title(self, item):
        with self.state.lock:
            seconds = self.state.interval
        return f"Interval: {self._format_seconds(seconds)}"

    @staticmethod
    def _format_seconds(seconds):
        return f"{seconds}s" if seconds < 60 else f"{seconds // 60} min"

    def _working_hours_label(self, item):
        with self.state.lock:
            start = self.state.working_hours_start
            end = self.state.working_hours_end
        return f"Follow working hours ({start}–{end})"

    def _working_hours_checked(self, item):
        with self.state.lock:
            return self.state.working_hours_enabled

    def _auto_stop_menu_title(self, item):
        with self.state.lock:
            choice = self.state.auto_stop_choice
        return f"Auto-stop: {choice}"

    def _randomize_checked(self, item):
        with self.state.lock:
            return self.state.randomize

    def _notifications_checked(self, item):
        with self.state.lock:
            return self.state.notifications_enabled

    def _running_checked(self, item):
        with self.state.lock:
            return self.state.running

    # -- menu construction -------------------------------------------------
    def build_menu(self):
        def interval_item(seconds):
            # `seconds` is a parameter of this factory call, so each item's
            # closures capture their own value — no loop late-binding issue,
            # and no extra default-arg parameter for pystray to choke on.
            def checked(item):
                with self.state.lock:
                    return self.state.interval == seconds

            def action(icon, item):
                self.set_interval(seconds)

            return pystray.MenuItem(
                self._format_seconds(seconds), action, checked=checked, radio=True
            )

        def auto_stop_item(choice):
            def checked(item):
                with self.state.lock:
                    return self.state.auto_stop_choice == choice

            def action(icon, item):
                self.set_auto_stop(choice)

            return pystray.MenuItem(choice, action, checked=checked, radio=True)

        interval_submenu = pystray.MenuItem(
            self._interval_menu_title,
            pystray.Menu(*[interval_item(s) for s in INTERVAL_CHOICES]),
        )
        auto_stop_submenu = pystray.MenuItem(
            self._auto_stop_menu_title,
            pystray.Menu(*[auto_stop_item(c) for c in AUTO_STOP_CHOICES]),
        )

        return pystray.Menu(
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings…", self.open_settings, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Keep online", self.toggle_running, checked=self._running_checked
            ),
            interval_submenu,
            pystray.MenuItem(
                self._working_hours_label,
                self.set_working_hours_enabled,
                checked=self._working_hours_checked,
            ),
            auto_stop_submenu,
            pystray.MenuItem(
                "Randomize interval", self.set_randomize, checked=self._randomize_checked
            ),
            pystray.MenuItem(
                "Notifications",
                self.set_notifications_enabled,
                checked=self._notifications_checked,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._runtime_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit),
        )

    # -- lifecycle -------------------------------------------------
    def _on_setup(self, icon):
        icon.visible = True
        self.refresh_icon()
        log_event("app_start")
        if sys.platform == "darwin":
            setup_macos_double_click(icon, self)
        if self.first_run and sys.platform == "darwin":
            self.notify(
                "Grant Accessibility (and Input Monitoring) permission in "
                "System Settings so Unidle can send keypresses.",
                title="Unidle — permission needed",
            )

    def run(self):
        self.worker.start()
        self.icon.run(setup=self._on_setup)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    migrate_legacy_files()

    if "--settings" in sys.argv[1:]:
        from settings_ui import run_settings_window

        run_settings_window(CONFIG_PATH)
        return

    if not acquire_single_instance():
        print(f"{APP_NAME} is already running.", file=sys.stderr)
        sys.exit(0)

    settings, first_run = load_settings()
    app = UnidleApp(settings, first_run)
    try:
        app.run()
    except KeyboardInterrupt:
        app.quit()


if __name__ == "__main__":
    main()
