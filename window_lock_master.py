# -*- coding: utf-8 -*-
"""
WindowLockMaster —— 窗口/鼠标显示器锁定管理器
================================================
功能:
  1. 窗口锁定到显示器: 锁定后窗口无法被移出指定显示器(拖动/移动/改分辨率都会被拉回)
  2. 鼠标锁定到显示器: 把鼠标限制在指定显示器范围内
  3. 黑名单: 按进程名/窗口标题匹配, 黑名单窗口不会被锁定
  4. 一键转移窗口: 右键菜单/热键把窗口移到相邻显示器(居中并保持尺寸)
  5. 右键窗口标题栏弹出菜单: 锁定/解锁/移动/置顶/加入黑名单
  6. 托盘右键菜单: 鼠标锁定、解锁全部、设置、开机自启、退出
优化:
  - 锁定/移动时窗口边框高亮闪烁反馈(点击穿透)
  - 持久锁定: 重启后自动恢复上次锁定的窗口
  - PerMonitorV2 DPI 感知, 多显示器负坐标处理
  - 显示器热插拔自适应(分辨率变化/拔线自动重钳制)
  - 拖动锁定窗口时即时拉回(WinEvent 钩子 + 定时器双保险)
  - 开机自启, 配置持久化, 单实例互斥
热键:
  Ctrl+Alt+Left / Right : 前台窗口移到上一个/下一个显示器
  Ctrl+Alt+L            : 锁定前台窗口到当前显示器
  Ctrl+Alt+U            : 解锁前台窗口
  Ctrl+Alt+M            : 切换鼠标锁定(锁到鼠标所在显示器/解除)
"""

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import queue
import sys
import threading
import time
from ctypes import wintypes

# ---------------------------------------------------------------- 基础 Win32 绑定
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

# 先设置 DPI 感知 (PerMonitorV2), 必须在创建任何窗口之前
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

# 常量
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_ASYNCWINDOWPOS = 0x4000
SW_RESTORE = 9
SW_MAXIMIZE = 3
SW_MINIMIZE = 6
GA_ROOT = 2
WS_CAPTION = 0x00C00000
WS_VISIBLE = 0x10000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
MONITORINFOF_PRIMARY = 1
DWMWA_CLOAKED = 14
WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_RBUTTONUP = 0x0205
WM_HOTKEY = 0x0312
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105
PM_REMOVE = 0x0001
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_GRAYED = 0x00000001
TPM_LEFTALIGN = 0x00000000
TPM_TOPALIGN = 0x00000000
TPM_RETURNCMD = 0x00000100
TPM_NONOTIFY = 0x00000080
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
WINEVENT_OUTOFCONTEXT = 0
SM_CYCAPTION = 4
SM_CYSIZEFRAME = 33
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_L = 0x4C
VK_U = 0x55
VK_M = 0x4D
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1

# 热键 ID
HK_MOVE_PREV = 1
HK_MOVE_NEXT = 2
HK_LOCK = 3
HK_UNLOCK = 4
HK_MOUSE = 5
HK_MOUSE_UNLOCK = 6


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    @property
    def width(self):
        return max(0, self.right - self.left)

    @property
    def height(self):
        return max(0, self.bottom - self.top)

    def __repr__(self):
        return f"RECT({self.left},{self.top} {self.width}x{self.height})"


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD)]


class MONITORINFOEX(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", ctypes.c_wchar * 32)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT),
                ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", POINT)]


def _bool(fn):
    fn.restype = wintypes.BOOL
    fn.argtypes = [wintypes.HWND]
    return fn


GetWindowRect = _bool(user32.GetWindowRect)
GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
IsWindowVisible = _bool(user32.IsWindowVisible)
IsWindow = _bool(user32.IsWindow)
IsZoomed = _bool(user32.IsZoomed)
IsIconic = _bool(user32.IsIconic)
GetForegroundWindow = user32.GetForegroundWindow
GetForegroundWindow.restype = wintypes.HWND
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
GetCursorPos = user32.GetCursorPos
GetCursorPos.restype = wintypes.BOOL
GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
SetProcessDpiAwarenessContext = user32.SetProcessDpiAwarenessContext

user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [POINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindowLongW.restype = wintypes.LONG
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

user32.MoveWindow.restype = wintypes.BOOL
user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.CreatePopupMenu.argtypes = []
user32.AppendMenuW.restype = wintypes.BOOL
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR]
user32.DestroyMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.TrackPopupMenuEx.restype = wintypes.UINT
user32.TrackPopupMenuEx.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.ShowWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ClipCursor.restype = wintypes.BOOL
user32.ClipCursor.argtypes = [ctypes.POINTER(RECT)]
user32.GetMessageW.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = wintypes.LPARAM
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = wintypes.LPARAM
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HINSTANCE,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
user32.UnhookWinEvent.restype = wintypes.BOOL
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEX)]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p, wintypes.LPARAM]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.GetLastError.restype = wintypes.DWORD
kernel32.GetLastError.argtypes = []

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.argtypes = []
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

# ---------------------------------------------------------------- 工具函数
APP_NAME = "WindowLockMaster"
APP_VERSION = "1.0.21"
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "app.log")
EXCLUDE_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow", "DWM Notification Window", "Default IME", "MSCTFIME UI", "Sogou_TSF_UI", "HintWnd"}


def setup_logging():
    os.makedirs(APP_DIR, exist_ok=True)
    logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    logging.getLogger().addHandler(logging.StreamHandler(sys.stderr))


def log(msg):
    logging.info(msg)


def get_window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def get_window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_window_rect(hwnd):
    r = RECT()
    if GetWindowRect(hwnd, ctypes.byref(r)):
        return r
    return None


def get_process_name(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)
    return ""


def is_cloaked(hwnd):
    cloaked = wintypes.DWORD(0)
    try:
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), 4) == 0:
            return cloaked.value != 0
    except Exception:
        pass
    return False


def is_own_window(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == os.getpid()


def is_normal_top_window(hwnd):
    """是否是可在窗口管理器中显示/操作的普通顶层窗口。

    不强制要求 WS_CAPTION：无边框播放器、游戏和自绘窗口可能没有传统标题栏，
    但只要有标题、可见且有实际尺寸，就应该出现在多显示器窗口列表中。
    """
    if not hwnd or not IsWindow(hwnd):
        return False
    # 最小化窗口通常 IsWindowVisible=False，但仍是用户可管理的顶层窗口。
    if not IsWindowVisible(hwnd) and not IsIconic(hwnd):
        return False
    if is_own_window(hwnd) or is_cloaked(hwnd):
        return False
    cls = get_window_class(hwnd)
    if cls in EXCLUDE_CLASSES:
        return False
    title = get_window_title(hwnd)
    if not title.strip():
        return False
    rect = get_window_rect(hwnd)
    # 最小化窗口的 GetWindowRect 通常是约 199x166 的占位矩形，
    # 不能用实际尺寸过滤；恢复后再读取真实尺寸。
    if not IsIconic(hwnd) and (not rect or rect.width < 80 or rect.height < 50):
        return False
    return True


def get_window_rect_safe(hwnd):
    return get_window_rect(hwnd)


# ---------------------------------------------------------------- 显示器管理
class Monitor:
    def __init__(self, index, name, rc_monitor, rc_work, is_primary):
        self.index = index
        self.name = name          # 如 \\.\DISPLAY1
        self.rc_monitor = rc_monitor
        self.rc_work = rc_work
        self.is_primary = is_primary

    @property
    def label(self):
        mark = " (主)" if self.is_primary else ""
        return f"显示器{self.index + 1}{mark} {self.name}  {self.rc_work.width}x{self.rc_work.height}"


def enum_monitors():
    result = []

    def cb(hmon, hdc, lprc, lparam):
        mi = MONITORINFOEX()
        mi.cbSize = ctypes.sizeof(MONITORINFOEX)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            result.append(Monitor(len(result), mi.szDevice.strip("\x00"),
                                  RECT(*[mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom]),
                                  RECT(*[mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom]),
                                  bool(mi.dwFlags & MONITORINFOF_PRIMARY)))
        return True

    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                            ctypes.POINTER(RECT), wintypes.LPARAM)
    user32.EnumDisplayMonitors(0, 0, CB(cb), 0)
    return result


def monitor_of_point(x, y, monitors):
    best, best_area = -1, 0
    for m in monitors:
        r = m.rc_work
        if r.left <= x < r.right and r.top <= y < r.bottom:
            return m.index
        # 不直接命中时取相交面积最大的
        inter_w = max(0, min(r.right, x + 1) - max(r.left, x))
        inter_h = max(0, min(r.bottom, y + 1) - max(r.top, y))
        area = inter_w * inter_h
        if area > best_area:
            best_area, best = area, m.index
    return best if best >= 0 else 0


def monitor_of_rect(rect, monitors):
    best, best_area = 0, -1
    for m in monitors:
        r = m.rc_work
        iw = max(0, min(r.right, rect.right) - max(r.left, rect.left))
        ih = max(0, min(r.bottom, rect.bottom) - max(r.top, rect.top))
        area = iw * ih
        if area > best_area:
            best_area, best = area, m.index
    return best


# ---------------------------------------------------------------- 配置
DEFAULT_CONFIG = {
    "rightclick_menu": True,
    "hotkeys": True,
    "persistent_lock": True,
    "flash_feedback": True,
    "autostart": False,
    "blacklist": [],
    "persistent_locks": [],
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception as e:
        log(f"配置读取失败: {e}")
    return cfg


def save_config(cfg):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"配置保存失败: {e}")


def set_autostart(enabled, exe_path=None):
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        if enabled:
            target = exe_path or sys.executable
            if target.lower().endswith("python.exe") or target.lower().endswith("pythonw.exe"):
                target = f'"{target}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, target)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        log(f"自启设置失败: {e}")
        return False


def is_autostart_enabled():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 应用主体
class WindowLockMasterApp:
    def __init__(self):
        self.cfg = load_config()
        self.lock = threading.RLock()
        self.action_q = queue.Queue()          # 主线程动作队列
        self.monitors = enum_monitors()
        self.locked = {}                       # hwnd -> dict(mon, proc, cls, title, t)
        self.checked_windows = set()           # 主界面自定义勾选的窗口句柄
        self.window_sort_column = "process"   # 默认按程序排序，避免动态标题导致列表跳动
        self.window_sort_reverse = False
        self.moving_windows = set()             # 显式移动期间暂停锁定纠正
        self.mouse_lock_mon = None             # 鼠标锁定的显示器索引, None=未锁定
        self.mouse_prev_rect = None            # 鼠标锁定前的光标位置(恢复用)
        self.hook_installed = False
        self.keyboard_hook_installed = False
        self.keyboard_modifiers = set()
        self.keyboard_mouse_latched = False
        self.poll_combo_latched = False
        self.running = True
        self.root = None                       # tkinter root
        self.settings_win = None
        self.last_clamp = {}                   # hwnd -> time (节流)
        self.winevent_hook = None

        # 恢复持久锁
        self._restore_persistent_locks()

    # ---------------- 配置持久化辅助
    def save(self):
        with self.lock:
            self.cfg["autostart"] = is_autostart_enabled()
            self.cfg["persistent_locks"] = [
                {"proc": v["proc"], "cls": v["cls"], "title": v["title"], "mon": v["mon"]}
                for v in self.locked.values() if self.cfg["persistent_lock"]
            ]
            save_config(self.cfg)

    # ---------------- 黑名单
    def is_blacklisted(self, proc, title):
        for p in self.cfg["blacklist"]:
            p = p.strip().lower()
            if not p:
                continue
            if p in proc or p in title.lower():
                return True
        return False

    def add_blacklist(self, pattern):
        with self.lock:
            if pattern.strip() and pattern.strip().lower() not in [p.lower() for p in self.cfg["blacklist"]]:
                self.cfg["blacklist"].append(pattern.strip())
                self.save()
        # 黑名单中的已锁定窗口自动解锁
        self.unlock_blacklisted()

    def remove_blacklist(self, pattern):
        with self.lock:
            self.cfg["blacklist"] = [p for p in self.cfg["blacklist"] if p.lower() != pattern.lower()]
            self.save()

    # ---------------- 窗口锁定
    def lock_window(self, hwnd, mon_index=None):
        if not is_normal_top_window(hwnd):
            return False
        proc = get_process_name(hwnd)
        title = get_window_title(hwnd)
        cls = get_window_class(hwnd)
        if self.is_blacklisted(proc, title):
            self.notify(f"已加入黑名单, 无法锁定: {title or proc}")
            return False
        if mon_index is None:
            rect = get_window_rect(hwnd)
            mon_index = monitor_of_rect(rect, self.monitors) if rect else 0
        if mon_index < 0 or mon_index >= len(self.monitors):
            mon_index = 0
        # 最小化窗口保持最小化，不主动还原或移动；恢复显示后由
        # WinEvent/后台维护逻辑把它拉回绑定的显示器。
        with self.lock:
            self.locked[hwnd] = {"mon": mon_index, "proc": proc, "cls": cls, "title": title,
                                 "t": time.time()}
            self.save()
        self.clamp_window(hwnd, force=True)
        if self.cfg["flash_feedback"]:
            self.flash_window(hwnd, "#00E5FF")
        self.notify(f"已锁定到 {self.monitors[mon_index].label}: {title or proc}")
        self.refresh_tray()
        return True

    def unlock_window(self, hwnd):
        with self.lock:
            removed = self.locked.pop(hwnd, None)
            self.save()
        if removed:
            if self.cfg["flash_feedback"]:
                self.flash_window(hwnd, "#FF5252")
            self.notify(f"已解锁: {removed['title'] or removed['proc']}")
            self.refresh_tray()
            return True
        return False

    def unlock_all(self):
        with self.lock:
            n = len(self.locked)
            self.locked.clear()
            self.save()
        if n:
            self.notify(f"已解锁全部 {n} 个窗口")
            self.refresh_tray()

    def unlock_blacklisted(self):
        changed = False
        for hwnd in list(self.locked.keys()):
            info = self.locked.get(hwnd)
            if info and self.is_blacklisted(info["proc"], info["title"]):
                with self.lock:
                    self.locked.pop(hwnd, None)
                changed = True
        if changed:
            self.save()
            self.refresh_tray()

    def is_locked(self, hwnd):
        return hwnd in self.locked

    def _fit_into_work(self, rect, work):
        """把窗口矩形钳制进工作区, 保持尺寸; 太大则缩小"""
        w = min(rect.width, work.width)
        h = min(rect.height, work.height)
        x = rect.left
        y = rect.top
        if x < work.left:
            x = work.left
        if y < work.top:
            y = work.top
        if x + w > work.right:
            x = max(work.left, work.right - w)
        if y + h > work.bottom:
            y = max(work.top, work.bottom - h)
        return x, y, w, h

    def clamp_window(self, hwnd, force=False):
        """把锁定窗口拉回其锁定显示器"""
        now = time.time()
        if not force and now - self.last_clamp.get(hwnd, 0) < 0.12:
            return
        self.last_clamp[hwnd] = now
        with self.lock:
            info = self.locked.get(hwnd)
            if not info or not IsWindow(hwnd) or hwnd in self.moving_windows:
                return
            mon_idx = info["mon"]
        if mon_idx >= len(self.monitors):
            # 显示器数量变化, 重新绑定或放弃
            rect = get_window_rect(hwnd)
            new_idx = monitor_of_rect(rect, self.monitors) if rect else 0
            with self.lock:
                if hwnd in self.locked:
                    self.locked[hwnd]["mon"] = new_idx
            return
        work = self.monitors[mon_idx].rc_work
        if IsIconic(hwnd):
            return
        # 最大化窗口: 若已不在锁定显示器上, 跟随用户(重新绑定)
        if IsZoomed(hwnd):
            rect = get_window_rect(hwnd)
            cur = monitor_of_rect(rect, self.monitors) if rect else mon_idx
            if cur != mon_idx:
                with self.lock:
                    if hwnd in self.locked:
                        self.locked[hwnd]["mon"] = cur
            return
        rect = get_window_rect(hwnd)
        if not rect:
            return
        margin = 4
        inside = (rect.left >= work.left - margin and rect.top >= work.top - margin and
                  rect.right <= work.right + margin and rect.bottom <= work.bottom + margin)
        if inside:
            return
        x, y, w, h = self._fit_into_work(rect, work)
        if (x, y, w, h) != (rect.left, rect.top, rect.width, rect.height):
            user32.SetWindowPos(hwnd, None, x, y, w, h,
                                SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)

    def _restore_persistent_locks(self):
        def attempt():
            time.sleep(2.0)
            self._match_persistent_locks()
            time.sleep(2.5)
            self._match_persistent_locks()

        threading.Thread(target=attempt, daemon=True).start()

    def _match_persistent_locks(self):
        if not self.cfg.get("persistent_lock"):
            return
        targets = self.cfg.get("persistent_locks", [])
        if not targets:
            return
        for t in targets:
            if len(self.locked) >= 40:
                break
            for hwnd in self.enum_app_windows():
                if hwnd in self.locked:
                    continue
                cls = get_window_class(hwnd)
                proc = get_process_name(hwnd)
                if proc == t.get("proc") and cls == t.get("cls"):
                    title = get_window_title(hwnd)
                    if t.get("title") and t["title"] not in title:
                        continue
                    if is_normal_top_window(hwnd):
                        self.lock_window(hwnd, t.get("mon", 0))
                    break

    def enum_app_windows(self):
        found = []

        def cb(hwnd, lparam):
            if is_normal_top_window(hwnd):
                found.append(hwnd)
            return True

        CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(CB(cb), 0)
        return found

    # ---------------- 窗口移动
    def move_window_to_monitor(self, hwnd, direction):
        """direction: +1 下一个, -1 上一个; 返回是否成功"""
        if not hwnd or not IsWindow(hwnd):
            return False
        if self.is_blacklisted(get_process_name(hwnd), get_window_title(hwnd)):
            self.notify("黑名单窗口不会移动")
            return False
        was_iconic = IsIconic(hwnd)
        if was_iconic:
            # 最小化窗口的矩形只是占位值，先恢复才能确定真实所属显示器。
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.12)
        rect = get_window_rect(hwnd)
        if not rect:
            if was_iconic:
                user32.ShowWindow(hwnd, SW_MINIMIZE)
            return False
        cur = monitor_of_rect(rect, self.monitors)
        n = len(self.monitors)
        if n <= 1:
            if was_iconic:
                user32.ShowWindow(hwnd, SW_MINIMIZE)
            self.notify("只有一个显示器, 无法转移")
            return False
        target = (cur + direction) % n
        actual = self._move_hwnd_to_monitor(hwnd, target)
        if was_iconic and IsWindow(hwnd):
            user32.ShowWindow(hwnd, SW_MINIMIZE)
        if actual != target:
            if self.cfg["flash_feedback"]:
                self.flash_window(hwnd, "#FFD740")
            self.notify(f"移动失败，目标显示器 {target + 1} 验证未通过: {get_window_title(hwnd) or get_process_name(hwnd)}")
            return False
        if self.cfg["flash_feedback"]:
            self.flash_window(hwnd, "#FFD740")
        self.notify(f"已移到 {self.monitors[target].label}: {get_window_title(hwnd) or get_process_name(hwnd)}")
        return True

    def move_foreground(self, direction):
        hwnd = GetForegroundWindow()
        if not is_normal_top_window(hwnd):
            self.notify("当前没有可操作的窗口")
            return
        self.move_window_to_monitor(hwnd, direction)

    # ---------------- 置顶
    def toggle_topmost(self, hwnd):
        user32.SetWindowPos(hwnd, HWND_TOPMOST if not self.is_topmost(hwnd) else HWND_NOTOPMOST,
                            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.notify(("已置顶: " if self.is_topmost(hwnd) else "取消置顶: ") + (get_window_title(hwnd) or "窗口"))
        self.refresh_tray()

    def is_topmost(self, hwnd):
        buf = ctypes.c_void_p()
        style = user32.GetWindowLongW(hwnd, -8)  # GWL_EXSTYLE
        return bool(style & 0x00000008)  # WS_EX_TOPMOST

    # ---------------- 鼠标锁定
    def set_mouse_lock(self, mon_index):
        if mon_index is None or mon_index < 0 or mon_index >= len(self.monitors):
            self.mouse_lock_mon = None
            user32.ClipCursor(None)
            self.notify("鼠标已解除锁定")
        else:
            self.mouse_lock_mon = mon_index
            # 使用完整显示器区域，而不是 rcWork；rcWork 会排除任务栏，
            # 导致鼠标锁定后无法点击任务栏。
            monitor_area = self.monitors[mon_index].rc_monitor
            user32.ClipCursor(ctypes.byref(monitor_area))
            self.notify(f"鼠标已锁定到 {self.monitors[mon_index].label}")
        self.refresh_tray()

    def toggle_mouse_lock(self):
        if self.mouse_lock_mon is not None:
            self.set_mouse_lock(None)
        else:
            pt = POINT()
            GetCursorPos(ctypes.byref(pt))
            idx = monitor_of_point(pt.x, pt.y, self.monitors)
            self.set_mouse_lock(idx)

    def poll_mouse_hotkey(self):
        """在 Tk 主循环中轮询鼠标快捷键，避免线程消息队列丢失。"""
        if not self.running or self.root is None:
            return
        if self.cfg.get("hotkeys", True):
            ctrl = any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL))
            alt = any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_MENU, VK_LMENU, VK_RMENU))
            shift = any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_SHIFT, VK_LSHIFT, VK_RSHIFT))
            m_down = bool(user32.GetAsyncKeyState(VK_M) & 0x8000)
            combo = ctrl and alt and m_down
            if combo and not self.poll_combo_latched:
                self.poll_combo_latched = True
                if shift:
                    log("主循环收到鼠标解锁快捷键")
                    self.set_mouse_lock(None)
                else:
                    log("主循环收到鼠标切换快捷键")
                    self.toggle_mouse_lock()
            elif not combo:
                self.poll_combo_latched = False
        else:
            self.poll_combo_latched = False
        self.root.after(20, self.poll_mouse_hotkey)

    def enforce_mouse_lock(self):
        if self.mouse_lock_mon is not None:
            if self.mouse_lock_mon >= len(self.monitors):
                self.set_mouse_lock(None)
                return
            monitor_area = self.monitors[self.mouse_lock_mon].rc_monitor
            user32.ClipCursor(ctypes.byref(monitor_area))

    # ---------------- 闪烁反馈 (点击穿透的彩色边框)
    def flash_window(self, hwnd, color):
        rect = get_window_rect(hwnd)
        if not rect:
            return
        self.action_q.put(("flash", rect, color))

    # ---------------- 右键菜单请求 (由钩子线程提交)
    def request_menu(self, hwnd, x, y):
        if not self.cfg.get("rightclick_menu", True):
            return
        log(f"[menu] 请求菜单 hwnd={hwnd} @({x},{y})")
        self.action_q.put(("menu", hwnd, x, y))

    # ---------------- 托盘通知 (线程安全)
    def notify(self, msg):
        log(msg)
        try:
            if self.tray is not None:
                self.tray.notify(msg, APP_NAME)
        except Exception:
            pass

    def refresh_tray(self):
        try:
            if self.tray is not None:
                self.tray.update_menu()
        except Exception:
            pass

    # ---------------- 显示器变化
    def on_monitors_changed(self):
        old = self.monitors
        self.monitors = enum_monitors()
        log(f"显示器变化: {len(old)} -> {len(self.monitors)}")
        # 鼠标锁定失效则解除
        if self.mouse_lock_mon is not None and self.mouse_lock_mon >= len(self.monitors):
            self.set_mouse_lock(None)
        else:
            self.enforce_mouse_lock()
        # 重新钳制所有锁定窗口
        for hwnd in list(self.locked.keys()):
            self.clamp_window(hwnd, force=True)
        self.refresh_tray()

    # ---------------- 钩子线程: 低层鼠标钩子 + 热键 + WinEvent
    def hook_thread_main(self):
        thread_id = kernel32.GetCurrentThreadId()

        def lowlevel_hook(nCode, wParam, lParam):
            if nCode >= 0 and wParam == WM_RBUTTONUP:
                if self.cfg.get("rightclick_menu", True):
                    data = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    pt = data.pt
                    hwnd = user32.WindowFromPoint(pt)
                    root = user32.GetAncestor(hwnd, GA_ROOT)
                    log(f"[hook] 右键释放 @({pt.x},{pt.y}) hwnd={root}")
                    if is_normal_top_window(root) and self._in_titlebar(root, pt):
                        log(f"[hook] 命中标题栏, 调度菜单")
                        # 吞掉标题栏右键，避免 Windows 系统菜单先进入模态状态；
                        # 自定义面板会在消息返回后延迟弹出。
                        self.request_menu(root, pt.x, pt.y)
                        return 1
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        def lowlevel_keyboard_hook(nCode, wParam, lParam):
            if nCode >= 0:
                data = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = int(data.vkCode)
                down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                up = wParam in (WM_KEYUP, WM_SYSKEYUP)
                modifier_vks = (VK_CONTROL, VK_LCONTROL, VK_RCONTROL,
                                VK_MENU, VK_LMENU, VK_RMENU,
                                VK_SHIFT, VK_LSHIFT, VK_RSHIFT)
                if vk in modifier_vks:
                    if down:
                        self.keyboard_modifiers.add(vk)
                    elif up:
                        self.keyboard_modifiers.discard(vk)
                elif vk == VK_M:
                    if down and not self.keyboard_mouse_latched:
                        # 同时检查异步键状态，兼容左右 Ctrl/Alt/Shift 和
                        # 不经过标准键盘消息的应用。
                        ctrl = any(user32.GetAsyncKeyState(k) & 0x8000 for k in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL))
                        alt = any(user32.GetAsyncKeyState(k) & 0x8000 for k in (VK_MENU, VK_LMENU, VK_RMENU))
                        shift = any(user32.GetAsyncKeyState(k) & 0x8000 for k in (VK_SHIFT, VK_LSHIFT, VK_RSHIFT))
                        if ctrl and alt:
                            self.keyboard_mouse_latched = True
                            if shift:
                                log("低级键盘钩子收到鼠标解锁快捷键")
                                self.action_q.put(("mouse_unlock",))
                            else:
                                log("低级键盘钩子收到鼠标切换快捷键")
                                self.action_q.put(("mouse_toggle",))
                    elif up:
                        self.keyboard_mouse_latched = False
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        def win_event_proc(hWinEventHook, hEvent, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
            # WinEventProc(hWinEventHook, event, hwnd, idObject, idChild, idEventThread, dwmsEventTime)
            if hEvent == EVENT_OBJECT_LOCATIONCHANGE and idObject == 0 and idChild == 0:
                self._on_window_moved(hwnd)

        HOOKPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                          wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)

        self._hook_cb = HOOKPROC(lowlevel_hook)
        self._keyboard_cb = HOOKPROC(lowlevel_keyboard_hook)
        self._event_cb = WINEVENTPROC(win_event_proc)

        # 标题栏右键菜单已移除；窗口操作统一从主界面列表完成。
        self.hook = None
        self.hook_installed = False
        self.keyboard_hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._keyboard_cb, kernel32.GetModuleHandleW(None), 0)
        self.keyboard_hook_installed = bool(self.keyboard_hook)
        log(f"低级键盘钩子: {'OK' if self.keyboard_hook_installed else '失败'}")
        log("标题栏右键菜单: 已禁用（使用窗口列表管理）")

        self.winevent_hook = user32.SetWinEventHook(EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE,
                                                    None, self._event_cb, 0, 0, WINEVENT_OUTOFCONTEXT)
        log(f"WinEvent 钩子: {'OK' if self.winevent_hook else '失败'}")

        # 消息循环
        msg = MSG()
        while self.running:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            if msg.message == WM_HOTKEY:
                self._on_hotkey(msg.wParam)
            else:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        if self.hook_installed:
            user32.UnhookWindowsHookEx(self.hook)
        if self.keyboard_hook_installed:
            user32.UnhookWindowsHookEx(self.keyboard_hook)
        if self.winevent_hook:
            user32.UnhookWinEvent(self.winevent_hook)
        self._unregister_hotkeys()

    def _register_hotkeys(self):
        MODS = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
        self._hotkeys = [
            (HK_MOVE_PREV, MODS, VK_LEFT),
            (HK_MOVE_NEXT, MODS, VK_RIGHT),
            (HK_LOCK, MODS, VK_L),
            (HK_UNLOCK, MODS, VK_U),
        ]
        self._hk_registered = []
        for hid, mods, vk in self._hotkeys:
            if user32.RegisterHotKey(None, hid, mods, vk):
                self._hk_registered.append(hid)
            else:
                log(f"热键注册失败 id={hid} vk={hex(vk)} err={kernel32.GetLastError()}")
        log(f"热键注册: {len(self._hk_registered)}/{len(self._hotkeys)}")

    def _setup_main_hotkeys(self):
        """在 Tk 主窗口线程注册全局热键，避免后台线程消息丢失。"""
        if self.root is None or not self.cfg.get("hotkeys", True):
            return
        mods = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
        self._main_hotkeys = [
            (HK_MOVE_PREV, mods, VK_LEFT),
            (HK_MOVE_NEXT, mods, VK_RIGHT),
            (HK_LOCK, mods, VK_L),
            (HK_UNLOCK, mods, VK_U),
            (HK_MOUSE, mods, VK_M),
            (HK_MOUSE_UNLOCK, mods | 0x0004, VK_M),
        ]
        self._main_hk_registered = []
        hwnd = self.root.winfo_id()
        for hid, hotkey_mods, vk in self._main_hotkeys:
            if user32.RegisterHotKey(hwnd, hid, hotkey_mods, vk):
                self._main_hk_registered.append(hid)
            else:
                log(f"主窗口热键注册失败 id={hid} vk={hex(vk)} err={kernel32.GetLastError()}")
        log(f"主窗口热键: {len(self._main_hk_registered)}/{len(self._main_hotkeys)}")

    def _poll_main_hotkeys(self):
        if not self.running or self.root is None:
            return
        msg = MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
            self._on_hotkey(int(msg.wParam))
        self.root.after(20, self._poll_main_hotkeys)

    def _unregister_hotkeys(self):
        hwnd = self.root.winfo_id() if self.root is not None else None
        for hid in getattr(self, "_main_hk_registered", []):
            user32.UnregisterHotKey(hwnd, hid)
        self._main_hk_registered = []
        for hid in getattr(self, "_hk_registered", []):
            user32.UnregisterHotKey(None, hid)

    def _on_hotkey(self, hid):
        if not self.cfg.get("hotkeys", True):
            return
        try:
            log(f"收到全局热键 id={int(hid)}")
            if hid == HK_MOVE_PREV:
                self.move_foreground(-1)
            elif hid == HK_MOVE_NEXT:
                self.move_foreground(1)
            elif hid == HK_LOCK:
                hwnd = GetForegroundWindow()
                if is_normal_top_window(hwnd):
                    self.lock_window(hwnd)
                else:
                    self.notify("当前窗口不可锁定")
            elif hid == HK_UNLOCK:
                hwnd = GetForegroundWindow()
                if not self.unlock_window(hwnd):
                    self.notify("当前窗口未锁定")
            elif hid == HK_MOUSE:
                self.toggle_mouse_lock()
            elif hid == HK_MOUSE_UNLOCK:
                self.set_mouse_lock(None)
        except Exception as e:
            log(f"热键处理错误: {e}")

    def _in_titlebar(self, hwnd, pt):
        """判断点是否在窗口标题栏区域"""
        rect = get_window_rect(hwnd)
        if not rect:
            return False
        if not (rect.left <= pt.x < rect.right):
            return False
        cap = user32.GetSystemMetrics(SM_CYCAPTION)
        frame = user32.GetSystemMetrics(SM_CYSIZEFRAME)
        strip = cap + frame + 2
        return rect.top <= pt.y <= rect.top + strip

    def _on_window_moved(self, hwnd):
        """窗口位置变化 -> 立即拉回锁定窗口(带节流)"""
        try:
            hwnd = int(hwnd.value) if hwnd else 0
        except (AttributeError, TypeError):
            hwnd = int(hwnd)
        if not hwnd or not self.locked:
            return
        if hwnd in self.locked and IsWindow(hwnd) and not IsIconic(hwnd) and hwnd not in self.moving_windows:
            self.clamp_window(hwnd)

    # ---------------- 后台线程: 定时强制
    def enforcer_thread_main(self):
        def mon_sig(mons):
            return [(m.rc_work.left, m.rc_work.top, m.rc_work.right, m.rc_work.bottom) for m in mons]

        last_mons = mon_sig(self.monitors)
        while self.running:
            time.sleep(0.3)
            try:
                # 显示器变化检测
                mons = enum_monitors()
                sig = mon_sig(mons)
                if sig != last_mons:
                    last_mons = sig
                    self.on_monitors_changed()
                    continue
                # 清理已关闭的窗口
                for hwnd in list(self.locked.keys()):
                    if not IsWindow(hwnd):
                        with self.lock:
                            self.locked.pop(hwnd, None)
                # 钳制
                for hwnd in list(self.locked.keys()):
                    if hwnd not in self.moving_windows:
                        self.clamp_window(hwnd)
                # 鼠标锁定维持
                self.enforce_mouse_lock()
            except Exception as e:
                log(f"后台线程错误: {e}")

    # ---------------- 托盘
    def build_tray_menu(self):
        import pystray
        locked_n = len(self.locked)
        mouse_state = f"鼠标: 锁定到显示器{self.mouse_lock_mon + 1}" if self.mouse_lock_mon is not None else "鼠标: 未锁定"
        status = f"已锁定 {locked_n} 个窗口 | {mouse_state}"

        def sub_mouse():
            items = []
            for m in self.monitors:
                items.append(pystray.MenuItem(
                    m.label,
                    lambda _, idx=m.index: self.set_mouse_lock(idx),
                    checked=lambda item, idx=m.index: self.mouse_lock_mon == idx,
                    radio=True))
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("解除鼠标锁定", lambda: self.set_mouse_lock(None),
                                          checked=lambda item: self.mouse_lock_mon is None, radio=True))
            return items

        return pystray.Menu(
            pystray.MenuItem(status, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("锁定鼠标到显示器", pystray.Menu(sub_mouse)),
            pystray.MenuItem("解锁全部窗口", lambda: self.unlock_all()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开窗口管理", lambda: self.action_q.put(("window_manager",))),
            pystray.MenuItem("开机自启", lambda: self.toggle_autostart(),
                             checked=lambda item: is_autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self.quit),
        )

    def toggle_autostart(self):
        ok = set_autostart(not is_autostart_enabled())
        self.notify("开机自启: 已开启" if ok and is_autostart_enabled() else "开机自启: 已关闭")
        self.refresh_tray()

    def make_icon(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([10, 26, 54, 56], radius=8, fill=(0, 229, 255, 255))
        d.rounded_rectangle([22, 14, 42, 30], radius=6, outline=(0, 229, 255, 255), width=6)
        d.rounded_rectangle([27, 34, 37, 44], radius=3, fill=(10, 26, 40, 255))
        return img

    def run_tray(self):
        import pystray
        try:
            self.tray = pystray.Icon(APP_NAME, self.make_icon(), APP_NAME,
                                     menu=self.build_tray_menu())
            self.tray.default_action = lambda: self.action_q.put(("settings",))
            self.tray.run_detached()
            log("托盘已启动")
        except Exception as e:
            log(f"托盘启动失败: {e}")

    # ---------------- 主线程 (tkinter)
    def main_loop(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self._poll_actions()
        self._setup_main_hotkeys()
        self.root.after(20, self._poll_main_hotkeys)
        self.root.after(250, self._open_window_manager)
        try:
            self.root.mainloop()
        except Exception as e:
            log(f"主循环错误: {e}")
        self.quit()

    def _poll_actions(self):
        if not self.running:
            return
        try:
            while True:
                item = self.action_q.get_nowait()
                kind = item[0]
                if kind == "menu":
                    # 等原始右键消息完全离开低层钩子后再弹出菜单。
                    args = item[1:]
                    log(f"[menu] 主线程收到请求 hwnd={args[0]}")
                    self.root.after(120, lambda a=args: self._show_context_menu_safe(*a))
                elif kind == "flash":
                    self._do_flash(item[1], item[2])
                elif kind == "settings":
                    self._open_settings()
                elif kind == "window_manager":
                    self._open_window_manager()
                elif kind == "mouse_lock":
                    self._mouse_lock_to_cursor_monitor()
                elif kind == "mouse_unlock":
                    self.set_mouse_lock(None)
                elif kind == "mouse_toggle":
                    self.toggle_mouse_lock()
        except queue.Empty:
            pass
        if self.running:
            self.root.after(60, self._poll_actions)

    def _show_context_menu_safe(self, hwnd, x, y):
        try:
            self._show_context_menu(hwnd, x, y)
        except Exception as e:
            log(f"[menu] 面板异常: {type(e).__name__}: {e}")

    # ---------------- 右键菜单（可点击的 Tk 弹出面板）
    def _show_context_menu(self, hwnd, x, y):
        if not IsWindow(hwnd):
            return
        import tkinter as tk
        proc = get_process_name(hwnd)
        title = get_window_title(hwnd) or proc or "窗口"
        locked = self.is_locked(hwnd)
        bl = self.is_blacklisted(proc, title)
        rect = get_window_rect(hwnd)
        mon_idx = self.locked[hwnd]["mon"] if locked else (monitor_of_rect(rect, self.monitors) if rect else 0)
        mon_label = self.monitors[mon_idx].label if mon_idx < len(self.monitors) else "?"
        log(f"[menu] 创建 Tk 面板 hwnd={hwnd}")

        old = getattr(self, "active_menu", None)
        if old is not None:
            try: old.destroy()
            except Exception: pass

        popup = tk.Toplevel(self.root)
        self.active_menu = popup
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg="#202124", padx=2, pady=2)
        body = tk.Frame(popup, bg="#202124")
        body.pack(fill="both", expand=True)

        def close():
            if getattr(self, "active_menu", None) is popup:
                self.active_menu = None
            try: popup.destroy()
            except Exception: pass

        def action(fn):
            close()
            self.root.after(1, fn)

        rows = []
        if locked:
            rows.append((f"✔ 已锁定到 {mon_label}", None))
            rows.append(("解锁窗口", lambda: self.unlock_window(hwnd)))
        else:
            rows.append((f"锁定到当前显示器 ({mon_label})", lambda: self.lock_window(hwnd)))
        rows += [
            ("移动到下一个显示器", lambda: self.move_window_to_monitor(hwnd, 1)),
            ("移动到上一个显示器", lambda: self.move_window_to_monitor(hwnd, -1)),
            (("取消窗口置顶" if self.is_topmost(hwnd) else "窗口置顶"), lambda: self.toggle_topmost(hwnd)),
            (("从黑名单移除" if bl else "加入黑名单 (不再锁定此窗口)"),
             lambda: self.remove_blacklist(bl_pattern_of(self, proc, title)) if bl else self.add_blacklist(proc)),
        ]
        for label, fn in rows:
            b = tk.Button(body, text=label, anchor="w", relief="flat", bd=0,
                          padx=12, pady=5, width=34, bg="#202124", fg="#f1f3f4",
                          activebackground="#3c4043", activeforeground="#ffffff",
                          disabledforeground="#777777", font=("Segoe UI", 10),
                          state="normal" if fn else "disabled",
                          command=(lambda f=fn: action(f)) if fn else None)
            b.pack(fill="x")
            if fn:
                b.bind("<Enter>", lambda e, w=b: w.configure(bg="#3c4043"))
                b.bind("<Leave>", lambda e, w=b: w.configure(bg="#202124"))
        tk.Label(body, text=title[:42], anchor="w", bg="#202124", fg="#888888",
                 padx=12, pady=5, font=("Segoe UI", 9)).pack(fill="x")
        popup.update_idletasks()
        px = self.root.winfo_pointerx()
        py = self.root.winfo_pointery()
        popup.geometry(f"{popup.winfo_reqwidth()}x{popup.winfo_reqheight()}+{px}+{py}")
        popup.focus_force()
        popup.bind("<Escape>", lambda e: close())
        popup.bind("<FocusOut>", lambda e: popup.after(200, lambda: close() if popup.winfo_exists() and self.root.focus_get() is None else None))

    def _show_context_menu_tk_legacy(self, hwnd, x, y):
        if not IsWindow(hwnd):
            return
        proc = get_process_name(hwnd)
        title = get_window_title(hwnd) or proc or "窗口"
        locked = self.is_locked(hwnd)
        bl = self.is_blacklisted(proc, title)
        rect = get_window_rect(hwnd)
        mon_idx = self.locked[hwnd]["mon"] if locked else (monitor_of_rect(rect, self.monitors) if rect else 0)
        mon_label = self.monitors[mon_idx].label if mon_idx < len(self.monitors) else "?"

        menu = user32.CreatePopupMenu()
        if not menu:
            log("[menu] CreatePopupMenu 失败")
            return
        actions = {}
        next_id = 1000

        def add(label, fn, enabled=True):
            nonlocal next_id
            item_id = next_id
            next_id += 1
            flags = MF_STRING if enabled else (MF_STRING | MF_GRAYED)
            user32.AppendMenuW(menu, flags, item_id, label)
            if enabled:
                actions[item_id] = fn

        def sep():
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)

        try:
            if locked:
                add(f"✔ 已锁定到 {mon_label}", None, enabled=False)
                add("解锁窗口", lambda: self.unlock_window(hwnd))
            else:
                add(f"锁定到当前显示器 ({mon_label})", lambda: self.lock_window(hwnd))
            sep()
            add("移动到下一个显示器", lambda: self.move_window_to_monitor(hwnd, 1))
            add("移动到上一个显示器", lambda: self.move_window_to_monitor(hwnd, -1))
            sep()
            if self.is_topmost(hwnd):
                add("取消窗口置顶", lambda: self.toggle_topmost(hwnd))
            else:
                add("窗口置顶", lambda: self.toggle_topmost(hwnd))
            sep()
            if bl:
                add("从黑名单移除", lambda: self.remove_blacklist(bl_pattern_of(self, proc, title)))
            else:
                add("加入黑名单 (不再锁定此窗口)", lambda: self.add_blacklist(proc))
            sep()
            add(f"— {title[:30]}", None, enabled=False)

            pt = POINT()
            GetCursorPos(ctypes.byref(pt))
            # 原生弹出菜单必须绑定到当前目标窗口；隐藏的 Tk 根窗口作为 owner
            # 在多显示器/高 DPI 环境下可能无法正确接收菜单结束消息。
            user32.SetForegroundWindow(hwnd)
            result = user32.TrackPopupMenuEx(
                menu, TPM_LEFTALIGN | TPM_TOPALIGN | TPM_RETURNCMD,
                pt.x, pt.y, hwnd, None)
            # 官方建议：菜单关闭后向 owner 发 WM_NULL，清理菜单捕获状态。
            user32.PostMessageW(hwnd, 0x0000, 0, 0)
            log(f"[menu] 原生菜单返回 id={result}")
            if result in actions:
                actions[result]()
        except Exception as e:
            log(f"[menu] 原生菜单异常: {e}")
        finally:
            user32.DestroyMenu(menu)

    def _show_context_menu_tk(self, hwnd, x, y):
        if not IsWindow(hwnd):
            log("[menu] 窗口已不存在, 跳过")
            return
        import tkinter as tk
        log(f"[menu] 弹出菜单 hwnd={hwnd} @({x},{y})")
        menu = tk.Menu(self.root, tearoff=0)
        proc = get_process_name(hwnd)
        title = get_window_title(hwnd) or proc or "窗口"
        locked = self.is_locked(hwnd)
        bl = self.is_blacklisted(proc, get_window_title(hwnd))
        mon_idx = self.locked[hwnd]["mon"] if locked else monitor_of_rect(
            get_window_rect(hwnd), self.monitors) if get_window_rect(hwnd) else 0
        mon_label = self.monitors[mon_idx].label if mon_idx < len(self.monitors) else "?"

        if locked:
            menu.add_command(label=f"✔ 已锁定到 {mon_label}", state="disabled")
            menu.add_command(label="解锁窗口", command=lambda: self.unlock_window(hwnd))
        else:
            menu.add_command(label=f"锁定到当前显示器 ({mon_label})",
                             command=lambda: self.lock_window(hwnd))
        menu.add_separator()
        menu.add_command(label="移动到下一个显示器", command=lambda: self.move_window_to_monitor(hwnd, 1))
        menu.add_command(label="移动到上一个显示器", command=lambda: self.move_window_to_monitor(hwnd, -1))
        menu.add_separator()
        if self.is_topmost(hwnd):
            menu.add_command(label="取消窗口置顶", command=lambda: self.toggle_topmost(hwnd))
        else:
            menu.add_command(label="窗口置顶", command=lambda: self.toggle_topmost(hwnd))
        menu.add_separator()
        if bl:
            menu.add_command(label="从黑名单移除", command=lambda: self.remove_blacklist(bl_pattern_of(self, proc, title)))
        else:
            menu.add_command(label="加入黑名单 (不再锁定此窗口)",
                             command=lambda: self.add_blacklist(proc))
        menu.add_separator()
        menu.add_command(label=f"— {title[:30]}", state="disabled")
        # tk_popup() 在 Windows 上会自行管理菜单的捕获和键盘导航。
        # 不要再调用 grab_set()/focus_force() 覆盖它的原生捕获，否则菜单可能
        # 能显示但鼠标命中不到菜单项。
        self.active_menu = menu

        def cleanup_menu(event=None):
            if getattr(self, "active_menu", None) is menu:
                self.active_menu = None
            try:
                menu.grab_release()
            except Exception:
                pass

        menu.bind("<Unmap>", cleanup_menu, add="+")
        menu.bind("<Escape>", lambda event: menu.unpost(), add="+")

        def invoke_menu_item(event):
            # Windows 高 DPI + 全局低层钩子组合下，Tk 原生菜单有时能显示但
            # 鼠标释放命中不稳定。按菜单自身坐标直接定位并执行菜单项。
            try:
                idx = menu.index(f"@{event.x},{event.y}")
                if idx is not None and menu.type(idx) == "command":
                    state = menu.entrycget(idx, "state")
                    if state != "disabled":
                        menu.invoke(idx)
                        menu.unpost()
                        return "break"
            except Exception as e:
                log(f"[menu] 鼠标选择失败: {e}")
            return None

        menu.bind("<ButtonRelease-1>", invoke_menu_item, add="+")
        try:
            # 全局低层钩子给的是物理坐标；Tk 在高 DPI 下可能使用逻辑坐标。
            # 从 Tk 当前指针位置取坐标，避免菜单与鼠标发生 DPI 偏移。
            popup_x = self.root.winfo_pointerx()
            popup_y = self.root.winfo_pointery()
            if popup_x < 0 or popup_y < 0:
                popup_x, popup_y = x, y
            menu.tk_popup(popup_x, popup_y)
        except Exception as e:
            log(f"[menu] 弹出失败: {e}")
            try:
                menu.grab_release()
                menu.destroy()
            except Exception:
                pass
            self.active_menu = None

    # ---------------- 闪烁
    def _do_flash(self, rect, color):
        try:
            import tkinter as tk
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.attributes("-transparentcolor", "magenta")
            win.configure(bg="magenta")
            w, h = rect.width, rect.height
            win.geometry(f"{w}x{h}+{rect.left}+{rect.top}")
            canvas = tk.Canvas(win, bg="magenta", highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            canvas.create_rectangle(3, 3, w - 3, h - 3, outline=color, width=4)
            win.after(900, win.destroy)
        except Exception as e:
            log(f"闪烁失败: {e}")

    # ---------------- 设置窗口
    def _open_settings(self):
        if self.settings_win is not None:
            try:
                self.settings_win.deiconify()
                self.settings_win.lift()
                return
            except Exception:
                self.settings_win = None
        import tkinter as tk
        from tkinter import ttk
        root = tk.Toplevel(self.root)
        root.title(f"{APP_NAME} - 设置")
        root.geometry("560x560")
        root.resizable(False, False)
        self.settings_win = root

        def on_close():
            self.settings_win = None
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_close)

        pad = {"padx": 12, "pady": 6}
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

        # ---- 选项
        opts = ttk.LabelFrame(frm, text="选项", padding=8)
        opts.pack(fill="x", **pad)

        var_menu = tk.BooleanVar(value=self.cfg.get("rightclick_menu", True))
        var_hk = tk.BooleanVar(value=self.cfg.get("hotkeys", True))
        var_persist = tk.BooleanVar(value=self.cfg.get("persistent_lock", True))
        var_flash = tk.BooleanVar(value=self.cfg.get("flash_feedback", True))

        def on_menu_toggle():
            self.cfg["rightclick_menu"] = var_menu.get()
            self.save()
            self.notify("右键菜单已启用" if var_menu.get() else "右键菜单已禁用(需重启热键钩子后完全生效, 锁定功能不受影响)")

        def on_hk_toggle():
            self.cfg["hotkeys"] = var_hk.get()
            self.save()

        def on_persist_toggle():
            self.cfg["persistent_lock"] = var_persist.get()
            self.save()

        def on_flash_toggle():
            self.cfg["flash_feedback"] = var_flash.get()
            self.save()

        ttk.Checkbutton(opts, text="右键窗口标题栏弹出菜单", variable=var_menu, command=on_menu_toggle).pack(anchor="w")
        ttk.Checkbutton(opts, text="全局热键 (Ctrl+Alt+←/→ 移动, L 锁定, U 解锁, M 鼠标锁定)", variable=var_hk, command=on_hk_toggle).pack(anchor="w")
        ttk.Checkbutton(opts, text="持久锁定 (重启后自动恢复锁定的窗口)", variable=var_persist, command=on_persist_toggle).pack(anchor="w")
        ttk.Checkbutton(opts, text="操作时闪烁边框反馈", variable=var_flash, command=on_flash_toggle).pack(anchor="w")

        # ---- 已锁定窗口
        lockfrm = ttk.LabelFrame(frm, text="已锁定窗口", padding=8)
        lockfrm.pack(fill="both", expand=True, **pad)
        self.lock_list = tk.Listbox(lockfrm, height=5)
        self.lock_list.pack(side="left", fill="both", expand=True, padx=(0, 8))
        scroll = ttk.Scrollbar(lockfrm, orient="vertical", command=self.lock_list.yview)
        scroll.pack(side="left", fill="y")
        self.lock_list.config(yscrollcommand=scroll.set)

        btns = ttk.Frame(lockfrm)
        btns.pack(side="left", fill="y")
        ttk.Button(btns, text="解锁选中", command=self._unlock_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="全部解锁", command=self.unlock_all).pack(fill="x", pady=2)
        ttk.Button(btns, text="刷新列表", command=self._refresh_lock_list).pack(fill="x", pady=2)

        # ---- 黑名单
        blfrm = ttk.LabelFrame(frm, text="黑名单 (进程名或标题关键字, 匹配则不可锁定)", padding=8)
        blfrm.pack(fill="x", **pad)
        row = ttk.Frame(blfrm)
        row.pack(fill="x")
        self.bl_entry = ttk.Entry(row)
        self.bl_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.bl_entry.bind("<Return>", lambda e: self._add_blacklist_from_entry())
        ttk.Button(row, text="添加", command=self._add_blacklist_from_entry).pack(side="left")
        self.bl_list = tk.Listbox(blfrm, height=4)
        self.bl_list.pack(fill="x", pady=(6, 4))
        ttk.Button(blfrm, text="删除选中", command=self._remove_blacklist_selected).pack(anchor="e")

        # ---- 鼠标锁定
        mfrm = ttk.LabelFrame(frm, text="鼠标锁定到显示器", padding=8)
        mfrm.pack(fill="x", **pad)
        self.mouse_var = tk.IntVar(value=self.mouse_lock_mon if self.mouse_lock_mon is not None else -1)
        for m in self.monitors:
            ttk.Radiobutton(mfrm, text=m.label, value=m.index, variable=self.mouse_var,
                            command=lambda idx=m.index: self.set_mouse_lock(idx)).pack(anchor="w")
        ttk.Radiobutton(mfrm, text="解除鼠标锁定", value=-1, variable=self.mouse_var,
                        command=lambda: self.set_mouse_lock(None)).pack(anchor="w")

        # ---- 热键提示
        tip = ttk.Label(frm, foreground="#888",
                        text="热键: Ctrl+Alt+←/→ 移动  |  Ctrl+Alt+L 锁定窗口  |  Ctrl+Alt+U 解锁窗口  |  Ctrl+Alt+M 锁定鼠标  |  Ctrl+Alt+Shift+M 解锁鼠标")
        tip.pack(**pad)

        self._refresh_lock_list()
        self._refresh_bl_list()

        def periodic_refresh():
            if self.settings_win is None:
                return
            self._refresh_lock_list()
            self._refresh_bl_list()
            root.after(2000, periodic_refresh)

        root.after(2000, periodic_refresh)

    def _unlock_selected(self):
        sel = self.lock_list.curselection()
        if not sel:
            return
        hwnds = list(self.locked.keys())
        self.unlock_window(hwnds[sel[0]])

    def _refresh_lock_list(self):
        if self.settings_win is None:
            return
        try:
            self.lock_list.delete(0, "end")
            for hwnd, info in list(self.locked.items()):
                m = self.monitors[info["mon"]].label if info["mon"] < len(self.monitors) else "?"
                self.lock_list.insert("end", f"[显示器{info['mon'] + 1}] {info['title'] or info['proc']}")
        except Exception:
            pass

    def _refresh_bl_list(self):
        if self.settings_win is None:
            return
        try:
            self.bl_list.delete(0, "end")
            for p in self.cfg["blacklist"]:
                self.bl_list.insert("end", p)
        except Exception:
            pass

    def _add_blacklist_from_entry(self):
        text = self.bl_entry.get().strip()
        if text:
            self.add_blacklist(text)
            self.bl_entry.delete(0, "end")
            self._refresh_bl_list()

    def _remove_blacklist_selected(self):
        sel = self.bl_list.curselection()
        if not sel:
            return
        pattern = self.bl_list.get(sel[0])
        self.remove_blacklist(pattern)
        self._refresh_bl_list()

    def _mouse_lock_to_cursor_monitor(self):
        pt = POINT()
        GetCursorPos(ctypes.byref(pt))
        idx = monitor_of_point(pt.x, pt.y, self.monitors)
        log(f"执行鼠标锁定快捷键，当前显示器={idx + 1}")
        self.set_mouse_lock(idx)

    # ---------------- 窗口列表管理器（主界面）
    def _open_window_manager(self):
        import tkinter as tk
        from tkinter import ttk
        if self.settings_win is not None:
            try:
                self.settings_win.deiconify(); self.settings_win.lift(); return
            except Exception:
                self.settings_win = None
        root = tk.Toplevel(self.root)
        self.settings_win = root
        root.title(f"{APP_NAME}  ·  窗口管理")
        root.geometry("1240x820")
        root.minsize(900, 620)
        root.configure(bg="#F4F7FB")
        style = ttk.Style(root)
        try: style.theme_use("clam")
        except Exception: pass
        style.configure("App.TFrame", background="#F4F7FB")
        style.configure("Card.TLabelframe", background="#FFFFFF", foreground="#17213A", bordercolor="#D8E1EE", relief="flat")
        style.configure("Card.TLabelframe.Label", background="#FFFFFF", foreground="#17213A", font=("Segoe UI", 10, "bold"))
        style.configure("Title.TLabel", background="#F4F7FB", foreground="#17213A", font=("Segoe UI", 22, "bold"))
        style.configure("Sub.TLabel", background="#F4F7FB", foreground="#687791", font=("Segoe UI", 10))
        style.configure("StatBlue.TLabel", background="#2563EB", foreground="#FFFFFF", font=("Segoe UI", 13, "bold"))
        style.configure("StatPurple.TLabel", background="#7C3AED", foreground="#FFFFFF", font=("Segoe UI", 13, "bold"))
        style.configure("StatOrange.TLabel", background="#EA580C", foreground="#FFFFFF", font=("Segoe UI", 13, "bold"))
        # 彩色按钮：高亮上沿 + raised relief，模拟轻微渐变和投影感。
        button_common = {"relief": "raised", "borderwidth": 2, "padding": (12, 7), "font": ("Segoe UI", 9, "bold")}
        style.configure("Flat.TButton", **button_common, background="#FFFFFF", foreground="#24324B", bordercolor="#B9C7DA")
        style.configure("Primary.TButton", **button_common, background="#18B8B0", foreground="#FFFFFF", bordercolor="#087F7A")
        style.configure("Blue.TButton", **button_common, background="#3F7CFF", foreground="#FFFFFF", bordercolor="#1748B5")
        style.configure("Purple.TButton", **button_common, background="#955CFF", foreground="#FFFFFF", bordercolor="#5420B5")
        style.configure("Orange.TButton", **button_common, background="#FF8A3D", foreground="#FFFFFF", bordercolor="#B94A0A")
        style.configure("Danger.TButton", **button_common, background="#F05261", foreground="#FFFFFF", bordercolor="#9F1F2C")
        style.map("Flat.TButton", background=[("pressed", "#DCE5F1"), ("active", "#F3F7FC")], relief=[("pressed", "sunken")])
        style.map("Primary.TButton", background=[("pressed", "#087F7A"), ("active", "#27C9C1")], relief=[("pressed", "sunken")])
        style.map("Blue.TButton", background=[("pressed", "#1748B5"), ("active", "#5A91FF")], relief=[("pressed", "sunken")])
        style.map("Purple.TButton", background=[("pressed", "#5420B5"), ("active", "#A875FF")], relief=[("pressed", "sunken")])
        style.map("Orange.TButton", background=[("pressed", "#B94A0A"), ("active", "#FF9F62")], relief=[("pressed", "sunken")])
        style.map("Danger.TButton", background=[("pressed", "#9F1F2C"), ("active", "#FF6876")], relief=[("pressed", "sunken")])
        style.configure("Treeview", background="#FFFFFF", fieldbackground="#FFFFFF", foreground="#24324B", rowheight=34, borderwidth=0, relief="flat", font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#E8EEF7", foreground="#31415B", font=("Segoe UI", 10, "bold"), relief="flat", padding=(8, 8))
        style.map("Treeview", background=[("selected", "#DCEBFF")], foreground=[("selected", "#163258")])
        root.protocol("WM_DELETE_WINDOW", lambda: (setattr(self, "settings_win", None), root.destroy()))

        # Modern desktop shell: compact navigation rail + focused work area.
        shell = tk.Frame(root, bg="#F4F7FB")
        shell.pack(fill="both", expand=True)
        rail = tk.Frame(shell, bg="#17213A", width=220)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        tk.Label(rail, text="WLM", bg="#17213A", fg="#5EEAD4",
                 font=("Segoe UI", 22, "bold")).pack(anchor="w", padx=22, pady=(24, 0))
        tk.Label(rail, text="WINDOW LOCK MASTER", bg="#17213A", fg="#AAB8D0",
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=24, pady=(0, 28))
        tk.Label(rail, text="WORKSPACE", bg="#17213A", fg="#8291AD",
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=24, pady=(0, 8))
        tk.Label(rail, text="▣   窗口管理", bg="#2563EB", fg="#FFFFFF",
                 anchor="w", padx=18, pady=11, font=("Segoe UI", 10, "bold")).pack(fill="x", padx=10)
        tk.Label(rail, text="◉   鼠标范围", bg="#17213A", fg="#CBD5E1",
                 anchor="w", padx=18, pady=11, font=("Segoe UI", 10)).pack(fill="x", padx=10)
        tk.Label(rail, text="⌕   快捷操作", bg="#17213A", fg="#CBD5E1",
                 anchor="w", padx=18, pady=11, font=("Segoe UI", 10)).pack(fill="x", padx=10)
        tk.Frame(rail, bg="#33415C", height=1).pack(fill="x", padx=22, pady=24)
        tk.Label(rail, text="LIVE STATUS", bg="#17213A", fg="#8291AD",
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=24, pady=(0, 10))
        self.rail_status = tk.Label(rail, text="●  SERVICE ACTIVE", bg="#17213A", fg="#5EEAD4",
                                    font=("Segoe UI", 9, "bold"))
        self.rail_status.pack(anchor="w", padx=24)
        tk.Label(rail, text="WinEvent monitor online\nGlobal hotkeys ready", bg="#17213A", fg="#AAB8D0",
                 justify="left", font=("Segoe UI", 9), pady=8).pack(anchor="w", padx=24)
        tk.Label(rail, text="Ctrl+Alt+L  锁定窗口\nCtrl+Alt+U  解锁窗口\nCtrl+Alt+M  锁定鼠标\nCtrl+Alt+Shift+M  解锁鼠标",
                 bg="#17213A", fg="#AAB8D0", justify="left", font=("Consolas", 8), pady=18).pack(anchor="w", padx=24, side="bottom")

        content = tk.Frame(shell, bg="#F4F7FB")
        content.pack(side="left", fill="both", expand=True)
        outer = ttk.Frame(content, style="App.TFrame", padding=22); outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer, style="App.TFrame"); header.pack(fill="x", pady=(0, 14))
        title_box = ttk.Frame(header, style="App.TFrame"); title_box.pack(side="left")
        ttk.Label(title_box, text="窗口管理", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_box, text="集中管理多显示器窗口、锁定状态和鼠标范围", style="Sub.TLabel").pack(anchor="w", pady=(3,0))
        self.window_count_label = ttk.Label(header, text="", style="Sub.TLabel"); self.window_count_label.pack(side="left", padx=24, pady=(10,0))
        ttk.Button(header, text="⟳  刷新", style="Flat.TButton", command=self._refresh_window_list).pack(side="right", pady=(10,0))
        ttk.Button(header, text="全部解锁", style="Danger.TButton", command=self.unlock_all).pack(side="right", padx=(0,8), pady=(10,0))

        stats = ttk.Frame(outer, style="App.TFrame"); stats.pack(fill="x", pady=(0,12))
        self.stat_total = ttk.Label(stats, text="窗口  0", style="StatBlue.TLabel", padding=(16,10)); self.stat_total.pack(side="left", fill="x", expand=True, padx=(0,8))
        self.stat_locked = ttk.Label(stats, text="已锁定  0", style="StatPurple.TLabel", padding=(16,10)); self.stat_locked.pack(side="left", fill="x", expand=True, padx=8)
        self.stat_mouse = ttk.Label(stats, text="鼠标  未锁定", style="StatOrange.TLabel", padding=(16,10)); self.stat_mouse.pack(side="left", fill="x", expand=True, padx=(8,0))

        search = ttk.Frame(outer, style="App.TFrame"); search.pack(fill="x", pady=(0,10))
        ttk.Label(search, text="筛选窗口", style="Sub.TLabel").pack(side="left", padx=(0,8))
        self.window_search_var = tk.StringVar()
        search_entry = ttk.Entry(search, textvariable=self.window_search_var, width=42)
        search_entry.pack(side="left", fill="x", expand=True)
        self.window_search_var.trace_add("write", lambda *_: self._refresh_window_list())
        ttk.Label(search, text="  右键窗口进行更多操作  ·  双击锁定", style="Sub.TLabel").pack(side="right")
        bar = ttk.Frame(outer, style="App.TFrame"); bar.pack(fill="x", pady=(0, 10))
        action_styles = {"全选（排除黑名单）": "Primary.TButton", "清除勾选": "Flat.TButton", "锁定勾选": "Purple.TButton", "解锁勾选": "Danger.TButton", "移动到显示器 1": "Blue.TButton", "移动到显示器 2": "Blue.TButton", "下一个显示器": "Orange.TButton", "上一个显示器": "Orange.TButton"}
        for text, fn in [("全选（排除黑名单）", self._check_all_non_blacklisted), ("清除勾选", self._clear_checked_windows),
                         ("锁定勾选", self._lock_selected_window), ("解锁勾选", self._unlock_selected_window),
                         ("移动到显示器 1", lambda: self._move_selected_to(0)),
                         ("移动到显示器 2", lambda: self._move_selected_to(1)),
                         ("下一个显示器", lambda: self._move_selected(1)),
                         ("上一个显示器", lambda: self._move_selected(-1))]:
            ttk.Button(bar, text=text, style=action_styles[text], command=fn).pack(side="left", padx=(0, 5))
        cols = ("check", "title", "process", "monitor", "status", "size")
        table_card = ttk.Frame(outer, style="App.TFrame"); table_card.pack(fill="both", expand=True)
        tree = ttk.Treeview(table_card, columns=cols, show="headings", selectmode="browse")
        self.window_tree = tree
        for col, text, width in [("check","选择",56),("title","窗口标题",320),("process","程序",150),("monitor","所在显示器",180),("status","状态",90),("size","尺寸",105)]:
            tree.heading(col, text=text, command=lambda c=col: self._sort_window_list(c)); tree.column(col, width=width, anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(table_card, orient="vertical", command=tree.yview); sb.pack(side="right", fill="y"); tree.configure(yscrollcommand=sb.set)
        tree.bind("<Button-3>", self._window_list_context_menu)
        tree.bind("<Button-1>", self._toggle_window_check)
        tree.bind("<Double-1>", lambda e: self._lock_selected_window())
        ttk.Label(outer, text="提示：点击表头排序；无边框播放器、游戏窗口也会显示在列表中。", style="Sub.TLabel").pack(fill="x", pady=(8,4))

        # 鼠标锁定
        mouse_frame = ttk.LabelFrame(outer, text="鼠标锁定范围", style="Card.TLabelframe", padding=8)
        mouse_frame.pack(fill="x", pady=(0,6))
        self.window_mouse_var = tk.IntVar(value=self.mouse_lock_mon if self.mouse_lock_mon is not None else -1)
        ttk.Label(mouse_frame, text="当前状态:").pack(side="left")
        self.window_mouse_status = ttk.Label(mouse_frame, text="未锁定")
        self.window_mouse_status.pack(side="left", padx=(4,14))
        for m in self.monitors:
            ttk.Radiobutton(mouse_frame, text=f"锁定到显示器 {m.index + 1}", value=m.index,
                            variable=self.window_mouse_var,
                            command=lambda idx=m.index: self._set_window_mouse_lock(idx)).pack(side="left", padx=5)
        ttk.Radiobutton(mouse_frame, text="解除锁定", value=-1,
                        variable=self.window_mouse_var,
                        command=lambda: self._set_window_mouse_lock(None)).pack(side="left", padx=5)

        # 黑名单
        blacklist_frame = ttk.LabelFrame(outer, text="黑名单  ·  进程名或窗口标题关键字", style="Card.TLabelframe", padding=8)
        blacklist_frame.pack(fill="x")
        self.window_blacklist_entry = ttk.Entry(blacklist_frame, width=34)
        self.window_blacklist_entry.pack(side="left", padx=(0,5))
        self.window_blacklist_entry.bind("<Return>", lambda e: self._add_window_blacklist())
        ttk.Button(blacklist_frame, text="添加黑名单", command=self._add_window_blacklist).pack(side="left")
        self.window_blacklist_list = tk.Listbox(blacklist_frame, height=3, width=38)
        self.window_blacklist_list.pack(side="left", padx=12)
        ttk.Button(blacklist_frame, text="删除选中", command=self._remove_window_blacklist).pack(side="left")
        self._refresh_window_blacklist()
        self._update_window_mouse_status()
        self._refresh_window_list()
        root.after(1500, self._periodic_window_refresh)

    def _periodic_window_refresh(self):
        if self.settings_win is not None:
            self._refresh_window_list(); self.settings_win.after(1500, self._periodic_window_refresh)

    def _list_selected_hwnd(self):
        checked = self._checked_hwnds()
        if checked:
            return checked[0]
        if not hasattr(self, "window_tree"): return None
        sel = self.window_tree.selection(); return int(sel[0]) if sel else None

    def _checked_hwnds(self):
        if not hasattr(self, "window_tree"):
            return []
        valid = []
        # 严格按 Treeview 当前排序顺序执行，避免 set 无序导致批量移动顺序乱跳。
        for item in self.window_tree.get_children():
            try:
                hwnd = int(item)
            except ValueError:
                continue
            if hwnd in self.checked_windows and IsWindow(hwnd) and is_normal_top_window(hwnd):
                valid.append(hwnd)
        for hwnd in list(self.checked_windows):
            if not IsWindow(hwnd) or not is_normal_top_window(hwnd):
                self.checked_windows.discard(hwnd)
        return valid

    def _check_all_non_blacklisted(self):
        """Check every window currently shown in the list except blacklist entries."""
        if not hasattr(self, "window_tree"):
            return
        selected = set()
        for item in self.window_tree.get_children():
            try:
                hwnd = int(item)
            except ValueError:
                continue
            proc = get_process_name(hwnd)
            title = get_window_title(hwnd)
            if IsWindow(hwnd) and not self.is_blacklisted(proc, title):
                selected.add(hwnd)
        self.checked_windows = selected
        self._refresh_window_list()
        self.notify(f"已全选 {len(selected)} 个窗口（已排除黑名单）")

    def _clear_checked_windows(self):
        self.checked_windows.clear()
        self._refresh_window_list()

    def _sort_window_list(self, column):
        if column == "check":
            return
        if self.window_sort_column == column:
            self.window_sort_reverse = not self.window_sort_reverse
        else:
            self.window_sort_column = column
            self.window_sort_reverse = False
        self._refresh_window_list()

    def _update_window_sort_headings(self):
        labels = {"check": "选择", "title": "窗口标题", "process": "程序",
                  "monitor": "所在显示器", "status": "状态", "size": "尺寸"}
        for column, label in labels.items():
            if column == self.window_sort_column:
                label += "  ▼" if self.window_sort_reverse else "  ▲"
            self.window_tree.heading(column, text=label)

    def _toggle_window_check(self, event):
        item = self.window_tree.identify_row(event.y)
        if not item:
            return
        col = self.window_tree.identify_column(event.x)
        if col == "#1":
            hwnd = int(item)
            if hwnd in self.checked_windows:
                self.checked_windows.remove(hwnd)
            else:
                self.checked_windows.add(hwnd)
            self._refresh_window_list()
            return "break"

    def _refresh_window_list(self):
        if self.settings_win is None or not hasattr(self, "window_tree"): return
        tree = self.window_tree; old = tree.selection(); old_id = old[0] if old else None
        self._update_window_sort_headings()
        checked = set(self.checked_windows)
        query = self.window_search_var.get().strip().lower() if hasattr(self, "window_search_var") else ""
        for i in tree.get_children(): tree.delete(i)
        rows = []
        for hwnd in self.enum_app_windows():
            rect = get_window_rect(hwnd)
            if not rect: continue
            title = get_window_title(hwnd) or "（无标题）"; proc = get_process_name(hwnd) or "未知"
            mi = self.locked[hwnd]["mon"] if hwnd in self.locked else monitor_of_rect(rect, self.monitors)
            if IsIconic(hwnd):
                mon = "最小化（恢复后判定）"
            else:
                mon = self.monitors[mi].label if mi < len(self.monitors) else "未知"
            blacklisted = self.is_blacklisted(proc, title)
            status = "黑名单" if blacklisted else ("已锁定" if hwnd in self.locked else ("最小化" if IsIconic(hwnd) else "未锁定"))
            if query and query not in title.lower() and query not in proc.lower():
                continue
            rows.append((title.lower(), str(hwnd), title, proc, mon, status, f"{rect.width} × {rect.height}", "☑" if hwnd in checked else "☐"))
        sort_index = {"title": 2, "process": 3, "monitor": 4, "status": 5, "size": 6}.get(self.window_sort_column, 2)
        rows.sort(key=lambda row: (str(row[sort_index]).casefold(), int(row[1])), reverse=self.window_sort_reverse)
        for _, iid, title, proc, mon, status, size, check in rows:
            hwnd = int(iid)
            tag = "locked" if status == "已锁定" else ("blacklisted" if status == "黑名单" else ("minimized" if status == "最小化" else ""))
            tree.insert("", "end", iid=iid, values=(check, title, proc, mon, status, size), tags=(tag,) if tag else ())
        tree.tag_configure("locked", foreground="#60a5fa")
        tree.tag_configure("blacklisted", foreground="#f87171")
        tree.tag_configure("minimized", foreground="#fbbf24")
        if old_id and tree.exists(old_id): tree.selection_set(old_id)
        total_visible = len(rows)
        self.window_count_label.configure(text=f"显示 {total_visible} 个")
        if hasattr(self, "stat_total"):
            self.stat_total.configure(text=f"窗口  {total_visible}")
            self.stat_locked.configure(text=f"已锁定  {len(self.locked)}")
            mouse_text = "未锁定" if self.mouse_lock_mon is None else f"显示器 {self.mouse_lock_mon + 1}"
            self.stat_mouse.configure(text=f"鼠标  {mouse_text}")

    def _lock_selected_window(self):
        hwnds = self._checked_hwnds() or ([self._list_selected_hwnd()] if self._list_selected_hwnd() else [])
        ok = 0
        for hwnd in hwnds:
            if not self.is_blacklisted(get_process_name(hwnd), get_window_title(hwnd)) and self.lock_window(hwnd): ok += 1
        if hwnds: self.notify(f"已锁定 {ok}/{len(hwnds)} 个窗口"); self._refresh_window_list()

    def _unlock_selected_window(self):
        hwnds = self._checked_hwnds() or ([self._list_selected_hwnd()] if self._list_selected_hwnd() else [])
        for hwnd in hwnds: self.unlock_window(hwnd)
        if hwnds: self._refresh_window_list()

    def _move_selected(self, direction):
        hwnds = self._checked_hwnds() or ([self._list_selected_hwnd()] if self._list_selected_hwnd() else [])
        moved = skipped = 0
        for hwnd in hwnds:
            if self.is_blacklisted(get_process_name(hwnd), get_window_title(hwnd)): skipped += 1
            elif self.move_window_to_monitor(hwnd, direction): moved += 1
        if hwnds: self.notify(f"已移动 {moved} 个窗口" + (f"，跳过黑名单 {skipped} 个" if skipped else "")); self._refresh_window_list()

    def _move_selected_to(self, target):
        hwnds = self._checked_hwnds() or ([self._list_selected_hwnd()] if self._list_selected_hwnd() else [])
        if not hwnds or target >= len(self.monitors):
            return
        moved = skipped = 0
        for hwnd in hwnds:
            if self.is_blacklisted(get_process_name(hwnd), get_window_title(hwnd)):
                skipped += 1; continue
            actual = self._move_hwnd_to_monitor(hwnd, target)
            if actual == target:
                moved += 1
        self.notify(f"已移动 {moved} 个窗口" + (f"，跳过黑名单 {skipped} 个" if skipped else ""))
        self._refresh_window_list()

    def _move_hwnd_to_monitor(self, hwnd, target):
        """移动并验证；最小化窗口先恢复后再判定实际显示器。"""
        if not hwnd or not IsWindow(hwnd) or target < 0 or target >= len(self.monitors):
            return None
        self.moving_windows.add(hwnd)
        try:
            was_zoomed = IsZoomed(hwnd)
            was_iconic = IsIconic(hwnd)
            rect = get_window_rect(hwnd)
            if not rect:
                return None
            old_locked_mon = self.locked[hwnd]["mon"] if hwnd in self.locked else None
            if was_iconic or was_zoomed:
                user32.ShowWindow(hwnd, SW_RESTORE)
                time.sleep(0.12)
                rect = get_window_rect(hwnd) or rect
            current = monitor_of_rect(rect, self.monitors)
            if current == target:
                log(f"窗口已在目标显示器，无需移动 hwnd={hwnd} 显示器={target + 1}")
                if was_zoomed:
                    user32.ShowWindow(hwnd, SW_MAXIMIZE)
                if was_iconic:
                    user32.ShowWindow(hwnd, SW_MINIMIZE)
                return target
            area = self.monitors[target].rc_monitor
            w = min(max(rect.width, 80), area.width)
            h = min(max(rect.height, 50), area.height)
            x = area.left + (area.width - w) // 2
            y = area.top + (area.height - h) // 2
            actual = None
            for attempt in range(1, 6):
                user32.SetWindowPos(hwnd, None, x, y, w, h,
                                    SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)
                time.sleep(0.12)
                moved = get_window_rect(hwnd)
                actual = monitor_of_rect(moved, self.monitors) if moved else None
                if actual == target:
                    if hwnd in self.locked:
                        self.locked[hwnd]["mon"] = target
                        self.save()
                    if was_zoomed:
                        user32.ShowWindow(hwnd, SW_MAXIMIZE)
                    if was_iconic:
                        user32.ShowWindow(hwnd, SW_MINIMIZE)
                    return target
                log(f"移动验证失败 hwnd={hwnd} 目标={target + 1} 实际={None if actual is None else actual + 1}，继续移动 {attempt}/5")
                if was_zoomed:
                    user32.ShowWindow(hwnd, SW_RESTORE)
            if hwnd in self.locked:
                self.locked[hwnd]["mon"] = old_locked_mon
            if was_zoomed:
                user32.ShowWindow(hwnd, SW_MAXIMIZE)
            if was_iconic:
                user32.ShowWindow(hwnd, SW_MINIMIZE)
            return actual
        finally:
            self.moving_windows.discard(hwnd)

    def _window_list_context_menu(self, event):
        item = self.window_tree.identify_row(event.y)
        if not item: return
        self.window_tree.selection_set(item); hwnd = int(item)
        checked_count = len(self._checked_hwnds())
        import tkinter as tk
        menu = tk.Menu(self.settings_win, tearoff=0)
        scope = f"已勾选 {checked_count} 个窗口" if checked_count else "当前窗口"
        menu.add_command(label=f"锁定（{scope}）", command=self._lock_selected_window)
        menu.add_command(label=f"解锁（{scope}）", command=self._unlock_selected_window)
        menu.add_command(label=f"移动到显示器 1（{scope}）", command=lambda: self._move_selected_to(0))
        if len(self.monitors) > 1: menu.add_command(label=f"移动到显示器 2（{scope}）", command=lambda: self._move_selected_to(1))
        menu.add_command(label=f"移动到下一个显示器（{scope}）", command=lambda: self._move_selected(1))
        menu.add_command(label=f"移动到上一个显示器（{scope}）", command=lambda: self._move_selected(-1))
        menu.add_separator()
        proc = get_process_name(hwnd)
        title = get_window_title(hwnd)
        if self.is_blacklisted(proc, title):
            menu.add_command(label="从黑名单移除", command=lambda: self._remove_blacklist_for_window(proc, title))
        else:
            menu.add_command(label=f"加入黑名单（{proc or '当前进程'}）", command=lambda: self._add_blacklist_for_window(proc, title))
        menu.add_separator()
        menu.add_command(label="窗口置顶/取消置顶", command=lambda: self.toggle_topmost(hwnd))
        menu.tk_popup(event.x_root, event.y_root)

    def _add_blacklist_for_window(self, proc, title):
        pattern = proc or title
        if pattern:
            self.add_blacklist(pattern)
            self._refresh_window_blacklist()
            self._refresh_window_list()
            self.notify(f"已加入黑名单: {pattern}")

    def _remove_blacklist_for_window(self, proc, title):
        pattern = None
        for item in self.cfg.get("blacklist", []):
            if item.lower() in proc.lower() or item.lower() in title.lower():
                pattern = item
                break
        if pattern:
            self.remove_blacklist(pattern)
            self._refresh_window_blacklist()
            self._refresh_window_list()
            self.notify(f"已从黑名单移除: {pattern}")

    def _set_window_mouse_lock(self, mon_index):
        self.set_mouse_lock(mon_index)
        self._update_window_mouse_status()

    def _update_window_mouse_status(self):
        if not hasattr(self, "window_mouse_status"):
            return
        if self.mouse_lock_mon is None:
            self.window_mouse_status.configure(text="未锁定")
            if hasattr(self, "window_mouse_var"): self.window_mouse_var.set(-1)
        else:
            self.window_mouse_status.configure(text=f"已锁定到显示器 {self.mouse_lock_mon + 1}")
            if hasattr(self, "window_mouse_var"): self.window_mouse_var.set(self.mouse_lock_mon)

    def _refresh_window_blacklist(self):
        if not hasattr(self, "window_blacklist_list"):
            return
        self.window_blacklist_list.delete(0, "end")
        for pattern in self.cfg.get("blacklist", []):
            self.window_blacklist_list.insert("end", pattern)

    def _add_window_blacklist(self):
        text = self.window_blacklist_entry.get().strip()
        if text:
            self.add_blacklist(text)
            self.window_blacklist_entry.delete(0, "end")
            self._refresh_window_blacklist()
            self._refresh_window_list()

    def _remove_window_blacklist(self):
        sel = self.window_blacklist_list.curselection()
        if sel:
            self.remove_blacklist(self.window_blacklist_list.get(sel[0]))
            self._refresh_window_blacklist()
            self._refresh_window_list()

    # ---------------- 退出
    def quit(self):
        if not self.running:
            return
        self.running = False
        try:
            if self.mouse_lock_mon is not None:
                user32.ClipCursor(None)
        except Exception:
            pass
        self.save()
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:
            pass
        try:
            if self.root is not None:
                self.root.quit()
        except Exception:
            pass
        log("已退出")
        os._exit(0)


def bl_pattern_of(app, proc, title):
    """查找匹配的完整黑名单条目"""
    for p in app.cfg["blacklist"]:
        pl = p.strip().lower()
        if pl and (pl in proc or pl in title.lower()):
            return p
    return proc


# ---------------------------------------------------------------- 入口
def main():
    setup_logging()
    # 单实例
    mutex = kernel32.CreateMutexW(None, True, f"Global\\{APP_NAME}_v4_Mutex")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        user32.MessageBoxW(None, f"{APP_NAME} 已在运行。", APP_NAME, 0x40)
        return

    log("========== 启动 ==========")
    log(f"显示器: {len(enum_monitors())} 个")
    app = WindowLockMasterApp()

    # 托盘线程
    threading.Thread(target=app.run_tray, daemon=True).start()
    # 钩子/热键线程
    threading.Thread(target=app.hook_thread_main, daemon=True).start()
    # 后台强制线程
    threading.Thread(target=app.enforcer_thread_main, daemon=True).start()
    # 主线程 = tkinter
    app.main_loop()


if __name__ == "__main__":
    main()
