"""观察：微信窗口恢复后，UIA 控件树会不会延迟长回来。

之前观察到：装完 4.1.8.28 时树有 50 个节点；被 wx4py 的 _minimize_window()
收进托盘、再恢复之后，树只剩 1 个节点。本脚本做三件事：
  1. 把窗口恢复并置前，持续观察 90 秒
  2. 中途尝试触发重绘（切换焦点 / 点击窗口标题栏空白处周边）
  3. 每次采样都记录"内存里 gate 字节的值"，看它是否被重置

只读 + ShowWindow / SetForegroundWindow，不做任何危险操作。

用法：.venv\\Scripts\\python.exe tools\\watch_uia_recover.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import win32con  # noqa: E402
import win32gui  # noqa: E402

from qq2wx.wxgate import (  # noqa: E402
    KNOWN_GATE_RVA,
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_OPERATION,
    PROCESS_VM_READ,
    PROCESS_VM_WRITE,
    _kernel32,
    file_version_of,
    find_module,
    find_wechat_hwnd,
    mmui_node_count,
    read_mem,
    scan_gate_candidates,
    weixin_pids,
)


def main() -> int:
    hwnd = find_wechat_hwnd()
    if not hwnd:
        print("找不到微信窗口")
        return 1

    # 找 gate 地址并打开进程
    handle = None
    gate_addr = 0
    gate_rva = 0
    for pid in weixin_pids():
        h = _kernel32().OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION,
            False, pid,
        )
        if not h:
            continue
        m = find_module(pid, "weixin.dll")
        if not m:
            _kernel32().CloseHandle(h)
            continue
        base, _sz, path = m
        ver = file_version_of(path)
        cands = ([KNOWN_GATE_RVA[ver]] if ver in KNOWN_GATE_RVA else []) + scan_gate_candidates(path)
        if cands:
            gate_rva = cands[0]
            gate_addr = base + gate_rva
        handle = h
        print(f"进程 PID={pid}  版本={ver}  gate RVA=0x{gate_rva:X}")
        break

    print("=" * 76)
    print("观察窗口恢复后 UIA 树是否长回来（90 秒）")
    print("=" * 76)
    print(f"{'时间':>6}  {'可见':<4} {'最小化':<6} {'节点':>4} {'mmui':>4}  gate字节")
    print("-" * 76)

    t0 = time.time()
    while time.time() - t0 < 90:
        el = int(time.time() - t0)
        try:
            vis = bool(win32gui.IsWindowVisible(hwnd))
            icon = bool(win32gui.IsIconic(hwnd))

            if not vis:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.8)
                win32gui.SetForegroundWindow(hwnd)
                vis = bool(win32gui.IsWindowVisible(hwnd))

            mmui, total = mmui_node_count(hwnd)
            byte = "-"
            if handle and gate_addr:
                rb = read_mem(handle, gate_addr, 1)
                byte = f"0x{rb[0]:02X}" if rb else "读不到"
            print(f"{el:>5}s  {'是' if vis else '否':<4} {'是' if icon else '否':<6} "
                  f"{total:>4} {mmui:>4}  {byte}")
        except Exception as exc:  # noqa: BLE001
            print(f"{el:>5}s  采样失败：{exc}")

        # 前半程多摸几次焦点，看能否触发重建
        if el in (10, 25, 40):
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                time.sleep(1.0)
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
                print(f"{el:>5}s  -> 主动最小化再恢复，试探重建")
            except Exception:
                pass

        time.sleep(5)

    if handle:
        _kernel32().CloseHandle(handle)

    print("-" * 76)
    mmui, total = mmui_node_count(hwnd)
    print(f"结束状态：节点 {total}，mmui {mmui}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
