<img src="assets/icon_256.png" width="96" height="96" alt="Unidle icon">

# Unidle

*Un-idle — keeps your status from going idle.*

A small system tray / menu bar app that keeps **Microsoft Teams, Slack,
Zoom, Webex — or anything else that watches OS idle time** — from flipping
your status to "Away". It works by sending an invisible **F15** keypress
on an interval — F15 has no effect on anything, it just resets the
operating system's idle timer.

Because every one of these apps reads the same OS idle timer, one
mechanism covers them all: there is nothing to configure per app, and
apps you install later are covered automatically.

No API access to any of these services, no reading of your data, no
credentials stored. It only ever does one thing: press a harmless key
every so often.

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
- A small local activity log (toggles, auto-stop, working-hours changes —
  no keystroke content, ever) is kept at `~/.unidle_log.jsonl` (capped to
  the most recent 500 entries) plus a one-line `~/.unidle_last.json` with
  the last keypress time. Both feed the Activity card in the Settings
  window; delete them any time, they're regenerated as needed.

## Settings window

Pick **Settings…** from the tray menu (or double-click the tray icon) to
open a small GUI window with everything in one place: the keep-online
toggle, interval, randomize, working hours (days + start/end time),
auto-stop, notifications, and an **Activity** card showing when the app
last sent a keypress and a short history of recent events (toggles,
auto-stop, entering/leaving working hours). A summary line updates live
as you change values, and **Save** writes `~/.unidle.json` and applies
the change to the running tray app within a couple of seconds — no
restart needed.

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
