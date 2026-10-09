"""QQ 侧：通过 NapCat 的 OneBot 11 接口收发群消息。

对外提供两种实现，主流程只用 QQConnection：
  - OneBotWsConnection : 正向 WebSocket，连到 NapCat 的 WS 服务器（默认）
  - ReplayConnection   : 从 JSONL 文件回放事件，用于离线验证整条链路
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import AppConfig
from .core import (
    extract_reply_id,
    get_logger,
    is_at_me,
    render_message,
    resolve_path,
    safe_get,
)

log = get_logger("qq2wx.qq")

# 收到这些事件说明登录态出了问题，值得提醒用户
NOTICE_EVENTS = {
    "notice",
}


@dataclass
class QqMessage:
    """一条规范化之后的 QQ 群消息。"""

    group_id: int
    group_name: str
    sender_id: str
    sender_name: str
    text: str
    time: float
    message_id: str = ""
    is_self: bool = False
    at_me: bool = False
    reply_to: str = ""
    # 需要单独转发的媒体（图片/文件）。文字里只留占位符。
    media: list = field(default_factory=list, repr=False)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def clock(self) -> str:
        return time.strftime("%H:%M", time.localtime(self.time))


class OneBotError(RuntimeError):
    pass


def payload_to_message(
    payload: dict,
    cfg: AppConfig,
    self_id: str = "",
    group_names: dict[int, str] | None = None,
) -> QqMessage | None:
    """把 OneBot 的群消息事件规范化成 QqMessage。返回 None 表示这条要丢弃。"""
    sender = payload.get("sender") or {}
    sender_id = str(payload.get("user_id") or sender.get("user_id") or "")
    raw_message = payload.get("message")

    text = render_message(raw_message, cfg.filters.segment_options())

    # 媒体（图片/文件）除了占位符，还要单独挑出来下载转发
    media = []
    if cfg.media.enabled:
        try:
            from .media import extract_media

            media = extract_media(raw_message, cfg)
        except Exception:  # noqa: BLE001
            media = []

    # 纯文字为空、又没有可转发的媒体 —— 这条没意义，丢掉
    if not text and not media:
        return None

    is_self = bool(self_id) and sender_id == self_id
    if is_self and cfg.filters.ignore_self:
        return None

    group_id = int(payload.get("group_id") or 0)
    name = (
        safe_get(sender, "card", default="")
        or safe_get(sender, "nickname", default="")
        or sender_id
    )

    return QqMessage(
        group_id=group_id,
        group_name=(group_names or {}).get(group_id) or str(group_id),
        sender_id=sender_id,
        sender_name=str(name),
        text=text,
        time=float(payload.get("time") or time.time()),
        message_id=str(payload.get("message_id") or ""),
        is_self=is_self,
        at_me=is_at_me(raw_message, self_id),
        reply_to=extract_reply_id(raw_message) or "",
        media=media,
        raw=payload,
    )


# ---------------------------------------------------------------- 正向 WS

class OneBotWsConnection:
    """连到 NapCat 的 OneBot 11 正向 WebSocket。

    收到群消息就调用 on_message(QqMessage)；断线自动重连（指数退避）。
    """

    def __init__(
        self,
        cfg: AppConfig,
        on_message: Callable[[QqMessage], None],
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.on_message = on_message
        self.on_event = on_event
        self.self_id = str(cfg.qq.self_id) if cfg.qq.self_id else ""
        self._stop = threading.Event()
        self._group_names: dict[int, str] = {}
        self._pending: dict[str, asyncio.Future] = {}  # echo -> 等待中的接口应答
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self.connected = threading.Event()
        self.stats = {"received": 0, "forwarded": 0, "reconnects": 0}
        # 抓包模式用：每个原始事件都会先喂给这个回调
        self.raw_sink: Callable[[dict], None] | None = None

    # ------------------------------------------------------------ 事件解析

    def _handle_payload(self, payload: dict) -> None:
        if self.raw_sink is not None:
            try:
                self.raw_sink(payload)
            except Exception:  # noqa: BLE001
                log.debug("raw_sink 异常", exc_info=True)

        post_type = payload.get("post_type")

        if post_type == "meta_event":
            meta = payload.get("meta_event_type")
            if meta == "lifecycle":
                sid = payload.get("self_id")
                if sid and not self.self_id:
                    self.self_id = str(sid)
                    log.info("识别到登录 QQ：%s", self.self_id)
            return

        if post_type != "message":
            if post_type in NOTICE_EVENTS and self.on_event:
                self.on_event(payload)
            return

        if payload.get("message_type") != "group":
            return

        msg = payload_to_message(payload, self.cfg, self.self_id, self._group_names)
        if msg is None:
            return

        self.stats["received"] += 1
        try:
            self.on_message(msg)
        except Exception as exc:  # noqa: BLE001
            log.exception("处理消息出错：%s", exc)

    # ------------------------------------------------------------ 主循环

    def run_until_stopped(self) -> None:
        try:
            asyncio.run(self._main())
        except KeyboardInterrupt:
            pass

    async def _main(self) -> None:
        try:
            import websockets  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "缺少 websockets 库，请先执行：pip install -r requirements.txt"
            ) from exc

        self._loop = asyncio.get_running_loop()
        backoff = 1.0

        while not self._stop.is_set():
            url = self.cfg.qq.ws_url
            headers = None
            if self.cfg.qq.access_token:
                headers = {"Authorization": f"Bearer {self.cfg.qq.access_token}"}

            try:
                log.info("正在连接 NapCat：%s", url)
                async with websockets.connect(
                    url,
                    additional_headers=headers,
                    open_timeout=15,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=None,
                ) as ws:
                    self._ws = ws
                    self.connected.set()
                    backoff = 1.0
                    log.info("已连上 NapCat，开始接收群消息")

                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            payload = json.loads(raw)
                        except (TypeError, ValueError):
                            log.debug("收到非 JSON 数据，已忽略")
                            continue

                        echo = payload.get("echo")
                        if echo and str(echo) in self._pending:
                            fut = self._pending.pop(str(echo))
                            if not fut.done():
                                fut.set_result(payload)
                            continue
                        if echo:
                            continue

                        self._handle_payload(payload)

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                self.stats["reconnects"] += 1
                log.warning("NapCat 连接断开（%s），%.0f 秒后重连", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._ws = None
                self.connected.clear()
                for fut in self._pending.values():
                    if not fut.done():
                        fut.cancel()
                self._pending.clear()

        log.info("QQ 侧已停止")

    # ------------------------------------------------------------ 调接口

    def call_api(self, action: str, params: dict | None = None, timeout: float = 15.0) -> Any:
        """调 OneBot 接口（同步，可在其他线程调用）。返回 data 字段。"""
        if self._loop is None or self._ws is None:
            raise OneBotError("还没连上 NapCat，无法调用接口")

        fut = asyncio.run_coroutine_threadsafe(
            self._call_api_async(action, params or {}, timeout), self._loop
        )
        return fut.result(timeout=timeout + 5)

    async def _call_api_async(self, action: str, params: dict, timeout: float) -> Any:
        echo = f"{action}-{int(time.time() * 1000)}-{id(params) & 0xFFFF}"
        assert self._loop is not None
        fut: asyncio.Future = self._loop.create_future()
        self._pending[echo] = fut
        try:
            assert self._ws is not None
            await self._ws.send(
                json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
            )
            reply = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(echo, None)

        if reply.get("status") == "failed":
            raise OneBotError(f"{action} 调用失败：{reply.get('message') or reply}")
        return reply.get("data")

    def fetch_group_names(self) -> dict[int, str]:
        """取一次群列表，把群号映射成群名。失败不影响主流程。"""
        try:
            data = self.call_api("get_group_list") or []
        except Exception as exc:  # noqa: BLE001
            log.debug("获取群列表失败（不影响转发）：%s", exc)
            return {}
        for item in data if isinstance(data, list) else []:
            gid = int(safe_get(item, "group_id", default=0) or 0)
            name = str(safe_get(item, "group_name", "groupName", default="") or "")
            if gid:
                self._group_names[gid] = name or str(gid)
        return dict(self._group_names)

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        loop = self._loop
        if ws is not None and loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- HTTP 备用

class OneBotHttpClient:
    """通过 NapCat 的 HTTP 接口调用 OneBot API。用来拿群名等辅助信息。"""

    def __init__(self, http_url: str, access_token: str = "", timeout: float = 10.0) -> None:
        self.http_url = http_url.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout

    def call(self, action: str, params: dict | None = None) -> Any:
        import urllib.error
        import urllib.request

        url = f"{self.http_url}/{action}"
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.URLError as exc:
            raise OneBotError(f"调用 {action} 失败：{exc}") from exc
        except (ValueError, OSError) as exc:
            raise OneBotError(f"调用 {action} 返回异常：{exc}") from exc

        if isinstance(data, dict) and data.get("status") == "failed":
            raise OneBotError(f"{action} 返回失败：{data.get('message') or data}")
        return data.get("data") if isinstance(data, dict) else data

    def group_list(self) -> dict[int, str]:
        try:
            data = self.call("get_group_list") or []
        except OneBotError as exc:
            log.debug("HTTP 取群列表失败：%s", exc)
            return {}
        out: dict[int, str] = {}
        for item in data if isinstance(data, list) else []:
            gid = int(safe_get(item, "group_id", default=0) or 0)
            name = str(safe_get(item, "group_name", "groupName", default="") or "")
            if gid:
                out[gid] = name or str(gid)
        return out


# ---------------------------------------------------------------- 反向 WS

class OneBotReverseWsConnection:
    """本机起一个 WebSocket 服务端，等 NapCat 主动连过来。

    对应 NapCat 网络配置里的「WebSocket 客户端」模式。没有公网/跨机需求时，
    这种模式通常更好配；缺点是断线由 NapCat 那边负责重连，我们只能被动等。

    地址写法：ws://127.0.0.1:6200/  或  ws://0.0.0.0:6200/onebot/v11/ws
    """

    def __init__(
        self,
        cfg: AppConfig,
        on_message: Callable[[QqMessage], None],
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.on_message = on_message
        self.on_event = on_event
        self.self_id = str(cfg.qq.self_id) if cfg.qq.self_id else ""
        self._group_names: dict[int, str] = {}
        self._stop = threading.Event()
        self._ws: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.connected = threading.Event()
        self.stats = {"received": 0, "forwarded": 0, "reconnects": 0}
        self._last_client: str = ""
        self.raw_sink: Callable[[dict], None] | None = None

    def _parse_bind(self) -> tuple[str, int, str]:
        from urllib.parse import urlparse

        u = urlparse(self.cfg.qq.ws_url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 6200
        path = u.path or "/"
        if u.query:
            path = f"{path}?{u.query}"
        return host, port, path

    def run_until_stopped(self) -> None:
        try:
            asyncio.run(self._main())
        except KeyboardInterrupt:
            pass

    async def _main(self) -> None:
        try:
            import websockets  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "缺少 websockets 库，请先执行：pip install -r requirements.txt"
            ) from exc

        host, port, path = self._parse_bind()
        expected_token = self.cfg.qq.access_token

        async def handler(websocket) -> None:
            peer = "unknown"
            try:
                peer = str(websocket.remote_address)
            except Exception:  # noqa: BLE001
                pass

            # 校验 token（NapCat 侧填什么这里就填什么；留空则不校验）
            if expected_token:
                headers = getattr(websocket, "request", None)
                headers = getattr(headers, "headers", None)
                auth = headers.get("Authorization") if headers else None
                if auth != f"Bearer {expected_token}":
                    log.warning("拒绝来自 %s 的连接：token 不匹配", peer)
                    await websocket.close(code=1008, reason="invalid token")
                    return

            self._ws = websocket
            self._last_client = peer
            self.connected.set()
            log.info("NapCat 已接入（来自 %s），开始接收群消息", peer)
            try:
                async for raw in websocket:
                    if self._stop.is_set():
                        break
                    try:
                        payload = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if payload.get("echo"):
                        continue
                    self._handle_payload(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("连接 %s 中断：%s", peer, exc)
            finally:
                if self._ws is websocket:
                    self._ws = None
                    self.connected.clear()
                log.info("NapCat 连接已断开，等待重新接入…")

        try:
            async with websockets.serve(handler, host, port, max_size=None) as server:
                self._loop = asyncio.get_running_loop()
                log.info("已监听 %s:%d%s，等待 NapCat 以「WebSocket 客户端」方式接入",
                         host, port, path)
                await server.serve_forever()
        except OSError as exc:
            raise OneBotError(
                f"无法监听 {host}:{port}（{exc}）。端口可能被占用，换个端口或关掉占用的程序。"
            ) from exc

    def _handle_payload(self, payload: dict) -> None:
        if self.raw_sink is not None:
            try:
                self.raw_sink(payload)
            except Exception:  # noqa: BLE001
                log.debug("raw_sink 异常", exc_info=True)

        post_type = payload.get("post_type")

        if post_type == "meta_event":
            if payload.get("meta_event_type") == "lifecycle":
                sid = payload.get("self_id")
                if sid and not self.self_id:
                    self.self_id = str(sid)
                    log.info("识别到登录 QQ：%s", self.self_id)
            return

        if post_type != "message" or payload.get("message_type") != "group":
            if post_type in NOTICE_EVENTS and self.on_event:
                self.on_event(payload)
            return

        msg = payload_to_message(payload, self.cfg, self.self_id, self._group_names)
        if msg is None:
            return
        self.stats["received"] += 1
        try:
            self.on_message(msg)
        except Exception as exc:  # noqa: BLE001
            log.exception("处理消息出错：%s", exc)

    def stop(self) -> None:
        self._stop.set()
        ws, loop = self._ws, self._loop
        if ws is not None and loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:  # noqa: BLE001
                pass

    def fetch_group_names(self) -> dict[int, str]:
        """反向模式下事件流不能应答 API 请求，改走 HTTP 接口。"""
        client = OneBotHttpClient(self.cfg.qq.http_url, self.cfg.qq.access_token)
        names = client.group_list()
        if names:
            self._group_names.update(names)
        return dict(self._group_names)


def is_reverse_ws(ws_url: str) -> bool:
    """判断这个地址是要我们监听（反向）还是要我们连出去（正向）。"""
    from urllib.parse import urlparse

    u = urlparse(ws_url)
    host = (u.hostname or "").lower()
    return host in {"0.0.0.0", "::", "*"}


def build_connection(
    cfg: AppConfig,
    on_message: Callable[[QqMessage], None],
    on_event: Callable[[dict], None] | None = None,
):
    """按 ws_url 自动选择正向连接还是反向监听。"""
    if is_reverse_ws(cfg.qq.ws_url):
        log.info("检测到监听地址 %s，使用反向 WebSocket 模式", cfg.qq.ws_url)
        return OneBotReverseWsConnection(cfg, on_message, on_event)
    return OneBotWsConnection(cfg, on_message, on_event)


# ---------------------------------------------------------------- 离线回放

class ReplayConnection:
    """从 JSONL 事件文件回放，用来在不接 QQ 的情况下验证整条链路。"""

    def __init__(
        self,
        path: str | Path,
        on_message: Callable[[QqMessage], None],
        cfg: AppConfig,
        speed: float = 0.0,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.path = resolve_path(path)
        self.on_message = on_message
        self.cfg = cfg
        self.speed = speed
        self.on_event = on_event
        self._stop = threading.Event()
        self.self_id = str(cfg.qq.self_id) if cfg.qq.self_id else ""
        self.connected = threading.Event()
        self.stats = {"received": 0, "forwarded": 0, "reconnects": 0}

    def iter_events(self) -> Iterator[dict]:
        with self.path.open("r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    log.warning("回放文件里有解析不了的行，已跳过")

    def run_until_stopped(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"回放文件不存在：{self.path}")

        self.connected.set()
        group_names: dict[int, str] = {}

        for payload in self.iter_events():
            if self._stop.is_set():
                break
            post_type = payload.get("post_type")
            if post_type == "meta_event":
                sid = payload.get("self_id")
                if sid and not self.self_id:
                    self.self_id = str(sid)
                continue
            if post_type != "message":
                continue
            msg = payload_to_message(payload, self.cfg, self.self_id, group_names)
            if msg is None:
                continue
            self.stats["received"] += 1
            self.on_message(msg)
            if self.speed > 0:
                time.sleep(self.speed)

        log.info("回放结束，共 %d 条消息", self.stats["received"])
        self.connected.clear()

    def stop(self) -> None:
        self._stop.set()
