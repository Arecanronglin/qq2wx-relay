"""一站式诊断：把「转发不动了」的所有可能原因一次查完。

用法：
    python -m qq2wx --doctor
    .venv\\Scripts\\python.exe -m qq2wx --doctor

设计目标：用户登录微信之后，或者转发突然不工作了，跑这一条命令就能知道
是哪个环节坏了、该怎么修。所有检查都是只读的（最多调用 ShowWindow
把窗口恢复出来）。
"""

from __future__ import annotations

import logging
import os
import time

from .config import AppConfig
from .core import get_logger

log = get_logger("qq2wx.doctor")

OK = "✓"
BAD = "✗"
WARN = "!"


class _Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.problems: list[str] = []
        self.fixes: list[str] = []

    def title(self, text: str) -> None:
        self.lines.append("")
        self.lines.append("─" * 72)
        self.lines.append(text)
        self.lines.append("─" * 72)

    def item(self, mark: str, text: str) -> None:
        self.lines.append(f"  [{mark}] {text}")

    def problem(self, text: str, fix: str = "") -> None:
        self.problems.append(text)
        if fix:
            self.fixes.append(fix)
        self.item(BAD, text)

    def render(self, healthy: bool) -> None:
        for line in self.lines:
            print(line)
        print()
        print("=" * 72)
        if healthy and not self.problems:
            print("✓ 一切正常，可以运行 4-正式运行.bat")
        else:
            print(f"发现 {len(self.problems)} 个问题：")
            for i, p in enumerate(self.problems, 1):
                print(f"  {i}. {p}")
            if self.fixes:
                print()
                print("怎么修：")
                for f in dict.fromkeys(self.fixes):
                    print(f"  · {f}")
        print("=" * 72)


def run_doctor(cfg: AppConfig, config_path: str) -> int:
    r = _Report()
    healthy = True

    # wx4py 自己的日志很吵（每次搜索都打一堆 INFO），会把诊断结果冲散。
    # 诊断期间只留 WARNING 以上。
    for name in ("wx4py", "wx4py.client", "wx4py.core.window", "wx4py.features.chat"):
        logging.getLogger(name).setLevel(logging.WARNING)

    print("=" * 72)
    print("qq2wx-relay 一站式诊断")
    print(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)

    driver = cfg.wechat.driver

    # ---------------------------------------------------------- 配置
    r.title("1. 配置")
    r.item(OK, f"配置文件：{config_path}")
    r.item(OK, f"发送通道：{driver}")
    groups = cfg.qq_groups
    if not groups:
        healthy = False
        r.problem("没有配置任何 QQ 群", "在 config.yaml 的 qq_groups 里加群号")
    else:
        for gid, targets in groups.items():
            if targets:
                r.item(OK, f"监听 {gid} -> {', '.join(targets)}")
            else:
                r.item(WARN, f"监听 {gid} 但没配微信目标，会跳过")
    if driver == "push":
        kind = str(cfg.wechat.push.get("kind") or "pushplus")
        token = str(cfg.wechat.push.get("token") or "")
        if token:
            r.item(OK, f"推送配置：{kind}，token 已填（{token[:6]}…{token[-4:]}）")
        else:
            healthy = False
            r.problem(
                f"driver=push 但 push.token 是空的（{kind}）",
                "微信扫码 pushplus.plus 拿 token，填进 config.yaml 的 wechat.push.token",
            )

    # ---------------------------------------------------------- QQ 侧
    r.title("2. QQ 侧（NapCat）")
    from .qq import OneBotHttpClient

    http = OneBotHttpClient(cfg.qq.http_url, cfg.qq.access_token)
    login = None
    try:
        login = http.call("get_login_info")
    except Exception as exc:  # noqa: BLE001
        healthy = False
        r.problem(
            f"连不上 NapCat 的 HTTP 接口（{cfg.qq.http_url}）：{exc}",
            "确认 NapCat 在运行、已登录 QQ；网络配置里有 HTTP 服务端指向这个端口",
        )

    if login:
        r.item(OK, f"机器人账号：{login.get('nickname')}（{login.get('user_id')}）")
        try:
            groups_seen = http.group_list()
            r.item(OK, f"能读到 {len(groups_seen)} 个群")
            for gid in cfg.qq_groups:
                if gid in groups_seen:
                    r.item(OK, f"群 {gid} 在机器人号里：{groups_seen[gid]}")
                else:
                    healthy = False
                    r.problem(
                        f"机器人号不在 QQ 群 {gid} 里，收不到该群消息",
                        f"用机器人号（{login.get('user_id')}）加进群 {gid}，"
                        "或把 config.yaml 里这个群注释掉",
                    )
        except Exception as exc:  # noqa: BLE001
            r.item(WARN, f"读群列表失败：{exc}")

    # WebSocket 端口
    try:
        import socket
        from urllib.parse import urlparse

        u = urlparse(cfg.qq.ws_url)
        host, port = u.hostname or "127.0.0.1", u.port or 3001
        with socket.create_connection((host, port), timeout=3):
            r.item(OK, f"WebSocket 端口 {host}:{port} 可连接")
    except Exception as exc:  # noqa: BLE001
        healthy = False
        r.problem(
            f"WebSocket 端口连不上（{cfg.qq.ws_url}）：{exc}",
            "NapCat 网络配置里确认有「WebSocket 服务端」监听 3001，格式 array",
        )

    # ---------------------------------------------------------- 微信侧
    if driver == "wx4py":
        r.title("3. 微信侧（wx4py）")
        from .senders import ensure_wechat_window_visible, wechat_uia_healthy

        # 版本
        try:
            from .wxgate import file_version_of, find_module, weixin_pids

            ver = None
            for pid in weixin_pids():
                m = find_module(pid, "weixin.dll")
                if m:
                    ver = file_version_of(m[2])
                    break
            if ver:
                if ver.startswith("4.1.8"):
                    r.item(OK, f"微信版本 {ver}（可自动化）")
                elif ver.startswith(("4.1.11", "4.1.13", "4.1.15")) or ver >= "4.1.9":
                    healthy = False
                    r.problem(
                        f"微信版本 {ver} 不暴露 UIA 控件树，无法自动化",
                        "按 当前状态.md 重新降级到 4.1.8.28，并确认更新封锁还在",
                    )
                else:
                    r.item(WARN, f"微信版本 {ver} 未经验证")
            else:
                r.item(WARN, "读不到微信版本")
        except Exception as exc:  # noqa: BLE001
            r.item(WARN, f"版本检查失败：{exc}")

        # 窗口可见性
        vis, why = ensure_wechat_window_visible()
        if vis:
            r.item(OK, f"窗口：{why}")
        else:
            healthy = False
            r.problem(f"窗口不可见：{why}", "手动点开微信窗口，别让它留在托盘里")

        # UIA 树 + 登录状态
        logged_in = False
        try:
            from .wxgate import find_wechat_hwnd, mmui_node_count

            hwnd = find_wechat_hwnd()
            if hwnd:
                from wx4py.core import uiautomation as uia

                root = uia.ControlFromHandle(hwnd)
                cls = str(getattr(root, "ClassName", "") or "")
                mmui, total = mmui_node_count(hwnd)
                if "MainWindow" in cls:
                    logged_in = True
                    r.item(OK, f"已登录（根节点 {cls}）")
                elif "LoginWindow" in cls:
                    healthy = False
                    r.problem(
                        "微信停在登录界面",
                        "点微信窗口里的「登录」按钮，然后用手机确认",
                    )
                else:
                    r.item(WARN, f"根节点 {cls}（既不是登录页也不是主界面）")

                if total >= 5:
                    r.item(OK, f"UIA 控件树 {total} 个节点（mmui {mmui} 个）")
                else:
                    healthy = False
                    r.problem(
                        f"UIA 控件树只有 {total} 个节点，自动化会失败",
                        "重启微信（完全退出后重新打开）。注意别让窗口最小化进托盘",
                    )
            else:
                healthy = False
                r.problem("找不到微信窗口", "确认微信在运行")
        except Exception as exc:  # noqa: BLE001
            r.item(WARN, f"UIA 检查失败：{exc}")

        # 目标微信群是否存在（登录页上搜不了，跳过以免误报）
        if logged_in:
            try:
                targets = sorted({t for ts in cfg.qq_groups.values() for t in ts})
                if targets:
                    import ctypes

                    ctypes.windll.ole32.CoInitializeEx(None, 0x2)
                    from wx4py import WeChatClient

                    c = WeChatClient()
                    if hasattr(c, "connect"):
                        c.connect()
                    found, missing = [], []
                    for t in targets:
                        try:
                            res = c.chat_window.search(t)
                            names = []
                            if isinstance(res, dict):
                                for items in res.values():
                                    for it in items:
                                        names.append(
                                            getattr(it, "name", None)
                                            or getattr(it, "Name", "")
                                        )
                            if any(t == n or t in n for n in names):
                                found.append(t)
                            else:
                                missing.append(t)
                        except Exception:  # noqa: BLE001
                            missing.append(t)
                    for t in found:
                        r.item(OK, f"微信群「{t}」存在")
                    for t in missing:
                        healthy = False
                        r.problem(
                            f"在微信里没搜到「{t}」",
                            "核对 config.yaml 里的群名，必须和微信里显示的一字不差",
                        )
                    try:
                        if hasattr(c, "disconnect"):
                            c.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001
                r.item(WARN, f"微信群名检查跳过：{exc}")
        else:
            r.item(WARN, "未登录，跳过微信群名检查（登录页上无法搜索）")

    elif driver == "push":
        r.title("3. 微信侧（推送通道）")
        r.item(OK, "推送通道不依赖微信客户端，无需检查窗口/UIA 树")
        r.item(WARN, "推送不能转发图片和文件，只会发文件名提示")

    # ---------------------------------------------------------- 限流与留档
    r.title("4. 限流与留档")
    r.item(OK, f"每天上限 {cfg.limits.max_sends_per_day} 条，"
               f"最小间隔 {cfg.limits.min_send_interval}s")
    if cfg.limits.max_sends_per_day and cfg.limits.max_sends_per_day < 50:
        r.item(WARN, "每天上限偏低，可能会提前停止转发")
    r.item(OK, f"留档目录：{cfg.logging.archive_dir}")

    r.render(healthy)
    return 0 if (healthy and not r.problems) else 1
