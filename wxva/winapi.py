"""纯 ctypes 的 Win32 封装（不依赖 pywin32 / uiautomation / pywinauto）。

为什么不用 uiautomation / pywinauto：
  微信 4.x 是 Qt 自绘界面，UIA 树基本是空的；而且对它做 UIA 深度查找、SetFocus
  很容易卡死（这正是「唤起微信窗口就死了」的常见原因）。
  win32gui.SetForegroundWindow 失败时还会直接抛 pywintypes.error 让脚本崩掉。

这里的原则：
  * 所有跨进程窗口操作都用「异步 / 不阻塞」的 API（ShowWindowAsync、SWP_ASYNCWINDOWPOS）；
  * 前台切换失败不抛异常，逐级降级（Alt 键解锁 -> AttachThreadInput -> 真实鼠标点击）；
  * 微信最小化到托盘时 IsWindowVisible()==0：通过「再次运行 Weixin.exe」让微信自己把
    主窗口显示出来（微信是单实例，重复启动只会激活已运行的实例），再退化到 Ctrl+Alt+W 热键。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

IS_WIN = sys.platform == "win32"

Rect = Tuple[int, int, int, int]  # left, top, right, bottom（物理像素）

# ---------------------------------------------------------------------------
# 常量
SW_SHOW = 5
SW_RESTORE = 9
SW_SHOWNORMAL = 1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_ASYNCWINDOWPOS = 0x4000
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800

VK_BACK = 0x08
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B
VK_END = 0x23
VK_HOME = 0x24
VK_DELETE = 0x2E
VK_F12 = 0x7B

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
TH32CS_SNAPPROCESS = 0x00000002
MONITOR_DEFAULTTONEAREST = 2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

WECHAT_MAIN_EXES = {"weixin.exe", "wechat.exe"}


def is_wechat_exe(name: str) -> bool:
    n = (name or "").lower()
    return n.startswith("weixin") or n.startswith("wechat")


class UserAbort(Exception):
    pass


class WinError(Exception):
    pass


# ---------------------------------------------------------------------------
if IS_WIN:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:  # pragma: no cover
        dwmapi = None

    ULONG_PTR = ctypes.c_size_t
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ULONG_PTR),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    def _proto(dll, name, restype, argtypes):
        f = getattr(dll, name)
        f.restype = restype
        f.argtypes = argtypes
        return f

    EnumWindows = _proto(user32, "EnumWindows", wintypes.BOOL, [WNDENUMPROC, wintypes.LPARAM])
    GetClassNameW = _proto(user32, "GetClassNameW", ctypes.c_int,
                           [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int])
    GetWindowTextW = _proto(user32, "GetWindowTextW", ctypes.c_int,
                            [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int])
    GetWindowTextLengthW = _proto(user32, "GetWindowTextLengthW", ctypes.c_int, [wintypes.HWND])
    GetWindowThreadProcessId = _proto(user32, "GetWindowThreadProcessId", wintypes.DWORD,
                                      [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)])
    IsWindow = _proto(user32, "IsWindow", wintypes.BOOL, [wintypes.HWND])
    IsWindowVisible = _proto(user32, "IsWindowVisible", wintypes.BOOL, [wintypes.HWND])
    IsIconic = _proto(user32, "IsIconic", wintypes.BOOL, [wintypes.HWND])
    IsHungAppWindow = _proto(user32, "IsHungAppWindow", wintypes.BOOL, [wintypes.HWND])
    GetWindowRect = _proto(user32, "GetWindowRect", wintypes.BOOL,
                           [wintypes.HWND, ctypes.POINTER(wintypes.RECT)])
    GetForegroundWindow = _proto(user32, "GetForegroundWindow", wintypes.HWND, [])
    SetForegroundWindow = _proto(user32, "SetForegroundWindow", wintypes.BOOL, [wintypes.HWND])
    BringWindowToTop = _proto(user32, "BringWindowToTop", wintypes.BOOL, [wintypes.HWND])
    ShowWindowAsync = _proto(user32, "ShowWindowAsync", wintypes.BOOL, [wintypes.HWND, ctypes.c_int])
    SetWindowPos = _proto(user32, "SetWindowPos", wintypes.BOOL,
                          [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, wintypes.UINT])
    AttachThreadInput = _proto(user32, "AttachThreadInput", wintypes.BOOL,
                               [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL])
    GetAncestor = _proto(user32, "GetAncestor", wintypes.HWND, [wintypes.HWND, wintypes.UINT])
    SetCursorPos = _proto(user32, "SetCursorPos", wintypes.BOOL, [ctypes.c_int, ctypes.c_int])
    GetCursorPos = _proto(user32, "GetCursorPos", wintypes.BOOL, [ctypes.POINTER(wintypes.POINT)])
    mouse_event = _proto(user32, "mouse_event", None,
                         [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ULONG_PTR])
    keybd_event = _proto(user32, "keybd_event", None,
                         [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ULONG_PTR])
    MapVirtualKeyW = _proto(user32, "MapVirtualKeyW", wintypes.UINT, [wintypes.UINT, wintypes.UINT])
    GetAsyncKeyState = _proto(user32, "GetAsyncKeyState", ctypes.c_short, [ctypes.c_int])
    OpenInputDesktop = _proto(user32, "OpenInputDesktop", wintypes.HANDLE,
                              [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])
    CloseDesktop = _proto(user32, "CloseDesktop", wintypes.BOOL, [wintypes.HANDLE])
    MonitorFromWindow = _proto(user32, "MonitorFromWindow", wintypes.HANDLE,
                               [wintypes.HWND, wintypes.DWORD])
    GetMonitorInfoW = _proto(user32, "GetMonitorInfoW", wintypes.BOOL,
                             [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)])
    OpenClipboard = _proto(user32, "OpenClipboard", wintypes.BOOL, [wintypes.HWND])
    CloseClipboard = _proto(user32, "CloseClipboard", wintypes.BOOL, [])
    EmptyClipboard = _proto(user32, "EmptyClipboard", wintypes.BOOL, [])
    GetClipboardData = _proto(user32, "GetClipboardData", wintypes.HANDLE, [wintypes.UINT])
    SetClipboardData = _proto(user32, "SetClipboardData", wintypes.HANDLE,
                              [wintypes.UINT, wintypes.HANDLE])
    IsClipboardFormatAvailable = _proto(user32, "IsClipboardFormatAvailable", wintypes.BOOL,
                                        [wintypes.UINT])
    CreateWindowExW = _proto(user32, "CreateWindowExW", wintypes.HWND,
                             [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID])
    DestroyWindow = _proto(user32, "DestroyWindow", wintypes.BOOL, [wintypes.HWND])
    GlobalAlloc = _proto(kernel32, "GlobalAlloc", wintypes.HGLOBAL, [wintypes.UINT, ctypes.c_size_t])
    GlobalLock = _proto(kernel32, "GlobalLock", wintypes.LPVOID, [wintypes.HGLOBAL])
    GlobalUnlock = _proto(kernel32, "GlobalUnlock", wintypes.BOOL, [wintypes.HGLOBAL])
    GlobalFree = _proto(kernel32, "GlobalFree", wintypes.HGLOBAL, [wintypes.HGLOBAL])
    GetCurrentThreadId = _proto(kernel32, "GetCurrentThreadId", wintypes.DWORD, [])
    OpenProcess = _proto(kernel32, "OpenProcess", wintypes.HANDLE,
                         [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])
    CloseHandle = _proto(kernel32, "CloseHandle", wintypes.BOOL, [wintypes.HANDLE])
    QueryFullProcessImageNameW = _proto(kernel32, "QueryFullProcessImageNameW", wintypes.BOOL,
                                        [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                         ctypes.POINTER(wintypes.DWORD)])
    CreateToolhelp32Snapshot = _proto(kernel32, "CreateToolhelp32Snapshot", wintypes.HANDLE,
                                      [wintypes.DWORD, wintypes.DWORD])
    Process32FirstW = _proto(kernel32, "Process32FirstW", wintypes.BOOL,
                             [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)])
    Process32NextW = _proto(kernel32, "Process32NextW", wintypes.BOOL,
                            [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)])
    if dwmapi is not None:
        DwmGetWindowAttribute = _proto(dwmapi, "DwmGetWindowAttribute", ctypes.c_long,
                                       [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p,
                                        wintypes.DWORD])
    else:  # pragma: no cover
        DwmGetWindowAttribute = None
    try:
        GetDpiForWindow = _proto(user32, "GetDpiForWindow", wintypes.UINT, [wintypes.HWND])
    except AttributeError:  # Win8.1 以下
        GetDpiForWindow = None


# ---------------------------------------------------------------------------
def enable_dpi_awareness() -> str:
    """必须最先调用：让所有坐标（窗口、截图、鼠标）统一为物理像素。"""
    if not IS_WIN:
        return "n/a"
    try:
        f = user32.SetProcessDpiAwarenessContext
        f.restype = wintypes.BOOL
        f.argtypes = [ctypes.c_void_p]
        if f(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return "per-monitor-v2"
    except AttributeError:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except Exception:
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except Exception:
        pass
    return "already-set-or-unknown"


def check_abort():
    """按住 F12 随时中止（不用 Esc，避免和网页快捷键冲突）。"""
    if IS_WIN and (GetAsyncKeyState(VK_F12) & 0x8000):
        raise UserAbort("用户按下 F12，已中止")


def sleep(sec: float):
    end = time.time() + sec
    while True:
        check_abort()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(0.05, left))


# ---------------------------------------------------------------------------
@dataclass
class WinInfo:
    hwnd: int
    pid: int
    exe: str          # 完整路径（可能为空：权限不足）
    cls: str
    title: str
    visible: bool
    iconic: bool
    rect: Rect

    @property
    def exe_name(self) -> str:
        return os.path.basename(self.exe).lower() if self.exe else ""

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def short(self) -> str:
        return ("hwnd=0x%X pid=%d exe=%s class=%s title=%r visible=%s iconic=%s rect=%s"
                % (self.hwnd, self.pid, self.exe_name, self.cls, self.title,
                   self.visible, self.iconic, self.rect))


_exe_cache: Dict[int, str] = {}


def process_path(pid: int) -> str:
    if pid in _exe_cache:
        return _exe_cache[pid]
    path = ""
    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                path = buf.value
        finally:
            CloseHandle(h)
    _exe_cache[pid] = path
    return path


def list_processes() -> List[Tuple[int, str]]:
    out = []
    snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return out
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            out.append((int(pe.th32ProcessID), pe.szExeFile))
            ok = Process32NextW(snap, ctypes.byref(pe))
    finally:
        CloseHandle(snap)
    return out


def window_rect(hwnd: int) -> Rect:
    """可见边框（去掉 Win10/11 的透明阴影边）。"""
    r = wintypes.RECT()
    if DwmGetWindowAttribute is not None:
        if DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r),
                                 ctypes.sizeof(r)) == 0:
            if r.right > r.left and r.bottom > r.top:
                return (r.left, r.top, r.right, r.bottom)
    GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def is_cloaked(hwnd: int) -> bool:
    if DwmGetWindowAttribute is None:
        return False
    v = wintypes.DWORD(0)
    if DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) == 0:
        return v.value != 0
    return False


def win_info(hwnd: int) -> WinInfo:
    pid = wintypes.DWORD(0)
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    cbuf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, cbuf, 256)
    n = GetWindowTextLengthW(hwnd)
    tbuf = ctypes.create_unicode_buffer(max(n + 1, 2))
    GetWindowTextW(hwnd, tbuf, n + 1)
    return WinInfo(hwnd=int(hwnd), pid=int(pid.value), exe=process_path(int(pid.value)),
                   cls=cbuf.value, title=tbuf.value,
                   visible=bool(IsWindowVisible(hwnd)) and not is_cloaked(hwnd),
                   iconic=bool(IsIconic(hwnd)), rect=window_rect(hwnd))


def enum_top_windows() -> List[int]:
    res: List[int] = []

    def cb(h, _l):
        if h:
            res.append(int(h))
        return True

    proc = WNDENUMPROC(cb)
    EnumWindows(proc, 0)
    return res


def wechat_windows() -> List[WinInfo]:
    out = []
    for h in enum_top_windows():
        try:
            wi = win_info(h)
        except Exception:
            continue
        if is_wechat_exe(wi.exe_name):
            out.append(wi)
    return out


def wechat_process() -> Optional[Tuple[int, str]]:
    """返回 (pid, 完整路径)；微信没运行返回 None。"""
    for pid, name in list_processes():
        if name.lower() in WECHAT_MAIN_EXES:
            return pid, process_path(pid)
    return None


def find_main_window() -> Optional[WinInfo]:
    """定位微信主窗口（兼容 4.x 的 Qt 窗口和 3.x 的 WeChatMainWndForPC）。

    注意：托盘状态下 IsWindowVisible()==0，Qt 无边框窗口也没有 WS_CAPTION，
    所以不能按「可见」或「有标题栏」过滤，只能打分。
    """
    best, best_score = None, -1e18
    for wi in wechat_windows():
        if wi.exe_name not in WECHAT_MAIN_EXES:
            continue
        cls = wi.cls
        s = 0.0
        if cls == "WeChatMainWndForPC":
            s += 100
        elif cls.startswith("Qt") and "QWindowIcon" in cls:
            s += 60
        elif cls.startswith("Qt") and "QWindow" in cls:
            s += 10
        else:
            continue
        if wi.title in ("微信", "Weixin", "WeChat"):
            s += 50
        elif wi.title:
            s -= 20   # 独立聊天窗口、图片查看器等，标题是别的
        else:
            s -= 40
        if wi.visible:
            s += 10
        s += min(wi.area, 4000 * 3000) / 1e6  # 大窗口优先
        if s > best_score:
            best, best_score = wi, s
    return best


def screen_locked() -> bool:
    h = OpenInputDesktop(0, False, 0x0001)
    if not h:
        return True
    CloseDesktop(h)
    return False


def foreground_hwnd() -> int:
    return int(GetForegroundWindow() or 0)


def foreground_pid() -> int:
    h = GetForegroundWindow()
    if not h:
        return 0
    pid = wintypes.DWORD(0)
    GetWindowThreadProcessId(h, ctypes.byref(pid))
    return int(pid.value)


def dpi_of(hwnd: int) -> int:
    if GetDpiForWindow is not None:
        try:
            v = int(GetDpiForWindow(hwnd))
            if v > 0:
                return v
        except Exception:
            pass
    return 96


def monitor_work_area(hwnd: int) -> Rect:
    mon = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if mon and GetMonitorInfoW(mon, ctypes.byref(mi)):
        w = mi.rcWork
        return (w.left, w.top, w.right, w.bottom)
    return (0, 0, 1920, 1080)


# ---------------------------------------------------------------------------
# 输入
def _key(vk: int, up: bool):
    scan = MapVirtualKeyW(vk, 0) & 0xFF
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in (VK_END, VK_HOME, VK_DELETE):
        flags |= KEYEVENTF_EXTENDEDKEY
    keybd_event(vk, scan, flags, 0)


def hotkey(*vks: int, hold: float = 0.03):
    check_abort()
    for vk in vks:
        _key(vk, False)
        time.sleep(hold)
    for vk in reversed(vks):
        _key(vk, True)
        time.sleep(hold)


def press(vk: int):
    hotkey(vk)


def cursor_pos() -> Optional[Tuple[int, int]]:
    p = wintypes.POINT()
    if GetCursorPos(ctypes.byref(p)):
        return (p.x, p.y)
    return None


def move_to(x: int, y: int):
    SetCursorPos(int(x), int(y))


def click(x: int, y: int, settle: float = 0.08):
    check_abort()
    SetCursorPos(int(x), int(y))
    time.sleep(settle)
    SetCursorPos(int(x), int(y))
    mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def wheel(x: int, y: int, notches: int, gap: float = 0.04):
    """notches > 0 向下滚。"""
    check_abort()
    SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    delta = -120 if notches > 0 else 120
    for _ in range(abs(int(notches))):
        mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta & 0xFFFFFFFF, 0)
        time.sleep(gap)


# ---------------------------------------------------------------------------
# 剪贴板
def _open_clipboard(hwnd=None, retries: int = 20) -> bool:
    for _ in range(retries):
        if OpenClipboard(hwnd):
            return True
        time.sleep(0.05)
    return False


def get_clipboard_text() -> Optional[str]:
    if not _open_clipboard():
        return None
    try:
        if not IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h = GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            GlobalUnlock(h)
    finally:
        CloseClipboard()


def set_clipboard_text(text: str):
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    # 文档：用 NULL 窗口 OpenClipboard 后 EmptyClipboard 会让 SetClipboardData 失败，
    # 所以建一个隐藏的临时窗口作为剪贴板所有者
    owner = CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
    try:
        _set_clipboard_with_owner(owner, data, size)
    finally:
        if owner:
            DestroyWindow(owner)


def _set_clipboard_with_owner(owner, data, size):
    if not _open_clipboard(owner):
        raise WinError("无法打开剪贴板（被其它程序占用）")
    try:
        EmptyClipboard()
        h = GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            raise WinError("GlobalAlloc 失败")
        p = GlobalLock(h)
        ctypes.memmove(p, data, size)
        GlobalUnlock(h)
        if not SetClipboardData(CF_UNICODETEXT, h):
            GlobalFree(h)
            raise WinError("SetClipboardData 失败")
    finally:
        CloseClipboard()


# ---------------------------------------------------------------------------
# 窗口激活
def _is_front(hwnd: int) -> bool:
    fg = foreground_hwnd()
    if not fg:
        return False
    if fg == hwnd:
        return True
    root = int(GetAncestor(fg, 3) or 0)  # GA_ROOTOWNER
    return root == hwnd


def show_async(hwnd: int, cmd: int):
    ShowWindowAsync(hwnd, cmd)


def try_set_foreground(hwnd: int) -> bool:
    """不会阻塞、不会抛异常的前台切换。"""
    if _is_front(hwnd):
        return True
    # 1) Alt 键：让系统认为本进程刚收到用户输入，从而允许 SetForegroundWindow
    _key(VK_MENU, False)
    _key(VK_MENU, True)
    fg = GetForegroundWindow()
    cur = GetCurrentThreadId()
    fg_tid = 0
    if fg and not IsHungAppWindow(fg):
        fg_tid = GetWindowThreadProcessId(fg, None)
    attached = False
    try:
        if fg_tid and fg_tid != cur:
            attached = bool(AttachThreadInput(cur, fg_tid, True))
        BringWindowToTop(hwnd)
        SetForegroundWindow(hwnd)
    finally:
        if attached:
            AttachThreadInput(cur, fg_tid, False)
    # 2) 置顶再取消置顶，保证至少在 Z 序最上面（异步，不会卡）
    SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_ASYNCWINDOWPOS | SWP_SHOWWINDOW)
    SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_ASYNCWINDOWPOS | SWP_SHOWWINDOW)
    for _ in range(10):
        time.sleep(0.05)
        if _is_front(hwnd):
            return True
    return False


def wait_until(pred: Callable[[], bool], timeout: float, interval: float = 0.15) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        check_abort()
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    try:
        return bool(pred())
    except Exception:
        return False


def launch_detached(path: str):
    """启动 exe 且不等待、不继承句柄（重复启动微信 = 激活已运行的微信）。"""
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen([path], close_fds=True, cwd=os.path.dirname(path) or None,
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def ensure_on_screen(hwnd: int, min_w: int = 0, min_h: int = 0) -> Rect:
    """窗口超出屏幕（或太小）时移动/放大到当前显示器工作区内。"""
    l, t, r, b = window_rect(hwnd)
    wl, wt, wr, wb = monitor_work_area(hwnd)
    w, h = r - l, b - t
    nw = min(max(w, min_w), wr - wl)
    nh = min(max(h, min_h), wb - wt)
    nl = min(max(l, wl), wr - nw)
    nt = min(max(t, wt), wb - nh)
    if (nl, nt, nw, nh) != (l, t, w, h):
        SetWindowPos(hwnd, None, nl, nt, nw, nh,
                     SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)
        time.sleep(0.6)
    return window_rect(hwnd)
