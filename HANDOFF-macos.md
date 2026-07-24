# HANDOFF — macOS runtime testing & fixes (Unidle)

> สำหรับ Claude ที่รันบนเครื่อง **macOS จริง** รับช่วงต่อ
> งานทั้งหมดต้อง build เป็น `.app` แล้วทดสอบพฤติกรรมจริง (เครื่องที่เขียนโค้ดก่อนหน้าเป็น Linux/Windows รัน GUI macOS ไม่ได้)
> อ่าน `CLAUDE.md` ก่อนเริ่ม — โดยเฉพาะส่วน "Two processes", platform layer, และ macOS double-click hook

---

## 0. Context ปัจจุบัน

Unidle เป็น tray app กัน Teams/Slack/Zoom เหลือง (ส่ง F15 เป็นระยะ). โค้ดหลัก `unidle.py` (~1650 บรรทัด) + `settings_ui.py` (pywebview). ทุกฟีเจอร์ cross-platform มี branch macOS ครบแล้ว แต่ **ยังไม่เคยผ่านการรันจริงบน Mac สักครั้ง** — งานนี้คือ verify + fix สิ่งที่โผล่ตอนใช้จริง

commit ล่าสุดที่เกี่ยวข้อง (ทั้งหมด macOS-focused): menu-bar-only (no Dock icon), right-click เปิดเมนู, prompt Accessibility ตอนเปิด, fix focus loss หลังเปิดเมนู

## 1. Build & smoke test

```bash
cd <repo>
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
./build_macos.sh                 # → dist/Unidle.app (มี LSUIElement, ไม่มี Dock icon)

# ตรวจว่า LSUIElement ติดจริง
plutil -p dist/Unidle.app/Contents/Info.plist | grep LSUIElement   # ควรเห็น => 1
```

ครั้งแรกเปิด: คลิกขวา `Unidle.app` → Open (ผ่าน Gatekeeper), แล้วเปิด Accessibility ให้เมื่อ dialog เด้ง

---

## 2. งานหลัก #1 (P0) — Settings เปิดแล้วสร้าง Accessibility entry ที่สอง

**อาการ:** ใน System Settings → Accessibility มี 2 entry: `Unidle` และ `Unidle.app`

**สาเหตุ (ยืนยันจากโค้ด `UnidleApp.open_settings`):**
```python
if is_frozen():
    args = [sys.executable, "--settings"]     # ← ปัญหา
```
ตอนรันใน .app, `sys.executable` = `Unidle.app/Contents/MacOS/Unidle` (binary ข้างใน) การ Popen ตรงๆ ไม่ผ่าน LaunchServices → macOS TCC มองเป็นคนละ identity กับบันเดิล → เกิด entry ที่สอง

**สิ่งที่ต้องทำ:**
- เปลี่ยนวิธี spawn ให้เปิดผ่านบันเดิลเดียวกันบน macOS frozen เช่น
  `open -n -a "<path to Unidle.app>" --args --settings`
  (หา bundle path จาก `sys.executable`: ตัด `/Contents/MacOS/Unidle` ออก)
- **ปัญหาที่ตามมา:** `open` คืนค่าทันที → `self._settings_process.poll()` เดิมใช้ track ว่า Settings เปิดอยู่ไหมไม่ได้อีก (ใช้ใน `settings_window_open()` เพื่อกันเปิดซ้อน + เร่ง poll cadence ตอนแก้ค่า)
  - ทางแก้: ให้ settings process เขียน **pid/marker file** (เช่น `~/.unidle_settings.pid`) ตอนเริ่ม แล้วลบตอนปิด; `settings_window_open()` เช็คไฟล์นี้ + ตรวจว่า pid ยังมีชีวิต (`os.kill(pid, 0)`)
  - หรือใช้ localhost socket แบบ single-instance ที่มีอยู่แล้วเป็นแบบอย่าง
- **fallback:** ถ้า `open` ล้ม → กลับไปใช้ `[sys.executable, "--settings"]` เดิม (อย่าให้ Settings เปิดไม่ได้)
- verify: เปิด/ปิด Settings หลายรอบ → ต้องมี Accessibility entry เดียว, กันเปิดซ้อนยังทำงาน, hot-reload ยังไว (≤2s)

**หมายเหตุ:** Settings window (pywebview) ไม่ต้องใช้สิทธิ์ Accessibility เลย — ถ้าแก้ให้ identity เดียว entry ที่สองจะหายไปเอง

---

## 3. งานหลัก #2 (P0) — ยืนยันว่า Teams ไม่เหลืองจริง

**อาการที่ผู้ใช้เจอ:** Teams ยังเหลืองทั้งที่แอปรันอยู่

**สมมติฐาน (เรียงตามน่าจะเป็น):**
1. **Accessibility ยังไม่ได้เปิด** → pynput ส่ง F15 เงียบๆ ไม่มี error (ถูก try/except กลืน) → ทดสอบ: เปิดสิทธิ์แล้วดูใหม่
2. **F15 keycode ผ่าน pynput ไม่รีเซ็ต idle timer ที่ Teams ใช้บน macOS** → ต้องพิสูจน์ด้วยการวัด idle จริง

**วิธีทดสอบเชิงประจักษ์ (ทำใน terminal คู่กับแอป):**
```bash
# ดู system HID idle time เป็นวินาที — ควร reset ใกล้ 0 ทุกครั้งที่แอปส่ง activity
ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print $NF/1000000000; exit}'
```
- ตั้ง interval สั้น (30s) + ปิด smart idle ชั่วคราวใน Settings → ปล่อยเครื่องนิ่ง → ค่า HIDIdleTime ควรตกลงใกล้ 0 ทุก ~30s. ถ้า**ไม่ตก** = F15 ไม่ถูก inject (สิทธิ์) หรือ keycode ไม่เวิร์ค
- ถ้า F15 ไม่เวิร์ค: ลองสลับ `activity_mode` เป็น `mouse` แล้ววัดใหม่ (mouse nudge มักชัวร์กว่าบน macOS)
- ถ้า mouse เวิร์คแต่ F15 ไม่ → พิจารณาเปลี่ยน **default activity_mode บน macOS เป็น mouse** หรือหา keycode ที่ macOS นับเป็น input (พิจารณาส่งผ่าน Quartz `CGEventCreateKeyboardEvent` ตรงๆ แทน pynput)

**ผลลัพธ์ที่ต้องได้:** ระบุชัดว่าโหมดไหนทำให้ HIDIdleTime reset จริง แล้วตั้งให้เป็น default ที่ใช้งานได้บน macOS + อัปเดต README/CLAUDE.md

---

## 4. Regression checklist (ต้องผ่านทุกข้อบน Mac จริง)

- [ ] เปิดแอป → ไม่มี Dock icon, มีไอคอนบน menu bar, มี notification "Unidle is running"
- [ ] คลิกซ้าย 1 ครั้งที่ไอคอน → เมนูเปิด **แล้วพิมพ์ Teams ต่อได้** (บั๊ก focus ที่เพิ่งแก้)
- [ ] ดับเบิลคลิก → เปิด Settings; คลิกขวา → เมนูเปิดทันที
- [ ] กด Cmd+Q / คลิกขวา Dock → ไอคอน menu bar **ไม่หาย** (ไม่มี Dock icon ให้ quit)
- [ ] Quit จากเมนู → แอปปิดสะอาด, caffeinate/prevent-sleep assertion หายเกลี้ยง (`pmset -g assertions`)
- [ ] เปิด Settings แก้ค่า Save → tray สะท้อนผล ≤2s, Accessibility มี entry เดียว
- [ ] Accessibility เปิดแล้ว → HIDIdleTime reset ตาม interval (งาน #3)
- [ ] lock จอ → log `lock_pause`, ไม่ส่ง activity; ปลดล็อก → resume
- [ ] `keep_system_awake` เปิด → `pmset -g assertions` เห็น assertion ของเรา
- [ ] global hotkey toggle ได้ (ต้องมีสิทธิ์ Accessibility)

## 5. จุดเสี่ยง / ของที่ต้องระวัง (จาก CLAUDE.md)

- **macOS double-click hook** แตะ Cocoa internals ของ pystray (pin 0.19.5) — เปราะสุด, ต้อง feature-detect + fallback เสมอ, ห้าม raise
- **AppKit UI call ต้องอยู่ main thread** — เพิ่งแก้บั๊ก `popUpStatusItemMenu_` ที่ถูกเรียกจาก background thread; อย่าเผลอพาไป thread อื่นอีก
- **PyInstaller hidden imports**: `ApplicationServices` (ใช้ขอสิทธิ์ Accessibility) และ `Quartz` (idle/lock detection) มาแบบ transitive ผ่าน pyobjc — ถ้า build แล้วฟีเจอร์เหล่านี้เงียบใน .app ให้เพิ่ม `--hidden-import ApplicationServices --hidden-import Quartz` ใน `build_macos.sh` + CI
- **config schema เป็น cross-file contract** — แก้ key ต้องแก้ทั้ง `unidle.py` และ `settings_ui.py` (ดู CLAUDE.md)
- **`dist/Unidle` binary เปล่า** ที่ `--onefile` สร้างคู่กับ .app — บอกผู้ใช้ให้ใช้แค่ `.app` และลบ TCC entry เก่าทั้ง "Unidle"/"Unidle.app" ก่อนทดสอบใหม่

## 6. Environment notes

- git: repo นี้เคยเจอปัญหา lock file ค้างเมื่อ commit ผ่าน mount ที่ลบไฟล์ `.git` ไม่ได้ — บนเครื่อง Mac จริง git ควรทำงานปกติ
- `venv/` ที่ check-in มาเป็น Linux venv ใช้บน Mac ไม่ได้ — สร้างใหม่
- ทดสอบ 2 แบบถ้าทำได้: รันจาก source (`python unidle.py`, dialog สิทธิ์จะขึ้นชื่อ Python/Terminal) และ `.app` ที่ build (ขึ้นชื่อ Unidle) — พฤติกรรม identity/สิทธิ์ต่างกัน
