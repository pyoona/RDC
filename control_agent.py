#!/usr/bin/env python3
"""
control_agent.py - ตัวเสริมของ Link Remote ให้อีกฝ่าย "ควบคุม" เครื่องนี้ได้

หน้าเว็บ Link Remote (ใน Chrome) ส่งภาพหน้าจอเองผ่าน WebRTC
โปรแกรมนี้มีหน้าที่อย่างเดียว: รับคำสั่งเมาส์/คีย์บอร์ดจากหน้าเว็บ แล้วกดให้จริงบนเครื่อง
เปิดรับเฉพาะ 127.0.0.1 (จากเครื่องตัวเองเท่านั้น) และรับเฉพาะเว็บที่อยู่ใน --origin

    pip install -r requirements.txt
    python control_agent.py --origin https://ชื่อคุณ.github.io
"""
import argparse
import json
import platform
import sys

# เว็บที่อนุญาตเป็นค่าเริ่มต้น (ฝังไว้ตอน build เป็น .exe จะได้ไม่ต้องใส่ --origin)
DEFAULT_ORIGINS = ["https://pyoona.github.io"]

if platform.system() == "Windows":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import mss
from aiohttp import WSMsgType, web
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController

mouse = MouseController()
keyboard = KeyboardController()

BUTTONS = {0: Button.left, 1: Button.middle, 2: Button.right}

_SPECIAL_NAMES = {
    "Enter": "enter", "Backspace": "backspace", "Tab": "tab", "Escape": "esc",
    "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right",
    "Shift": "shift", "Control": "ctrl", "Alt": "alt", "AltGraph": "alt_gr",
    "Meta": "cmd", "OS": "cmd", "Delete": "delete", "Home": "home", "End": "end",
    "PageUp": "page_up", "PageDown": "page_down", "CapsLock": "caps_lock",
    "Insert": "insert", "ContextMenu": "menu", "PrintScreen": "print_screen",
    "NumLock": "num_lock", "ScrollLock": "scroll_lock", "Pause": "pause",
}
_SPECIAL_NAMES.update({f"F{i}": f"f{i}" for i in range(1, 21)})
SPECIAL = {k: getattr(Key, v) for k, v in _SPECIAL_NAMES.items() if hasattr(Key, v)}


def _shortcut_char(key, code):
    """ใช้ตำแหน่งปุ่ม (code) แทนตัวอักษร เพื่อให้ Ctrl+C ฯลฯ ทำงานแม้คีย์บอร์ดฝั่งผู้ควบคุมเป็นภาษาไทย"""
    if code.startswith("Key") and len(code) == 4:
        return code[3].lower()
    if code.startswith("Digit") and len(code) == 6:
        return code[5]
    return key.lower()


class InputHandler:
    def __init__(self):
        self.pressed_keys = {}
        self.pressed_buttons = set()

    @staticmethod
    def _to_screen(mon, x, y):
        x = min(max(float(x), 0.0), 1.0)
        y = min(max(float(y), 0.0), 1.0)
        return (
            int(mon["left"] + x * (mon["width"] - 1)),
            int(mon["top"] + y * (mon["height"] - 1)),
        )

    def move(self, mon, x, y):
        mouse.position = self._to_screen(mon, x, y)

    def button(self, mon, d, down):
        if "x" in d and "y" in d:
            self.move(mon, d["x"], d["y"])
        btn = BUTTONS.get(int(d.get("b", 0)), Button.left)
        if down:
            mouse.press(btn)
            self.pressed_buttons.add(btn)
        else:
            mouse.release(btn)
            self.pressed_buttons.discard(btn)

    def wheel(self, d):
        dx, dy = int(d.get("dx", 0)), int(d.get("dy", 0))
        # เบราว์เซอร์: dy บวก = เลื่อนลง / pynput: dy ลบ = เลื่อนลง
        mouse.scroll(-dx, -dy)

    def key(self, d, down):
        k = str(d.get("key", ""))
        code = str(d.get("code", ""))
        mods = d.get("ctrl") or d.get("alt") or d.get("meta")
        ident = code or k

        if not down:
            pk = self.pressed_keys.pop(ident, None)
            if pk is not None:
                keyboard.release(pk)
            return

        if k in SPECIAL:
            pk = SPECIAL[k]
        elif k == " ":
            pk = Key.space
        elif len(k) == 1:
            if not mods:
                # ตัวอักษรธรรมดา (รวมภาษาไทย) พิมพ์ตรง ๆ แบบ unicode ไม่ขึ้นกับ layout เครื่องปลายทาง
                keyboard.type(k)
                return
            pk = KeyCode.from_char(_shortcut_char(k, code))
        else:
            return  # ปุ่มที่ไม่รู้จัก เช่น "Unidentified", "Process"

        if ident in self.pressed_keys:  # auto-repeat
            keyboard.release(self.pressed_keys[ident])
        keyboard.press(pk)
        self.pressed_keys[ident] = pk

    def release_all(self):
        for pk in list(self.pressed_keys.values()):
            try:
                keyboard.release(pk)
            except Exception:
                pass
        self.pressed_keys.clear()
        for b in list(self.pressed_buttons):
            try:
                mouse.release(b)
            except Exception:
                pass
        self.pressed_buttons.clear()



# ---------------------------------------------------------------------------
# WebSocket จากหน้าเว็บ Link Remote
# ---------------------------------------------------------------------------
def list_monitors():
    with mss.mss() as sct:
        return [dict(m) for m in sct.monitors[1:]]


async def ws_handler(request):
    app = request.app
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin not in app["origins"]:
        print(f"[!] ปฏิเสธการเชื่อมต่อจากเว็บ {origin or '(ไม่ทราบ)'} — ไม่อยู่ใน --origin")
        return web.Response(status=403, text="origin not allowed")

    if app["busy"]:
        return web.Response(status=409, text="another page is connected")

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 18)
    await ws.prepare(request)
    app["busy"] = True
    monitors = list_monitors()
    mon = monitors[0]
    await ws.send_json({"t": "hello", "monitors": [
        {"width": m["width"], "height": m["height"]} for m in monitors]})
    print(f"[+] หน้าเว็บเชื่อมต่อแล้ว ({origin})")

    inp = InputHandler()
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
                t = d.get("t")
                if t == "mon":
                    i = int(d.get("index", 0))
                    monitors = list_monitors()
                    mon = monitors[i] if 0 <= i < len(monitors) else monitors[0]
                elif t == "mm":
                    inp.move(mon, d["x"], d["y"])
                elif t in ("md", "mu"):
                    inp.button(mon, d, t == "md")
                elif t == "wh":
                    inp.wheel(d)
                elif t in ("kd", "ku"):
                    inp.key(d, t == "kd")
                elif t == "text":
                    keyboard.type(str(d.get("s", ""))[:5000])
                elif t == "release":
                    inp.release_all()
            except Exception as e:
                print(f"[!] คำสั่งผิดพลาด: {e}")
    finally:
        inp.release_all()
        app["busy"] = False
        print("[-] หน้าเว็บตัดการเชื่อมต่อ")
    return ws


def main():
    ap = argparse.ArgumentParser(description="ตัวเสริมให้ Link Remote ควบคุมเมาส์/คีย์บอร์ดเครื่องนี้ได้")
    ap.add_argument("--origin", action="append", default=[],
                    help="เว็บที่อนุญาต เช่น https://ชื่อคุณ.github.io (ใส่ได้หลายครั้ง)")
    ap.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()

    origins = {o.rstrip("/") for o in (args.origin or DEFAULT_ORIGINS)}

    app = web.Application()
    app["origins"] = origins
    app["busy"] = False
    app.router.add_get("/", ws_handler)

    print("=" * 56)
    print(" Link Remote - ตัวควบคุมพร้อมทำงาน")
    print(f"  อนุญาตเว็บ : {', '.join(sorted(origins))}")
    print("  เปิดหน้าเว็บ Link Remote แล้วกด 'เริ่มแชร์หน้าจอ' ได้เลย")
    print("  กด Ctrl+C เพื่อปิด (ปิดแล้วอีกฝ่ายจะดูได้อย่างเดียว)")
    print("=" * 56)
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\n[X] เกิดข้อผิดพลาด: {e}")
        # ถ้ารันเป็น .exe (ดับเบิลคลิก) ให้ค้างหน้าต่างไว้ จะได้อ่าน error ทัน
        if getattr(sys, "frozen", False):
            input("\nกด Enter เพื่อปิด...")
