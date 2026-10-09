"""深入检查 Weixin.exe 的每个顶层窗口：谁才是真正的主界面。

只读 + 最多调用 ShowWindow 让它可见（不做点击/输入/发送）。
用法：
    .venv\\Scripts\\python.exe tools\\inspect_wechat_windows.py [--no-show]
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402

SHOW = "--no-show" not in sys.argv
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# 微信主界面上一定会有的控件名（搜索框 / 聊天列表 / 通讯录按钮等）
MAIN_UI_HINTS = ("搜索", "Search", "聊天", "通讯录", "收藏", "微信", "朋友圈", "看一看")


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


def walk_names(ctrl, max_depth: int = 6, limit: int = 60) -> list[str]:
    """收集控件树里的 Name / ControlType，用来判断这是不是主界面。"""
    out: list[str] = []
    stack = [(ctrl, 0)]
    while stack and len(out) < limit:
        node, depth = stack.pop()
        try:
            name = str(getattr(node, "Name", "") or "")
            ctype = str(getattr(node, "ControlTypeName", "") or "")
            if name or ctype:
                out.append(f"{ctype}:{name}" if name else ctype)
        except Exception:
            pass
        if depth >= max_depth:
            continue
        try:
            for ch in node.GetChildren():
                stack.append((ch, depth + 1))
        except Exception:
            pass
    return out


def main() -> int:
    print("=" * 96)
    print("Weixin.exe 顶层窗口深入检查")
    print("=" * 96)

    targets: list[tuple[int, int, str]] = []

    def cb(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe = exe_of_pid(pid)
        except Exception:
            return True
        if os.path.basename(exe).lower() != "weixin.exe":
            return True
        cls = win32gui.GetClassName(hwnd) or ""
        targets.append((hwnd, pid, cls))
        return True

    win32gui.EnumWindows(cb, None)

    if not targets:
        print("\n没有 Weixin.exe 的顶层窗口。微信没在运行？")
        return 1

    print(f"\n找到 {len(targets)} 个 Weixin.exe 顶层窗口\n")

    try:
        from wx4py.core import uiautomation as uia
    except Exception as exc:  # noqa: BLE001
        print(f"无法导入 wx4py 的 uiautomation：{exc}")
        return 2

    scored: list[tuple[int, int, str, list[str]]] = []

    for hwnd, pid, cls in targets:
        title = win32gui.GetWindowText(hwnd) or ""
        try:
            rect = win32gui.GetWindowRect(hwnd)
            w, h = rect[2] - rect[0], rect[3] - rect[1]
        except Exception:
            w = h = 0

        print("-" * 96)
        print(f"HWND={hwnd}  PID={pid}")
        print(f"  类名   : {cls}")
        print(f"  标题   : «{title}»")
        print(f"  尺寸   : {w}x{h}")
        print(f"  可见   : {'是' if win32gui.IsWindowVisible(hwnd) else '否'}"
              f"   最小化: {'是' if win32gui.IsIconic(hwnd) else '否'}")

        # 如果是隐藏的，尝试恢复（这是让主界面出来的关键一步）
        if SHOW and not win32gui.IsWindowVisible(hwnd) and w > 400:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.8)
                print(f"  -> 已调用 ShowWindow(SW_RESTORE)，"
                      f"现在可见={'是' if win32gui.IsWindowVisible(hwnd) else '否'}")
            except Exception as exc:  # noqa: BLE001
                print(f"  -> ShowWindow 失败：{exc}")

        # 看 UIA 树内容
        names: list[str] = []
        try:
            root = uia.ControlFromHandle(hwnd)
            if root:
                names = walk_names(root)
        except Exception as exc:  # noqa: BLE001
            print(f"  UIA 读取失败：{exc}")

        print(f"  UIA 节点数: {len(names)}")
        if names:
            preview = ", ".join(names[:14])
            print(f"  控件预览 : {preview}")

        # 打分：是不是主界面
        hint_hits = sum(1 for n in names if any(h in n for h in MAIN_UI_HINTS))
        score = len(names) + hint_hits * 10
        print(f"  主界面特征命中: {hint_hits}  评分={score}")
        scored.append((score, hwnd, title, names))

    print("\n" + "=" * 96)
    print("结论")
    print("=" * 96)
    scored.sort(key=lambda x: x[0], reverse=True)
    for score, hwnd, title, names in scored:
        flag = "  <== 最像主界面" if score == scored[0][0] else ""
        print(f"  评分={score:4d}  HWND={hwnd:>9}  标题=«{title}»  "
              f"可见={'是' if win32gui.IsWindowVisible(hwnd) else '否'}  "
              f"节点={len(names)}{flag}")

    print("\nwx4py 的 find_wechat_window() 会选择：")
    try:
        from wx4py.core.win32 import find_wechat_window

        picked = find_wechat_window()
        ptitle = win32gui.GetWindowText(picked) or ""
        print(f"  HWND={picked} 标题=«{ptitle}» "
              f"可见={'是' if win32gui.IsWindowVisible(picked) else '否'}")
        best = scored[0][1]
        if picked != best:
            print(f"  ⚠️  和上面评分最高的 HWND={best} 不是同一个窗口 —— 这就是选错窗口了")
        else:
            print("  选中的就是评分最高的窗口，没问题")
    except Exception as exc:  # noqa: BLE001
        print(f"  调用失败：{exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
