"""就绪检查：确认用户回来时系统一切就绪。

逐项验证并给出结论：能不能立刻开始转发、还差什么。

用法：
    .venv\\Scripts\\python.exe tools\\ready_check.py
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK = "✓"
BAD = "✗"
WAIT = "⏳"

items: list[tuple[str, str, str]] = []   # (状态, 项目, 说明)


def add(mark: str, name: str, detail: str = "") -> None:
    items.append((mark, name, detail))


def main() -> int:
    ctypes.windll.ole32.CoInitializeEx(None, 0x2)

    print("=" * 74)
    print("qq2wx-relay 就绪检查")
    print(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 74)

    blocking: list[str] = []

    # ---------------------------------------------------------- 微信版本
    try:
        from qq2wx.wxgate import file_version_of, find_module, weixin_pids

        ver = ""
        for pid in weixin_pids():
            m = find_module(pid, "weixin.dll")
            if m:
                ver = file_version_of(m[2])
                break
        if ver == "4.1.8.28":
            add(OK, "微信版本", f"{ver}（可自动化）")
        elif ver:
            add(BAD, "微信版本", f"{ver}（不是 4.1.8.28，无法自动化）")
            blocking.append("微信版本不对，需要重新降级")
        else:
            add(BAD, "微信版本", "读不到（微信没在运行？）")
            blocking.append("微信没在运行")
    except Exception as exc:  # noqa: BLE001
        add(BAD, "微信版本", str(exc))
        blocking.append("版本检查失败")

    # ---------------------------------------------------------- 更新封锁
    locked = 0
    total_upd = 0
    for base in (Path(r"D:\wechat418\Weixin"), Path(r"D:\wechat\Weixin")):
        if not base.exists():
            continue
        for vd in base.iterdir():
            if not vd.is_dir():
                continue
            upd = vd / "WeixinUpdate.exe"
            if upd.exists():
                total_upd += 1
                if upd.stat().st_size == 0:
                    locked += 1
    if total_upd and locked == total_upd:
        add(OK, "自动更新封锁", f"{locked}/{total_upd} 处已封锁")
    elif total_upd:
        add(BAD, "自动更新封锁", f"只封锁 {locked}/{total_upd} 处")
        blocking.append("有更新程序未封锁")
    else:
        add(BAD, "自动更新封锁", "找不到更新程序（安装异常？）")

    # ---------------------------------------------------------- 微信登录 / UIA
    try:
        import win32gui
        from qq2wx.wxgate import find_wechat_hwnd, mmui_node_count
        from wx4py.core import uiautomation as uia

        h = find_wechat_hwnd()
        if not h:
            add(BAD, "微信窗口", "找不到")
            blocking.append("找不到微信窗口")
        else:
            root = uia.ControlFromHandle(h)
            cls = str(getattr(root, "ClassName", "") or "")
            m, t = mmui_node_count(h)
            vis = bool(win32gui.IsWindowVisible(h))
            add(OK if vis else BAD, "窗口可见", "是" if vis else "否（在托盘里）")
            if not vis:
                blocking.append("微信窗口在托盘里，需要点开")
            if "MainWindow" in cls:
                add(OK, "微信登录", "已登录")
            elif "LoginWindow" in cls:
                add(WAIT, "微信登录", "停在登录页，需要点「登录」+ 手机确认")
                blocking.append("微信未登录")
            else:
                add(BAD, "微信登录", f"状态未知（根节点 {cls}）")
                blocking.append("微信状态未知")
            if t >= 5:
                add(OK, "UIA 控件树", f"{t} 个节点（mmui {m} 个）")
            else:
                add(BAD, "UIA 控件树", f"只有 {t} 个节点，需要重启微信")
                blocking.append("UIA 树塌陷，需要重启微信")
    except Exception as exc:  # noqa: BLE001
        add(BAD, "微信 UIA", str(exc))
        blocking.append("UIA 检查失败")

    # ---------------------------------------------------------- NapCat / QQ
    try:
        from qq2wx.config import AppConfig
        from qq2wx.qq import OneBotHttpClient

        cfg = AppConfig.load("config.yaml")
        http = OneBotHttpClient(cfg.qq.http_url, cfg.qq.access_token)
        info = http.call("get_login_info")
        add(OK, "NapCat", f"{info.get('nickname')}（{info.get('user_id')}）")
        groups = http.group_list()
        add(OK, "群列表", f"{len(groups)} 个")
        for gid in cfg.qq_groups:
            if gid in groups:
                add(OK, f"监听群 {gid}", groups[gid])
            else:
                add(BAD, f"监听群 {gid}", "机器人号不在这个群里")
                blocking.append(f"机器人号不在群 {gid} 里")
    except Exception as exc:  # noqa: BLE001
        add(BAD, "NapCat", f"连不上：{exc}")
        blocking.append("NapCat 连不上")

    # ---------------------------------------------------------- 端口
    try:
        import socket
        from urllib.parse import urlparse

        u = urlparse(cfg.qq.ws_url)
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 3001),
                                      timeout=3):
            add(OK, "WebSocket 端口", f"{u.hostname}:{u.port} 可连接")
    except Exception as exc:  # noqa: BLE001
        add(BAD, "WebSocket 端口", str(exc))
        blocking.append("NapCat WebSocket 端口连不上")

    # ---------------------------------------------------------- 配置
    add(OK, "发送通道", cfg.wechat.driver)
    for gid, targets in cfg.qq_groups.items():
        add(OK, "转发规则", f"{gid} -> {', '.join(targets)}")

    # ---------------------------------------------------------- 输出
    print()
    for mark, name, detail in items:
        line = f"  [{mark}] {name:<16}"
        if detail:
            line += f" {detail}"
        print(line)

    print()
    print("=" * 74)
    if not blocking:
        print("✓ 全部就绪，可以双击 4-正式运行.bat 开始转发")
        code = 0
    else:
        print(f"还差 {len(blocking)} 件事：")
        for i, b in enumerate(blocking, 1):
            print(f"  {i}. {b}")
        print()
        if any("登录" in b for b in blocking):
            print("最关键的是最后一步：")
            print("  → 点微信窗口里的「登录」按钮，然后手机上点确认")
            print("  → 确认后重新跑一次本检查，或者直接跑 6-全面诊断.bat")
        code = 1
    print("=" * 74)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
