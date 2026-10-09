"""命令行入口。

常用：
  python -m qq2wx --probe-wx                 自检微信通道（会给文件传输助手发一条）
  python -m qq2wx --check                    校验配置，不连任何东西
  python -m qq2wx --test                     给配置里的微信群发一条测试消息
  python -m qq2wx                           正式跑（连 NapCat，开始转发）
  python -m qq2wx --replay events.jsonl      离线回放，验证整合逻辑
  python -m qq2wx --capture                  只连 QQ，把原始事件存下来供回放
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .config import AppConfig
from .core import get_logger, resolve_path, setup_logging
from .pipeline import Pipeline, SingleInstance, StatsPrinter, summarize
from .qq import OneBotHttpClient, OneBotWsConnection, ReplayConnection, build_connection
from .senders import (
    SendError,
    build_sender,
    probe_wx4py,
    run_wx_gate,
    run_wx_health,
    selfcheck_wx4py,
)

DEFAULT_CONFIG = "config.yaml"


def harden_console_encoding() -> None:
    """Windows 控制台默认是 GBK，打印中文/符号会 UnicodeEncodeError。

    这里把标准输出和日志输出都切到 UTF-8，并让无法编码的字符降级而不是崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


# 中文提示统一放在 Python 里打印：.bat 文件保持纯 ASCII，避免 cmd 在
# GBK 码页下把多字节字符切坏（那个坑踩过一次，见 tools/gen_bats.py 的注释）。
BANNERS: dict[str, list[str]] = {
    "check": [
        "qq2wx-relay · 校验配置",
        "不会连接 NapCat，也不会碰微信，随时可以运行。",
    ],
    "dry-run": [
        "第 1 阶段：QQ 群 → 转发链路（演练模式）",
        "",
        "· 不会往微信发任何东西，结果只打印在这个窗口里",
        "· 请确认 NapCat 已启动、QQ 已登录",
        "· 然后用【另一个 QQ 号】在群里发几条消息",
        "· 最多等 60 秒（默认汇总窗口），汇总结果就会出现",
        "",
        "想停止：在这个窗口按 Ctrl+C",
    ],
    "probe-wx": [
        "微信通道自检",
        "",
        "· 会连接微信，给【文件传输助手】发一条测试消息",
        "· 不会打扰任何微信群",
        "",
        "！！从现在起请不要动鼠标和键盘 ！！",
        "它是靠模拟点击操作微信窗口的，你一动就可能点错地方。",
    ],
    "live": [
        "正式运行：QQ 群 → 微信群",
        "",
        "运行前请确认：",
        "  1. NapCat 已启动，QQ 已登录",
        "  2. 微信已登录，且窗口【没有最小化】",
        "  3. config.yaml 里 driver 已经改成 wx4py",
        "",
        "运行期间最好不要用鼠标键盘操作微信窗口。",
        "",
        "想停止：按 Ctrl+C",
    ],
}


def print_banner(mode: str) -> None:
    lines = BANNERS.get(mode)
    if not lines:
        return
    bar = "=" * 62
    print(bar)
    for line in lines:
        print(f"  {line}" if line else "")
    print(bar)
    print(flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qq2wx",
        description="把 QQ 群消息整合后转发到微信群（NapCat + wx4py）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="配置文件路径（默认 config.yaml）")
    p.add_argument("--check", action="store_true", help="只校验配置并打印将要执行的转发规则")
    p.add_argument("--live", action="store_true",
                   help="正式运行（等同不带参数运行，供 .bat 启动器调用）")
    p.add_argument("--test", action="store_true", help="给配置中的微信群发一条测试消息后退出")
    p.add_argument("--probe-wx", action="store_true", help="自检微信通道（wx4py），不连 QQ")
    p.add_argument("--wx-health", action="store_true",
                   help="微信接口体检：查注册表/窗口可见性/UIA 控件树，只调 ShowWindow，安全")
    p.add_argument("--wx-gate", action="store_true",
                   help="热激活微信的 Qt 无障碍开关并报告结果（会写微信进程的 1 个字节，失败自动回滚）")
    p.add_argument("--doctor", action="store_true",
                   help="一站式诊断：配置/QQ侧/微信侧/限流 一次全查，并给出修复建议")
    p.add_argument("--probe-push", action="store_true",
                   help="推送通道自检：按 config.yaml 的 push 配置发一条测试消息到手机")
    p.add_argument("--selfcheck-wx", action="store_true",
                   help="只静态检查 wx4py 接口形状，不连接微信、不动键鼠")
    p.add_argument("--replay", metavar="FILE", help="从 JSONL 事件文件回放，不连 QQ")
    p.add_argument("--capture", metavar="FILE", nargs="?", const="logs/captured.jsonl",
                   help="只连 QQ 并把原始事件写入 JSONL，方便之后 --replay")
    p.add_argument("--speed", type=float, default=0.0, help="回放时每条之间的间隔秒数（0=尽快）")
    p.add_argument("--window", type=int, help="临时覆盖 aggregate.window_seconds（0=实时逐条）")
    p.add_argument("--driver", choices=["wx4py", "webhook", "dry-run", "push"],
                   help="临时覆盖 wechat.driver")
    p.add_argument("--version", action="version", version=f"qq2wx-relay {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    harden_console_encoding()
    args = build_parser().parse_args(argv)

    # ---- 微信自检不需要配置文件
    if args.selfcheck_wx:
        return selfcheck_wx4py()
    if args.wx_health:
        return run_wx_health()
    if args.wx_gate:
        return run_wx_gate()
    if args.probe_wx:
        print_banner("probe-wx")
        return probe_wx4py()

    try:
        cfg = AppConfig.load(args.config)
    except (FileNotFoundError, ValueError, ImportError) as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    # ---- 推送自检需要配置（要读 push.token），但不需要连 QQ/微信
    if args.probe_push:
        from .push import probe_push

        return probe_push(cfg.wechat.push)

    # ---- 命令行覆盖
    if args.window is not None:
        cfg.aggregate.window_seconds = max(0, args.window)
    if args.driver:
        cfg.wechat.driver = args.driver

    setup_logging(cfg.logging.level, cfg.logging.file or None)
    log = get_logger("qq2wx.main")

    try:
        cfg.validate()
    except ValueError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    if args.check:
        print_banner("check")
        print(_describe(cfg))
        return 0

    if args.doctor:
        from .doctor import run_doctor

        return run_doctor(cfg, str(args.config))

    # ---- 建发送通道
    try:
        sender = build_sender(cfg)
    except SendError as exc:
        print(f"[发送通道不可用] {exc}", file=sys.stderr)
        return 2

    pipeline = Pipeline(cfg, sender)

    try:
        if args.test:
            log.info("== 测试模式：向配置的微信群发送一条测试消息 ==")
            ok = pipeline.send_test()
            pipeline.sender.close()
            return 0 if ok else 1

        if args.replay:
            return _run_replay(cfg, pipeline, args)

        if args.capture:
            return _run_capture(cfg, pipeline, args)

        # 正式运行（带 --live 或什么都不带）
        # 但如果 driver 还是 dry-run，横幅要提示"这只是演练"，别误导人
        if cfg.wechat.driver == "dry-run" and args.live:
            print_banner("dry-run")
        else:
            print_banner("live")
        return _run_live(cfg, pipeline, log)
    finally:
        try:
            pipeline.sender.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 各模式

class _NullSender:
    """只用于 --check 的占位，不做任何事。"""

    def describe(self) -> str:
        return "（未创建）"

    def send(self, target: str, text: str):  # pragma: no cover
        raise RuntimeError("check 模式不会发送")

    def close(self) -> None:
        pass


def _describe(cfg: AppConfig, sender_desc: str | None = None) -> str:
    from .core import PROJECT_ROOT

    lines = [
        "─" * 60,
        "配置校验通过",
        "─" * 60,
        cfg.describe(),
        f"微信通道     : {sender_desc or cfg.wechat.driver}",
        f"限流         : 每天 ≤ {cfg.limits.max_sends_per_day} 条，间隔 ≥ {cfg.limits.min_send_interval}s",
        f"配置文件     : {cfg.source_path}",
        f"日志         : {cfg.logging.file or '（仅控制台）'}",
        f"留档         : {cfg.logging.archive_dir or '（关闭）'}",
        f"项目根目录   : {PROJECT_ROOT}",
        "─" * 60,
    ]
    if cfg.wechat.driver == "dry-run":
        lines.append("提示：当前是 dry-run，只会把内容写进日志，不会真的发到微信。")
        lines.append("     确认链路没问题后，把 config.yaml 的 wechat.driver 改成 wx4py。")
    return "\n".join(lines)


def _run_live(cfg: AppConfig, pipeline: Pipeline, log) -> int:
    from .senders import Wx4pySender

    # dry-run 是"演练"，提示语和正式运行不一样
    if cfg.wechat.driver == "dry-run" and not isinstance(pipeline.sender, Wx4pySender):
        print_banner("dry-run")

    guard = SingleInstance("qq2wx")
    if not guard.acquire():
        return 3

    # 起之前先把微信通道连上，免得 QQ 消息都进来了通道还没就绪
    if isinstance(pipeline.sender, Wx4pySender):
        log.info("预热微信通道（首次连接 UIA 可能要十几秒）…")
        ok, detail = pipeline.sender.warmup()
        if not ok:
            log.error("微信通道不可用：%s", detail)
            log.error("先解决微信连接问题，或者用 --driver dry-run 先跑通 QQ 那半边。")
            return 4
        log.info("微信通道就绪")

    conn = build_connection(cfg, pipeline.on_qq_message)
    stop_event = threading.Event()

    def handle_signal(signum, frame):  # noqa: ARG001
        log.info("收到退出信号，正在收尾…")
        stop_event.set()
        conn.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
            pass

    print(summarize(cfg, pipeline.sender), flush=True)
    pipeline.start()
    stats = StatsPrinter(pipeline)
    stats.start()

    # 后台线程里同步群名，失败也不影响转发
    def sync_group_names() -> None:
        conn.connected.wait(timeout=30)
        if not conn.connected.is_set():
            return
        names = conn.fetch_group_names()
        if names:
            log.info("已同步 %d 个群名", len(names))

    threading.Thread(target=sync_group_names, name="group-names", daemon=True).start()

    try:
        conn.run_until_stopped()
    except KeyboardInterrupt:
        log.info("手动中断")
    finally:
        stop_event.set()
        stats.stop()
        pipeline.stop()
        guard.release()

    return 0


def _run_replay(cfg: AppConfig, pipeline: Pipeline, args) -> int:
    log = get_logger("qq2wx.main")
    path = resolve_path(args.replay)
    if not path.exists():
        print(f"[错误] 回放文件不存在：{path}", file=sys.stderr)
        return 2

    log.info("== 回放模式：%s ==", path)
    pipeline.start()
    conn = ReplayConnection(path, pipeline.on_qq_message, cfg, speed=args.speed)
    try:
        conn.run_until_stopped()
    except KeyboardInterrupt:
        log.info("回放中断")
    finally:
        pipeline.aggregator.flush_all(reason="回放结束")
        time.sleep(0.3)
        pipeline.stop()
    return 0


def _run_capture(cfg: AppConfig, pipeline: Pipeline, args) -> int:
    log = get_logger("qq2wx.main")
    out = resolve_path(args.capture)
    out.parent.mkdir(parents=True, exist_ok=True)
    log.info("== 抓包模式：把 QQ 原始事件写入 %s ==", out)
    log.info("这个模式不会向微信发送任何东西。按 Ctrl+C 结束。")

    fh = out.open("a", encoding="utf-8")
    lock = threading.Lock()
    counter = {"n": 0}

    def write_event(payload: dict) -> None:
        with lock:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            fh.flush()
            counter["n"] += 1
            if counter["n"] % 20 == 0:
                log.info("已记录 %d 条事件", counter["n"])

    def on_message(msg) -> None:  # 抓包模式不转发，只在控制台提示
        log.debug("（抓包）[%s] %s: %s", msg.group_name, msg.sender_name, msg.text[:80])

    conn = build_connection(cfg, on_message)
    conn.raw_sink = write_event

    try:
        conn.run_until_stopped()
    except KeyboardInterrupt:
        log.info("抓包结束")
    finally:
        with lock:
            fh.close()
    log.info("共记录 %d 条事件 -> %s", counter["n"], out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
