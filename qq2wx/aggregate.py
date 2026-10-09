"""消息整合引擎：把 QQ 群里的零散消息按时间窗聚合成一条发出去。

设计要点
  - 每个 QQ 群一个独立窗口，互不影响
  - 窗口内第一条消息到达时开窗，窗口到期后统一渲染发送
  - 条数超过 max_batch_size 立即发送，不等窗口到期
  - message_id 去重，防止 NapCat 重连后重复推送
  - 渲染时按微信字数上限分片，避免超长消息发不出去
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .config import AppConfig
from .core import get_logger, now_hhmm, now_stamp, resolve_path, split_text
from .qq import QqMessage

log = get_logger("qq2wx.aggregate")

DEDUP_MEMORY = 5000  # 每个群记住多少条已处理的 message_id


@dataclass
class RenderedBatch:
    """一批已经渲染好、可以直接发给微信的内容。"""

    group_id: int
    group_name: str
    targets: list[str]
    chunks: list[str]
    messages: list[QqMessage] = field(default_factory=list)
    # 每个分片里各包含多少条消息。切成多片时用来记录每片的真实条数。
    chunk_counts: list[int] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.messages)

    def archive_record(self) -> dict[str, Any]:
        return {
            "time": now_stamp(),
            "group_id": self.group_id,
            "group_name": self.group_name,
            "targets": self.targets,
            "count": len(self.messages),
            "chunk_count": len(self.chunks),
            "chunk_counts": list(self.chunk_counts),
            "chunks": list(self.chunks),
            "text": "\n\n".join(self.chunks),
            "messages": [
                {
                    "sender": m.sender_name,
                    "sender_id": m.sender_id,
                    "time": m.clock,
                    "text": m.text,
                }
                for m in self.messages
            ],
        }


class _GroupWindow:
    """单个 QQ 群的待发送缓冲区。"""

    __slots__ = ("messages", "opened_at", "last_at", "seen")

    def __init__(self) -> None:
        self.messages: list[QqMessage] = []
        self.opened_at: float = 0.0
        self.last_at: float = 0.0
        self.seen: deque[str] = deque(maxlen=DEDUP_MEMORY)

    def is_new(self, msg: QqMessage) -> bool:
        if not msg.message_id:
            return True  # 没有 message_id 的就没法去重，放行
        if msg.message_id in self.seen:
            return False
        self.seen.append(msg.message_id)
        return True

    def add(self, msg: QqMessage, now: float) -> None:
        if not self.messages:
            self.opened_at = now
        self.messages.append(msg)
        self.last_at = now

    def take(self) -> list[QqMessage]:
        out = self.messages
        self.messages = []
        self.opened_at = 0.0
        self.last_at = 0.0
        return out


class Aggregator:
    """按时间窗聚合消息，到期后通过 on_batch 回调交给发送层。"""

    def __init__(self, cfg: AppConfig, on_batch) -> None:
        self.cfg = cfg
        self.on_batch = on_batch
        self._windows: dict[int, _GroupWindow] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._groups_seen: set[int] = set()
        self.stats = {
            "received": 0,
            "dropped_filter": 0,
            "dropped_self": 0,
            "dropped_duplicate": 0,
            "dropped_no_target": 0,
            "batches": 0,
        }

    # ------------------------------------------------------------ 判定

    def _accept(self, msg: QqMessage) -> bool:
        """过滤：这个群要不要看、这个人/这条内容要不要转。"""
        cfg = self.cfg

        if msg.group_id not in cfg.qq_groups:
            return False

        if not cfg.targets_for(msg.group_id):
            self.stats["dropped_no_target"] += 1
            return False

        if msg.sender_id in cfg.filters.ignore_senders:
            self.stats["dropped_self"] += 1
            return False

        if not cfg.filters.content_allowed(msg.text):
            self.stats["dropped_filter"] += 1
            return False

        return True

    # ------------------------------------------------------------ 投递

    def submit(self, msg: QqMessage) -> None:
        """QQ 侧每收到一条消息就调这里。线程安全。"""
        if not self._accept(msg):
            return

        now = time.monotonic()
        with self._lock:
            win = self._windows.get(msg.group_id)
            if win is None:
                win = _GroupWindow()
                self._windows[msg.group_id] = win

            if not win.is_new(msg):
                self.stats["dropped_duplicate"] += 1
                return

            win.add(msg, now)
            self.stats["received"] += 1
            self._groups_seen.add(msg.group_id)

            max_batch = self.cfg.aggregate.max_batch_size
            if max_batch > 0 and len(win.messages) >= max_batch:
                log.debug("群 %s 达到 %d 条，立即发送", msg.group_id, max_batch)
                self._emit_locked(msg.group_id)

        # 窗口为 0 表示实时逐条转发
        if self.cfg.aggregate.window_seconds <= 0:
            with self._lock:
                self._emit_locked(msg.group_id)

    # ------------------------------------------------------------ 定时器

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="aggregate-flush", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        interval = max(0.2, self.cfg.aggregate.flush_interval)
        while not self._stop.wait(interval):
            try:
                self.flush_due()
            except Exception as exc:  # noqa: BLE001
                log.exception("定时汇总出错：%s", exc)

    def flush_due(self) -> int:
        """把已经到期的窗口发出去，返回发出的批次数。"""
        window = self.cfg.aggregate.window_seconds
        if window <= 0:
            return 0

        now = time.monotonic()
        quiet = self.cfg.aggregate.quiet_seconds
        due: list[int] = []

        with self._lock:
            for gid, win in self._windows.items():
                if not win.messages:
                    continue
                if now - win.opened_at < window:
                    continue
                # 静默期：最后一条消息太新就再等等，让连续对话进同一条汇总
                if quiet > 0 and now - win.last_at < quiet:
                    continue
                due.append(gid)

            for gid in due:
                self._emit_locked(gid)

        return len(due)

    def flush_all(self, reason: str = "手动") -> int:
        """不管窗口到期没有，立刻把攒着的都发出去。"""
        with self._lock:
            gids = [gid for gid, win in self._windows.items() if win.messages]
            for gid in gids:
                self._emit_locked(gid)
        if gids:
            log.info("%s触发：发出 %d 个群的汇总", reason, len(gids))
        return len(gids)

    def stop(self, final_flush: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if final_flush:
            self.flush_all(reason="退出前")

    # ------------------------------------------------------------ 渲染

    def _emit_locked(self, group_id: int) -> None:
        """必须在持锁状态下调用。"""
        win = self._windows.get(group_id)
        if win is None or not win.messages:
            return

        messages = win.take()
        batch = self.render(group_id, messages)
        self.stats["batches"] += 1

        try:
            self.on_batch(batch)
        except Exception as exc:  # noqa: BLE001
            log.exception("发送批次失败（群 %s）：%s", group_id, exc)

    def render(self, group_id: int, messages: list[QqMessage]) -> RenderedBatch:
        agg = self.cfg.aggregate
        group_name = messages[0].group_name if messages else str(group_id)

        def one_line(m: QqMessage) -> str:
            content = m.text
            cap = agg.max_line_chars
            if cap > 0 and len(content) > cap:
                # 超长单条截断，避免一条长消息把整批挤爆
                content = content[:cap].rstrip() + "…（已截断）"
            return agg.line_template.format(
                sender=m.sender_name, content=content, time=m.clock
            ).strip()

        def build(chunk_messages: list[QqMessage]) -> str:
            body = "\n".join(
                line for line in (one_line(m) for m in chunk_messages) if line
            )
            return agg.template.format(
                group_name=group_name,
                group_id=group_id,
                count=len(chunk_messages),
                time=now_hhmm(),
                messages=body,
            ).strip()

        # 按条数上限切块。这样"单批最多 N 条"是硬保证，不受字数影响。
        per_batch = agg.max_lines_per_batch
        if per_batch > 0 and len(messages) > per_batch:
            blocks = [
                messages[i:i + per_batch] for i in range(0, len(messages), per_batch)
            ]
        else:
            blocks = [messages]

        # 每块内部再按字符数上限分片（防止超长被微信截断）
        chunks: list[str] = []
        chunk_counts: list[int] = []
        for block in blocks:
            text = build(block)
            pieces = split_text(text, self.cfg.wechat.wx4py_max_chars)
            chunks.extend(pieces)
            # 一个块被字数再切时，条数只能记在它的第一片上（无法精确分摊）
            chunk_counts.extend([len(block)] + [0] * (len(pieces) - 1))

        if len(chunks) > 1 and agg.chunk_suffix:
            total = len(chunks)
            chunks = [
                f"{chunk}\n{agg.chunk_suffix.format(index=i + 1, total=total)}"
                for i, chunk in enumerate(chunks)
            ]

        return RenderedBatch(
            group_id=group_id,
            group_name=group_name,
            targets=self.cfg.targets_for(group_id),
            chunks=chunks,
            messages=messages,
            chunk_counts=chunk_counts,
        )


# ---------------------------------------------------------------- 留档

class Archiver:
    """把每批转发内容按天写进 JSONL，方便回溯"到底发了什么"。"""

    def __init__(self, archive_dir: str | None) -> None:
        self.dir = resolve_path(archive_dir) if archive_dir else None
        self._lock = threading.Lock()
        if self.dir:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("留档目录创建失败，已关闭留档：%s", exc)
                self.dir = None

    def cleanup_old(self, keep_days: float) -> int:
        """删除超过保留期的留档文件。返回删除数量。

        为什么要这个：留档每天一个 JSONL，长期挂着跑会一直堆积。
        单文件很小（每批一行），但日积月累也没必要一直留着。
        """
        if not self.dir or keep_days <= 0:
            return 0
        deadline = time.time() - keep_days * 86400
        removed = 0
        try:
            for p in self.dir.glob("*.jsonl"):
                try:
                    if p.stat().st_mtime < deadline:
                        p.unlink()
                        removed += 1
                except OSError:
                    pass
        except OSError:
            return 0
        if removed:
            log.info("清理了 %d 个过期留档（保留 %d 天）", removed, keep_days)
        return removed

    def write(self, batch: RenderedBatch) -> None:
        if self.dir is None:
            return
        import json

        path = self.dir / f"{time.strftime('%Y-%m-%d')}.jsonl"
        line = json.dumps(batch.archive_record(), ensure_ascii=False)
        try:
            with self._lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            log.warning("留档写入失败：%s", exc)
