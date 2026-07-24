# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Unidle — a Windows/macOS system-tray app that keeps Microsoft Teams (or anything watching OS idle time) from going "Away" by sending an invisible F15 keypress on an interval. No Teams/Graph API, no credentials; the only side effect is the keypress.

## Commands

```bash
# Run from source (needs a venv for the target OS; the checked-in venv/ is Linux-only)
pip install -r requirements.txt
python unidle.py               # tray app
python unidle.py --settings    # settings window alone (useful for UI work)

# Sanity check (this is what CI runs; there is no test suite)
python -m py_compile unidle.py settings_ui.py

# Icons are gitignored and generated, not checked in
python assets/generate_icon.py

# Standalone builds — must run on the target OS (PyInstaller can't cross-build)
build_windows.bat              # → dist/Unidle.exe
./build_macos.sh               # → dist/Unidle.app
```

CI (`.github/workflows/build.yml`) builds both platforms on a `v*` tag push or manual `workflow_dispatch`, and attaches zips to a GitHub Release for tags.

## Architecture

**Two processes, communicating only through files in `~`.** The tray app (`unidle.py`, pystray) and the Settings window (`settings_ui.py`, pywebview) cannot share a process: on macOS both libraries demand the main thread. So "Settings…" spawns the same executable again with `--settings` (see `main()` and `open_settings()` — the spawn differs for frozen vs. source). The settings process writes `~/.unidle.json` atomically on Save and exits; the tray app polls the file's mtime every worker tick and hot-reloads without restart.

**The config schema is a cross-file contract.** `SETTINGS_KEYS`, `INTERVAL_CHOICES`, `AUTO_STOP_CHOICES`, and the validation helpers are deliberately duplicated in both `unidle.py` and `settings_ui.py` (no shared import, to keep each file self-contained for PyInstaller onefile). If you add/change a settings key, update both files, keep old keys loadable (users have existing `~/.unidle.json` files), and check `migrate_legacy_files()` — it still migrates pre-rename `~/.always_online*` files.

**State files** (all in the user's home dir, all safe to delete): `.unidle.json` (settings), `.unidle_log.jsonl` (activity log, capped at 500 lines by `_truncate_log_if_needed`), `.unidle_last.json` (last-keypress timestamp, read by the Settings Activity card every ~5s). Keypresses are only logged on the first press after a resume — never per-press — to avoid spam.

**Worker loop** (`Worker.run`): wakes every `TICK_SECONDS` (1s) and, in order, checks config hot-reload, auto-stop deadline, and working-hours transitions before deciding whether to send F15. Toggle/interval changes therefore take effect within a second. Randomize jitters ±25% via `reschedule()`.

**Tray UI is drawn in code.** `build_icon_image()` renders the three icon states (on = green dot, off = gray, out-of-working-hours = gray + clock badge) with Pillow at 64px for HiDPI. The app logo (`assets/generate_icon.py`) is a separate thing: it produces the .ico/.icns/.png used by PyInstaller and the Settings window title bar, regenerated at build time.

**Fragile spot — macOS double-click:** `setup_macos_double_click()` reaches into pystray's Cocoa internals (NSStatusItem, clickCount) to make double-click open Settings. pystray is pinned to 0.19.5 because of this. The whole hook is wrapped in feature-detection with a fallback to normal menu behavior; keep it that way, and don't let any change there raise.

**Settings window HTML** is one embedded string (`_HTML` in `settings_ui.py`) — HTML/CSS/JS inline, talking to Python through pywebview's `js_api` (`Api` class). No HTTP server, no external files. Windows needs the EdgeWebView2 runtime (README covers the user-facing note).

**Single instance** is a localhost socket bind on port 58731 (`acquire_single_instance`) — the OS releases it on crash, so there's no stale-lock handling. The `--settings` process intentionally skips this guard.

## Repo conventions

- `PLAN.md` … `PLAN-4.md` are the historical, already-implemented work plans; `IDEA.md` is the researched feature backlog (with a decided "won't do" list — check it before proposing features). Don't edit old PLAN files retroactively.
- The project was renamed from "Always Online" to "Unidle" (PLAN-4). Don't reintroduce the old name anywhere except the migration code and historical PLAN files.
- User-facing copy is English, sentence case, no dev jargon (settings labels, menu items, notifications).
