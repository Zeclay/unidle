# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Unidle — a Windows/macOS system-tray app that keeps Microsoft Teams (or anything watching OS idle time) from going "Away" by sending an invisible F15 keypress (or a mouse/scroll nudge) on an interval. No Teams/Graph API, no credentials; the only side effect is the activity itself. PLAN-5 added energy-efficiency work (adaptive scheduler, lock/battery awareness, prevent-sleep), smart idle detection, alternate activity modes, a global hotkey, app-aware triggering, auto-start, and profiles — see `PLAN-5.md` for the design rationale.

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

**The config schema is a cross-file contract.** `SETTINGS_KEYS`, `INTERVAL_CHOICES`, `AUTO_STOP_CHOICES`, `PROFILE_KEYS`, and the validation helpers are deliberately duplicated in both `unidle.py` and `settings_ui.py` (no shared import, to keep each file self-contained for PyInstaller onefile). Both files build `AppState`/`DEFAULT_SETTINGS` from the same `SETTINGS_KEYS` tuple via a loop, so adding a key to the tuple + both `default_settings()`/`DEFAULT_SETTINGS` dicts + both validation blocks is enough — there's no separate list of `AppState` attributes or `as_dict()` fields to keep in sync. If you add/change a settings key, update both files, keep old keys loadable (users have existing `~/.unidle.json` files), and check `migrate_legacy_files()` — it still migrates pre-rename `~/.always_online*` files. `PROFILE_KEYS` is `SETTINGS_KEYS` minus `profiles`/`active_profile` — a profile snapshot must never nest the profiles dict inside itself.

**State files** (all in the user's home dir, all safe to delete): `.unidle.json` (settings), `.unidle_log.jsonl` (activity log, capped at 500 lines by `_truncate_log_if_needed`), `.unidle_last.json` (last-activity timestamp, read by the Settings Activity card every ~5s). Activity is only logged on the first send after a resume — never per-send — to avoid spam.

**Worker loop** (`Worker.run`) is an adaptive scheduler, not a fixed tick: it sleeps on `self._wake_event.wait(timeout)` until the next thing that could matter — the next scheduled activity, an auto-stop deadline, the next working-hours edge (`seconds_until_next_working_hours_edge`), a pause-state recheck (lock/battery/app-trigger/smart-idle each hint their own recheck delay from `_should_send`), or, only while the Settings window is open, a 1s poll (`SETTINGS_POLL_SECONDS`) so hot-reload still feels instant while editing — plus a `MAX_SLEEP_SECONDS` safety fallback so a missed `wake()` call self-heals. **Any code path that changes state must call `worker.wake()`** or the change won't take effect until the next natural wake — `UnidleApp.save()` calls it centrally so most setters get this for free by virtue of calling `save()`; `check_config_reload()` calls it explicitly since it doesn't go through `save()`. This was the single biggest risk in the PLAN-5 rewrite (a forgotten `wake()` call manifests as "the toggle doesn't do anything for a while, then suddenly does").

Smart idle compensates for its own side effect: our own F15/mouse/scroll send resets the OS idle timer, so naively reading "seconds since last input" would make the app think the user is perpetually active. `Worker._effective_idle_seconds()` tracks a `_true_idle_anchor` — the last moment a *real* human input was detected — and only advances it when the OS-reported idle time is shorter than the time since our own last send (i.e. something newer than our own synthetic input happened).

**Tray UI is drawn in code.** `build_icon_image()` renders the icon states with Pillow at 64px for HiDPI: on = green dot, off = hollow gray ring, and everything else (`PAUSED_STATUSES`: out-of-working-hours, screen locked, low battery, waiting for a tracked app) shares one gray + clock badge visual — they're all "temporarily not sending for a reason visible in the status text/title," so they don't each get their own icon variant. The app logo (`assets/generate_icon.py`) is a separate thing: it produces the .ico/.icns/.png used by PyInstaller and the Settings window title bar, regenerated at build time.

**Platform layer** (idle/lock/battery/prevent-sleep/app-trigger/autostart, all in `unidle.py` between the macOS double-click hook and the `Worker` class) is per-OS functions behind `sys.platform` checks, each wrapped in try/except so an unsupported OS/API degrades to a safe default instead of crashing: idle detection failing returns `None` (treated as "assume idle," the pre-PLAN-5 behavior), lock detection failing returns `False` (assume unlocked, don't block). **`SetThreadExecutionState` (Windows prevent-sleep) is per-thread** — it must only ever be called from the worker thread, never from the tray/main thread, which is why cleanup on quit happens inside `Worker.run()`'s own loop-exit path (after `_stop_event` is set) rather than in `UnidleApp.quit()` directly; `quit()` calls `worker.join(timeout=1.0)` to give that cleanup a chance to run before the process exits. `psutil` (battery + process list) is a real dependency now, imported unconditionally at the top of `unidle.py`.

**Fragile spot — macOS double-click:** `setup_macos_double_click()` reaches into pystray's Cocoa internals (NSStatusItem, clickCount) to make double-click open Settings. pystray is pinned to 0.19.5 because of this. The whole hook is wrapped in feature-detection with a fallback to normal menu behavior; keep it that way, and don't let any change there raise.

**Settings window HTML** is one embedded string (`_HTML` in `settings_ui.py`) — HTML/CSS/JS inline, talking to Python through pywebview's `js_api` (`Api` class). No HTTP server, no external files. Windows needs the EdgeWebView2 runtime (README covers the user-facing note).

**Single instance** is a localhost socket bind on port 58731 (`acquire_single_instance`) — the OS releases it on crash, so there's no stale-lock handling. The `--settings` process intentionally skips this guard.

## Repo conventions

- `PLAN.md` … `PLAN-4.md` are the historical, already-implemented work plans; `IDEA.md` is the researched feature backlog (with a decided "won't do" list — check it before proposing features). Don't edit old PLAN files retroactively.
- The project was renamed from "Always Online" to "Unidle" (PLAN-4). Don't reintroduce the old name anywhere except the migration code and historical PLAN files.
- User-facing copy is English, sentence case, no dev jargon (settings labels, menu items, notifications).
