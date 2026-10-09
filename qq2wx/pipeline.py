"""主流程：把 QQ 侧、整合引擎、发送层、限流、留档串起来。"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from .aggregate import Aggregator, Archiver, RenderedBatch
from .config import AppConfig
from .core import DailyCounter, JsonState, MinInterval, get_logger
from .media import MediaDownloader, MediaItem
from .qq import QqMessage
from .senders import Sender

log = get_logger("qq2wx.pipeline")

# 历史消息判定容差（秒）。消息时间戳来自腾讯服务器，与本机时钟可能有偏差；
# 不留容差的话，程序刚启动时进来的第一条新消息会被误当成历史丢掉。
_HISTORY_TOLERANCE = 3.0


class Pipeline:
    """消息从 QQ 进来、到微信出去的完整管道。"""

    def __init__(self, cfg: AppConfig, sender: Sender) -> None:
        self.cfg = cfg
        self.sender = sender
        self.state = JsonState("logs/state.json")
        self.counter = DailyCounter(
            self.state, "sends_per_day", cfg.limits.max_sends_per_day
        )
        self.interval = MinInterval(cfg.limits.min_send_interval)
        self.archiver = Archiver(cfg.logging.archive_dir)
        self.aggregator = Aggregator(cfg, self.on_batch)
        self.downloader = MediaDownloader(cfg)
        # 只转发启动之后的消息（用户要求：不要倒历史）
        self._started_at = time.time()
        self.stats = {
            "sent_ok": 0,
            "sent_fail": 0,
            "skipped_limit": 0,
            "media_ok": 0,
            "media_fail": 0,
            "dropped_history": 0,
        }

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        log.info("发送通道：%s", self.sender.describe())
        if self.cfg.media.enabled:
            log.info(
                "媒体转发：开启（单文件上限 %.0fMB，每批最多 %d 个）",
                self.cfg.media.max_mb, self.cfg.media.max_per_batch,
            )
            self.downloader.cleanup_old()
        else:
            log.info("媒体转发：关闭（图片/文件只发文字占位符）")
        self.aggregator.start()

    def stop(self) -> None:
        self.aggregator.stop(final_flush=True)
        self.sender.close()
        log.info(
            "收工。QQ 收到 %d 条，发出 %d 批；成功 %d / 失败 %d / 被限流跳过 %d"
            "；媒体成功 %d / 失败 %d；丢弃历史消息 %d 条",
            self.aggregator.stats["received"],
            self.aggregator.stats["batches"],
            self.stats["sent_ok"],
            self.stats["sent_fail"],
            self.stats["skipped_limit"],
            self.stats["media_ok"],
            self.stats["media_fail"],
            self.stats["dropped_history"],
        )

    # ------------------------------------------------------------ 入口

    def on_qq_message(self, msg: QqMessage) -> None:
        """QQ 侧每来一条消息都会调这里。必须轻量，不能阻塞收消息。

        **只转发启动之后的新消息**（用户明确要求，不要一股脑倒历史）。

        为什么需要这道保险：
            正常运行时程序只收 NapCat 的实时事件流，本来就不会碰历史消息。
            但两种情况下会把旧消息灌进来：
              1. NapCat 重连后可能补推一小段离线消息
              2. 手动用 --replay 回放历史（测试时干过，那时是 dry-run）
            一旦旧消息混进来，就是几十条一起涌进微信群刷屏。
            这里用"启动时刻"做闸门：时间戳早于启动时间的直接丢弃，
            并且把丢弃数量记进统计、打一次日志，方便事后确认。
        """
        if msg.time < self._started_at - _HISTORY_TOLERANCE:
            self.stats["dropped_history"] += 1
            if self.stats["dropped_history"] == 1:
                age = int(self._started_at - msg.time)
                log.info(
                    "丢弃启动前的历史消息（最早一条早 %d 秒）：%s | %s",
                    age, msg.group_name, msg.text[:80].replace("\n", " "),
                )
            return

        log.debug(
            "[%s] %s: %s", msg.group_name, msg.sender_name, msg.text[:120].replace("\n", " ")
        )
        self.aggregator.submit(msg)

    # ------------------------------------------------------------ 出口

    def on_batch(self, batch: RenderedBatch) -> None:
        """一批汇总渲染完成，准备发到微信群。"""
        self.archiver.write(batch)

        targets = batch.targets
        if not targets:
            log.warning("群 %s 没有配置微信目标，跳过", batch.group_name)
            return

        preview = batch.chunks[0].replace("\n", " / ")[:120]
        log.info(
            "准备发送：QQ群「%s」%d 条消息 -> 微信 %s（%d 片）｜%s",
            batch.group_name, batch.count, "、".join(targets), len(batch.chunks), preview,
        )

        for target in targets:
            for index, chunk in enumerate(batch.chunks, start=1):
                if not self._allowed(batch, target, index):
                    continue
                # 第一批用常规最小间隔；后续分片按配置的 chunk_delay 拉开
                if index > 1 and self.cfg.aggregate.chunk_delay > 0:
                    log.debug("分片 %d/%d 之间等待 %.0f 秒",
                              index, len(batch.chunks), self.cfg.aggregate.chunk_delay)
                    time.sleep(self.cfg.aggregate.chunk_delay)
                self.interval.wait()
                result = self.sender.send(target, chunk)
                if result.ok:
                    self.stats["sent_ok"] += 1
                    self.counter.bump(1)
                    log.info("已发送 -> 微信「%s」%d/%d", target, index, len(batch.chunks))
                else:
                    self.stats["sent_fail"] += 1
                    log.error("发送失败 -> 微信「%s」：%s", target, result.detail)

            # 文字发完后，把这一批里的图片/文件也发过去
            self._send_media(batch, target)

    def _send_media(self, batch: RenderedBatch, target: str) -> None:
        """下载并转发这一批里的媒体。文字里已经留了占位符。"""
        if not self.cfg.media.enabled:
            return

        # 汇总所有消息的媒体（一条消息可能带多个附件）
        items: list[MediaItem] = []
        for msg in batch.messages:
            for item in getattr(msg, "media", []) or []:
                items.append(item)
        if not items:
            return

        log.info("本批含 %d 个媒体，开始下载（最多 %d 个）",
                 len(items), self.cfg.media.max_per_batch)
        ready = self.downloader.download_all(items)

        if not ready:
            log.warning("本批 %d 个媒体全部未能下载，只发了文字占位符", len(items))
            self.stats["media_fail"] += len(items)
            return

        for item in ready:
            if not self._allowed(batch, target, 0):
                return
            self.interval.wait()
            caption = self._media_caption(item)
            result = self.sender.send_file(target, item.local_path, caption)
            if result.ok:
                self.stats["media_ok"] += 1
                self.counter.bump(1)
                log.info("已发送媒体 -> 微信「%s」：%s", target, os.path.basename(item.local_path))
            else:
                self.stats["media_fail"] += 1
                log.error("媒体发送失败 -> 微信「%s」：%s", target, result.detail)

        # 没下载成功的那些，统计一下
        failed = len(items) - len(ready)
        if failed > 0:
            self.stats["media_fail"] += failed

    def _media_caption(self, item: MediaItem) -> str:
        """发文件时附带的说明文字（微信里显示为文件名下方的小字）。"""
        if item.kind == "file" and item.filename:
            return item.filename
        return ""

    def _allowed(self, batch: RenderedBatch, target: str, index: int) -> bool:
        if not self.counter.allow():
            self.stats["skipped_limit"] += 1
            log.warning(
                "今日发送已达上限 %d 条，跳过「%s」的剩余汇总（改 limits.max_sends_per_day 可放宽）",
                self.cfg.limits.max_sends_per_day, target,
            )
            return False
        return True

    # ------------------------------------------------------------ 测试

    def send_test(self) -> bool:
        """立刻构造一条测试消息走一遍真实发送链路。"""
        targets = sorted({t for ts in self.cfg.qq_groups.values() for t in ts})
        if not targets:
            log.error("配置里没有任何微信群目标，先把 config.yaml 的 qq_groups 填好")
            return False

        text = (
            "【qq2wx-relay 链路测试】\n"
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            "如果你在微信群里看到这条消息，说明发送通道是通的。"
        )
        ok_all = True
        for target in targets:
            if not self.counter.allow():
                log.warning("今日发送已达上限，跳过测试消息")
                return False
            result = self.sender.send(target, text)
            if result.ok:
                self.counter.bump(1)
                log.info("测试消息已发送 -> 微信「%s」", target)
            else:
                ok_all = False
                log.error("测试消息发送失败 -> 微信「%s」：%s", target, result.detail)
        return ok_all

    def feed(self, messages: list[QqMessage]) -> None:
        """离线灌入消息（给 --replay / 单元测试用）。"""
        for msg in messages:
            self.on_qq_message(msg)


# ---------------------------------------------------------------- 单实例

class SingleInstance:
    """防止同一份配置被跑两遍（两遍会对同一个微信群重复发送）。"""

    def __init__(self, tag: str = "qq2wx") -> None:
        from .core import PROJECT_ROOT

        self.path = PROJECT_ROOT / "logs" / f"{tag}.lock"
        self.acquired = False

    def acquire(self) -> bool:
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                old = int(self.path.read_text(encoding="utf-8").strip() or 0)
            except (ValueError, OSError):
                old = 0
            if old and _pid_alive(old):
                log.error(
                    "已经有一个实例在运行（PID %d）。同一个配置跑两遍会重复发送，已退出。",
                    old,
                )
                return False
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        self.acquired = True
        return True

    def release(self) -> None:
        if self.acquired:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            self.acquired = False


def _pid_alive(pid: int) -> bool:
    try:
        import os

        if os.name == "nt":
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 统计输出

class StatsPrinter:
    """定期往日志里打一行心跳，方便挂在后台时观察是否还活着。

    同时监控微信版本有没有变。版本一变，UIA 控件树就会失效，
    整条转发链路会静默停摆 —— 所以要在日志里第一时间看出来。
    """

    def __init__(self, pipeline: Pipeline, every: float = 300.0) -> None:
        self.pipeline = pipeline
        self.every = every
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._baseline_version: str | None = None
        self._warned = False

    def start(self) -> None:
        self._baseline_version = self._current_version()
        if self._baseline_version:
            log.info("微信客户端版本：%s（会持续监控，变了会告警）", self._baseline_version)
        self._thread = threading.Thread(target=self._run, name="stats", daemon=True)
        self._thread.start()

    @staticmethod
    def _current_version() -> str | None:
        """读正在运行的微信加载的 Weixin.dll 版本。"""
        try:
            from .wxgate import file_version_of, find_module, weixin_pids

            for pid in weixin_pids():
                m = find_module(pid, "weixin.dll")
                if m:
                    return file_version_of(m[2]) or None
        except Exception:  # noqa: BLE001
            pass
        return None

    def _run(self) -> None:
        # 心跳间隔 5 分钟；清理类工作不用那么勤，每 12 次（约 1 小时）做一回
        ticks = 0
        while not self._stop.wait(self.every):
            ticks += 1
            s = self.pipeline.aggregator.stats
            log.info(
                "心跳：收 %d 条 / 过滤 %d / 重复 %d / 待发 %d / 已发批次 %d",
                s["received"], s["dropped_filter"], s["dropped_duplicate"],
                self.pipeline.stats["sent_ok"], s["batches"],
            )

            if ticks % 12 == 0:
                self._housekeeping()

            # 版本漂移检测
            if self.pipeline.cfg.wechat.driver != "wx4py":
                continue
            now = self._current_version()
            if not now:
                continue
            if self._baseline_version is None:
                self._baseline_version = now
                continue
            if now != self._baseline_version and not self._warned:
                self._warned = True
                log.error(
                    "!! 微信版本变了：%s -> %s !! 自动更新可能绕过了封锁。"
                    "新版本不暴露 UIA 控件树，转发会失效。"
                    "请运行：python tools\\fix_wechat_version.py --status，"
                    "并按 当前状态.md 里的步骤重新降级。",
                    self._baseline_version, now,
                )

    def _housekeeping(self) -> None:
        """定期清理：媒体缓存 + 留档。

        这套东西是设计来长期挂着的，不清理会一直堆积。
        """
        cfg = self.pipeline.cfg
        try:
            self.pipeline.downloader.cleanup_old()
        except Exception as exc:  # noqa: BLE001
            log.debug("媒体清理失败：%s", exc)
        try:
            self.pipeline.archiver.cleanup_old(cfg.logging.archive_keep_days)
        except Exception as exc:  # noqa: BLE001
            log.debug("留档清理失败：%s", exc)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)


def summarize(cfg: AppConfig, sender: Sender) -> str:
    return "\n".join(
        [
            "─" * 60,
            "qq2wx-relay 启动",
            "─" * 60,
            cfg.describe(),
            f"微信通道     : {sender.describe()}",
            f"限流         : 每天最多 {cfg.limits.max_sends_per_day} 条，"
            f"间隔 ≥ {cfg.limits.min_send_interval}s",
            "─" * 60,
        ]
    )
