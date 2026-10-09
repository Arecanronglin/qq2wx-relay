"""把微信主窗口的 UIA 树完整 dump 出来，不限深度。

之前的健康检查限制了 max_depth=4，可能根本没走到真正的控件层。
这个脚本不限深度，把整棵树打出来，看控件到底在不在。

只读，不点击、不输入。
用法：.venv\\Scripts\\python.exe tools\\dump_uia_tree.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import win32gui  # noqa: E402
import win32process  # noqa: E402
import os  # noqa: E402
import ctypes  # noqa: E402


def find_main():
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
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

    def exe_of(pid):
        h = op(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not h:
            return ""
        try:
            size = ctypes.c_uint32(1024)
            buf = ctypes.create_unicode_buffer(1024)
            return buf.value if qn(h, 0, buf, ctypes.byref(size)) else ""
        finally:
            ch(h)

    best = None

    def cb(hwnd, _):
        nonlocal best
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if os.path.basename(exe_of(pid)).lower() != "weixin.exe":
                return True
        except Exception:
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        if title not in ("微信", "Weixin", "WeChat"):
            return True
        try:
            r = win32gui.GetWindowRect(hwnd)
            w, h = r[2] - r[0], r[3] - r[1]
        except Exception:
            return True
        if w < 600 or h < 400:
            return True
        if best is None or w * h > best[1] * best[2]:
            best = (hwnd, w, h)
        return True

    win32gui.EnumWindows(cb, None)
    return best or (0, 0, 0)


def main() -> int:
    hwnd, w, h = find_main()
    if not hwnd:
        print("没找到微信主窗口")
        return 1

    print("=" * 92)
    print(f"微信主窗口 UIA 树完整 dump   HWND={hwnd}  {w}x{h}")
    print("=" * 92)

    from wx4py.core import uiautomation as uia

    root = uia.ControlFromHandle(hwnd)
    if not root:
        print("ControlFromHandle 返回空")
        return 1

    # ---- 手法 1：UIA 的 GetChildren 递归（和 wx4py 用的一样）
    print("\n【手法 1】walk GetChildren（不限深度，最多 400 个节点）")
    total = 0
    named = 0
    max_depth_seen = 0

    def walk(ctrl, depth=0):
        nonlocal total, named, max_depth_seen
        if total >= 400:
            return
        total += 1
        max_depth_seen = max(max_depth_seen, depth)
        try:
            name = str(getattr(ctrl, "Name", "") or "")
            ctype = str(getattr(ctrl, "ControlTypeName", "") or "")
            cls = str(getattr(ctrl, "ClassName", "") or "")
        except Exception:
            name = ctype = cls = ""
        if name:
            named += 1
        if depth <= 12 and (name or cls):
            indent = "  " * min(depth, 12)
            print(f"{indent}[{depth}] {ctype} name=«{name}» cls={cls}")
        try:
            for child in ctrl.GetChildren():
                walk(child, depth + 1)
        except Exception:
            pass

    walk(root)
    print(f"\n  → 总节点数={total}  有名字的={named}  最大深度={max_depth_seen}")

    # ---- 手法 2：从 UIA 根走 Application -> Window
    print("\n【手法 2】从桌面根节点定位微信 Application 再往下走")
    try:
        desktop = uia.GetRootControl()
        for app in desktop.GetChildren():
            nm = str(getattr(app, "Name", "") or "")
            if "微信" not in nm and "Weixin" not in nm:
                continue
            print(f"  找到应用节点：name=«{nm}» cls={getattr(app, 'ClassName', '')}")
            n2 = 0
            for win in app.GetChildren():
                n2 += 1
                wname = str(getattr(win, "Name", "") or "")
                wcls = str(getattr(win, "ClassName", "") or "")
                print(f"    Window: name=«{wname}» cls={wcls}")
                cnt = 0
                for sub in win.GetChildren():
                    cnt += 1
                    sname = str(getattr(sub, "Name", "") or "")
                    scls = str(getattr(sub, "ClassName", "") or "")
                    stype = str(getattr(sub, "ControlTypeName", "") or "")
                    print(f"      -> [{stype}] name=«{sname}» cls={scls}")
                    if cnt > 30:
                        break
            print(f"    共 {n2} 个 window 子节点")
    except Exception as exc:  # noqa: BLE001
        print(f"  失败：{exc}")

    # ---- 手法 3：搜索框搜索
    print("\n【手法 3】直接在窗口子树里搜常见控件")
    try:
        for kw in ("搜索", "Search", "聊天", "通讯录", "文件传输助手"):
            ctrl = uia.Control(searchFromControl=root, Name=kw)
            found = ctrl.Exists(maxSearchSeconds=1.0) if ctrl else False
            print(f"  Name 含 «{kw}» 的控件：{'找到' if found else '没找到'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  搜索失败：{exc}")

    print("\n" + "=" * 92)
    if total >= 5:
        print(f"✓ 控件树其实是有的（{total} 个节点），是之前的深度限制太小看漏了。")
    else:
        print(f"✗ 确实只有 {total} 个节点，微信没有暴露内部控件。")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
