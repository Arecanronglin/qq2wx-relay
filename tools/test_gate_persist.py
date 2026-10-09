"""测试 gate 写入后的持续时间，判断需不需要反复激活。

只写 1 个字节，失败自动回滚。用法：
    .venv\\Scripts\\python.exe tools\\test_gate_persist.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qq2wx.wxgate import (  # noqa: E402
    KNOWN_GATE_RVA,
    _kernel32,
    file_version_of,
    find_module,
    find_wechat_hwnd,
    mmui_node_count,
    read_mem,
    weixin_pids,
    write_byte,
    PROCESS_QUERY_INFORMATION,
    PROCESS_VM_OPERATION,
    PROCESS_VM_READ,
    PROCESS_VM_WRITE,
)


def sample(tag: str) -> int:
    hwnd = find_wechat_hwnd()
    if not hwnd:
        print(f"  [{tag}] 找不到窗口")
        return -1
    mmui, total = mmui_node_count(hwnd)
    print(f"  [{tag}] mmui={mmui} total={total}")
    return mmui


def main() -> int:
    print("=" * 80)
    print("gate 持续时间测试")
    print("=" * 80)

    hwnd = find_wechat_hwnd()
    print(f"\n微信窗口 HWND={hwnd}")

    print("\n--- 激活前 ---")
    sample("t=0")

    # 找到进程和地址
    pid_used = base = addr = 0
    version = ""
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
        b, _s, path = m
        v = file_version_of(path)
        if v in KNOWN_GATE_RVA:
            pid_used, base, version = pid, b, v
            addr = b + KNOWN_GATE_RVA[v]
            handle = h
            break
        _kernel32().CloseHandle(h)

    if not pid_used:
        print("\n没有可用的进程/版本组合")
        return 1

    print(f"\nPID={pid_used}  版本={version}  gate 地址=0x{addr:016X}")

    # 写入
    print("\n--- 写入 gate=1 ---")
    if not write_byte(handle, addr, 1):
        print("  写入失败！")
        return 1
    cur = read_mem(handle, addr, 1)
    print(f"  回读 = {cur.hex() if cur else 'None'}")

    # 采样
    print("\n--- 持续采样 ---")
    results = []
    for i, delay in enumerate([0.5, 1, 2, 3, 5, 10, 15, 20, 30]):
        if i:
            time.sleep(delay)
        mmui = sample(f"累计约 {sum([0.5,1,2,3,5,10,15,20,30][:i+1]):.1f}s")
        rb = read_mem(handle, addr, 1)
        byte = rb[0] if rb else None
        print(f"        进程内字节 = {byte}")
        results.append((mmui, byte))

    print("\n" + "=" * 80)
    print("结论")
    print("=" * 80)
    zeros = [r for r in results if r[1] == 0]
    ones = [r for r in results if r[1] == 1]
    print(f"  字节保持 1 的采样数：{len(ones)}")
    print(f"  字节被改回 0 的采样数：{len(zeros)}")
    if ones and not zeros:
        print("  → 写入是持久的，不需要反复激活。")
    elif zeros and not ones:
        print("  → 立刻被重置，需要在每次操作前重新激活。")
    else:
        print(f"  → 部分持久：第 {len(ones)} 次采样后失效。")

    print("\n  详细：", results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
