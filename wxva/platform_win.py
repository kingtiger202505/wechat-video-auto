"""Windows 平台实现：窗口唤起 / 前台 / 截图 / 鼠标键盘。

自动化流程（automator.py）只通过这个类和系统打交道，
测试时用模拟平台替换它，所以流程逻辑可以在任何系统上跑单元测试。
"""
from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

from PIL import Image

from . import winapi as w
from .winapi import WinError, WinInfo

log = logging.getLogger("wxva")


class WinPlatform:
    def __init__(self):
        if not w.IS_WIN:
            raise RuntimeError("只能在 Windows 上运行")
        self.dpi_mode = w.enable_dpi_awareness()

    # ------------------------------------------------------------------ 基础
    def sleep(self, sec: float):
        w.sleep(sec)

    def check_abort(self):
        w.check_abort()

    def list_windows(self) -> List[WinInfo]:
        return [x for x in w.wechat_windows()]

    def refresh(self, win: WinInfo) -> Optional[WinInfo]:
        if not w.IsWindow(win.hwnd):
            return None
        return w.win_info(win.hwnd)

    def scale(self, win: WinInfo) -> float:
        return w.dpi_of(win.hwnd) / 96.0

    def is_front(self, win: WinInfo) -> bool:
        # 严格判断：前台窗口就是它（或它拥有的弹出层，如搜索下拉框）
        return w._is_front(win.hwnd)

    def capture(self, rect) -> Image.Image:
        from PIL import ImageGrab
        l, t, r, b = [int(v) for v in rect]
        img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
        return img.convert("RGB")

    def click(self, x, y):
        w.click(int(x), int(y))

    def wheel(self, x, y, notches: int):
        w.wheel(int(x), int(y), notches)

    def move_to(self, x, y):
        w.move_to(int(x), int(y))

    def paste_text(self, text: str):
        for _ in range(3):
            w.set_clipboard_text(text)
            time.sleep(0.1)
            if w.get_clipboard_text() == text:
                break
            time.sleep(0.3)
        else:
            raise WinError("写入剪贴板失败（可能被剪贴板管理软件占用）")
        w.hotkey(w.VK_CONTROL, ord("V"))

    def select_all(self):
        w.hotkey(w.VK_CONTROL, ord("A"))

    def delete(self):
        w.press(w.VK_BACK)

    def enter(self):
        w.press(w.VK_RETURN)

    def ctrl_f(self):
        w.hotkey(w.VK_CONTROL, ord("F"))

    def get_clipboard(self) -> Optional[str]:
        try:
            return w.get_clipboard_text()
        except Exception:
            return None

    def set_clipboard(self, text: str):
        try:
            w.set_clipboard_text(text)
        except Exception:
            pass

    def ensure_on_screen(self, win: WinInfo, min_w: int = 0, min_h: int = 0) -> WinInfo:
        w.ensure_on_screen(win.hwnd, min_w, min_h)
        return self.refresh(win) or win

    # ------------------------------------------------------------------ 前台
    def focus(self, win: WinInfo, click_fallback: bool = True) -> bool:
        """把窗口切到前台。不会卡死、不会抛异常；返回是否成功。"""
        if win.iconic:
            w.show_async(win.hwnd, w.SW_RESTORE)
            w.wait_until(lambda: not w.IsIconic(win.hwnd), 3)
        for _ in range(3):
            if w.try_set_foreground(win.hwnd):
                return True
            time.sleep(0.25)
        if click_fallback:
            # 真实鼠标点击是系统认可的前台切换理由；点标题栏空白处（不会触发任何功能）
            cur = self.refresh(win) or win
            l, t, r, b = cur.rect
            s = self.scale(cur)
            x = l + int((r - l) * 0.55)
            y = t + max(4, int(6 * s))
            log.info("  SetForegroundWindow 被系统拒绝，改用点击窗口顶部空白处激活 (%d,%d)", x, y)
            w.click(x, y)
            if w.wait_until(lambda: self.is_front(cur), 2):
                return True
        return self.is_front(win)

    # ------------------------------------------------------------------ 唤起主窗口
    def bring_up_main(self) -> WinInfo:
        if w.screen_locked():
            raise WinError("屏幕处于锁定状态，模拟输入无法送达。请解锁后再运行。")
        proc = w.wechat_process()
        if proc is None:
            raise WinError("没有检测到微信进程（Weixin.exe / WeChat.exe），请先启动并登录微信。")
        pid, exe_path = proc
        log.info("检测到微信进程 pid=%d path=%s", pid, exe_path or "(无权限读取路径)")

        main = w.find_main_window()
        if main is not None:
            log.info("主窗口候选: %s", main.short())

        if main is None or not main.visible:
            log.info("主窗口不可见（最小化到托盘），尝试唤起……")
            main = self._reveal_hidden_main(main, exe_path)

        if main is None or not main.visible:
            raise WinError(self._diag("无法唤起微信主窗口"))

        if main.iconic:
            log.info("主窗口处于最小化，还原")
            w.show_async(main.hwnd, w.SW_RESTORE)
            w.wait_until(lambda: not w.IsIconic(main.hwnd), 3)
            time.sleep(0.5)
            main = self.refresh(main) or main

        s = self.scale(main)
        # 登录窗口很小（约 280x380 逻辑像素）
        if main.width < 420 * s and main.height < 620 * s:
            raise WinError("检测到的是微信登录窗口（%dx%d），请先扫码登录微信。" % (main.width, main.height))

        main = self.ensure_on_screen(main, int(760 * s), int(560 * s))
        if not self.focus(main):
            raise WinError(self._diag("微信主窗口无法切换到前台（可能被全屏程序/管理员权限窗口遮挡）"))
        time.sleep(0.3)
        main = self.refresh(main) or main
        log.info("主窗口已在前台: %s", main.short())
        return main

    def _reveal_hidden_main(self, main: Optional[WinInfo], exe_path: str) -> Optional[WinInfo]:
        def visible_main():
            m = w.find_main_window()
            return m if (m is not None and m.visible) else None

        # 方法 1：再次运行 Weixin.exe（微信单实例，会把已运行实例的主窗口显示出来）
        exe = (main.exe if main is not None and main.exe else "") or exe_path
        if exe and os.path.isfile(exe):
            log.info("  方法1：重新运行 %s 让微信自己显示主窗口", exe)
            try:
                w.launch_detached(exe)
                if w.wait_until(lambda: visible_main() is not None, 8, 0.25):
                    time.sleep(0.8)
                    return visible_main()
            except Exception as e:
                log.info("  方法1失败: %r", e)
        # 方法 2：微信默认快捷键 Ctrl+Alt+W（显示/隐藏窗口）
        log.info("  方法2：发送微信快捷键 Ctrl+Alt+W")
        w.hotkey(w.VK_CONTROL, w.VK_MENU, ord("W"))
        if w.wait_until(lambda: visible_main() is not None, 4, 0.25):
            time.sleep(0.8)
            return visible_main()
        # 方法 3：直接 ShowWindowAsync（异步，不会卡）
        m = w.find_main_window()
        if m is not None:
            log.info("  方法3：ShowWindowAsync(SW_SHOW/SW_RESTORE)")
            w.show_async(m.hwnd, w.SW_SHOW)
            time.sleep(0.3)
            w.show_async(m.hwnd, w.SW_RESTORE)
            if w.wait_until(lambda: visible_main() is not None, 4, 0.25):
                time.sleep(0.8)
                return visible_main()
        return visible_main()

    def _diag(self, head: str) -> str:
        lines = [head, "当前微信相关窗口："]
        for x in self.list_windows():
            lines.append("  " + x.short())
        lines.append("前台窗口: 0x%X  锁屏: %s  DPI模式: %s"
                     % (w.foreground_hwnd(), w.screen_locked(), self.dpi_mode))
        lines.append("提示：如果微信是「以管理员身份运行」的，本脚本也必须以管理员身份运行。")
        return "\n".join(lines)
