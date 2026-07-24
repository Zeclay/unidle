<img src="assets/icon_256.png" width="96" height="96" alt="Unidle icon">

# Unidle

*Un-idle — keeps your status from going idle.*

A small system tray / menu bar app that keeps **Microsoft Teams, Slack,
Zoom, Webex — or anything else that watches OS idle time** — from flipping
your status to "Away". By default it works by sending an invisible **F15**
keypress on an interval — F15 has no effect on anything, it just resets
the operating system's idle timer. Mouse/scroll nudges are available as
alternate activity modes if something in your environment filters
synthetic key presses.

Because every one of these apps reads the same OS idle timer, one
mechanism covers them all: there is nothing to configure per app, and
apps you install later are covered automatically.

No API access to any of these services, no reading of your data, no
credentials stored. It only ever does one thing: send a harmless bit of
activity every so often — and, with **Smart idle** on (the default), only
when you're actually away.

Unidle is also built to use close to no energy at idle: the worker thread
sleeps until the next thing that could actually matter (the next
scheduled activity, an auto-stop deadline, a working-hours edge) instead
of waking up every second, and it pauses itself automatically when your
screen is locked or your battery is low.

### Picking an interval

Each app flips you to "Away" after its own period of inactivity, so any
interval shorter than your strictest app works for everything:

| App | Goes "Away" after (approx.) |
|---|---|
| Microsoft Teams | ~5 minutes |
| Slack | ~30 minutes |
| Zoom / Webex | varies (often admin-configured) |

The default interval (1 minute) is comfortably inside all of these.
These thresholds are the apps' defaults and can change or be overridden
by your organization.

## Requirements

- Python 3.10+
- Windows or macOS

## Run from source

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS
source venv/bin/activate

pip install -r requirements.txt
python unidle.py
```

An icon appears in the system tray (Windows) or menu bar (macOS).

## Build a standalone executable

You must build on the target OS — PyInstaller does not cross-build.

**Windows:**

```bat
build_windows.bat
```

Produces `dist\Unidle.exe` — a single file, no installer needed.

**macOS:**

```bash
chmod +x build_macos.sh
./build_macos.sh
```

Produces `dist/Unidle.app`.

### Getting builds without owning both machines: GitHub Actions

If this project is pushed to a GitHub repo, `.github/workflows/build.yml`
will build both the Windows `.exe` and macOS `.app` for you automatically
(GitHub's free tier is plenty for this).

- Push a tag like `v1.0.0` to trigger a build and attach both files to a
  GitHub Release:

  ```bash
  git tag v1.0.0
  git push origin v1.0.0
  ```

- Or trigger it manually from the Actions tab (`workflow_dispatch`).
- Either way, download the built files from the workflow run's
  **Artifacts** section, or from the repo's **Releases** page if it was
  triggered by a tag.

## Using the tray menu

- **Settings…** — opens the Settings window (see below) for editing
  everything in one place. Double-clicking the tray icon does the same.
- **Keep online** — top toggle, turns keypresses on/off.
- **Interval** — how often to send the keypress (30s / 1min / 2min / 5min).
- **Follow working hours** — when on, keypresses only happen inside the
  configured schedule (default Mon–Fri 09:00–18:00). Edit the days/hours
  via the Settings window.
- **Auto-stop** — automatically turns "Keep online" off after 1h / 4h / 8h.
  Resets whenever you toggle it back on.
- **Randomize interval** — jitters the interval by ±25% so keypresses
  don't land on a suspiciously exact clock tick.
- **Notifications** — small desktop notifications on toggle/auto-stop
  events. Off by default.
- The status line and runtime indicator (how long it's been active, time
  since the last keypress) are read-only and update whenever you open the
  menu.
- All of the above is saved to `~/.unidle.json` and restored the next time
  you launch the app. Opening a second copy while one is already running
  just prints a message and exits immediately — it won't double up on
  tray icons or keypresses.
- A small local activity log (toggles, auto-stop, working-hours changes,
  lock/battery pauses — no keystroke content, ever) is kept at
  `~/.unidle_log.jsonl` (capped to the most recent 500 entries) plus a
  one-line `~/.unidle_last.json` with the last activity time. Both feed
  the Activity card in the Settings window; delete them any time, they're
  regenerated as needed.
- **Profiles** — save the current settings as a named profile from the
  Settings window, then switch between them from the tray's **Profiles**
  submenu (e.g. a "Work" profile with working hours on, and an "OnCall"
  profile with them off). Changing any setting after applying a profile
  clears the active-profile marker — it's now a custom, unsaved mix.

## Settings window

Pick **Settings…** from the tray menu (or double-click the tray icon) to
open a small GUI window with everything in one place: the keep-online
toggle, interval, randomize, working hours (days + start/end time),
auto-stop, notifications, activity mode, power/energy options, a global
hotkey, app-aware triggering, start-at-login, profiles, and an
**Activity** card showing when the app last sent activity and a short
history of recent events. A summary line updates live as you change
values, and **Save** writes `~/.unidle.json` and applies the change to
the running tray app within a couple of seconds — no restart needed.

### Activity mode and smart idle

- **Activity mode** — `F15 key` (default, invisible everywhere), `Mouse`
  (nudges the cursor 1px and back), or `Scroll` (scrolls one unit and
  back). Only switch off F15 if something in your setup actually filters
  synthetic key events.
- **Smart idle** (on by default) — only sends activity once you've
  actually been away for a configurable number of seconds, instead of
  sending on a fixed clock regardless of whether you're at the keyboard.
  This also keeps the activity log meaningfully sparse.

### Power options

- **Keep system awake** — in addition to the periodic activity, actively
  tells the OS not to sleep (`SetThreadExecutionState` on Windows,
  `caffeinate -i` on macOS). The display can still turn off.
- **Keep display awake** — the stronger version: the screen never dims
  or turns off either. Uses more battery — off by default, and the
  Settings window says so.
- **Pause on low battery** (on by default, 20%) — automatically pauses
  when unplugged and below the threshold, and resumes once you plug in
  or charge back up past it (with a small buffer so it doesn't flap
  right at the threshold).

### Global hotkey

Optionally bind a global hotkey (default `<ctrl>+<alt>+u`, off by
default) to toggle Keep online from anywhere, using
[pynput's `GlobalHotKeys` format](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys).
On macOS this needs the same Accessibility permission as sending
activity.

### App-aware trigger

Optionally restrict activity to times when a tracked app (Teams, Slack,
Zoom, Webex, Discord, or your own custom process names) is actually
running — checked about once a minute so it doesn't add wake-ups. Google
Chat is web-based and can't be detected this way.

### Start at login

Launches Unidle automatically when you log in (Windows: a `Run` registry
value; macOS: a `LaunchAgent`). Only available in a **built** app — from
source, `sys.executable` is the Python interpreter, not something worth
auto-starting, so the toggle is disabled with an explanation.

The window runs as a separate process from the tray app (this keeps
`pywebview` off the tray app's own thread, which matters on macOS), so
opening it doesn't block the tray icon, and closing it without saving
just discards your edits after a confirm prompt.

**Windows:** the Settings window needs the **Microsoft Edge WebView2
Runtime**. It ships with Windows 10/11 by default; if it's missing,
pywebview will fail to open the window — install it from
[Microsoft's WebView2 download page](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)
if that happens.

**macOS:** uses the built-in WKWebView, no extra runtime needed.

## Important notes

**macOS permissions:** the app sends keypresses via `pynput`, which
requires Accessibility permission (and sometimes Input Monitoring) on
macOS. If keypresses don't seem to do anything:

1. Open **System Settings → Privacy & Security → Accessibility**
2. Add/enable the app (or your terminal, if running from source)
3. Do the same under **Input Monitoring** if it's still not working

**macOS Gatekeeper:** since the built app isn't signed/notarized, the
first launch will be blocked. Right-click the app → **Open** to bypass
this once; after that it opens normally.

**Antivirus false positives:** PyInstaller `--onefile` binaries are
sometimes flagged by antivirus software as suspicious purely because of
how they're packaged (a self-extracting single executable), not because
of anything the code does. If this happens, allowlist the file or switch
the build to `--onedir` in `build_windows.bat` / `build_macos.sh`.

**Please use responsibly.** This is meant to smooth over short breaks so
your status doesn't flicker to Away while you're still around — it isn't
a substitute for actually being available, and using it to misrepresent
your availability may run against your company's policies.
