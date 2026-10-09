"""媒体转发：把 QQ 群里的图片/文件下载到本地，再交给微信单独发送。

为什么要单独发而不能塞进文字里：
    原来的实现只把图片/文件渲染成 ``[图片]`` / ``[文件]`` 这样的占位符，
    微信那边看到的就是一行字。要真的把文件送过去，必须：
        1. 从 OneBot 消息段里拿到下载地址
        2. 下载到本地临时目录（带大小上限和超时）
        3. 用 wx4py 的 send_file_to() 当作文件发给微信群

安全与清理：
    - 限制单个文件大小（默认 20 MB），超了只留占位符
    - 限制下载超时，避免卡住整条流水线
    - 文件落在 media_dir，按天分目录，启动时清理超过 N 天的旧文件
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import AppConfig
from .core import get_logger, resolve_path, safe_get

log = get_logger("qq2wx.media")


@dataclass
class MediaItem:
    """一条待下载/待发送的媒体。"""

    kind: str            # image | file | video | record
    url: str = ""        # 下载地址（onebot 给的）
    filename: str = ""   # 原始文件名（文件类消息一般有）
    file_id: str = ""    # NapCat 的内部 file id
    # 下载后填入
    local_path: str = ""
    size: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.local_path) and not self.error


# OneBot 各类型消息段里可能出现的 URL 字段
_URL_KEYS = ("url", "file", "path")

_SAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_filename(name: str, fallback: str) -> str:
    """把任意文件名洗成 Windows 能用的名字。"""
    name = (name or "").strip()
    if not name:
        return fallback
    # 有些实现会把路径塞进 file 字段
    name = os.path.basename(name.replace("\\", "/"))
    name = _SAFE_NAME.sub("_", name)
    name = name.strip(". ")
    if not name:
        return fallback
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:100] + dot + ext[:16]) if dot else name[:120]
    return name


def extract_media(message: object, cfg: AppConfig) -> list[MediaItem]:
    """从 OneBot 的 message 字段里挑出需要单独转发的媒体。

    NapCat 的真实字段结构（2026-10-09 用 get_group_msg_history 实测）:

        file:  {"file": "教育部认定...目录.pdf",       <- 真文件名在这里
                "file_id": "/30d8ec96-be37-...",       <- 内部 id，不是文件名
                "file_size": "108363",
                "url": "https://njc-download.ftn.qq.com/ftn_handler/..."}

        image: {"file": "20BC677A....png",             <- 文件名（哈希.png）
                "url": "https://multimedia.nt.qq.com.cn/download?...",
                "file_size": "1149758"}

    踩过的坑：早先把 file_id 当成了 file:// 地址来源，导致 filename 被
    file_id 覆盖，下载下来变成一串 UUID.bin。现在明确区分：
        file 字段   -> 文件名
        file_id     -> 内部 id（只在没有 url 时当作 file:// 兜底）
        url         -> 唯一的下载地址
    """
    filters = cfg.filters
    wanted = {
        "image": filters.forward_images,
        "file": filters.forward_files,
        "video": filters.forward_videos,
    }
    if not any(wanted.values()):
        return []

    items: list[MediaItem] = []

    def push(seg_type: str, data: dict) -> None:
        seg_type = (seg_type or "").lower()
        if not wanted.get(seg_type, False):
            return

        raw_file = str(data.get("file") or "").strip()      # 文件名
        file_id = str(data.get("file_id") or "").strip()    # 内部 id
        name = str(data.get("name") or data.get("file_name") or "").strip()

        # 下载地址：优先 url / path 这类真地址
        url = ""
        for key in ("url", "path"):
            val = str(data.get(key) or "").strip()
            if val.startswith(("http://", "https://", "file://")):
                url = val
                break

        # 没有 http 地址时，看 file 字段本身是不是地址
        if not url and raw_file.startswith(("http://", "https://", "file://")):
            url = raw_file

        # 文件名优先级：显式 name > file 字段（排除它本身是地址的情况）
        if not name and raw_file and not raw_file.startswith(("http://", "https://", "file://")):
            name = raw_file
        if not name and seg_type == "image" and raw_file:
            name = raw_file

        # 没有可用地址就跳过（file_id 不是地址，当不了下载源）
        if not url:
            return

        items.append(MediaItem(kind=seg_type, url=url, filename=name, file_id=file_id))

    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict):
                push(str(seg.get("type", "")), seg.get("data") or {})

    return items


class MediaDownloader:
    """下载媒体到本地临时目录。线程安全，可被多批同时调用。"""

    def __init__(self, cfg: AppConfig, http_call=None) -> None:
        self.cfg = cfg
        self.media_dir = resolve_path(cfg.media.dir)
        self.max_bytes = int(cfg.media.max_mb * 1024 * 1024)
        self.timeout = cfg.media.download_timeout
        self._lock = threading.Lock()
        self._seq = 0
        self._counter = {"downloaded": 0, "skipped": 0, "failed": 0}
        self._http_call = http_call  # 可选：调用 OneBot 的 get_* 拿直链

        try:
            self.media_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("媒体目录创建失败，媒体转发将不可用：%s", exc)

    # ------------------------------------------------------------ 清理

    def cleanup_old(self, keep_days: float | None = None) -> int:
        """删掉超过保留期的旧媒体文件。返回删除数量。"""
        keep_days = self.cfg.media.keep_days if keep_days is None else keep_days
        if keep_days <= 0 or not self.media_dir.exists():
            return 0
        deadline = time.time() - keep_days * 86400
        removed = 0
        for path in self.media_dir.rglob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < deadline:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
        if removed:
            log.info("清理了 %d 个过期媒体文件", removed)
        return removed

    # ------------------------------------------------------------ 下载

    def _target_path(self, item: MediaItem) -> Path:
        day = time.strftime("%Y-%m-%d")
        with self._lock:
            self._seq += 1
            seq = self._seq

        # 文件名：优先用消息里带的真文件名（含扩展名）
        if item.filename:
            base = os.path.splitext(item.filename)[0]
            ext = os.path.splitext(item.filename)[1]
        else:
            base = ""
            ext = ""

        # 没有扩展名就从 url 猜（下载后还会用文件头再校正一次）
        if not ext:
            ext = _guess_ext(item.kind, item.url)

        base = _safe_filename(base, item.kind) or item.kind
        name = f"{base}_{seq}{ext}"
        return self.media_dir / day / name

    def download(self, item: MediaItem) -> MediaItem:
        """下载单个媒体。失败时 item.error 有原因，调用方退回占位符。"""
        if not item.url:
            item.error = "没有可用的下载地址"
            self._counter["skipped"] += 1
            return item

        dest = self._target_path(item)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            item.error = f"创建目录失败：{exc}"
            self._counter["failed"] += 1
            return item

        try:
            self._fetch(item.url, dest)
        except Exception as exc:  # noqa: BLE001
            item.error = f"下载失败：{exc}"
            self._counter["failed"] += 1
            log.warning("媒体下载失败（%s）：%s", item.kind, exc)
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            return item

        try:
            size = dest.stat().st_size
        except OSError as exc:
            item.error = f"读取文件失败：{exc}"
            self._counter["failed"] += 1
            return item

        if size <= 0:
            item.error = "下载到的是空文件"
            self._counter["failed"] += 1
            dest.unlink(missing_ok=True)
            return item

        if size > self.max_bytes:
            item.error = f"文件超过 {self.cfg.media.max_mb:.0f}MB 上限"
            self._counter["skipped"] += 1
            dest.unlink(missing_ok=True)
            return item

        # 按文件头校正扩展名。QQ 的文件下载地址常常不带扩展名，
        # 光靠 url 猜会得到 .bin，微信那边就认不出类型了。
        real_ext = sniff_ext(str(dest))
        if real_ext and dest.suffix.lower() != real_ext:
            better = dest.with_suffix(real_ext)
            try:
                dest.replace(better)
                dest = better
                log.debug("按文件头校正扩展名 -> %s", dest.name)
            except OSError:
                pass

        item.local_path = str(dest)
        item.size = size
        self._counter["downloaded"] += 1
        log.info("媒体已下载：%s %s（%.1f KB）",
                 item.kind, dest.name, size / 1024)
        return item

    def _fetch(self, url: str, dest: Path) -> None:
        """下载 url 到 dest。支持 http(s) 和 file://。"""
        parsed = urlparse(url)

        if parsed.scheme == "file":
            src = Path(unquote(parsed.path.lstrip("/")))
            if not src.exists():
                raise FileNotFoundError(f"本地路径不存在：{src}")
            if src.stat().st_size > self.max_bytes:
                raise ValueError(f"文件超过 {self.cfg.media.max_mb:.0f}MB 上限")
            dest.write_bytes(src.read_bytes())
            return

        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"不支持的地址协议：{parsed.scheme or '(空)'}")

        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "qq2wx-relay"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                # 先看 Content-Length，避免下载超大文件
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    raise ValueError(
                        f"文件 {int(declared)/1048576:.1f}MB 超过 "
                        f"{self.cfg.media.max_mb:.0f}MB 上限"
                    )
                written = 0
                with dest.open("wb") as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > self.max_bytes:
                            raise ValueError(
                                f"文件超过 {self.cfg.media.max_mb:.0f}MB 上限"
                            )
                        fh.write(chunk)
        except urllib.error.URLError as exc:
            raise RuntimeError(str(exc)) from exc

    # ------------------------------------------------------------ 批量

    def download_all(self, items: list[MediaItem], limit: int | None = None) -> list[MediaItem]:
        """按顺序下载，返回成功的那些。limit 限制每批最多下载几个。"""
        if limit is None:
            limit = self.cfg.media.max_per_batch
        out: list[MediaItem] = []
        for item in items:
            if limit and len(out) >= limit:
                log.info("本批媒体数超过上限 %d，其余只留占位符", limit)
                break
            self.download(item)
            if item.ok:
                out.append(item)
        return out

    def stats(self) -> dict[str, int]:
        return dict(self._counter)


def _guess_ext(kind: str, url: str) -> str:
    """从 url 猜扩展名，猜不到就按类型给默认值。"""
    path = urlparse(url).path if url else ""
    ext = os.path.splitext(path)[1]
    if ext and len(ext) <= 8 and ext.isascii():
        return ext
    return {"image": ".jpg", "video": ".mp4", "record": ".amr"}.get(kind, ".bin")


# 常见文件类型的魔数。QQ 的 file 消息 url 里常常不带扩展名，
# 而实际内容可能是 PDF / 图片 / 压缩包。按内容判断比猜可靠得多。
_MAGIC: list[tuple[bytes, str]] = [
    (b"%PDF", ".pdf"),
    (b"PK\x03\x04", ".zip"),
    (b"Rar!\x1a\x07", ".rar"),
    (b"7z\xbc\xaf\x27\x1c", ".7z"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"RIFF", ".webp"),          # RIFF....WEBP，粗略归到 webp
    (b"\xd0\xcf\x11\xe0", ".doc"),  # 老版 Office 复合文档
    (b"{\\rtf", ".rtf"),
]


def sniff_ext(path: str, fallback: str = "") -> str:
    """按文件头判断真实类型。认不出来就返回 fallback。

    特别注意 Office 文件：docx/xlsx/pptx 本质上都是 ZIP 包，
    只看 ZIP 魔数会全部误判成 .zip（实测踩过：一个 .xlsx 表格被
    当成 .zip 发到微信，收件人打开会一头雾水）。
    所以命中 ZIP 魔数后还要往包里看一眼，按内部结构区分。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return fallback

    for magic, ext in _MAGIC:
        if head.startswith(magic):
            if ext == ".zip":
                return _office_ext(path) or ".zip"
            return ext

    # 纯文本类：大量可见 ASCII 且没有空字节
    if head and b"\x00" not in head:
        try:
            sample = head.decode("utf-8")
            if sample.isprintable():
                return ".txt"
        except UnicodeDecodeError:
            pass
    return fallback


def _office_ext(path: str) -> str:
    """ZIP 包里如果出现 Office 的目录结构，返回对应的扩展名。"""
    import zipfile

    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile):
        return ""

    joined = "\n".join(names[:200])
    if "word/" in joined:
        return ".docx"
    if "xl/" in joined:
        return ".xlsx"
    if "ppt/" in joined:
        return ".pptx"
    # 其它 OOXML / ODF 变体
    if "mimetype" in names and any(n.startswith("META-INF") for n in names):
        return ".odf"
    return ""


def media_placeholder(item: MediaItem) -> str:
    """媒体没法转发时，在文字里放这个占位符。"""
    if item.kind == "image":
        return "[图片]"
    if item.filename:
        return f"[文件: {item.filename}]"
    return "[文件]"
