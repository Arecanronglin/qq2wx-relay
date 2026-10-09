"""按上游标准验证 gate 是否真的生效，并找到承载 mmui 控件的窗口。

上游 wechatauto-replica 的 _mmui_present() 判定标准是：
    根控件 ClassName 以 "mmui::" 开头，或它的**直接子节点**里有 mmui:: 控件
只要满足就算物化成功（不需要整棵子树都铺开）。

本脚本激活 gate 后按这个标准逐个微信窗口检查，并打印每个窗口的
直接子节点，找出 mmui 控件真正挂在哪个窗口下。

用法：.venv\\Scripts\\python.exe tools\\verify_gate.py
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import win32gui  # noqa: E402
import win32process  # noqa: E402

from qq2wx.wxgate import (  # noqa: E402
    activate_gate,
    find_module,
    gate_health,
    weixin_pids,
)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def exe_of(pid: int) -> str:
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


def wechat_windows() -> list[tuple[int, int, str, str, int, int]]:
    """所有 Weixin.exe 且加载了 Weixin.dll 的顶层窗口：(hwnd, pid, title, cls, w, h)"""
    dll_pids: set[int] = set()
    for pid in weixin_pids():
        if find_module(pid, "weixin.dll"):
            dll_pids.add(pid)

    out = []

    def cb(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if pid not in dll_pids:
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        cls = win32gui.GetClassName(hwnd) or ""
        try:
            r = win32gui.GetWindowRect(hwnd)
            w, h = r[2] - r[0], r[3] - r[1]
        except Exception:
            w = h = 0
        if w < 200 or h < 200:
            return True
        out.append((hwnd, pid, title, cls, w, h))
        return True

    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda t: t[4] * t[5], reverse=True)
    return out


def mmui_present(hwnd: int) -> tuple[bool, str]:
    """上游标准的物化判定。"""
    from wx4py.core import uiautomation as uia

    try:
        c = uia.ControlFromHandle(hwnd)
    except Exception as exc:  # noqa: BLE001
        return False, f"ControlFromHandle 异常：{exc}"
    if c is None:
        return False, "ControlFromHandle 返回 None"

    cls = str(getattr(c, "ClassName", "") or "")
    if cls.startswith("mmui::"):
        return True, f"根节点即 {cls}"

    kids = []
    try:
        kids = c.GetChildren()
    except Exception:  # noqa: BLE001
        pass
    names = [str(getattr(k, "ClassName", "") or "") for k in kids]
    hit = [n for n in names if n.startswith("mmui::")]
    if hit:
        return True, f"直接子节点含 {hit}"
    return False, f"根={cls} 子节点={names}"


def main() -> int:
    print("=" * 90)
    print("gate 生效验证（按上游 mmui_present 标准）")
    print("=" * 90)

    wins = wechat_windows()
    print(f"\n加载了 Weixin.dll 的微信顶层窗口：{len(wins)} 个")

    print("\n--- 激活前 ---")
    for hwnd, pid, title, cls, w, h in wins:
        ok, why = mmui_present(hwnd)
        print(f"  HWND={hwnd:>9} PID={pid:>6} {w}x{h} «{title}»")
        print(f"      {cls}  ->  物化={ok}  ({why})")

    print("\n--- 热激活 ---")
    res = activate_gate()
    print(res.summary())
    time.sleep(1.0)

    print("\n--- 激活后 ---")
    best = None
    for hwnd, pid, title, cls, w, h in wechat_windows():
        ok, why = mmui_present(hwnd)
        live_cls = ""
        try:
            from wx4py.core import uiautomation as uia

            live_cls = str(getattr(uia.ControlFromHandle(hwnd), "ClassName", "") or "")
        except Exception:  # noqa: BLE001
            pass
        print(f"  HWND={hwnd:>9} PID={pid:>6} {w}x{h} «{title}»")
        print(f"      窗口类名={cls}  UIA根类名={live_cls}  ->  物化={ok}  ({why})")
        if ok and best is None:
            best = hwnd

    print("\n--- 直接子节点明细（物化成功的那个窗口）---")
    target = best or (wins[0][0] if wins else 0)
    if target:
        try:
            from wx4py.core import uiautomation as uia

            c = uia.ControlFromHandle(target)
            print(f"  根：{getattr(c, 'ClassName', '')}  «{getattr(c, 'Name', '')}»")
            for k in c.GetChildren():
                print(f"    - {getattr(k, 'ClassName', '')}  «{str(getattr(k, 'Name', ''))[:40]}»"
                      f"  aid={getattr(k, 'AutomationId', '')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  读取失败：{exc}")

    print("\n" + "=" * 90)
    print("结论")
    print("=" * 90)
    print(f"  gate 健康检查：{gate_health()}")
    if best:
        print(f"  ✓ 物化成功，承载 mmui 控件的窗口 HWND={best}")
        print(f"    注意：这个 HWND 可能和激活前找到的不是同一个 ——")
        print(f"    激活后必须【重新定位窗口】，不能复用旧的句柄。")
    else:
        print("  ✗ 没有窗口达到物化标准")
    print("=" * 90)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
