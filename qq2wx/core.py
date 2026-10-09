"""通用工具：路径、日志、限流、持久化、OneBot 消息段渲染。"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------- 路径

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_path(p: str | os.PathLike[str]) -> Path:
    """相对路径按项目根目录解析，绝对路径原样返回。"""
    path = Path(p)
    return path if path.is_absolute() else (PROJECT_ROOT / path)


# ---------------------------------------------------------------- 日志

_LOG_CONFIGURED = False


def setup_logging(level: str = "INFO", log_file: str | None = None) -> logging.Logger:
    global _LOG_CONFIGURED
    logger = logging.getLogger("qq2wx")
    if _LOG_CONFIGURED:
        return logger

    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if log_file:
        path = resolve_path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 用 RotatingFileHandler 而不是 FileHandler：
        # 这套东西是要长期挂着跑的（服务器全天），普通 FileHandler 会让
        # 日志无限增长。5MB x 4 个文件封顶 ≈ 20MB。
        from logging.handlers import RotatingFileHandler

        fh = RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    # Windows 控制台是 GBK，写中文日志会抛 UnicodeEncodeError，这里统一降级处理
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler) and hasattr(handler.stream, "reconfigure"):
            try:
                handler.stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    _LOG_CONFIGURED = True
    return logger


def get_logger(name: str = "qq2wx") -> logging.Logger:
    return logging.getLogger(name)


# ---------------------------------------------------------------- 状态持久化

class JsonState:
    """一个简单的、线程安全的、原子写入的 JSON 状态文件。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = resolve_path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # 状态文件坏了不该让程序起不来
            self._data = {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._save_locked()

    def _save_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception as exc:  # noqa: BLE001
            get_logger().warning("状态文件写入失败 %s: %s", self.path, exc)


# ---------------------------------------------------------------- 限流

class DailyCounter:
    """按天计数的限流器，跨重启保留。"""

    def __init__(self, state: JsonState, key: str, limit: int) -> None:
        self.state = state
        self.key = key
        self.limit = int(limit or 0)
        self._lock = threading.Lock()

    def _today(self) -> str:
        return date.today().isoformat()

    def count(self) -> int:
        raw = self.state.get(self.key) or {}
        if raw.get("date") != self._today():
            return 0
        return int(raw.get("count", 0))

    def allow(self) -> bool:
        if self.limit <= 0:
            return True
        return self.count() < self.limit

    def bump(self, n: int = 1) -> int:
        with self._lock:
            cur = self.count()
            cur += n
            self.state.set(self.key, {"date": self._today(), "count": cur})
            return cur


class MinInterval:
    """两次操作之间的最小间隔。"""

    def __init__(self, seconds: float) -> None:
        self.seconds = float(seconds or 0)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> float:
        """必要时睡眠，返回实际睡了多久。"""
        if self.seconds <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            sleep_for = max(0.0, self.seconds - delta)
            if sleep_for:
                time.sleep(sleep_for)
            self._last = time.monotonic()
            return sleep_for


# ---------------------------------------------------------------- OneBot 消息渲染

# 消息段类型 -> 中文标签
_SEGMENT_LABELS = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "face": "[表情]",
    "mface": "[表情]",
    "bface": "[表情]",
    "sface": "[表情]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
    "music": "[音乐]",
    "forward": "[合并转发]",
    "node": "[合并转发]",
    "poke": "[戳一戳]",
    "gift": "[礼物]",
    "contact": "[推荐联系人]",
    "location": "[位置]",
    "reply": "",  # 引用回复本身不出文本，单独处理
}


def _unescape_cq(text: str) -> str:
    return (
        text.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",")
        .replace("&amp;", "&")
    )


_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")


def _cq_params(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.strip(",").split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = _unescape_cq(v)
    return out


def render_message(message: Any, options: dict[str, Any] | None = None) -> str:
    """把 OneBot 11 的 message 字段渲染成可读纯文本。

    支持两种格式：
      - 数组格式 [{"type": "text", "data": {"text": "hi"}}, ...]
      - CQ 码字符串 "hi[CQ:image,file=x.jpg]"
    """
    opts = options or {}
    parts: list[str] = []

    def want(kind: str) -> bool:
        key = {
            "image": "forward_images",
            "file": "forward_files",
            "video": "forward_videos",
            "face": "forward_face",
            "at": "forward_at",
            "reply": "forward_reply",
        }.get(kind)
        if key is None:
            return bool(opts.get("forward_other", True))
        return bool(opts.get(key, True))

    def push_segment(seg_type: str, data: dict[str, Any]) -> None:
        seg_type = (seg_type or "").lower()

        if seg_type == "text":
            txt = _unescape_cq(str(data.get("text", "")))
            if txt:
                parts.append(txt)
            return

        if seg_type == "at":
            if want("at"):
                qq = str(data.get("qq", ""))
                if qq == "all":
                    parts.append("@全体成员")
                else:
                    name = data.get("name") or data.get("card")
                    parts.append(f"@{name}" if name else f"@{qq}")
            return

        if seg_type == "reply":
            return  # 引用内容不重复输出

        if not want(seg_type):
            return

        label = _SEGMENT_LABELS.get(seg_type)
        if label is None:
            label = f"[{seg_type}]" if seg_type else ""
        if seg_type == "file" and data.get("name"):
            label = f"[文件: {data['name']}]"
        if label:
            parts.append(label)

    if isinstance(message, list):
        for seg in message:
            if not isinstance(seg, dict):
                continue
            push_segment(str(seg.get("type", "")), seg.get("data") or {})
    elif isinstance(message, str):
        pos = 0
        for m in _CQ_RE.finditer(message):
            if m.start() > pos:
                chunk = _unescape_cq(message[pos:m.start()])
                if chunk:
                    parts.append(chunk)
            push_segment(m.group(1), _cq_params(m.group(2)))
            pos = m.end()
        tail = _unescape_cq(message[pos:])
        if tail:
            parts.append(tail)
    elif message is None:
        return ""

    return "".join(parts).strip()


def extract_reply_id(message: Any) -> str | None:
    """从消息里取出引用回复的 message_id（有的话）。"""
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and str(seg.get("type")) == "reply":
                return str((seg.get("data") or {}).get("id") or "") or None
    elif isinstance(message, str):
        for match in _CQ_RE.finditer(message):
            if match.group(1) == "reply":
                return _cq_params(match.group(2)).get("id")
    return None


def is_at_me(message: Any, self_id: int | str | None) -> bool:
    """判断消息里是否 @ 了机器人自己。"""
    if not self_id:
        return False
    target = str(self_id)
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and str(seg.get("type")) == "at":
                if str((seg.get("data") or {}).get("qq")) == target:
                    return True
    elif isinstance(message, str):
        for match in _CQ_RE.finditer(message):
            if match.group(1) == "at" and _cq_params(match.group(2)).get("qq") == target:
                return True
    return False


# ---------------------------------------------------------------- 杂项

def now_hhmm() -> str:
    return datetime.now().strftime("%H:%M")


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def split_text(text: str, limit: int) -> list[str]:
    """按长度分片，尽量在换行处断开。"""
    if limit <= 0 or len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:  # 找不到合适的换行，硬切
            cut = limit
        chunks.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """从 dict 里按多个候选键取值（兼容 camelCase / snake_case）。"""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def iter_first(seq: Iterable[Any], default: Any = None) -> Any:
    for item in seq:
        return item
    return default
