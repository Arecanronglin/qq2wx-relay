"""配置加载与校验。支持 YAML（装了 PyYAML）和 JSON（内置，永远可用）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core import get_logger, resolve_path

SAMPLE = "config.example.yaml"


def _as_dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_bool(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass
class QqConfig:
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: str = ""
    http_url: str = "http://127.0.0.1:3000"
    self_id: int = 0


@dataclass
class WechatConfig:
    driver: str = "dry-run"
    webhook: dict = field(default_factory=dict)
    wx4py: dict = field(default_factory=dict)
    push: dict = field(default_factory=dict)

    @property
    def wx4py_target_type(self) -> str:
        return str(self.wx4py.get("target_type") or "group")

    @property
    def wx4py_retries(self) -> int:
        return _as_int(self.wx4py.get("retries"), 2)

    @property
    def wx4py_retry_delay(self) -> float:
        return _as_float(self.wx4py.get("retry_delay"), 3.0)

    @property
    def wx4py_per_message_delay(self) -> float:
        return _as_float(self.wx4py.get("per_message_delay"), 1.0)

    @property
    def wx4py_max_chars(self) -> int:
        return _as_int(self.wx4py.get("max_chars"), 1800)


@dataclass
class AggregateConfig:
    window_seconds: int = 60
    flush_interval: float = 5.0
    max_batch_size: int = 40
    quiet_seconds: float = 0.0
    template: str = "【QQ群 · {group_name}】{count} 条新消息\n{messages}"
    line_template: str = "{sender}: {content}"
    chunk_suffix: str = "（{index}/{total}）"
    # 单条汇总里最多放几条消息。超过的部分拆成后续消息发送。0 = 不限制
    max_lines_per_batch: int = 0
    # 拆成多条发送时，两条之间的间隔秒数
    chunk_delay: float = 5.0
    # 单条消息内容超过这个字数就截断（0 = 不截断）
    max_line_chars: int = 0


@dataclass
class MediaConfig:
    """媒体（图片/文件）转发设置。"""

    enabled: bool = True
    dir: str = "media"
    max_mb: float = 20.0
    max_per_batch: int = 5
    keep_days: float = 7.0
    download_timeout: float = 30.0


@dataclass
class FilterConfig:
    ignore_senders: set[str] = field(default_factory=set)
    ignore_keywords: list[str] = field(default_factory=list)
    include_keywords: list[str] = field(default_factory=list)
    forward_images: bool = True
    forward_files: bool = True
    forward_videos: bool = False
    forward_face: bool = False
    forward_at: bool = True
    forward_reply: bool = True
    forward_other: bool = True
    ignore_self: bool = True

    def segment_options(self) -> dict[str, Any]:
        return {
            "forward_images": self.forward_images,
            "forward_files": self.forward_files,
            "forward_videos": self.forward_videos,
            "forward_face": self.forward_face,
            "forward_at": self.forward_at,
            "forward_reply": self.forward_reply,
            "forward_other": self.forward_other,
        }

    def content_allowed(self, text: str) -> bool:
        """按关键词决定这条消息要不要转发。"""
        if self.ignore_keywords and any(k and k in text for k in self.ignore_keywords):
            return False
        if self.include_keywords:
            return any(k and k in text for k in self.include_keywords)
        return True


@dataclass
class LimitsConfig:
    max_sends_per_day: int = 200
    min_send_interval: float = 3.0


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/qq2wx.log"
    archive_dir: str = "logs/archive"
    # 留档保留天数，超了自动删。0 = 永不删除
    archive_keep_days: float = 30.0


@dataclass
class AppConfig:
    qq: QqConfig = field(default_factory=QqConfig)
    qq_groups: dict[int, list[str]] = field(default_factory=dict)
    wechat: WechatConfig = field(default_factory=WechatConfig)
    aggregate: AggregateConfig = field(default_factory=AggregateConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    source_path: Path | None = None

    # ------------------------------------------------------------ 派生信息

    @property
    def watched_group_ids(self) -> set[str]:
        return {str(g) for g in self.qq_groups}

    def group_id(self, raw: Any) -> int:
        return _as_int(raw, 0)

    def targets_for(self, group_id: Any) -> list[str]:
        return list(self.qq_groups.get(self.group_id(group_id), []))

    def describe(self) -> str:
        lines = [
            f"QQ 接入      : {self.qq.ws_url}",
            f"微信通道     : {self.wechat.driver}",
            f"汇总窗口     : {self.aggregate.window_seconds}s"
            + ("（实时逐条）" if self.aggregate.window_seconds <= 0 else ""),
            f"监听群       : {len(self.qq_groups)} 个",
        ]
        for gid, targets in self.qq_groups.items():
            lines.append(f"  {gid} -> {', '.join(targets) if targets else '（未配置目标，将跳过）'}")
        return "\n".join(lines)

    def validate(self) -> list[str]:
        """返回警告列表。致命错误直接抛 ValueError。"""
        problems: list[str] = []
        warns: list[str] = []

        if not self.qq.ws_url.startswith(("ws://", "wss://")):
            problems.append(f"qq.ws_url 必须是 ws:// 或 wss:// 开头，当前是 {self.qq.ws_url!r}")
        if self.wechat.driver not in {"wx4py", "webhook", "dry-run", "push"}:
            problems.append(
                f"wechat.driver 只能是 wx4py / webhook / dry-run，当前是 {self.wechat.driver!r}"
            )
        if not self.qq_groups:
            problems.append("qq_groups 是空的，没有任何 QQ 群会被转发")

        for gid, targets in self.qq_groups.items():
            if gid == 0:
                problems.append("qq_groups 里有无法解析成群号的 key")
            if not targets:
                warns.append(f"QQ 群 {gid} 没有配微信群目标，收到消息会跳过")

        if self.wechat.driver == "webhook":
            if not _as_dict(self.wechat.webhook).get("url"):
                problems.append("wechat.driver=webhook 但没填 wechat.webhook.url")

        if self.wechat.driver == "push":
            push = _as_dict(self.wechat.push)
            kind = str(push.get("kind") or "pushplus").lower()
            if kind in ("pushplus", "serverchan") and not push.get("token"):
                problems.append(
                    f"wechat.driver=push 但没填 wechat.push.token（{kind} 必须）"
                )
            if kind == "webhook" and not push.get("url"):
                problems.append("wechat.driver=push 且 kind=webhook，但没填 wechat.push.url")

        if self.aggregate.window_seconds < 0:
            problems.append("aggregate.window_seconds 不能是负数")
        if self.aggregate.flush_interval <= 0:
            problems.append("aggregate.flush_interval 必须大于 0")
        if self.aggregate.flush_interval > max(self.aggregate.window_seconds, 1):
            warns.append(
                "aggregate.flush_interval 比 window_seconds 还大，汇总会明显延迟"
            )

        # 模板变量合法性：只允许白名单变量，写错会 KeyError
        allowed = {"group_name", "group_id", "count", "time", "messages", "sender", "content", "index", "total"}
        for key in ("template", "line_template", "chunk_suffix"):
            value = getattr(self.aggregate, key)
            for var in _template_vars(value):
                if var not in allowed:
                    problems.append(
                        f"aggregate.{key} 里用了不支持的变量 {{{var}}}，"
                        f"可用变量：{', '.join(sorted(allowed))}"
                    )

        for w in warns:
            get_logger().warning(w)
        if problems:
            raise ValueError("配置有问题：\n  - " + "\n  - ".join(problems))
        return warns

    # ------------------------------------------------------------ 加载

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        p = resolve_path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"找不到配置文件 {p}\n请先复制一份示例：copy {SAMPLE} config.yaml"
            )
        raw = _read_config_file(p)
        if not isinstance(raw, dict):
            raise ValueError(f"{p} 的顶层结构必须是字典（key: value）")

        cfg = cls(source_path=p)
        cfg.qq = _build_qq(_as_dict(raw.get("qq")))
        cfg.qq_groups = _build_groups(raw.get("qq_groups"))
        cfg.wechat = _build_wechat(_as_dict(raw.get("wechat")))
        cfg.aggregate = _build_aggregate(_as_dict(raw.get("aggregate")))
        cfg.filters = _build_filters(_as_dict(raw.get("filters")))
        cfg.media = _build_media(_as_dict(raw.get("media")))
        cfg.limits = _build_limits(_as_dict(raw.get("limits")))
        cfg.logging = _build_logging(_as_dict(raw.get("logging")))
        return cfg


# ---------------------------------------------------------------- 内部构造

def _template_vars(text: str) -> list[str]:
    import re

    return re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", text or "")


def _read_config_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()

    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "读取 YAML 配置需要 PyYAML，请先 pip install PyYAML，"
                "或者改用 JSON 格式的配置文件。"
            ) from exc
        return yaml.safe_load(text) or {}

    if suffix == ".json":
        return json.loads(text) or {}

    # 后缀不认识：先当 YAML 试，失败再当 JSON
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001
        return json.loads(text) or {}


def _build_qq(d: dict) -> QqConfig:
    return QqConfig(
        ws_url=str(d.get("ws_url") or "ws://127.0.0.1:3001").strip(),
        access_token=str(d.get("access_token") or "").strip(),
        http_url=str(d.get("http_url") or "http://127.0.0.1:3000").strip(),
        self_id=_as_int(d.get("self_id"), 0),
    )


def _build_groups(raw: Any) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        # 也支持 [{"group_id": 123, "targets": ["群A"]}, ...]
        items = []
        for entry in raw:
            e = _as_dict(entry)
            gid = e.get("group_id") or e.get("id")
            targets = e.get("targets") or e.get("groups") or e.get("wechat")
            items.append((gid, targets))
    else:
        return out

    for key, value in items:
        if key is None:
            continue
        gid = _as_int(key, 0)
        if gid == 0:
            get_logger().warning("忽略无法解析的群号配置项：%r", key)
            continue
        if isinstance(value, dict):
            targets = _as_list(value.get("targets") or value.get("groups") or value.get("wechat"))
        else:
            targets = _as_list(value)
        out[gid] = [str(t).strip() for t in targets if str(t).strip()]
    return out


def _build_wechat(d: dict) -> WechatConfig:
    return WechatConfig(
        driver=str(d.get("driver") or "dry-run").strip().lower(),
        webhook=_as_dict(d.get("webhook")),
        wx4py=_as_dict(d.get("wx4py")),
        push=_as_dict(d.get("push")),
    )


def _build_aggregate(d: dict) -> AggregateConfig:
    default = AggregateConfig()
    return AggregateConfig(
        window_seconds=_as_int(d.get("window_seconds"), default.window_seconds),
        flush_interval=_as_float(d.get("flush_interval"), default.flush_interval),
        max_batch_size=_as_int(d.get("max_batch_size"), default.max_batch_size),
        quiet_seconds=_as_float(d.get("quiet_seconds"), default.quiet_seconds),
        template=str(d.get("template") or default.template),
        line_template=str(d.get("line_template") or default.line_template),
        chunk_suffix=str(d.get("chunk_suffix") or default.chunk_suffix),
        max_lines_per_batch=_as_int(
            d.get("max_lines_per_batch"), default.max_lines_per_batch
        ),
        chunk_delay=_as_float(d.get("chunk_delay"), default.chunk_delay),
        max_line_chars=_as_int(d.get("max_line_chars"), default.max_line_chars),
    )


def _build_filters(d: dict) -> FilterConfig:
    return FilterConfig(
        ignore_senders={str(s).strip() for s in _as_list(d.get("ignore_senders")) if str(s).strip()},
        ignore_keywords=[str(k) for k in _as_list(d.get("ignore_keywords")) if str(k)],
        include_keywords=[str(k) for k in _as_list(d.get("include_keywords")) if str(k)],
        forward_images=_as_bool(d.get("forward_images"), True),
        forward_files=_as_bool(d.get("forward_files"), True),
        forward_videos=_as_bool(d.get("forward_videos"), False),
        forward_face=_as_bool(d.get("forward_face"), False),
        forward_at=_as_bool(d.get("forward_at"), True),
        forward_reply=_as_bool(d.get("forward_reply"), True),
        forward_other=_as_bool(d.get("forward_other"), True),
        ignore_self=_as_bool(d.get("ignore_self"), True),
    )


def _build_media(d: dict) -> MediaConfig:
    default = MediaConfig()
    return MediaConfig(
        enabled=_as_bool(d.get("enabled"), default.enabled),
        dir=str(d.get("dir") or default.dir),
        max_mb=_as_float(d.get("max_mb"), default.max_mb),
        max_per_batch=_as_int(d.get("max_per_batch"), default.max_per_batch),
        keep_days=_as_float(d.get("keep_days"), default.keep_days),
        download_timeout=_as_float(d.get("download_timeout"), default.download_timeout),
    )


def _build_limits(d: dict) -> LimitsConfig:
    default = LimitsConfig()
    return LimitsConfig(
        max_sends_per_day=_as_int(d.get("max_sends_per_day"), default.max_sends_per_day),
        min_send_interval=_as_float(d.get("min_send_interval"), default.min_send_interval),
    )


def _build_logging(d: dict) -> LoggingConfig:
    default = LoggingConfig()
    return LoggingConfig(
        level=str(d.get("level") or default.level).upper(),
        file=str(d.get("file") if d.get("file") is not None else default.file),
        archive_dir=str(
            d.get("archive_dir") if d.get("archive_dir") is not None else default.archive_dir
        ),
        archive_keep_days=_as_float(
            d.get("archive_keep_days"), default.archive_keep_days
        ),
    )
