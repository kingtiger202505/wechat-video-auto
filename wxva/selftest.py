"""不依赖微信的环境自检：用记事本验证 窗口查找 / 最小化后唤起到前台 / 剪贴板粘贴 / 截图 / OCR。

如果自检全部通过而跑微信时出问题，就说明是微信界面识别的问题（把 debug 目录发给开发者）；
如果自检就失败，说明是本机环境问题（权限、锁屏、远程桌面、安全软件拦截模拟输入等）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import time

from . import winapi as w

log = logging.getLogger("wxva")

TEST_TEXT = "wxva自测 羽绒服库存 12345"


def _find_new_notepad(before, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        for h in w.enum_top_windows():
            if h in before:
                continue
            try:
                wi = w.win_info(h)
            except Exception:
                continue
            if wi.visible and wi.cls == "Notepad":
                return wi
        time.sleep(0.2)
    return None


def selftest(out_dir: str) -> int:
    from .ocr import OcrEngine, norm_text
    from .platform_win import WinPlatform

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        log.info("  [%s] %s %s", "通过" if ok else "失败", name, detail)
        return ok

    plat = WinPlatform()
    log.info("DPI 感知模式: %s", plat.dpi_mode)
    if not check("屏幕未锁定", not w.screen_locked()):
        return 2
    t0 = time.time()
    ocr = OcrEngine()
    check("OCR 引擎加载", True, "%s，%.1f 秒" % (ocr.kind, time.time() - t0))

    before = set(w.enum_top_windows())
    subprocess.Popen(["notepad.exe"])
    note = _find_new_notepad(before)
    if not check("启动并找到记事本窗口", note is not None, note.short() if note else ""):
        return 2

    try:
        # 最小化后再唤起，模拟「微信在后台/最小化」
        w.show_async(note.hwnd, w.SW_MINIMIZE)
        w.wait_until(lambda: w.IsIconic(note.hwnd), 3)
        time.sleep(0.5)
        note = w.win_info(note.hwnd)
        t0 = time.time()
        ok = plat.focus(note)
        check("最小化窗口唤起到前台", ok, "%.1f 秒" % (time.time() - t0))
        time.sleep(0.5)
        note = w.win_info(note.hwnd)

        # 点击编辑区，粘贴中文
        l, t, r, b = note.rect
        plat.click((l + r) // 2, (t + b) // 2)
        time.sleep(0.3)
        old = plat.get_clipboard()
        plat.paste_text(TEST_TEXT)
        time.sleep(0.8)
        if old is not None:
            plat.set_clipboard(old)
        check("剪贴板写入并恢复", True)

        img = plat.capture(note.rect)
        p = os.path.join(out_dir, "selftest_notepad.png")
        img.save(p)
        check("截图", img.size[0] > 50 and img.size[1] > 50, "%s -> %s" % (img.size, p))

        lines = ocr.recognize(img, scale=max(1.0, min(2.0, 2.0 / plat.scale(note))))
        text = "".join(norm_text(x.text) for x in lines)
        check("粘贴的中文被 OCR 识别到（验证 粘贴+截图+OCR 全链路）", "羽绒服库存" in text,
              "OCR: %s" % text[:60])

        # 滚轮 / 按键 不报错
        plat.wheel((l + r) // 2, (t + b) // 2, 1)
        check("鼠标滚轮", True)
    finally:
        try:
            plat.select_all()
            time.sleep(0.1)
            plat.delete()
            time.sleep(0.3)
            w.PostMessageW(note.hwnd, w.WM_CLOSE, 0, 0)
        except Exception:
            pass

    failed = [n for n, ok in results if not ok]
    log.info("=" * 50)
    if failed:
        log.info("✗ 自检未通过: %s", "，".join(failed))
        log.info("  常见原因：屏幕锁定/远程桌面最小化、安全软件拦截模拟输入、窗口是管理员权限")
        return 2
    log.info("✓ 自检全部通过：本机窗口激活、输入、截图、OCR 都正常")
    return 0
