"""诊断：列出微信相关的所有顶层窗口，找出 wx4py 为什么判断"窗口不可见"。

只读操作：不点击、不发送、不重启微信、不移动鼠标。
用法：
    .venv\\Scripts\\python.exe tools\\diag_wechat_window.py
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def exe_of_pid(pid: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    query_name = kernel32.QueryFullProcessImageNameW
    query_name.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)
    ]
    query_name.restype = ctypes.c_int

    h = open_process(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not h:
        return ""
    try:
        size = ctypes.c_uint32(1024)
        buf = ctypes.create_unicode_buffer(1024)
        return buf.value if query_name(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        close_handle(h)


def uia_node_count(hwnd: int, max_depth: int = 4, limit: int = 25) -> int:
    """统计 UIA 控件树节点数（和 wx4py 的健康检查同一套逻辑）。"""
    try:
        from wx4py.core import uiautomation as uia
    except Exception as exc:  # noqa: BLE001
        return -1

    try:
        root = uia.ControlFromHandle(hwnd)
    except Exception:
        return -1
    if not root:
        return -1

    count = 0
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count >= limit:
            return count
        if depth >= max_depth:
            continue
        try:
            for ch in node.GetChildren():
                stack.append((ch, depth + 1))
        except Exception:
            pass
    return count


def main() -> int:
    print("=" * 96)
    print("微信窗口诊断 — 只读，不会点击/发送/重启微信")
    print("=" * 96)

    # 微信 3.9 用 Qt51514QWindowIcon；4.x 是 Qt5.15.15，类名是 Qt51515QWindowIcon
    OLD_CLASS = "Qt51514QWindowIcon"

    rows: list[dict] = []

    def cb(hwnd, _):
        title = win32gui.GetWindowText(hwnd) or ""
        cls = win32gui.GetClassName(hwnd) or ""

        # 只关心可能和微信有关的窗口
        if not (
            "微信" in title
            or "WeChat" in title
            or cls.startswith("Qt")
            or "WeChat" in cls
        ):
            return True

        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe = exe_of_pid(pid)
        except Exception:
            pid, exe = 0, ""

        base = os.path.basename(exe).lower()
        if base not in {"weixin.exe", "wechat.exe", "wechatappex.exe"} and not cls.startswith("Qt"):
            return True

        rows.append(
            {
                "hwnd": hwnd,
                "pid": pid,
                "exe": os.path.basename(exe),
                "cls": cls,
                "title": title,
                "visible": bool(win32gui.IsWindowVisible(hwnd)),
                "iconic": bool(win32gui.IsIconic(hwnd)),
                "owner": win32gui.GetWindow(hwnd, win32con.GW_OWNER) or 0,
                "nodes": uia_node_count(hwnd),
                "is_old_class": cls == OLD_CLASS,
            }
        )
        return True

    win32gui.EnumWindows(cb, None)
    rows.sort(key=lambda r: (r["exe"], r["hwnd"]))

    if not rows:
        print("\n没有找到任何微信相关窗口。微信是不是没在运行？")
        return 1

    print(f"\n共 {len(rows)} 个候选窗口：\n")
    header = f"{'HWND':>10} {'PID':>7} {'进程':<16} {'可见':<5} {'最小化':<7} {'UIA节点':>8}  类名 / 标题"
    print(header)
    print("-" * 96)
    for r in rows:
        print(
            f"{r['hwnd']:>10} {r['pid']:>7} {r['exe']:<16} "
            f"{'是' if r['visible'] else '否':<5} {'是' if r['iconic'] else '否':<7} "
            f"{r['nodes']:>8}  {r['cls']}"
            + (f"  «{r['title']}»" if r["title"] else "")
        )

    # ---- 用 wx4py 自己的逻辑选一次，看它选了什么
    print("\n" + "-" * 96)
    print("wx4py 的 find_wechat_window() 会选择：")
    try:
        from wx4py.core.win32 import find_wechat_window, is_window_visible

        picked = find_wechat_window()
        if picked:
            pcls = win32gui.GetClassName(picked)
            ptitle = win32gui.GetWindowText(picked) or ""
            _, ppid = win32process.GetWindowThreadProcessId(picked)
            print(f"  HWND={picked}  ClassName={pcls}  标题=«{ptitle}»  PID={ppid}")
            print(f"  IsWindowVisible = {is_window_visible(picked)}")
            match = next((r for r in rows if r["hwnd"] == picked), None)
            if match:
                print(f"  UIA 节点数 = {match['nodes']}")
            print(f"  类名含 'MainWindow' = {'MainWindow' in pcls}")
        else:
            print("  None（没找到）")
    except Exception as exc:  # noqa: BLE001
        print(f"  调用失败：{exc}")

    # ---- 结论
    print("\n" + "=" * 96)
    print("诊断结论")
    print("=" * 96)

    vis = [r for r in rows if r["visible"]]
    print(f"· 可见的微信窗口：{len(vis)} 个")
    for r in vis:
        print(f"    HWND={r['hwnd']} {r['exe']} {r['cls']} UIA节点={r['nodes']}")

    old = [r for r in rows if r["is_old_class"]]
    if old:
        print(f"\n· 发现 {len(old)} 个旧版类名窗口 {OLD_CLASS}：")
        for r in old:
            print(
                f"    HWND={r['hwnd']} 可见={'是' if r['visible'] else '否'} "
                f"UIA节点={r['nodes']} 标题=«{r['title']}»"
            )
        print("  ⚠️  这是微信 3.9 的老类名。你的微信是 4.x，")
        print("     wx4py 的兜底 FindWindow('Qt51514QWindowIcon') 可能选中了这个错误窗口。")
    else:
        print(f"\n· 没有 {OLD_CLASS} 类名的窗口。")

    newq = [r for r in rows if r["cls"].startswith("Qt") and not r["is_old_class"]]
    if newq:
        print(f"\n· 其它 Qt 类名窗口（微信 4.x 主窗口候选）：")
        for r in newq:
            print(
                f"    HWND={r['hwnd']} {r['exe']} {r['cls']} "
                f"可见={'是' if r['visible'] else '否'} UIA节点={r['nodes']}"
            )

    big = [r for r in rows if r["nodes"] >= 5]
    print(f"\n· UIA 控件树 >=5 节点（wx4py 认为健康）的窗口：{len(big)} 个")
    for r in big:
        print(f"    HWND={r['hwnd']} {r['cls']} UIA节点={r['nodes']} 可见={'是' if r['visible'] else '否'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
