"""Unidle — Settings window.

A pywebview GUI for editing ``~/.unidle.json`` without going through
the tray submenus one at a time. Runs as a separate process from the tray
app (see unidle.py's ``--settings`` flag) because pystray and
pywebview both want the main thread on macOS.

HTML/CSS/JS is embedded as a string rather than split into separate files
so a PyInstaller ``--onefile`` build has nothing extra to bundle.
"""

import json
import os
import sys
import threading
from datetime import datetime

import webview

INTERVAL_CHOICES = [30, 60, 120, 300]
AUTO_STOP_CHOICES = {"Off": 0, "1h": 3600, "4h": 4 * 3600, "8h": 8 * 3600}

LOG_PATH = os.path.join(os.path.expanduser("~"), ".unidle_log.jsonl")
LAST_KEYPRESS_PATH = os.path.join(os.path.expanduser("~"), ".unidle_last.json")
ACTIVITY_EVENTS_SHOWN = 10

# (icon, human text). "auto_stop" gets its duration appended when present.
EVENT_TEXT = {
    "app_start": ("🚀", "App started"),
    "app_quit": ("🛑", "App quit"),
    "toggle_on": ("🟢", "Keeping online turned on"),
    "toggle_off": ("⚪", "Keeping online turned off"),
    "auto_stop": ("⏱️", "Auto-stopped"),
    "wh_enter": ("🌤️", "Entered working hours"),
    "wh_exit": ("🌙", "Left working hours"),
    "keypress": ("⌨️", "Resumed sending keypresses"),
}

DEFAULT_SETTINGS = {
    "running": True,
    "interval": 60,
    "randomize": True,
    "working_hours_enabled": False,
    "working_hours_start": "09:00",
    "working_hours_end": "18:00",
    "working_days": [0, 1, 2, 3, 4],
    "auto_stop_choice": "Off",
    "notifications_enabled": False,
}

SETTINGS_KEYS = tuple(DEFAULT_SETTINGS.keys())


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


def _load(config_path):
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in SETTINGS_KEYS:
            if key in data:
                settings[key] = data[key]
        if settings["interval"] not in INTERVAL_CHOICES:
            settings["interval"] = DEFAULT_SETTINGS["interval"]
        if settings["auto_stop_choice"] not in AUTO_STOP_CHOICES:
            settings["auto_stop_choice"] = DEFAULT_SETTINGS["auto_stop_choice"]
        if not _is_valid_time_str(settings["working_hours_start"]):
            settings["working_hours_start"] = DEFAULT_SETTINGS["working_hours_start"]
        if not _is_valid_time_str(settings["working_hours_end"]):
            settings["working_hours_end"] = DEFAULT_SETTINGS["working_hours_end"]
        if not _is_valid_days(settings["working_days"]):
            settings["working_days"] = list(DEFAULT_SETTINGS["working_days"])
        for bool_key in ("running", "randomize", "working_hours_enabled", "notifications_enabled"):
            if not isinstance(settings[bool_key], bool):
                settings[bool_key] = DEFAULT_SETTINGS[bool_key]
    except Exception:
        pass
    return settings


def _parse_ts(ts_str):
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def _format_relative_or_time(dt):
    now = datetime.now().astimezone()
    if dt.tzinfo is None:
        dt = dt.astimezone()
    delta = (now - dt).total_seconds()
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    return dt.strftime("%H:%M")


def _read_activity():
    last_text = "No activity yet"
    try:
        with open(LAST_KEYPRESS_PATH, "r", encoding="utf-8") as f:
            last_data = json.load(f)
        dt = _parse_ts(last_data.get("ts", ""))
        if dt is not None:
            last_text = "Last keypress: " + _format_relative_or_time(dt)
    except Exception:
        pass

    events = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines[-ACTIVITY_EVENTS_SHOWN:]):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            event = entry.get("event")
            if event not in EVENT_TEXT:
                continue
            dt = _parse_ts(entry.get("ts", ""))
            if dt is None:
                continue
            icon, text = EVENT_TEXT[event]
            detail = entry.get("detail")
            if event == "auto_stop" and detail:
                text = f"Auto-stopped after {detail}"
            events.append({"icon": icon, "text": text, "time_text": _format_relative_or_time(dt)})
    except Exception:
        pass

    return {"last_keypress_text": last_text, "events": events}


def _save(config_path, data):
    # Merge onto whatever is already on disk so keys this window doesn't
    # know about (future versions, manual edits) survive untouched.
    merged = {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            merged = json.load(f)
    except Exception:
        merged = {}
    for key in SETTINGS_KEYS:
        if key in data:
            merged[key] = data[key]
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp_path, config_path)


class Api:
    def __init__(self, config_path):
        # Leading underscore matters: pywebview's js_api introspection walks
        # every public, non-callable attribute recursively to build the JS
        # bridge. A public `window` attribute drags in the native WinForms
        # control tree and hangs on `.native.AccessibleObject.Bounds.Empty`
        # (infinite recursion, no cycle detection). Underscore-prefixed
        # attributes are skipped by that introspection.
        self.config_path = config_path
        self._window = None
        self._force_close = False
        # Pushed from JS on every render() (fire-and-forget, JS->Python is
        # the safe direction). The native close handler reads this instead
        # of calling evaluate_js — see run_settings_window()'s on_closing
        # for why the reverse direction deadlocks.
        self._is_dirty = False

    def load(self):
        return _load(self.config_path)

    def defaults(self):
        return dict(DEFAULT_SETTINGS)

    def notify_dirty(self, is_dirty):
        self._is_dirty = bool(is_dirty)

    def activity(self):
        try:
            return _read_activity()
        except Exception:
            return {"last_keypress_text": "No activity yet", "events": []}

    def save(self, payload):
        try:
            data = dict(payload)
            if data.get("interval") not in INTERVAL_CHOICES:
                return {"ok": False, "error": "Invalid interval"}
            if data.get("auto_stop_choice") not in AUTO_STOP_CHOICES:
                return {"ok": False, "error": "Invalid auto-stop choice"}
            if not _is_valid_time_str(data.get("working_hours_start")):
                return {"ok": False, "error": "Invalid start time"}
            if not _is_valid_time_str(data.get("working_hours_end")):
                return {"ok": False, "error": "Invalid end time"}
            if not _is_valid_days(data.get("working_days")):
                return {"ok": False, "error": "Select at least one day"}
            _save(self.config_path, data)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def close(self):
        self._force_close = True
        if self._window is not None:
            self._window.destroy()


def _resolve_icon_path():
    # Only set when running from source: a frozen build's own .exe/.app icon
    # (baked in at build time via PyInstaller --icon) already covers the
    # common case, and bundling assets/icon.ico as a runtime data file just
    # for this cosmetic touch isn't worth the added build complexity.
    if getattr(sys, "frozen", False):
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "assets", "icon.ico")
    return path if os.path.exists(path) else None


def run_settings_window(config_path):
    """Blocking call: opens the Settings window and returns once it closes."""
    api = Api(config_path)
    window = webview.create_window(
        "Unidle — Settings",
        html=_HTML,
        js_api=api,
        width=440,
        height=600,
        resizable=True,
        min_size=(380, 520),
    )
    api._window = window

    def on_closing():
        # FormClosing runs synchronously on the UI thread. Calling
        # evaluate_js() from here would block waiting on a result that can
        # only arrive via that same thread's message pump -> deadlock (the
        # window hangs as "Not Responding"). So this only ever reads a
        # plain Python flag pushed from JS ahead of time (see notify_dirty),
        # and defers the JS call that shows the confirm modal onto a
        # separate thread, after this handler has returned and the UI
        # thread is free to pump messages again.
        if api._force_close:
            return True
        if not api._is_dirty:
            return True

        def _prompt_on_ui_thread():
            try:
                window.evaluate_js(
                    "window.__promptCloseConfirm && window.__promptCloseConfirm()"
                )
            except Exception:
                pass

        threading.Thread(target=_prompt_on_ui_thread, daemon=True).start()
        return False

    window.events.closing += on_closing
    webview.start(icon=_resolve_icon_path())


_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  :root {
    --accent: #6BB700;
    --accent-contrast: #ffffff;
    --bg: #f5f6f7;
    --card-bg: #ffffff;
    --text: #1b1f23;
    --text-muted: #5c6570;
    --border: #e2e5e8;
    --danger: #d64545;
    --shadow: 0 1px 2px rgba(0,0,0,0.06), 0 4px 12px rgba(0,0,0,0.05);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1c1e22;
      --card-bg: #26292e;
      --text: #f0f1f3;
      --text-muted: #9aa2ab;
      --border: #383c42;
      --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 4px 16px rgba(0,0,0,0.35);
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 13px;
  }
  button, input { font-family: inherit; }
  *:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  #app { display: flex; flex-direction: column; height: 100%; overflow: hidden; }
  .scroll {
    flex: 1; overflow-y: auto; overflow-x: hidden;
    scrollbar-width: thin; scrollbar-color: rgba(120,120,120,.35) transparent;
  }
  .scroll::-webkit-scrollbar { width: 6px; }
  .scroll::-webkit-scrollbar-track { background: transparent; }
  .scroll::-webkit-scrollbar-thumb { background: transparent; border-radius: 999px; }
  .scroll:hover::-webkit-scrollbar-thumb { background: rgba(120,120,120,.35); }
  .scroll::-webkit-scrollbar-thumb:hover { background: rgba(120,120,120,.55); }

  .hero {
    display: flex; align-items: center; justify-content: space-between;
    padding: 20px 20px 16px; flex: none;
    border-bottom: 1px solid transparent;
    transition: border-color .15s, box-shadow .15s;
  }
  .hero.scrolled { border-bottom-color: var(--border); box-shadow: 0 4px 10px -6px rgba(0,0,0,.25); }
  .hero-left { display: flex; align-items: center; gap: 12px; }
  .status-dot {
    width: 14px; height: 14px; border-radius: 50%;
    background: var(--border); transition: .15s;
  }
  .status-dot.on { background: var(--accent); box-shadow: 0 0 0 4px rgba(107,183,0,.18); }
  .hero-text { font-size: 16px; font-weight: 600; }

  .summary { padding: 16px 20px 16px; font-size: 12px; color: var(--text-muted); line-height: 1.5; }

  .card {
    background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px;
    padding: 16px; margin: 0 20px 16px;
    box-shadow: var(--shadow);
    transition: opacity .15s ease;
  }
  .card-header { display: flex; align-items: center; justify-content: space-between; }
  .card-title { font-size: 14px; font-weight: 600; }
  .card-sub { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
  .card-body { margin-top: 12px; }
  .card-body.dimmed { opacity: .4; pointer-events: none; }
  .row { display: flex; align-items: center; justify-content: space-between; }
  .row + .row { margin-top: 12px; }

  .switch { position: relative; width: 40px; height: 24px; flex: none; }
  .switch input { opacity: 0; width: 0; height: 0; position: absolute; }
  .slider {
    position: absolute; inset: 0; background: var(--border); border-radius: 999px;
    transition: .15s; cursor: pointer;
  }
  .slider:before {
    content: ""; position: absolute; width: 18px; height: 18px; left: 3px; top: 3px;
    background: #fff; border-radius: 50%; transition: .15s;
    box-shadow: 0 1px 2px rgba(0,0,0,.3);
  }
  .switch input:checked + .slider { background: var(--accent); }
  .switch input:checked + .slider:before { transform: translateX(16px); }
  .switch input:focus-visible + .slider { outline: 2px solid var(--accent); outline-offset: 2px; }
  .switch.big { width: 48px; height: 28px; }
  .switch.big .slider:before { width: 22px; height: 22px; }
  .switch.big input:checked + .slider:before { transform: translateX(20px); }

  .segmented {
    display: flex; background: var(--bg); border: 1px solid var(--border);
    border-radius: 999px; padding: 3px; gap: 2px;
  }
  .segmented button {
    flex: 1; border: none; background: transparent; padding: 6px 8px;
    border-radius: 999px; font-size: 12px; color: var(--text-muted);
    cursor: pointer; transition: .15s;
  }
  .segmented button.active { background: var(--accent); color: var(--accent-contrast); font-weight: 600; }

  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip {
    border: 1px solid var(--border); background: var(--card-bg); border-radius: 999px;
    padding: 6px 10px; font-size: 12px; cursor: pointer; color: var(--text);
    transition: .15s;
  }
  .chip.active { background: var(--accent); border-color: var(--accent); color: var(--accent-contrast); font-weight: 600; }

  .time-row { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
  .time-field { display: flex; flex-direction: column; gap: 4px; flex: 1; }
  .time-field label { font-size: 11px; color: var(--text-muted); }
  input[type=time] {
    font-size: 13px; padding: 6px 8px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text); width: 100%;
  }
  input[type=time].invalid { border-color: var(--danger); }

  .error-text { color: var(--danger); font-size: 11px; margin-top: 6px; display: none; }
  .error-text.show { display: block; }

  .hint { font-size: 11px; color: var(--text-muted); margin-top: 8px; }

  .footer {
    border-top: 1px solid transparent; padding: 12px 20px;
    display: flex; align-items: center; justify-content: space-between;
    background: var(--bg); flex: none;
    transition: border-color .15s, box-shadow .15s;
  }
  .footer.scrolled { border-top-color: var(--border); box-shadow: 0 -4px 10px -6px rgba(0,0,0,.25); }

  .activity-headline { font-size: 12px; font-weight: 600; margin-bottom: 10px; }
  .activity-list { display: flex; flex-direction: column; gap: 10px; }
  .activity-row { display: flex; align-items: center; gap: 8px; font-size: 12px; }
  .activity-icon { flex: none; font-size: 13px; }
  .activity-text { flex: 1; color: var(--text); }
  .activity-time { flex: none; color: var(--text-muted); font-size: 11px; white-space: nowrap; }
  .activity-empty { color: var(--text-muted); font-size: 12px; }
  .btn {
    border: none; border-radius: 8px; padding: 9px 16px; font-size: 13px;
    font-weight: 600; cursor: pointer; transition: .15s;
  }
  .btn-primary { background: var(--accent); color: var(--accent-contrast); min-width: 84px; }
  .btn-primary:disabled { opacity: .4; cursor: not-allowed; }
  .btn-secondary { background: transparent; color: var(--text-muted); border: 1px solid var(--border); }
  .link-reset {
    font-size: 11px; color: var(--text-muted); text-decoration: underline;
    cursor: pointer; background: none; border: none; padding: 0;
  }
  .footer-left { display: flex; align-items: center; gap: 12px; }
  .footer-right { display: flex; align-items: center; gap: 8px; }

  .modal-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,.4);
    display: flex; align-items: center; justify-content: center;
    opacity: 0; pointer-events: none; transition: .15s;
  }
  .modal-overlay.show { opacity: 1; pointer-events: auto; }
  .modal { background: var(--card-bg); border-radius: 12px; padding: 20px; width: 260px; box-shadow: var(--shadow); }
  .modal h3 { font-size: 14px; margin: 0 0 8px; }
  .modal p { font-size: 12px; color: var(--text-muted); margin: 0 0 16px; }
  .modal-actions { display: flex; justify-content: flex-end; gap: 8px; }
</style>
</head>
<body>
<div id="app">
  <div class="hero" id="hero">
    <div class="hero-left">
      <div class="status-dot" id="statusDot"></div>
      <div class="hero-text" id="heroText">Keeping you online</div>
    </div>
    <label class="switch big">
      <input type="checkbox" id="runningToggle">
      <span class="slider"></span>
    </label>
  </div>
  <div class="scroll" id="scrollArea">
    <div class="summary" id="summaryLine"></div>

    <div class="card">
      <div class="card-header">
        <div class="card-title">Interval</div>
      </div>
      <div class="card-body">
        <div class="segmented" id="intervalSegmented">
          <button data-value="30">30s</button>
          <button data-value="60">1m</button>
          <button data-value="120">2m</button>
          <button data-value="300">5m</button>
        </div>
        <div class="row" style="margin-top: 14px;">
          <div>
            <div>Randomize timing</div>
            <div class="card-sub" id="randomizeHint"></div>
          </div>
          <label class="switch">
            <input type="checkbox" id="randomizeToggle">
            <span class="slider"></span>
          </label>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">Working hours</div>
          <div class="card-sub">Only send keypresses inside this schedule</div>
        </div>
        <label class="switch">
          <input type="checkbox" id="workingHoursToggle">
          <span class="slider"></span>
        </label>
      </div>
      <div class="card-body" id="workingHoursBody">
        <div class="chips" id="dayChips">
          <button class="chip" data-day="0">Mon</button>
          <button class="chip" data-day="1">Tue</button>
          <button class="chip" data-day="2">Wed</button>
          <button class="chip" data-day="3">Thu</button>
          <button class="chip" data-day="4">Fri</button>
          <button class="chip" data-day="5">Sat</button>
          <button class="chip" data-day="6">Sun</button>
        </div>
        <div class="error-text" id="daysError">Select at least one day.</div>
        <div class="time-row">
          <div class="time-field">
            <label for="startTime">Start</label>
            <input type="time" id="startTime">
          </div>
          <div class="time-field">
            <label for="endTime">End</label>
            <input type="time" id="endTime">
          </div>
        </div>
        <div class="error-text" id="timeError">End time must be after start time.</div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <div class="card-title">Auto-stop</div>
      </div>
      <div class="card-body">
        <div class="segmented" id="autoStopSegmented">
          <button data-value="Off">Off</button>
          <button data-value="1h">1h</button>
          <button data-value="4h">4h</button>
          <button data-value="8h">8h</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">Notifications</div>
          <div class="card-sub">Small alerts when toggling or auto-stopping</div>
        </div>
        <label class="switch">
          <input type="checkbox" id="notificationsToggle">
          <span class="slider"></span>
        </label>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <div class="card-title">Activity</div>
      </div>
      <div class="card-body">
        <div class="activity-headline" id="lastKeypressText">No activity yet</div>
        <div class="activity-list" id="activityList"></div>
      </div>
    </div>
  </div>

  <div class="footer" id="footer">
    <div class="footer-left">
      <button class="link-reset" id="resetLink">Reset to defaults</button>
    </div>
    <div class="footer-right">
      <button class="btn btn-secondary" id="cancelBtn">Cancel</button>
      <button class="btn btn-primary" id="saveBtn" disabled>Save</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="modalOverlay">
  <div class="modal">
    <h3 id="modalTitle"></h3>
    <p id="modalBody"></p>
    <div class="modal-actions">
      <button class="btn btn-secondary" id="modalSecondary"></button>
      <button class="btn btn-primary" id="modalPrimary"></button>
    </div>
  </div>
</div>

<script>
var DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
var INTERVAL_LABELS = {30: "30s", 60: "1m", 120: "2m", 300: "5m"};

var original = null;
var current = null;

function deepEqual(a, b) {
  return JSON.stringify(sortedCopy(a)) === JSON.stringify(sortedCopy(b));
}
function sortedCopy(obj) {
  var out = {};
  Object.keys(obj).sort().forEach(function(k) {
    var v = obj[k];
    out[k] = Array.isArray(v) ? v.slice().sort() : v;
  });
  return out;
}

function summarizeDays(days) {
  var sorted = days.slice().sort(function(a, b) { return a - b; });
  var key = sorted.join(",");
  if (key === "0,1,2,3,4,5,6") return "every day";
  if (key === "0,1,2,3,4") return "Mon–Fri";
  if (key === "0,1,2,3,4,5") return "Mon–Sat";
  if (key === "5,6") return "Sat–Sun";
  return sorted.map(function(d) { return DAY_NAMES[d]; }).join(", ");
}

function jitterRange(interval) {
  var jitter = interval * 0.25;
  var low = Math.max(1, Math.round(interval - jitter));
  var high = Math.round(interval + jitter);
  return low + "–" + high + "s";
}

function buildSummary() {
  if (!current.running) return "Paused — keypresses are off.";
  var parts = [];
  if (current.working_hours_enabled) {
    parts.push("Active " + summarizeDays(current.working_days) + ", " + current.working_hours_start + "–" + current.working_hours_end);
  } else {
    parts.push("Active anytime");
  }
  var intervalText = "keypress every ~" + INTERVAL_LABELS[current.interval];
  if (current.randomize) {
    intervalText = "keypress every ~" + jitterRange(current.interval);
  }
  parts.push(intervalText);
  if (current.auto_stop_choice !== "Off") {
    parts.push("auto-stop " + current.auto_stop_choice);
  }
  return parts.join(" · ");
}

function validate() {
  var errors = {};
  if (current.working_hours_enabled) {
    if (!current.working_days || current.working_days.length === 0) {
      errors.days = true;
    }
    if (current.working_hours_start >= current.working_hours_end) {
      errors.time = true;
    }
  }
  return errors;
}

function render() {
  document.getElementById("runningToggle").checked = current.running;
  document.getElementById("statusDot").classList.toggle("on", current.running);
  document.getElementById("heroText").textContent = current.running ? "Keeping you online" : "Paused";

  document.getElementById("summaryLine").textContent = buildSummary();

  setSegmented("intervalSegmented", String(current.interval));
  document.getElementById("randomizeToggle").checked = current.randomize;
  document.getElementById("randomizeHint").textContent = current.randomize
    ? "Will randomize between " + jitterRange(current.interval)
    : "Same interval every time";

  document.getElementById("workingHoursToggle").checked = current.working_hours_enabled;
  document.getElementById("workingHoursBody").classList.toggle("dimmed", !current.working_hours_enabled);
  document.querySelectorAll("#dayChips .chip").forEach(function(chip) {
    var day = parseInt(chip.getAttribute("data-day"), 10);
    chip.classList.toggle("active", current.working_days.indexOf(day) !== -1);
  });
  document.getElementById("startTime").value = current.working_hours_start;
  document.getElementById("endTime").value = current.working_hours_end;

  setSegmented("autoStopSegmented", current.auto_stop_choice);
  document.getElementById("notificationsToggle").checked = current.notifications_enabled;

  var errors = validate();
  document.getElementById("daysError").classList.toggle("show", !!errors.days);
  document.getElementById("timeError").classList.toggle("show", !!errors.time);
  document.getElementById("endTime").classList.toggle("invalid", !!errors.time);

  var dirty = !deepEqual(current, original);
  var hasErrors = Object.keys(errors).length > 0;
  document.getElementById("saveBtn").disabled = !dirty || hasErrors;
  pywebview.api.notify_dirty(dirty);
}

function setSegmented(containerId, value) {
  document.querySelectorAll("#" + containerId + " button").forEach(function(btn) {
    btn.classList.toggle("active", btn.getAttribute("data-value") === value);
  });
}

function bindEvents() {
  document.getElementById("runningToggle").addEventListener("change", function(e) {
    current.running = e.target.checked;
    render();
  });
  document.getElementById("randomizeToggle").addEventListener("change", function(e) {
    current.randomize = e.target.checked;
    render();
  });
  document.getElementById("workingHoursToggle").addEventListener("change", function(e) {
    current.working_hours_enabled = e.target.checked;
    render();
  });
  document.getElementById("notificationsToggle").addEventListener("change", function(e) {
    current.notifications_enabled = e.target.checked;
    render();
  });

  document.querySelectorAll("#intervalSegmented button").forEach(function(btn) {
    btn.addEventListener("click", function() {
      current.interval = parseInt(btn.getAttribute("data-value"), 10);
      render();
    });
  });
  document.querySelectorAll("#autoStopSegmented button").forEach(function(btn) {
    btn.addEventListener("click", function() {
      current.auto_stop_choice = btn.getAttribute("data-value");
      render();
    });
  });
  document.querySelectorAll("#dayChips .chip").forEach(function(chip) {
    chip.addEventListener("click", function() {
      var day = parseInt(chip.getAttribute("data-day"), 10);
      var idx = current.working_days.indexOf(day);
      if (idx === -1) {
        current.working_days.push(day);
      } else {
        current.working_days.splice(idx, 1);
      }
      render();
    });
  });
  document.getElementById("startTime").addEventListener("change", function(e) {
    current.working_hours_start = e.target.value;
    render();
  });
  document.getElementById("endTime").addEventListener("change", function(e) {
    current.working_hours_end = e.target.value;
    render();
  });

  document.getElementById("saveBtn").addEventListener("click", save);
  document.getElementById("cancelBtn").addEventListener("click", cancelOrClose);
  document.getElementById("resetLink").addEventListener("click", function() {
    showModal("reset");
  });

  document.addEventListener("keydown", function(e) {
    if (e.key === "Escape") {
      e.preventDefault();
      cancelOrClose();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      save();
    }
  });
}

function save() {
  var errors = validate();
  if (Object.keys(errors).length > 0) {
    render();
    return;
  }
  var saveBtn = document.getElementById("saveBtn");
  if (saveBtn.disabled) return;
  saveBtn.disabled = true;
  pywebview.api.save(current).then(function(res) {
    if (res.ok) {
      original = JSON.parse(JSON.stringify(current));
      saveBtn.textContent = "Saved ✓";
      setTimeout(function() {
        saveBtn.textContent = "Save";
        render();
      }, 600);
    } else {
      saveBtn.textContent = "Save";
      render();
    }
  });
}

function cancelOrClose() {
  if (!deepEqual(current, original)) {
    showModal("discard");
  } else {
    pywebview.api.close();
  }
}

function showModal(type) {
  var overlay = document.getElementById("modalOverlay");
  var title = document.getElementById("modalTitle");
  var body = document.getElementById("modalBody");
  var primary = document.getElementById("modalPrimary");
  var secondary = document.getElementById("modalSecondary");

  if (type === "discard") {
    title.textContent = "Discard changes?";
    body.textContent = "You have unsaved changes. Discard them and close?";
    secondary.textContent = "Keep editing";
    primary.textContent = "Discard";
    primary.onclick = function() {
      hideModal();
      pywebview.api.close();
    };
  } else if (type === "reset") {
    title.textContent = "Reset to defaults?";
    body.textContent = "This resets all fields in this form. Nothing is saved until you press Save.";
    secondary.textContent = "Cancel";
    primary.textContent = "Reset";
    primary.onclick = function() {
      hideModal();
      pywebview.api.defaults().then(function(defaults) {
        current = defaults;
        render();
      });
    };
  }
  secondary.onclick = hideModal;
  overlay.classList.add("show");
}

function hideModal() {
  document.getElementById("modalOverlay").classList.remove("show");
}

window.__promptCloseConfirm = function() {
  showModal("discard");
};

function setupScrollShadow() {
  var scrollArea = document.getElementById("scrollArea");
  var hero = document.getElementById("hero");
  var footer = document.getElementById("footer");
  function update() {
    var atTop = scrollArea.scrollTop <= 0;
    var atBottom = scrollArea.scrollTop + scrollArea.clientHeight >= scrollArea.scrollHeight - 1;
    hero.classList.toggle("scrolled", !atTop);
    footer.classList.toggle("scrolled", !atBottom);
  }
  scrollArea.addEventListener("scroll", update);
  window.addEventListener("resize", update);
  update();
}

function renderActivity(data) {
  document.getElementById("lastKeypressText").textContent = data.last_keypress_text;
  var list = document.getElementById("activityList");
  list.innerHTML = "";
  if (!data.events || data.events.length === 0) {
    var empty = document.createElement("div");
    empty.className = "activity-empty";
    empty.textContent = "No activity yet";
    list.appendChild(empty);
    return;
  }
  data.events.forEach(function(ev) {
    var row = document.createElement("div");
    row.className = "activity-row";

    var icon = document.createElement("span");
    icon.className = "activity-icon";
    icon.textContent = ev.icon;

    var text = document.createElement("span");
    text.className = "activity-text";
    text.textContent = ev.text;

    var time = document.createElement("span");
    time.className = "activity-time";
    time.textContent = ev.time_text;

    row.appendChild(icon);
    row.appendChild(text);
    row.appendChild(time);
    list.appendChild(row);
  });
}

function pollActivity() {
  pywebview.api.activity().then(renderActivity).catch(function() {
    renderActivity({last_keypress_text: "No activity yet", events: []});
  });
}

window.addEventListener("pywebviewready", function() {
  bindEvents();
  setupScrollShadow();
  pollActivity();
  setInterval(pollActivity, 5000);
  pywebview.api.load().then(function(data) {
    original = data;
    current = JSON.parse(JSON.stringify(data));
    render();
  });
});
</script>
</body>
</html>
"""
