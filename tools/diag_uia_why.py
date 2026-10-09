"""深挖：为什么微信 4.x 不暴露 UIA 控件树。

检查三项系统级辅助功能前提条件：
  1. SPI_GETSCREENREADER   —— 系统"屏幕阅读器正在运行"标志（Qt 据此决定是否暴露树）
  2. 注册表 RunningState    —— UIAutomation 客户端相关
  3. UIAutomation 的 Core 开关 —— UIA_IsScreenReaderActive 等

并检查微信进程本身的窗口层次，看主窗口挂在哪个进程上。

只读，不改任何设置、不点击、不输入。
用法：.venv\\Scripts\\python.exe tools\\diag_uia_why.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import win32gui  # noqa: E402
import win32process  # noqa: E402

SPI_GETSCREENREADER = 0x0046
SPI_SETSCREENREADER = 0x0047
SPI_GETCLIENTAREAANIMATION = 0x1042
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def exe_of_pid(pid: int) -> str:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    op = k.OpenProcess
    op.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    op.restype = ctypes.c_void_p
    ch = k.CloseHandle
    ch.argtypes = [ctypes.c_void_p]
    ch.restype = ctypes.c_int
    qn = k.QueryFullProcessImageNameW
    qn.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
                   ctypes.POINTER(ctypes.c_uint32)]
    qn.restype = ctypes.c_int
    h = op(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not h:
        return ""
    try:
        size = ctypes.c_uint32(1024)
        buf = ctypes.create_unicode_buffer(1024)
        return buf.value if qn(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        ch(h)


def check_screen_reader() -> bool:
    pv = ctypes.wintypes.BOOL()
    ok = ctypes.windll.user32.SystemParametersInfoW(
        SPI_GETSCREENREADER, 0, ctypes.byref(pv), 0
    )
    print(f"  SystemParametersInfoW(SPI_GETSCREENREADER) 调用成功={bool(ok)}")
    print(f"  → 屏幕阅读器标志 = {bool(pv.value)}"
          f"  {'✓ 已开启' if pv.value else '✗ 未开启（Qt 不会暴露辅助功能树）'}")
    return bool(pv.value)


def check_registry() -> None:
    import winreg

    for path, name in (
        (r"SOFTWARE\Microsoft\Narrator\NoRoam", "RunningState"),
        (r"SOFTWARE\Microsoft\Narrator\NoRoam", "RunningStateType"),
        (r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Accessibility", "Configuration"),
    ):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ)
            try:
                val, typ = winreg.QueryValueEx(key, name)
                print(f"  HKCU\\{path}\\{name} = {val!r} (type={typ})")
            except FileNotFoundError:
                print(f"  HKCU\\{path}\\{name} —— 不存在")
            finally:
                winreg.CloseKey(key)
        except Exception as exc:  # noqa: BLE001
            print(f"  HKCU\\{path} 打开失败：{exc}")


def check_uia_global() -> None:
    """看 UIAutomation 客户端自己怎么判断"辅助功能是否可用"。"""
    try:
        from wx4py.core import uiautomation as uia
    except Exception as exc:  # noqa: BLE001
        print(f"  无法导入 uiautomation：{exc}")
        return

    for attr in (
        "IsScreenReaderActive",
        "IsHighContrastActive",
        "IsUIAutomationAvailable",
    ):
        try:
            val = getattr(uia, attr, "<不存在>")
            print(f"  uiautomation.{attr} = {val}")
        except Exception as exc:  # noqa: BLE001
            print(f"  uiautomation.{attr} 读取失败：{exc}")

    # 试试看从根节点能不能看到微信的控件
    try:
        root = uia.GetRootControl()
        found = []
        for app in root.GetChildren():
            try:
                nm = str(getattr(app, "Name", "") or "")
                if "微信" in nm or "Weixin" in nm or "WeChat" in nm:
                    found.append((nm, str(getattr(app, "ClassName", "") or "")))
            except Exception:
                pass
        print(f"  从 UIA 根节点看到的微信应用节点：{found or '（一个都没有）'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  遍历 UIA 根节点失败：{exc}")


def main() -> int:
    print("=" * 88)
    print("深挖：微信为什么没暴露 UIA 控件树")
    print("=" * 88)

    print("\n[1] 系统屏幕阅读器标志（最关键的一项）")
    sr = check_screen_reader()

    print("\n[2] 相关注册表项")
    check_registry()

    print("\n[3] uiautomation 库的全局判断")
    check_uia_global()

    print("\n[4] 微信进程与窗口")
    procs: dict[int, str] = {}
    for p in (win32gui.GetWindowThreadProcessId(h) for h in []):
        _ = p

    import subprocess

    names = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="replace",
    ).stdout
    print("  tasklist 结果：")
    for line in names.strip().splitlines():
        if line.strip():
            print(f"    {line.strip()}")

    def cb(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe = exe_of_pid(pid)
        except Exception:
            return True
        if os.path.basename(exe).lower() != "weixin.exe":
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        cls = win32gui.GetClassName(hwnd) or ""
        if not title:
            return True
        try:
            rect = win32gui.GetWindowRect(hwnd)
            w, h = rect[2] - rect[0], rect[3] - rect[1]
        except Exception:
            w = h = 0
        print(f"    HWND={hwnd:>9} PID={pid:>6} {w}x{h} "
              f"可见={'是' if win32gui.IsWindowVisible(hwnd) else '否'} "
              f"{cls} «{title}»")
        return True

    win32gui.EnumWindows(cb, None)

    print("\n" + "=" * 88)
    print("结论")
    print("=" * 88)
    if not sr:
        print("✗ 系统屏幕阅读器标志没开 —— 这是 Qt 不暴露控件树的直接原因。")
        print()
        print("  微信 4.x 用 Qt 写的。Qt 只在检测到'有屏幕阅读器在运行'时才构建")
        print("  辅助功能控件树。这个标志没开，UIA 就永远只能看到窗口外壳。")
        print()
        print("  开启方法（需要管理员权限的 PowerShell 或 CMD）：")
        print()
        print("    reg add \"HKCU\\SOFTWARE\\Microsoft\\Narrator\\NoRoam\" /v RunningState /t REG_DWORD /d 1 /f")
        print("    powershell -Command \"Add-Type -Namespace W -Name U -MemberDefinition '[DllImport(\\\"user32.dll\\\")]public static extern bool SystemParametersInfo(uint a,uint b,ref bool c,uint d);'; [bool]$v=$true; [W.U]::SystemParametersInfo(0x47,1,[ref]$v,3)\"")
        print()
        print("  或者最简单：直接打开一次 Windows 的'讲述人'(Narrator)，")
        print("  它会自动把系统标记为'有屏幕阅读器'，然后再重启微信。")
        print("  设置 → 辅助功能 → 讲述人 → 打开开关（打开后立刻关掉也行）")
    else:
        print("✓ 屏幕阅读器标志已开启。那问题在别处，看上面第 4 节的窗口列表。")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
