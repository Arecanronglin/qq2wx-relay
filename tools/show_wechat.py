"""只读诊断：把微信主窗口恢复出来并置前，不做任何点击/发送。

这一步只调用 Win32 的 ShowWindow / SetForegroundWindow，不改微信数据、
不模拟点击、不发送消息。

用法：
    .venv\\Scripts\\python.exe tools\\show_wechat.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import win32con  # noqa: E402
import win32gui  # noqa: E402

SW_SHOW = 5
SW_RESTORE = 9
SW_SHOWNORMAL = 1


def describe(hwnd: int, label: str) -> None:
    title = win32gui.GetWindowText(hwnd) or ""
    cls = win32gui.GetClassName(hwnd) or ""
    visible = bool(win32gui.IsWindowVisible(hwnd))
    iconic = bool(win32gui.IsIconic(hwnd))
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:
        rect = (0, 0, 0, 0)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    print(
        f"  [{label}] HWND={hwnd} 可见={'是' if visible else '否'} "
        f"最小化={'是' if iconic else '否'} 尺寸={w}x{h} 类名={cls} 标题=«{title}»"
    )


def find_wechat_main() -> int:
    """挑微信主窗口：Weixin.exe 的、标题为「微信」或「Weixin」的 Qt 窗口。"""
    from wx4py.core.win32 import find_wechat_window

    return find_wechat_window() or 0


def main() -> int:
    print("=" * 92)
    print("微信窗口恢复 — 只调用 Win32，不点击、不发送、不动鼠标")
    print("=" * 92)

    hwnd = find_wechat_main()
    if not hwnd:
        print("\n没找到微信窗口。微信在运行吗？")
        return 1

    print("\n【恢复前】")
    describe(hwnd, "选中")

    if win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
        print("\n窗口本来就是可见状态，不需要恢复。")
        return 0

    print("\n【开始恢复】")
    attempts = [
        ("ShowWindow(SW_RESTORE)", lambda: win32gui.ShowWindow(hwnd, SW_RESTORE)),
        ("ShowWindow(SW_SHOW)", lambda: win32gui.ShowWindow(hwnd, SW_SHOW)),
        ("ShowWindow(SW_SHOWNORMAL)", lambda: win32gui.ShowWindow(hwnd, SW_SHOWNORMAL)),
    ]

    for name, fn in attempts:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  {name} 抛异常：{exc}")
            continue
        time.sleep(0.6)
        visible = bool(win32gui.IsWindowVisible(hwnd))
        print(f"  {name} -> 可见={'是' if visible else '否'}")
        if visible:
            break

    if not win32gui.IsWindowVisible(hwnd):
        print("\nShowWindow 没能让窗口可见。再试 SetWindowPos + 置前。")
        try:
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  SetWindowPos 失败：{exc}")
        time.sleep(0.6)

    # 置前（SetForegroundWindow 对非前台进程有限制，失败不算错）
    try:
        win32gui.SetForegroundWindow(hwnd)
        print("  SetForegroundWindow -> 已请求置前")
    except Exception as exc:  # noqa: BLE001
        print(f"  SetForegroundWindow 失败（正常，Windows 有限制）：{exc}")

    time.sleep(0.8)

    print("\n【恢复后】")
    describe(hwnd, "选中")

    # 顺带看 UIA 节点数变没变
    try:
        from wx4py.core import uiautomation as uia

        root = uia.ControlFromHandle(hwnd)
        count, stack = 0, [(root, 0)]
        while stack and count < 25:
            node, depth = stack.pop()
            count += 1
            if depth >= 4:
                continue
            try:
                for ch in node.GetChildren():
                    stack.append((ch, depth + 1))
            except Exception:
                pass
        print(f"  UIA 控件树节点数 = {count}")
    except Exception as exc:  # noqa: BLE001
        print(f"  UIA 检查失败：{exc}")

    ok = bool(win32gui.IsWindowVisible(hwnd))
    print("\n" + "=" * 92)
    if ok:
        print("✓ 微信窗口现在可见了。")
        print("  请到屏幕上确认微信窗口确实出现了，然后告诉我。")
    else:
        print("✗ 仍然不可见。")
    print("=" * 92)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
