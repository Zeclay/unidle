"""Unidle — keeps Microsoft Teams (or any app) from going idle.

Sends an invisible F15 keypress (or a mouse/scroll nudge) on an interval to
reset the OS idle timer. Lives entirely in the system tray/menu bar. No
credentials, no network, no interaction with the tracked apps themselves.
"""

import ctypes
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import psutil
from PIL import Image, ImageDraw
import pystray
from pynput.keyboard import Controller, GlobalHotKeys, HotKey, Key
from pynput.mouse import Controller as MouseController

# ---------------------------------------------------------------------------
# Config — edit these to change defaults. Everything here is a plain
# constant so it is easy to find and tweak in one place.
# ---------------------------------------------------------------------------

APP_NAME = "Unidle"

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".unidle.json")
LOG_PATH = os.path.join(os.path.expanduser("~"), ".unidle_log.jsonl")
LAST_KEYPRESS_PATH = os.path.join(os.path.expanduser("~"), ".unidle_last.json")
SETTINGS_PID_PATH = os.path.join(os.path.expanduser("~"), ".unidle_settings.pid")
LOG_MAX_LINES = 500
LOG_TRUNCATE_CHECK_INTERVAL = 100  # only check/truncate every N appends; cheap
SINGLE_INSTANCE_PORT = 58731  # localhost-only mutex; OS frees it if we crash

DEFAULT_INTERVAL = 60  # seconds between activity sends
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

ACTIVITY_MODES = ("f15", "mouse", "scroll")
ACTIVITY_MODE_DEFAULT = "f15"

SMART_IDLE_DEFAULT = True
SMART_IDLE_THRESHOLD_DEFAULT = 30

KEEP_SYSTEM_AWAKE_DEFAULT = False
KEEP_DISPLAY_AWAKE_DEFAULT = False

PAUSE_ON_LOW_BATTERY_DEFAULT = True
LOW_BATTERY_PERCENT_DEFAULT = 20

HOTKEY_ENABLED_DEFAULT = False
HOTKEY_DEFAULT = "<ctrl>+<alt>+u"

APP_TRIGGER_ENABLED_DEFAULT = False
APP_TRIGGER_APPS_DEFAULT = ["teams"]
APP_TRIGGER_CUSTOM_DEFAULT = []
APP_TRIGGER_IDS = ("teams", "slack", "zoom", "webex", "discord", "gchat")

# preset id -> substrings matched case-insensitively against process names
# (psutil.process_iter(['name'])). "gchat" is web-based and can't be
# detected this way; it's listed so the UI can say so explicitly.
APP_PROCESS_PRESETS = {
    "teams": ["ms-teams", "teams"],
    "slack": ["slack"],
    "zoom": ["zoom", "cptHost".lower()],
    "webex": ["webex", "ciscocollabhost"],
    "discord": ["discord"],
    "gchat": [],
}

AUTOSTART_DEFAULT = False

AUTO_START_TICK_SECONDS = 1  # kept only as the Settings-window-open poll interval

SETTINGS_POLL_SECONDS = 1  # worker wakes at this cadence only while Settings is open
MAX_SLEEP_SECONDS = 300  # safety fallback so a missed wake() self-heals quickly
LOCK_RECHECK_SECONDS = 3
BATTERY_RECHECK_SECONDS = 60
BATTERY_HYSTERESIS_PERCENT = 5
APP_TRIGGER_CACHE_SECONDS = 60

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
    # Phase 1 (PLAN-5)
    "activity_mode",
    "smart_idle",
    "smart_idle_threshold",
    "keep_system_awake",
    "keep_display_awake",
    "pause_on_low_battery",
    "low_battery_percent",
    "hotkey_enabled",
    "hotkey",
    # Phase 2 (PLAN-5)
    "app_trigger_enabled",
    "app_trigger_apps",
    "app_trigger_custom",
    "autostart",
    "profiles",
    "active_profile",
)

# Keys captured when saving a profile snapshot — everything except the
# profile bookkeeping keys themselves (a profile must never nest "profiles").
PROFILE_KEYS = tuple(k for k in SETTINGS_KEYS if k not in ("profiles", "active_profile"))


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
        "activity_mode": ACTIVITY_MODE_DEFAULT,
        "smart_idle": SMART_IDLE_DEFAULT,
        "smart_idle_threshold": SMART_IDLE_THRESHOLD_DEFAULT,
        "keep_system_awake": KEEP_SYSTEM_AWAKE_DEFAULT,
        "keep_display_awake": KEEP_DISPLAY_AWAKE_DEFAULT,
        "pause_on_low_battery": PAUSE_ON_LOW_BATTERY_DEFAULT,
        "low_battery_percent": LOW_BATTERY_PERCENT_DEFAULT,
        "hotkey_enabled": HOTKEY_ENABLED_DEFAULT,
        "hotkey": HOTKEY_DEFAULT,
        "app_trigger_enabled": APP_TRIGGER_ENABLED_DEFAULT,
        "app_trigger_apps": list(APP_TRIGGER_APPS_DEFAULT),
        "app_trigger_custom": list(APP_TRIGGER_CUSTOM_DEFAULT),
        "autostart": AUTOSTART_DEFAULT,
        "profiles": {},
        "active_profile": None,
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


def _is_valid_percent(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100


def _is_valid_hotkey(value):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        HotKey.parse(value)
        return True
    except Exception:
        return False


def _is_valid_string_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _is_valid_app_trigger_apps(value):
    return isinstance(value, list) and all(isinstance(v, str) and v in APP_TRIGGER_IDS for v in value)


def _is_valid_profiles(value):
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, dict) for k, v in value.items()
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
        if settings["activity_mode"] not in ACTIVITY_MODES:
            settings["activity_mode"] = ACTIVITY_MODE_DEFAULT
        if not isinstance(settings["smart_idle"], bool):
            settings["smart_idle"] = SMART_IDLE_DEFAULT
        if not _is_valid_percent(settings["smart_idle_threshold"]) or settings["smart_idle_threshold"] < 1:
            settings["smart_idle_threshold"] = SMART_IDLE_THRESHOLD_DEFAULT
        if not isinstance(settings["keep_system_awake"], bool):
            settings["keep_system_awake"] = KEEP_SYSTEM_AWAKE_DEFAULT
        if not isinstance(settings["keep_display_awake"], bool):
            settings["keep_display_awake"] = KEEP_DISPLAY_AWAKE_DEFAULT
        if not isinstance(settings["pause_on_low_battery"], bool):
            settings["pause_on_low_battery"] = PAUSE_ON_LOW_BATTERY_DEFAULT
        if not _is_valid_percent(settings["low_battery_percent"]):
            settings["low_battery_percent"] = LOW_BATTERY_PERCENT_DEFAULT
        if not isinstance(settings["hotkey_enabled"], bool):
            settings["hotkey_enabled"] = HOTKEY_ENABLED_DEFAULT
        if not _is_valid_hotkey(settings["hotkey"]):
            settings["hotkey"] = HOTKEY_DEFAULT
        if not isinstance(settings["app_trigger_enabled"], bool):
            settings["app_trigger_enabled"] = APP_TRIGGER_ENABLED_DEFAULT
        if not _is_valid_app_trigger_apps(settings["app_trigger_apps"]):
            settings["app_trigger_apps"] = list(APP_TRIGGER_APPS_DEFAULT)
        if not _is_valid_string_list(settings["app_trigger_custom"]):
            settings["app_trigger_custom"] = list(APP_TRIGGER_CUSTOM_DEFAULT)
        if not isinstance(settings["autostart"], bool):
            settings["autostart"] = AUTOSTART_DEFAULT
        if not _is_valid_profiles(settings["profiles"]):
            settings["profiles"] = {}
        if settings["active_profile"] is not None and settings["active_profile"] not in settings["profiles"]:
            settings["active_profile"] = None
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


def is_frozen():
    return getattr(sys, "frozen", False)


def _macos_bundle_path():
    """Return the .app bundle path when running frozen inside one, else None.

    Inside a bundle, ``sys.executable`` is
    ``<Bundle>.app/Contents/MacOS/<exe>``; strip that tail to get the .app so
    we can relaunch through LaunchServices under the same bundle identity.
    """
    marker = "/Contents/MacOS/"
    idx = sys.executable.find(marker)
    if idx == -1:
        return None
    bundle = sys.executable[:idx]
    return bundle if bundle.endswith(".app") else None


def _write_settings_pid():
    """Marker so the tray app can tell the Settings window is open.

    Needed because the macOS launch path (`open -n -a`) returns immediately,
    so the tray app can't track the real Settings child via its own Popen
    handle. The --settings process owns this file: written on start, removed
    on exit; a live pid in it means the window is open.
    """
    try:
        with open(SETTINGS_PID_PATH, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _clear_settings_pid():
    try:
        os.remove(SETTINGS_PID_PATH)
    except OSError:
        pass


def _settings_pid_alive():
    """True if SETTINGS_PID_PATH names a live process; cleans up if stale."""
    try:
        with open(SETTINGS_PID_PATH) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        _clear_settings_pid()  # stale marker; owner is gone
        return False
    except PermissionError:
        return True  # exists but not ours to signal — still alive
    except OSError:
        return False
    return True


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


def show_already_running_alert():
    """Best-effort feedback when a second instance is launched.

    There is no tray icon in this short-lived process to call notify() on,
    so this falls back straight to a native alert (MessageBox on Windows,
    osascript on macOS) — the point is that double-clicking the exe a
    second time must never be silently swallowed.
    """
    try:
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(
                0,
                f"{APP_NAME} is already running in the tray.",
                APP_NAME,
                0x40,  # MB_ICONINFORMATION
            )
        elif sys.platform == "darwin":
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display alert "{APP_NAME}" message "{APP_NAME} is already running in the tray."',
                ],
                capture_output=True,
            )
    except Exception:
        pass


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


def seconds_until_next_working_hours_edge(start, end, days, now):
    """Seconds until the next entry/exit boundary of the working-hours
    window, looked ahead up to 8 days (always finds one — `days` is
    validated non-empty). Used so the adaptive scheduler can wake exactly
    when a working-hours transition happens instead of polling for it."""
    start_t = datetime.strptime(start, "%H:%M").time()
    end_t = datetime.strptime(end, "%H:%M").time()
    for add_days in range(0, 8):
        day = now + timedelta(days=add_days)
        if day.weekday() not in days:
            continue
        for edge in sorted((datetime.combine(day.date(), start_t), datetime.combine(day.date(), end_t))):
            if edge > now:
                return (edge - now).total_seconds()
    return 3600.0


# ---------------------------------------------------------------------------
# Icon drawing
#
# Distinct states, drawn as simple presence-style dots so they read at
# 16-22px: ON = solid green, OFF = hollow gray ring, everything else
# ("outside working hours", locked, low battery, waiting for a tracked
# app) = gray dot with a small clock badge — they're all "temporarily not
# sending" for a reason visible in the status text/title, so they share
# one icon to avoid a wall of near-identical badge variants. A light
# outline keeps them visible on both light and dark tray/menu-bar
# backgrounds.
# ---------------------------------------------------------------------------

PAUSED_STATUSES = ("outside_hours", "lock_paused", "battery_paused", "waiting_app")


def _dot(draw, color, outline, fill=True, width=4):
    cx = cy = ICON_SIZE / 2
    r = ICON_SIZE * 0.32
    bbox = [cx - r, cy - r, cx + r, cy + r]
    if fill:
        draw.ellipse(bbox, fill=color, outline=outline, width=3)
    else:
        draw.ellipse(bbox, outline=outline, width=width)


def build_icon_image(status):
    """status: 'on' | 'off' | one of PAUSED_STATUSES"""
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    soft_outline = (255, 255, 255, 190)

    if status == "on":
        _dot(draw, (46, 204, 113, 255), soft_outline)
    elif status in PAUSED_STATUSES:
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

def setup_macos_menu_bar_only():
    """Hide the Dock icon so Unidle lives only in the menu bar.

    With a Dock icon present, macOS offers its own Quit paths (Dock
    right-click → Quit, and Cmd+Q), which kill the process — and the menu
    bar icon vanishes with it. Switching to the "accessory" activation
    policy removes the Dock icon entirely, so the only way to quit is the
    menu bar icon's own Quit item, which is what users expect from a
    background tray app. Feature-detected and wrapped so a failure just
    leaves the default (Dock icon present) behavior instead of crashing.
    """
    try:
        import AppKit

        AppKit.NSApp.setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory
        )
    except Exception:
        pass


def macos_accessibility_trusted():
    """Return True if this process already has Accessibility permission.

    Returns True on any failure (or non-macOS) so we never nag when we
    can't actually tell — the keypress path degrades safely on its own.
    """
    if sys.platform != "darwin":
        return True
    try:
        import ApplicationServices

        return bool(ApplicationServices.AXIsProcessTrusted())
    except Exception:
        return True


def request_macos_accessibility():
    """Pop the native Accessibility permission dialog on macOS.

    ``AXIsProcessTrustedWithOptions`` with the prompt option shows the
    system dialog ("Unidle would like to control this computer…") with an
    Open System Settings button, taking the user straight to the right
    pane. If the framework isn't available, fall back to opening that pane
    directly. Best-effort and fully wrapped: it must never raise.
    """
    try:
        import ApplicationServices

        options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        return bool(
            ApplicationServices.AXIsProcessTrustedWithOptions(options)
        )
    except Exception:
        pass
    try:
        subprocess.run(
            [
                "open",
                "x-apple.systempreferences:com.apple.preference.security"
                "?Privacy_Accessibility",
            ],
            check=False,
        )
    except Exception:
        pass
    return False


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

    show_menu_selector = b"unidleShowMenu:"

    def _show_menu_now():
        # MUST run on the main thread — popUpStatusItemMenu_ is an AppKit UI
        # call, and invoking it from a background thread corrupts macOS event
        # routing (symptom: the previously focused app, e.g. Teams, can no
        # longer receive keystrokes after the menu is shown).
        try:
            menu_handle = icon._menu_handle
            if not menu_handle:
                return
            status_item.popUpStatusItemMenu_(menu_handle[0])
        except Exception:
            pass

    def _on_show_menu(_self, _obj):
        _show_menu_now()

    def _on_click(_self, _sender):
        try:
            event = nsapp.currentEvent()
            event_type = event.type()
            click_count = event.clickCount()
        except Exception:
            event_type = None
            click_count = 1
        # Cancel any pending single-click menu-open scheduled below; a second
        # click within the delay means this is a double-click.
        try:
            AppKit.NSObject.cancelPreviousPerformRequestsWithTarget_selector_object_(
                delegate, show_menu_selector, None
            )
        except Exception:
            pass
        # Right-click (or control-click, which macOS reports as a right
        # mouse up) opens the menu immediately — no double-click wait, since
        # right-click has no "open Settings" gesture to disambiguate from.
        is_right_click = event_type == AppKit.NSEventTypeRightMouseUp
        if not is_right_click and event_type == AppKit.NSEventTypeLeftMouseUp:
            try:
                ctrl_held = bool(
                    event.modifierFlags() & AppKit.NSEventModifierFlagControl
                )
            except Exception:
                ctrl_held = False
            is_right_click = ctrl_held
        if is_right_click:
            _show_menu_now()
            return
        if click_count >= 2:
            try:
                app.open_settings()
            except Exception:
                pass
            return
        # Single left-click: wait briefly for a possible second click, then
        # show the menu. Scheduled via performSelector:afterDelay: so it fires
        # on the main run loop (we're on the main thread here) — never a
        # background thread. cancelPrevious... above collapses a double-click.
        try:
            delegate.performSelector_withObject_afterDelay_(
                show_menu_selector, None, 0.25
            )
        except Exception:
            _show_menu_now()

    try:
        selector_name = b"unidleStatusClick:"
        action = objc.selector(_on_click, selector=selector_name, signature=b"v@:@")
        show_action = objc.selector(
            _on_show_menu, selector=show_menu_selector, signature=b"v@:@"
        )
        objc.classAddMethods(type(delegate), [action, show_action])
        button.setTarget_(delegate)
        button.setAction_(selector_name)
        button.sendActionOn_(
            AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp
        )

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
# Platform layer — idle time, session lock, battery, prevent-sleep.
#
# Every function here is wrapped in try/except and feature-detects its
# platform: on an unsupported OS/API it returns a safe "don't block
# anything" value rather than crashing. Idle detection failing returns
# None (meaning "assume idle", i.e. the old always-send behavior); lock
# detection failing returns False (assume unlocked, i.e. don't block).
# ---------------------------------------------------------------------------

class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def _get_idle_seconds_windows():
    try:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(_LastInputInfo)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return max(0.0, millis / 1000.0)
    except Exception:
        return None


def _get_idle_seconds_macos():
    try:
        import Quartz

        return Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType
        )
    except Exception:
        return None


def get_user_idle_seconds():
    """Seconds since the last real input event, or None if unknown/unsupported."""
    if sys.platform == "win32":
        return _get_idle_seconds_windows()
    if sys.platform == "darwin":
        return _get_idle_seconds_macos()
    return None


def _is_locked_windows():
    try:
        # The lock/login screen runs on a separate, non-interactive desktop;
        # OpenInputDesktop only succeeds when the current session's desktop
        # is the one receiving input, i.e. when it's unlocked.
        desktop = ctypes.windll.user32.OpenInputDesktop(0, False, 0)
        if desktop:
            ctypes.windll.user32.CloseDesktop(desktop)
            return False
        return True
    except Exception:
        return False


def _is_locked_macos():
    try:
        import Quartz

        info = Quartz.CGSessionCopyCurrentDictionary()
        if not info:
            return False
        return bool(info.get("CGSSessionScreenIsLocked", False))
    except Exception:
        return False


def is_session_locked():
    if sys.platform == "win32":
        return _is_locked_windows()
    if sys.platform == "darwin":
        return _is_locked_macos()
    return False


def get_battery_status():
    """Returns (percent, plugged_in) or None (desktop / no battery / unsupported)."""
    try:
        battery = psutil.sensors_battery()
        if battery is None:
            return None
        return (battery.percent, battery.power_plugged)
    except Exception:
        return None


_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


def _set_sleep_prevention_windows(system, display):
    try:
        flags = _ES_CONTINUOUS
        if system or display:
            flags |= _ES_SYSTEM_REQUIRED
        if display:
            flags |= _ES_DISPLAY_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


_mac_caffeinate = {"proc": None, "mode": None}


def _set_sleep_prevention_macos(system, display):
    if display:
        mode, args = "display", ["caffeinate", "-di"]
    elif system:
        mode, args = "system", ["caffeinate", "-i"]
    else:
        mode, args = None, None

    if _mac_caffeinate["mode"] == mode:
        return

    old_proc = _mac_caffeinate["proc"]
    if old_proc is not None:
        try:
            old_proc.terminate()
        except Exception:
            pass

    new_proc = None
    if args is not None:
        try:
            new_proc = subprocess.Popen(args)
        except Exception:
            new_proc = None

    _mac_caffeinate["proc"] = new_proc
    _mac_caffeinate["mode"] = mode


def set_sleep_prevention(system, display):
    """Must always be called from the same thread (the worker thread) —
    SetThreadExecutionState is a per-thread API on Windows."""
    try:
        if sys.platform == "win32":
            _set_sleep_prevention_windows(system, display)
        elif sys.platform == "darwin":
            _set_sleep_prevention_macos(system, display)
    except Exception:
        pass


def is_any_tracked_app_running(app_ids, custom_names):
    """OR match: True if any preset app or custom process name substring is
    found among running processes. Fails open (returns True, i.e. don't
    block sending) if the process list can't be read at all."""
    needles = []
    for app_id in app_ids:
        needles.extend(APP_PROCESS_PRESETS.get(app_id, []))
    needles.extend(n.strip().lower() for n in custom_names if n and n.strip())
    if not needles:
        return False
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if any(needle in name for needle in needles):
                return True
    except Exception:
        return True
    return False


_AUTOSTART_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_VALUE_NAME = APP_NAME
_LAUNCH_AGENT_LABEL = "com.unidle.app"


def _launch_agent_path():
    return os.path.join(os.path.expanduser("~/Library/LaunchAgents"), f"{_LAUNCH_AGENT_LABEL}.plist")


def _set_autostart_windows(enabled):
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_REGISTRY_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, _AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, sys.executable)
            else:
                try:
                    winreg.DeleteValue(key, _AUTOSTART_VALUE_NAME)
                except FileNotFoundError:
                    pass
    except Exception:
        pass


def _set_autostart_macos(enabled):
    path = _launch_agent_path()
    try:
        if enabled:
            plist = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0">\n<dict>\n'
                f"    <key>Label</key><string>{_LAUNCH_AGENT_LABEL}</string>\n"
                f"    <key>ProgramArguments</key><array><string>{sys.executable}</string></array>\n"
                "    <key>RunAtLoad</key><true/>\n"
                "</dict>\n</plist>\n"
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(plist)
            subprocess.run(["launchctl", "load", path], capture_output=True)
        elif os.path.exists(path):
            subprocess.run(["launchctl", "unload", path], capture_output=True)
            os.remove(path)
    except Exception:
        pass


def set_autostart(enabled):
    """Only meaningful for a frozen (PyInstaller) build — from source,
    sys.executable is the interpreter, not the app, so callers must gate
    this on is_frozen()."""
    try:
        if sys.platform == "win32":
            _set_autostart_windows(enabled)
        elif sys.platform == "darwin":
            _set_autostart_macos(enabled)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Global hotkey — separate listener thread (managed by pynput), started
# only when enabled. Feature-detects hotkey-string parse failures and
# permission issues (macOS Accessibility) by just not starting, silently.
# ---------------------------------------------------------------------------

class HotkeyListener:
    def __init__(self, on_trigger):
        self._on_trigger = on_trigger
        self._listener = None
        self._current_hotkey = None

    def apply(self, enabled, hotkey):
        if not enabled:
            self.stop()
            return
        if self._listener is not None and self._current_hotkey == hotkey:
            return  # already running with this exact binding
        self.stop()
        try:
            self._listener = GlobalHotKeys({hotkey: self._on_trigger})
            self._listener.start()
            self._current_hotkey = hotkey
        except Exception:
            self._listener = None
            self._current_hotkey = None
            log_event("hotkey_error", detail=hotkey)

    def stop(self):
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
        self._listener = None
        self._current_hotkey = None


# ---------------------------------------------------------------------------
# Worker thread — sends the keepalive activity on an adaptive schedule.
#
# Instead of ticking every second, the loop sleeps on an Event until the
# next thing that could matter: the next scheduled activity, an auto-stop
# deadline, a working-hours boundary, a pause-state recheck (lock/battery/
# app-trigger/smart-idle), or — only while the Settings window is open —
# a 1s poll so hot-reload still feels instant while the user is editing.
# Any code path that changes state calls wake() (via UnidleApp.save()) so
# toggles/interval changes take effect immediately instead of waiting for
# the next natural wake.
# ---------------------------------------------------------------------------

class Worker(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._next_press_at = 0.0
        self._keyboard = Controller()
        self._mouse = None

        self._lock_paused = False
        self._battery_paused = False
        self._app_trigger_active = True
        self._app_trigger_cache_ts = None
        self._app_trigger_cache_result = True

        self._last_own_activity_ts = None
        self._true_idle_anchor = None

        self._sleep_prevention_applied = (False, False)

        self.reschedule()

    # -- public, read by UnidleApp for status text -------------------------------------------------
    @property
    def lock_paused(self):
        return self._lock_paused

    @property
    def battery_paused(self):
        return self._battery_paused

    @property
    def app_trigger_active(self):
        return self._app_trigger_active

    # -- main loop -------------------------------------------------
    def run(self):
        while not self._stop_event.is_set():
            self.app.check_config_reload()
            if self._stop_event.is_set():
                break
            self.app.check_auto_stop()
            self.app.check_working_hours_transition()

            now = time.time()
            should_send, recheck_hint = self._should_send(now)
            self._apply_sleep_prevention()
            if should_send and now >= self._next_press_at:
                self._send_activity()
                self.reschedule()

            next_wake = self._compute_next_wake(time.time(), recheck_hint)
            timeout = max(0.0, next_wake - time.time())
            self._wake_event.wait(timeout)
            self._wake_event.clear()

        # Cleanup must happen on this thread: SetThreadExecutionState is
        # per-thread on Windows, so releasing it from any other thread
        # would be a no-op (or worse, affect the wrong thread's state).
        try:
            set_sleep_prevention(False, False)
        except Exception:
            pass

    def wake(self):
        self._wake_event.set()

    def stop(self):
        self._stop_event.set()
        self._wake_event.set()

    # -- gating logic -------------------------------------------------
    def _should_send(self, now):
        """Returns (should_send, recheck_after_seconds_or_None)."""
        state = self.app.state
        with state.lock:
            running = state.running
            wh_enabled = state.working_hours_enabled
            wh_start = state.working_hours_start
            wh_end = state.working_hours_end
            wh_days = state.working_days
            smart_idle = state.smart_idle
            smart_idle_threshold = state.smart_idle_threshold
            pause_on_low_battery = state.pause_on_low_battery
            low_battery_percent = state.low_battery_percent
            app_trigger_enabled = state.app_trigger_enabled
            app_trigger_apps = list(state.app_trigger_apps)
            app_trigger_custom = list(state.app_trigger_custom)

        if not running:
            return False, None

        if wh_enabled and not is_within_working_hours(wh_start, wh_end, wh_days, datetime.now()):
            return False, None  # recheck comes from the wh-edge candidate in _compute_next_wake

        locked = is_session_locked()
        if locked != self._lock_paused:
            self._lock_paused = locked
            log_event("lock_pause" if locked else "unlock_resume")
        if locked:
            return False, LOCK_RECHECK_SECONDS

        if pause_on_low_battery:
            battery = get_battery_status()
            if battery is not None:
                percent, plugged = battery
                if plugged:
                    paused = False
                elif self._battery_paused:
                    paused = percent < low_battery_percent + BATTERY_HYSTERESIS_PERCENT
                else:
                    paused = percent < low_battery_percent
                if paused != self._battery_paused:
                    self._battery_paused = paused
                    if paused:
                        log_event("battery_pause", detail=f"{percent}%")
                        self.app.notify(f"Battery low ({percent}%) — keep-online paused.")
                    else:
                        log_event("battery_resume")
                        self.app.notify("Battery okay — keep-online resumed.")
                if paused:
                    return False, BATTERY_RECHECK_SECONDS
            elif self._battery_paused:
                self._battery_paused = False

        if app_trigger_enabled:
            now_ts = time.time()
            if (
                self._app_trigger_cache_ts is None
                or now_ts - self._app_trigger_cache_ts >= APP_TRIGGER_CACHE_SECONDS
            ):
                self._app_trigger_cache_result = is_any_tracked_app_running(
                    app_trigger_apps, app_trigger_custom
                )
                self._app_trigger_cache_ts = now_ts
            self._app_trigger_active = self._app_trigger_cache_result
            if not self._app_trigger_active:
                recheck = APP_TRIGGER_CACHE_SECONDS - (now_ts - self._app_trigger_cache_ts)
                return False, max(1.0, recheck)
        else:
            self._app_trigger_active = True

        if smart_idle:
            effective_idle = self._effective_idle_seconds(now)
            if effective_idle is not None and effective_idle < smart_idle_threshold:
                return False, smart_idle_threshold - effective_idle

        return True, None

    def _effective_idle_seconds(self, now):
        """Seconds since the last *real* human input — compensated so our
        own synthetic activity doesn't reset it and make smart_idle think
        the user is perpetually active. We track the most recent moment we
        know a real human touched the machine (``_true_idle_anchor``): if
        the OS-reported idle time is shorter than the time since our last
        send, some newer event (necessarily human) reset it and we advance
        the anchor; otherwise nothing has touched the machine since our own
        send and the anchor is left alone."""
        os_idle = get_user_idle_seconds()
        if os_idle is None:
            return None
        since_our_send = (
            now - self._last_own_activity_ts if self._last_own_activity_ts is not None else None
        )
        if since_our_send is None or os_idle < since_our_send - 1.0:
            self._true_idle_anchor = now - os_idle
        if self._true_idle_anchor is None:
            return os_idle
        return now - self._true_idle_anchor

    def _apply_sleep_prevention(self):
        state = self.app.state
        with state.lock:
            keep_system = state.keep_system_awake
            keep_display = state.keep_display_awake
            running = state.running
            wh_enabled = state.working_hours_enabled
            wh_start = state.working_hours_start
            wh_end = state.working_hours_end
            wh_days = state.working_days
            app_trigger_enabled = state.app_trigger_enabled

        within_hours = (not wh_enabled) or is_within_working_hours(wh_start, wh_end, wh_days)
        app_ok = (not app_trigger_enabled) or self._app_trigger_active
        active = running and within_hours and app_ok and not self._lock_paused and not self._battery_paused

        want_system = active and (keep_system or keep_display)
        want_display = active and keep_display
        desired = (want_system, want_display)
        if desired != self._sleep_prevention_applied:
            set_sleep_prevention(want_system, want_display)
            self._sleep_prevention_applied = desired

    def _compute_next_wake(self, now, recheck_hint):
        candidates = [now + MAX_SLEEP_SECONDS, self._next_press_at]

        if self.app.settings_window_open():
            candidates.append(now + SETTINGS_POLL_SECONDS)

        with self.app.state.lock:
            auto_stop_deadline = self.app.state.auto_stop_deadline
            running = self.app.state.running
            wh_enabled = self.app.state.working_hours_enabled
            wh_start = self.app.state.working_hours_start
            wh_end = self.app.state.working_hours_end
            wh_days = self.app.state.working_days

        if auto_stop_deadline:
            candidates.append(auto_stop_deadline)
        if running and wh_enabled:
            candidates.append(
                now + seconds_until_next_working_hours_edge(wh_start, wh_end, wh_days, datetime.now())
            )
        if recheck_hint is not None:
            candidates.append(now + recheck_hint)

        return max(now, min(candidates))

    # -- scheduling / sending -------------------------------------------------
    def reschedule(self):
        state = self.app.state
        with state.lock:
            interval = state.interval
            randomize = state.randomize
        if randomize:
            jitter = interval * RANDOMIZE_JITTER
            interval = random.uniform(max(1.0, interval - jitter), interval + jitter)
        self._next_press_at = time.time() + max(1.0, interval)

    def _send_activity(self):
        state = self.app.state
        with state.lock:
            mode = state.activity_mode
        try:
            if mode == "mouse":
                self._send_mouse_nudge()
            elif mode == "scroll":
                self._send_scroll_nudge()
            else:
                self._keyboard.press(Key.f15)
                self._keyboard.release(Key.f15)
        except Exception:
            return

        now = time.time()
        self._last_own_activity_ts = now
        with state.lock:
            first_since_resume = state.last_keypress_time is None
            state.last_keypress_time = now
        write_last_keypress()
        if first_since_resume:
            log_event("activity")

    def _get_mouse(self):
        if self._mouse is None:
            self._mouse = MouseController()
        return self._mouse

    def _send_mouse_nudge(self):
        mouse = self._get_mouse()
        x, y = mouse.position
        mouse.position = (x + 1, y)
        mouse.position = (x, y)

    def _send_scroll_nudge(self):
        mouse = self._get_mouse()
        mouse.scroll(0, 1)
        mouse.scroll(0, -1)


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self, settings):
        self.lock = threading.RLock()
        for key in SETTINGS_KEYS:
            setattr(self, key, settings[key])

        self.start_time = time.time() if self.running else None
        self.last_keypress_time = None
        self.auto_stop_deadline = None
        self._refresh_auto_stop_deadline()

    def _refresh_auto_stop_deadline(self):
        secs = AUTO_STOP_CHOICES.get(self.auto_stop_choice, 0)
        self.auto_stop_deadline = time.time() + secs if (self.running and secs) else None

    def as_dict(self):
        return {key: getattr(self, key) for key in SETTINGS_KEYS}


# ---------------------------------------------------------------------------
# Main application: menu, icon, and glue between state / worker / tray
# ---------------------------------------------------------------------------

class UnidleApp:
    def __init__(self, settings, first_run):
        self.state = AppState(settings)
        self.first_run = first_run
        self.worker = Worker(self)
        self.hotkey_listener = HotkeyListener(self._on_hotkey_triggered)
        self.icon = pystray.Icon(APP_NAME, menu=self.build_menu())
        self.icon.icon = self._current_icon_image()
        self.icon.title = APP_NAME
        self._settings_process = None
        self._settings_launch_time = None
        self._last_config_mtime = _config_mtime()
        self._last_wh_status = None

    # -- persistence -------------------------------------------------
    def save(self):
        save_settings(self.state.as_dict())
        self._last_config_mtime = _config_mtime()
        self.worker.wake()

    # -- settings window / hot-reload -------------------------------------------------
    def settings_window_open(self):
        # Source of truth is the pid marker the --settings process maintains:
        # on macOS frozen we launch via `open`, which returns immediately, so
        # our own _settings_process handle can't see the real child.
        if _settings_pid_alive():
            return True
        # A launch we just kicked off may not have written its marker yet;
        # treat that brief window as "open" so a quick second click can't
        # spawn a duplicate Settings window.
        if (
            self._settings_launch_time is not None
            and time.time() - self._settings_launch_time < 4
        ):
            return True
        # Fallback for the direct-Popen path (running from source, or if the
        # macOS `open` launch failed and we fell back to Popen).
        return self._settings_process is not None and self._settings_process.poll() is None

    def open_settings(self, icon=None, item=None):
        if self.settings_window_open():
            return
        args = None
        if is_frozen() and sys.platform == "darwin":
            # Relaunch through LaunchServices so the Settings process runs
            # under the SAME bundle identity as the tray app. Popen-ing the
            # inner binary directly makes macOS TCC treat it as a separate
            # app and create a duplicate Accessibility entry (see HANDOFF).
            bundle = _macos_bundle_path()
            if bundle:
                args = ["open", "-n", "-a", bundle, "--args", "--settings"]
        if args is None:
            if is_frozen():
                args = [sys.executable, "--settings"]
            else:
                args = [sys.executable, os.path.abspath(__file__), "--settings"]
        try:
            self._settings_process = subprocess.Popen(args)
            self._settings_launch_time = time.time()
        except Exception:
            # If `open` failed, fall back to spawning the binary directly so
            # Settings still opens (accepting the duplicate-entry tradeoff).
            if args and args[0] == "open":
                try:
                    self._settings_process = subprocess.Popen([sys.executable, "--settings"])
                    self._settings_launch_time = time.time()
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
            hotkey_changed = (
                settings["hotkey_enabled"] != self.state.hotkey_enabled
                or settings["hotkey"] != self.state.hotkey
            )
            autostart_changed = settings["autostart"] != self.state.autostart
            for key in SETTINGS_KEYS:
                setattr(self.state, key, settings[key])
            self.state._refresh_auto_stop_deadline()

        self.worker.reschedule()
        self.worker.wake()
        self.refresh_icon()
        self.icon.menu = self.build_menu()
        try:
            self.icon.update_menu()
        except Exception:
            pass
        if hotkey_changed:
            self.hotkey_listener.apply(settings["hotkey_enabled"], settings["hotkey"])
        if autostart_changed and is_frozen():
            set_autostart(settings["autostart"])
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
            running = self.state.running
            wh_enabled = self.state.working_hours_enabled
            wh_start = self.state.working_hours_start
            wh_end = self.state.working_hours_end
            wh_days = self.state.working_days
            app_trigger_enabled = self.state.app_trigger_enabled
        if not running:
            return "off"
        if wh_enabled and not is_within_working_hours(wh_start, wh_end, wh_days):
            return "outside_hours"
        if self.worker.lock_paused:
            return "lock_paused"
        if self.worker.battery_paused:
            return "battery_paused"
        if app_trigger_enabled and not self.worker.app_trigger_active:
            return "waiting_app"
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
            "lock_paused": "paused — screen locked",
            "battery_paused": "paused — low battery",
            "waiting_app": "waiting for tracked app",
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
            self.state.active_profile = None
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

    def _on_hotkey_triggered(self):
        self.toggle_running()

    def set_interval(self, seconds):
        with self.state.lock:
            self.state.interval = seconds
            self.state.active_profile = None
        self.worker.reschedule()
        self.refresh_icon()
        self.save()

    def set_randomize(self, icon=None, item=None):
        with self.state.lock:
            self.state.randomize = not self.state.randomize
            self.state.active_profile = None
        self.worker.reschedule()
        self.save()

    def set_working_hours_enabled(self, icon=None, item=None):
        with self.state.lock:
            self.state.working_hours_enabled = not self.state.working_hours_enabled
            self.state.active_profile = None
        self.refresh_icon()
        self.save()

    def set_auto_stop(self, choice):
        with self.state.lock:
            self.state.auto_stop_choice = choice
            self.state.active_profile = None
            self.state._refresh_auto_stop_deadline()
        self.save()

    def set_notifications_enabled(self, icon=None, item=None):
        with self.state.lock:
            self.state.notifications_enabled = not self.state.notifications_enabled
            self.state.active_profile = None
        self.save()

    # -- profiles (Phase 2) -------------------------------------------------
    def apply_profile(self, name, icon=None, item=None):
        with self.state.lock:
            profile = self.state.profiles.get(name)
            if profile is None:
                return
            for key in PROFILE_KEYS:
                if key in profile:
                    setattr(self.state, key, profile[key])
            self.state.active_profile = name
            self.state._refresh_auto_stop_deadline()
        self.worker.reschedule()
        self.refresh_icon()
        self.save()
        log_event("profile_applied", detail=name)
        self.notify(f'Applied profile "{name}".')

    def quit(self, icon=None, item=None):
        log_event("app_quit")
        self.hotkey_listener.stop()
        self.worker.stop()
        self.worker.join(timeout=1.0)
        save_settings(self.state.as_dict())
        self.icon.stop()

    # -- menu text/state helpers -------------------------------------------------
    def _status_text(self, item):
        status = self.current_status()
        return {
            "on": "🟢  Keeping online",
            "off": "⚪  Keep-online is off",
            "outside_hours": "🌙  Outside working hours",
            "lock_paused": "🔒  Paused — screen locked",
            "battery_paused": "🔋  Paused — low battery",
            "waiting_app": "⏳  Waiting for tracked app…",
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
            return f"Active for {duration} · last activity {ago}s ago"
        return f"Active for {duration} · no activity yet"

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

        def profile_item(name):
            def checked(item):
                with self.state.lock:
                    return self.state.active_profile == name

            def action(icon, item):
                self.apply_profile(name)

            return pystray.MenuItem(name, action, checked=checked, radio=True)

        interval_submenu = pystray.MenuItem(
            self._interval_menu_title,
            pystray.Menu(*[interval_item(s) for s in INTERVAL_CHOICES]),
        )
        auto_stop_submenu = pystray.MenuItem(
            self._auto_stop_menu_title,
            pystray.Menu(*[auto_stop_item(c) for c in AUTO_STOP_CHOICES]),
        )

        with self.state.lock:
            profile_names = sorted(self.state.profiles.keys())
        if profile_names:
            profiles_submenu = pystray.Menu(
                *[profile_item(n) for n in profile_names],
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Save current as… (opens Settings)", self.open_settings),
            )
        else:
            profiles_submenu = pystray.Menu(
                pystray.MenuItem("No profiles yet — add one in Settings", self.open_settings),
            )
        profiles_menu_item = pystray.MenuItem("Profiles", profiles_submenu)

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
            profiles_menu_item,
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
            setup_macos_menu_bar_only()
            setup_macos_double_click(icon, self)

        with self.state.lock:
            hotkey_enabled = self.state.hotkey_enabled
            hotkey = self.state.hotkey
        self.hotkey_listener.apply(hotkey_enabled, hotkey)

        if self.first_run:
            startup_msg = (
                f"{APP_NAME} is running — find it in the system tray. "
                "Double-click the icon to open Settings and get started."
            )
        else:
            startup_msg = f"{APP_NAME} is running — find it in the system tray. Double-click the icon for settings."
        try:
            icon.notify(startup_msg, APP_NAME)
        except Exception:
            pass

        # macOS: proactively request Accessibility permission. We ask
        # whenever it isn't granted yet — not just on first run — so a user
        # who dismissed it once still gets prompted on the next launch
        # instead of silently having dead keypresses. The system dialog has
        # an "Open System Settings" button that lands on the right pane.
        if sys.platform == "darwin" and not macos_accessibility_trusted():
            request_macos_accessibility()
            self.notify(
                "Enable Unidle under Accessibility (and Input Monitoring, if "
                "keypresses still don't work), then it's ready to go.",
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

        _write_settings_pid()
        try:
            run_settings_window(CONFIG_PATH)
        finally:
            _clear_settings_pid()
        return

    if not acquire_single_instance():
        print(f"{APP_NAME} is already running.", file=sys.stderr)
        show_already_running_alert()
        sys.exit(0)

    settings, first_run = load_settings()
    app = UnidleApp(settings, first_run)
    try:
        app.run()
    except KeyboardInterrupt:
        app.quit()


if __name__ == "__main__":
    main()
