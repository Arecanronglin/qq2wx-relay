"""微信发送适配层。

三个通道，同一套接口：
  - DryRunSender    仅打印，用来先验证 QQ 那半边链路
  - WebhookSender   企业微信 / 钉钉群机器人（最稳，但到不了普通微信群）
  - Wx4pySender     真实发到微信群，走 wx4py（微信 4.x UIA 自动化）

Wx4pySender 把微信操作放在自己的后台线程里串行执行 —— wx4py 是 UI 自动化，
天然不能并发，同一个 client 反复 connect/disconnect 也容易出问题，所以这里
只连一次、常驻一个线程。
"""

from __future__ import annotations

import inspect
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import AppConfig
from .core import get_logger

log = get_logger("qq2wx.sender")


class SendError(RuntimeError):
    pass


@dataclass
class SendResult:
    target: str
    ok: bool
    detail: str = ""


# ---------------------------------------------------------------- 基类

class Sender:
    name = "base"

    def send(self, target: str, text: str) -> SendResult:
        raise NotImplementedError

    def send_file(self, target: str, file_path: str, caption: str = "") -> SendResult:
        """把本地文件发给目标。默认不支持，子类按需覆盖。"""
        return SendResult(target, False, f"{self.name} 通道不支持发送文件")

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return self.name


# ---------------------------------------------------------------- 干跑

class DryRunSender(Sender):
    """不真的发送，只把内容打到日志和文件里。

    用来在没装 wx4py / 没配 NapCat 的情况下先把整条链路跑通。
    """

    name = "dry-run"

    def __init__(self, log_file: str | None = None) -> None:
        self._lock = threading.Lock()
        self._fh = None
        if log_file:
            from .core import resolve_path

            path = resolve_path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", encoding="utf-8")

    def send(self, target: str, text: str) -> SendResult:
        header = f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} -> 微信「{target}」 ====="
        with self._lock:
            print(f"\n{header}\n{text}\n{'=' * len(header)}", flush=True)
            if self._fh:
                self._fh.write(f"{header}\n{text}\n\n")
                self._fh.flush()
        return SendResult(target, True, "dry-run 未实际发送")

    def send_file(self, target: str, file_path: str, caption: str = "") -> SendResult:
        import os

        size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        header = f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} -> 微信「{target}」[文件] ====="
        with self._lock:
            print(f"\n{header}\n  {file_path}  ({size/1024:.1f} KB)\n{'=' * len(header)}",
                  flush=True)
            if self._fh:
                self._fh.write(f"{header}\n  {file_path}  ({size/1024:.1f} KB)\n\n")
                self._fh.flush()
        return SendResult(target, True, "dry-run 未实际发送")

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------- Webhook

class WebhookSender(Sender):
    """企业微信 / 钉钉群机器人 webhook。"""

    name = "webhook"

    def __init__(self, conf: dict) -> None:
        self.url = str(conf.get("url") or "").strip()
        self.kind = str(conf.get("kind") or "wecom").strip().lower()
        self.timeout = float(conf.get("timeout") or 10)
        if not self.url:
            raise SendError("WebhookSender 没配 url")

    def _payload(self, text: str) -> dict:
        if self.kind == "dingtalk":
            return {"msgtype": "markdown", "markdown": {"title": "QQ群消息", "text": text}}
        if self.kind == "plain":
            return {"text": text}
        # 企业微信群机器人 markdown
        return {"msgtype": "markdown", "markdown": {"content": text}}

    def send(self, target: str, text: str) -> SendResult:
        import json
        import urllib.error
        import urllib.request

        # webhook 是固定的一个群，target 只用来在正文里标明来源
        body = json.dumps(self._payload(text), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            return SendResult(target, False, f"网络错误：{exc}")
        except OSError as exc:
            return SendResult(target, False, f"请求失败：{exc}")

        if self.kind in {"wecom", "dingtalk"}:
            try:
                data = json.loads(raw or "{}")
            except ValueError:
                data = {}
            code = data.get("errcode", data.get("errcode", 0))
            if code not in (0, None):
                return SendResult(target, False, f"接口返回 errcode={code} msg={data.get('errmsg')}")
        return SendResult(target, True, raw[:200])

    def describe(self) -> str:
        return f"webhook({self.kind})"

    def send_file(self, target: str, file_path: str, caption: str = "") -> SendResult:
        # 群机器人 webhook 不支持传文件，只能退化成一句提示
        import os

        name = os.path.basename(file_path)
        size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        note = f"（{caption}）" if caption else ""
        self.send(target, f"[文件无法通过 webhook 转发] {name} {size/1024:.1f} KB{note}")
        return SendResult(target, False, "webhook 不支持发送文件，已退化为文字提示")


# ---------------------------------------------------------------- wx4py

# ---------------------------------------------------------------- 任务描述

@dataclass
class _TextJob:
    """发一条文字。"""

    target: str
    text: str


@dataclass
class _FileJob:
    """发一个本地文件。"""

    target: str
    path: str
    caption: str = ""


_Job = "_TextJob | _FileJob"


# ---------------------------------------------------------------- wx4py

class Wx4pySender(Sender):
    """真实发送到微信群，基于 wx4py。"""

    name = "wx4py"

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.retries = cfg.wechat.wx4py_retries
        self.retry_delay = cfg.wechat.wx4py_retry_delay
        self.per_message_delay = cfg.wechat.wx4py_per_message_delay
        self.target_type = cfg.wechat.wx4py_target_type

        self._queue: "queue.Queue[tuple[Any, Any]]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._client: Any = None
        self._sender_call: Callable[..., Any] | None = None
        self._file_call: Callable[..., Any] | None = None
        # 连续失败计数：树塌陷时用来判断是否该彻底停手
        self._uia_failures = 0
        self._paused = False
        self._ready = threading.Event()
        self._init_error: str = ""
        self._init_lock = threading.Lock()
        self._stopped = False

    # ------------------------------------------------------------ 线程

    def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        with self._init_lock:
            if self._worker is not None:
                return
            self._worker = threading.Thread(
                target=self._run, name="wechat-sender", daemon=True
            )
            self._worker.start()

    def _connect(self) -> None:
        try:
            import wx4py  # type: ignore
        except ImportError as exc:
            raise SendError(
                "没装 wx4py。请先执行：pip install -r requirements.txt"
            ) from exc

        # 先把窗口弄出来。wx4py 自己的托盘恢复在微信 4.x 上不生效，
        # 不先修好这一步，后面每一步都会失败（找不到搜索框）。
        visible, why = ensure_wechat_window_visible()
        log.info("微信窗口检查：%s", why)
        if not visible:
            raise SendError(why)

        # UIA 控件树为空的话，没必要继续折腾，直接给明确指引
        try:
            from wx4py.core.win32 import find_wechat_window

            hwnd = find_wechat_window() or 0
            if hwnd:
                nodes = wechat_uia_node_count(hwnd)
                if 0 <= nodes < 5:
                    raise SendError(UIA_EMPTY_HINT)
        except SendError:
            raise
        except Exception:  # noqa: BLE001
            pass  # 检查本身失败不该挡住主流程

        client_cls = getattr(wx4py, "WeChatClient", None)
        if client_cls is None:
            raise SendError(
                f"wx4py 版本不匹配：模块里没有 WeChatClient（实际有：{dir(wx4py)[:20]}）"
            )

        log.info("正在连接微信（wx4py）…")
        client = client_cls()
        if hasattr(client, "connect"):
            ok = client.connect()
            if ok is False:
                raise SendError("wx4py 连接微信失败，请确认微信已登录且窗口没有最小化")
        self._client = client

        chat_window = getattr(client, "chat_window", None)
        if chat_window is None:
            raise SendError("wx4py 的 client 上没有 chat_window，接口对不上")

        self._sender_call = _resolve_send_callable(chat_window, self.target_type)
        log.info("微信连接就绪，发送接口：%s", self._sender_call)
        try:
            self._file_call = _resolve_file_callable(chat_window, self.target_type)
            log.info("微信发文件接口：%s", self._file_call)
        except SendError as exc:
            self._file_call = None
            log.warning("微信发文件接口不可用，图片/文件只会转发文字占位符：%s", exc)

    def _run(self) -> None:
        # 工作线程必须先初始化 COM，否则 UIAutomation 加载不了
        _com_initialize()
        try:
            self._connect()
        except Exception as exc:  # noqa: BLE001
            self._init_error = str(exc)
            log.error("微信通道初始化失败：%s", exc)
            self._ready.set()
            return

        self._ready.set()
        while not self._stopped:
            try:
                job, reply = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if job is None:  # 退出信号
                self._queue.task_done()
                break

            try:
                if isinstance(job, _FileJob):
                    detail = self._file_with_retry(job.target, job.path, job.caption)
                else:
                    detail = self._send_with_retry(job.target, job.text)
                reply.put((True, detail))
            except Exception as exc:  # noqa: BLE001
                reply.put((False, str(exc)))
            finally:
                self._queue.task_done()
                if self.per_message_delay > 0:
                    time.sleep(self.per_message_delay)

    def _send_with_retry(self, target: str, text: str) -> str:
        assert self._sender_call is not None

        # 守卫：树塌了就不要再操作微信，连续失败会把它拖垮
        ok, why = wechat_uia_healthy()
        if not ok:
            self._uia_failures += 1
            raise SendError(f"跳过发送：{why}")

        last = ""
        for attempt in range(self.retries + 1):
            try:
                result = self._sender_call(target, text)
            except TypeError as exc:
                # 方法签名对不上，属于配置/版本问题，重试没意义
                raise SendError(
                    f"调用微信发送接口时参数不匹配：{exc}。请用 --probe-wx 看实际可用签名"
                ) from exc
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                if attempt < self.retries:
                    log.warning(
                        "发往「%s」失败（第 %d 次）：%s，%.0f 秒后重试",
                        target, attempt + 1, exc, self.retry_delay,
                    )
                    time.sleep(self.retry_delay)
                    continue
                raise SendError(f"重试 {self.retries} 次仍失败：{last}") from exc

            if result is False:
                last = "wx4py 返回 False（没找到该群，或者微信窗口被遮挡）"
                if attempt < self.retries:
                    log.warning("发往「%s」返回 False，%.0f 秒后重试", target, self.retry_delay)
                    time.sleep(self.retry_delay)
                    continue
                raise SendError(last)

            return "ok"
        raise SendError(last or "未知失败")

    def _file_with_retry(self, target: str, path: str, caption: str = "") -> str:
        """发文件，带重试。逻辑和 _send_with_retry 一致。"""
        if self._file_call is None:
            raise SendError("当前微信通道没有可用的发文件接口")
        import os

        if not os.path.exists(path):
            raise SendError(f"文件不存在：{path}")

        # 守卫：UIA 树不健康时不要碰微信。
        # 发文件会走 "打开聊天 → 粘贴 → 回车 → 最小化窗口"，失败时 wx4py
        # 会反复恢复/最小化窗口，在树已塌陷的状态下足以把微信搞崩（实测过）。
        ok, why = wechat_uia_healthy()
        if not ok:
            raise SendError(f"跳过发文件：{why}")

        last = ""
        for attempt in range(self.retries + 1):
            try:
                result = self._file_call(target, path, caption)
            except TypeError as exc:
                raise SendError(f"调用微信发文件接口时参数不匹配：{exc}") from exc
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                if attempt < self.retries:
                    log.warning(
                        "发文件到「%s」失败（第 %d 次）：%s，%.0f 秒后重试",
                        target, attempt + 1, exc, self.retry_delay,
                    )
                    time.sleep(self.retry_delay)
                    continue
                raise SendError(f"发文件重试 {self.retries} 次仍失败：{last}") from exc

            if result is False:
                last = "wx4py 发文件返回 False"
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
                    continue
                raise SendError(last)
            return "ok"
        raise SendError(last or "未知失败")

    # ------------------------------------------------------------ 对外

    def warmup(self, timeout: float = 90.0) -> tuple[bool, str]:
        """提前把微信连上。返回 (是否就绪, 说明)。"""
        self._ensure_worker()
        if not self._ready.wait(timeout=timeout):
            return False, f"等待微信连接超时（{timeout:.0f}s）；确认微信已登录且窗口未最小化"
        if self._init_error:
            return False, self._init_error
        return True, "就绪"

    def _submit(self, job: Any, timeout: float = 180) -> SendResult:
        """把任务丢给工作线程并等结果。"""
        if self._stopped:
            return SendResult(getattr(job, "target", "?"), False, "通道已关闭")
        self._ensure_worker()
        if not self._ready.wait(timeout=60):
            return SendResult(getattr(job, "target", "?"), False, "等待微信通道初始化超时")
        if self._init_error:
            return SendResult(getattr(job, "target", "?"), False, self._init_error)

        reply: "queue.Queue[tuple[bool, str]]" = queue.Queue(maxsize=1)
        self._queue.put((job, reply))
        try:
            ok, detail = reply.get(timeout=timeout)
        except queue.Empty:
            return SendResult(
                getattr(job, "target", "?"), False,
                "等待发送结果超时（微信可能被弹窗挡住）",
            )
        return SendResult(getattr(job, "target", "?"), ok, detail)

    def send(self, target: str, text: str) -> SendResult:
        return self._submit(_TextJob(target=target, text=text))

    def send_file(self, target: str, file_path: str, caption: str = "") -> SendResult:
        # 文件比文字慢，给更长的等待时间
        return self._submit(
            _FileJob(target=target, path=file_path, caption=caption), timeout=300
        )

    def close(self) -> None:
        self._stopped = True
        if self._worker is not None and self._worker.is_alive():
            try:
                self._queue.put((None, queue.Queue()))  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                pass
            self._worker.join(timeout=10)
        client = self._client
        if client is not None:
            try:
                if hasattr(client, "disconnect"):
                    client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._client = None

    def describe(self) -> str:
        return f"wx4py(target_type={self.target_type})"


def _pick_call(method: Any, sig: inspect.Signature, kw_type: str | None,
               target_type: str, positional: int) -> Callable[[tuple], Any] | None:
    """按签名安全地构造调用：位置参数优先，关键字只用于可选参数。

    为什么不用全关键字调用（2026-10-09 抓到真 bug）：
        不同 wx4py / wxauto 版本的参数名不一样：
            send_to(target, message, target_type=)
            send_msg(who, msg)
            send_files(who, filepath)
        早先我按"候选参数名"拼关键字调用，结果在 send_msg 上用了
        `target=`，而人家要的是 `who=`，直接抛
            TypeError: send_msg() got an unexpected keyword argument 'target'
        登录后必然踩到。
        改成：**前 N 个参数一律按位置传**，位置绑定对参数名完全免疫；
        只有可选参数（目标类型）才用关键字，且传之前先确认签名里有它。
    """
    params = list(sig.parameters.values())
    positional_params = [
        p for p in params
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind == p.VAR_POSITIONAL for p in params)
    if not has_varargs and len(positional_params) < positional:
        return None

    def call(args: tuple) -> Any:
        pos = list(args[:positional])
        kw = {}
        if kw_type:
            p = sig.parameters.get(kw_type)
            if p is not None and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
                kw[kw_type] = target_type
        return method(*pos, **kw)

    return call


def _resolve_send_callable(chat_window: Any, target_type: str) -> Callable[[str, str], Any]:
    """找出「给某个群发一条文本」该调哪个方法。

    探测顺序按"确认存在且参数个数够"来定，参数名不参与判断（位置传参）。
    """
    candidates: list[tuple[str, int, str | None]] = [
        ("send_to", 2, "target_type"),
        ("send_to", 2, "chat_type"),
        ("send_message_to", 2, "target_type"),
        ("send_msg", 2, "target_type"),
        ("send_message", 2, "target_type"),
        ("send_message", 2, None),
        ("send_msg", 2, None),
    ]

    for method_name, npos, kw_type in candidates:
        method = getattr(chat_window, method_name, None)
        if method is None or not callable(method):
            continue
        try:
            sig = inspect.signature(method)
        except (TypeError, ValueError):
            continue

        picked = _pick_call(method, sig, kw_type, target_type, npos)
        if picked is None:
            continue

        def make(picked=picked, method=method, sig=sig):
            def call(target: str, text: str) -> Any:
                return picked((target, text))

            call.__name__ = f"{method.__name__}{sig}"     # type: ignore[attr-defined]
            return call

        return make()

    available = [n for n in dir(chat_window)
                 if not n.startswith("_") and callable(getattr(chat_window, n, None))]
    raise SendError(
        "在 wx4py 的 chat_window 上找不到可用的发消息方法。"
        f"该对象可用的方法有：{available}"
    )


def _resolve_file_callable(chat_window: Any, target_type: str) -> Callable[[str, str, str], Any]:
    """找出「给某个群发一个本地文件」该调哪个方法。

    和 _resolve_send_callable 一样：**前两个参数按位置传**（目标、路径），
    类型/附言这类可选参数才用关键字，且传之前先确认签名里有它。
    这样 wx4py 把 file_path 改叫 filepath、target 改叫 who 都不影响。

    附言参数的候选名：message / caption / text。找不到就只发文件本身。
    """
    # (方法名, 类型参数候选, 附言参数候选)
    candidates: list[tuple[str, str | None, tuple[str, ...]]] = [
        ("send_file_to", "target_type", ("message", "caption", "text")),
        ("send_file_to", "chat_type", ("message", "caption", "text")),
        ("send_file", "target_type", ("message", "caption", "text")),
        ("send_file", None, ("message", "caption", "text")),
        ("send_files", None, ("message", "caption", "text")),
    ]

    for method_name, kw_type, msg_names in candidates:
        method = getattr(chat_window, method_name, None)
        if method is None or not callable(method):
            continue
        try:
            sig = inspect.signature(method)
        except (TypeError, ValueError):
            continue

        params = sig.parameters
        # 找一个真实存在的附言参数名
        msg_name = None
        for cand in msg_names:
            p = params.get(cand)
            if p is not None and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
                msg_name = cand
                break

        def make(method=method, sig=sig, kw_type=kw_type, msg_name=msg_name):
            def call(target: str, path: str, caption: str = "") -> Any:
                pos = [target, path]
                kw = {}
                if kw_type and kw_type in sig.parameters:
                    kw[kw_type] = target_type
                if msg_name and caption:
                    kw[msg_name] = caption
                return method(*pos, **kw)

            call.__name__ = f"{method.__name__}{sig}"      # type: ignore[attr-defined]
            return call

        picked = make()
        # 位置参数个数校验：至少要能接两个（目标 + 路径）
        positional_params = [
            p for p in params.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == p.VAR_POSITIONAL for p in params.values())
        if not has_varargs and len(positional_params) < 2:
            continue
        return picked

    available = [n for n in dir(chat_window) if not n.startswith("_") and callable(getattr(chat_window, n, None))]
    raise SendError(
        "在 wx4py 的 chat_window 上找不到可用的发文件方法。"
        f"该对象可用的方法有：{available}"
    )


def selfcheck_wx4py() -> int:
    """不连接微信、不碰键鼠的纯静态自检：只检查接口形状是否对得上。

    真实收发验证请用 --probe-wx（会连微信并给文件传输助手发一条消息）。
    """
    print("== wx4py 接口自检（不会连接微信）==")
    try:
        import wx4py  # type: ignore
    except ImportError:
        print("[x] 没装 wx4py。先执行：pip install -r requirements.txt")
        return 2

    print(f"[+] 模块已安装：{getattr(wx4py, '__version__', '未知版本')}")

    problems: list[str] = []

    client_cls = getattr(wx4py, "WeChatClient", None)
    if client_cls is None:
        print(f"[x] 没有 WeChatClient。实际导出：{sorted(n for n in dir(wx4py) if not n.startswith('_'))}")
        return 2
    print("[+] WeChatClient 存在")

    for attr in ("connect", "chat_window", "group_manager"):
        if hasattr(client_cls, attr) or attr in getattr(client_cls, "__annotations__", {}):
            print(f"[+] WeChatClient.{attr} 存在")
        else:
            # 实例属性查不到，只要不是硬性缺失就先记下来
            print(f"[?] WeChatClient.{attr} 在类上查不到（可能是实例属性，运行时才知道）")

    # 检查转发/监听相关导出（本工具不依赖，仅作信息）
    for name in ("process_groups", "ForwardRuleHandler", "GroupForwardRule", "CallbackHandler"):
        print(f"[i] wx4py.{name}: {'有' if hasattr(wx4py, name) else '无'}")

    if problems:
        for p in problems:
            print(f"[x] {p}")
        return 4
    print("[✓] 静态接口自检通过。下一步执行 python -m qq2wx --probe-wx 做真实发送验证。")
    return 0


def _com_initialize() -> None:
    """在调用线程里初始化 COM。

    必须做，否则 UIAutomation 会报「尚未调用 CoInitialize」，
    wx4py 整个用不了。踩坑记录：
        --probe-wx 是在主线程跑的，主线程本来就有 COM，所以一直没暴露问题；
        但正式运行时发送跑在 wechat-sender 工作线程里，那个线程没有 COM，
        于是连接微信必然失败。这是个只在真实运行路径上才会出现的 bug。

    CoInitializeEx 是幂等的（同线程重复调用返回 S_FALSE / RPC_E_CHANGED_MODE），
    两种返回值都不影响后续使用。
    """
    try:
        import ctypes

        COINIT_APARTMENTTHREADED = 0x2
        hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        # 0 = S_OK，1 = S_FALSE（已初始化），负数 = 错误（多为模式冲突，仍可用）
        log.debug("CoInitializeEx -> %s", hr)
    except Exception as exc:  # noqa: BLE001
        log.debug("CoInitializeEx 调用失败（不影响非 UIA 通道）：%s", exc)


# 微信 UIA 树健康阈值：少于这个节点数就别再尝试操作微信了
_UIA_MIN_NODES = 5
# 连着失败这么多次就暂停微信通道，避免反复操作把微信搞崩
_UIA_FAILURE_LIMIT = 3


def wechat_uia_healthy() -> tuple[bool, str]:
    """检查微信 UIA 树是否可用。

    判定策略（两层，避免误判）：

      1. **主路径**：`find_wechat_window()` 返回的句柄上，根节点类名以
         ``mmui::`` 开头 —— 最强信号（微信只在无障碍接口真的可用时才把根
         节点暴露成 mmui::MainWindow / mmui::LoginWindow）。
      2. **兜底**：如果那个句柄上的树没铺开（只剩 QWidget 外壳），再从
         **UIA 桌面根节点**找微信窗口重试。这步很关键：
         `find_wechat_window()` 是按"类名+标题+进程名"打分选的，可能选中
         一个树没铺开的辅助窗口，从而把"可用"误判成"不可用"，导致明明能发
         却拒绝发送。

    为什么要这个守卫（2026-10-09 实测事故）：
        微信 4.1.8.28 装好后 UIA 树有 50 个节点，能正常发消息；但窗口被
        wx4py 的 _minimize_window() 收进托盘后，控件树塌成 1 个节点，
        而且恢复窗口也不会自己长回来。
        在那种状态下每调一次 send_file_to()，wx4py 都会走一遍
        "打开聊天 → 失败 → 恢复窗口 → 最小化窗口" 的流程，反复折腾微信
        的渲染。所以：树不健康时直接跳过，不要一遍遍去戳它。
    """
    def _probe(hwnd: int) -> tuple[bool, str, int]:
        """检查某个句柄。返回 (可用, 说明, 节点总数)。"""
        try:
            from wx4py.core import uiautomation as uia

            root = uia.ControlFromHandle(hwnd)
        except Exception:  # noqa: BLE001
            return False, "", 0
        if not root:
            return False, "", 0

        cls = str(getattr(root, "ClassName", "") or "")
        if cls.startswith("mmui::"):
            return True, f"根节点即 {cls}", 0      # 最强信号，无需再数节点

        mmui, total = mmui_node_count(hwnd)
        if mmui > 0:
            return True, f"树内有 {mmui} 个 mmui 控件", total
        return False, f"根节点是 {cls or '未知'}，无 mmui 控件", total

    def _from_uia_root() -> int:
        """从 UIA 桌面根节点找微信窗口句柄。找不到返回 0。"""
        try:
            from wx4py.core import uiautomation as uia

            for app in uia.GetRootControl().GetChildren():
                nm = str(getattr(app, "Name", "") or "")
                cl = str(getattr(app, "ClassName", "") or "")
                if "微信" in nm or "Weixin" in nm or cl.startswith("mmui::"):
                    h = int(getattr(app, "NativeWindowHandle", 0) or 0)
                    if h:
                        return h
        except Exception:  # noqa: BLE001
            pass
        return 0

    try:
        from wx4py.core.win32 import find_wechat_window
    except Exception as exc:  # noqa: BLE001
        return True, f"无法检查（{exc}），按可用处理"

    try:
        hwnd = find_wechat_window()
    except Exception as exc:  # noqa: BLE001
        return True, f"查找窗口失败（{exc}），按可用处理"

    total_seen = 0
    if hwnd:
        ok, why, total = _probe(hwnd)
        if ok:
            return True, why
        total_seen = total

    # 主句柄不行 → 从 UIA 根节点兜底
    alt = _from_uia_root()
    if alt and alt != hwnd:
        ok2, why2, _ = _probe(alt)
        if ok2:
            return True, f"{why2}（UIA 根节点找到的窗口 HWND={alt}）"

    if not hwnd and not alt:
        return False, "找不到微信主窗口"

    if total_seen >= _UIA_MIN_NODES:
        return True, f"节点数 {total_seen}（根节点类名不标准，按可用处理）"

    return False, (
        f"微信 UIA 控件树只有 {total_seen} 个节点（需要 ≥{_UIA_MIN_NODES}）。"
        "微信可能被最小化过或需要重启。"
    )


def ensure_wechat_window_visible() -> tuple[bool, str]:
    """确保微信主窗口是可见的。

    为什么需要这一步（实测踩坑记录）：
      wx4py 自带的窗口恢复是"点击托盘图标"，在微信 4.x 上无效，日志表现为
      "检测到微信窗口不可见，但未能通过托盘图标恢复，已跳过窗口兜底激活"，
      之后所有操作都会失败（找不到搜索框）。
      微信 4.x 把主窗口收进托盘时，窗口并没有最小化，只是 IsWindowVisible=False，
      直接调 Win32 的 ShowWindow(SW_RESTORE) 就能拉出来，稳定有效。

    返回 (是否可见, 说明)。只调用 Win32，不点击、不输入、不发消息。
    """
    try:
        import win32con
        import win32gui
    except ImportError:
        return True, "没装 pywin32，跳过窗口可见性检查"

    try:
        from wx4py.core.win32 import find_wechat_window
    except Exception as exc:  # noqa: BLE001
        return True, f"无法导入 wx4py 的窗口查找（{exc}），跳过检查"

    try:
        hwnd = find_wechat_window()
    except Exception as exc:  # noqa: BLE001
        return True, f"查找微信窗口失败：{exc}"

    if not hwnd:
        return False, "没找到微信窗口，请确认微信正在运行"

    if win32gui.IsWindowVisible(hwnd):
        return True, "微信窗口本来就是可见的"

    for sw, name in ((win32con.SW_RESTORE, "SW_RESTORE"), (win32con.SW_SHOW, "SW_SHOW")):
        try:
            win32gui.ShowWindow(hwnd, sw)
        except Exception:  # noqa: BLE001
            continue
        time.sleep(0.6)
        if win32gui.IsWindowVisible(hwnd):
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
            return True, f"微信窗口原本收在托盘里，已用 {name} 恢复"

    return False, "微信窗口不可见，且自动恢复失败。请手动点开微信窗口。"


def wechat_uia_node_count(hwnd: int, max_depth: int = 4, limit: int = 20) -> int:
    """统计微信主窗口的 UIA 控件树节点数。返回 -1 表示读取失败。"""
    try:
        from wx4py.core import uiautomation as uia

        root = uia.ControlFromHandle(hwnd)
    except Exception:  # noqa: BLE001
        return -1
    if not root:
        return -1

    count = 0
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count >= limit:
            return count
        if depth >= max_depth:
            continue
        try:
            for ch in node.GetChildren():
                stack.append((ch, depth + 1))
        except Exception:
            pass
    return count


UIA_EMPTY_HINT = """
微信没有暴露辅助功能接口（UIA 控件树是空的，只有窗口外壳）。
这会导致所有微信操作失败：找不到搜索框、发不出消息。

原因：注册表 HKCU\\SOFTWARE\\Microsoft\\Narrator\\NoRoam\\RunningState
      必须在【微信启动之前】就是 1，微信才会构建控件树。
      你的微信是在这个值生效之前启动的。

修复（唯一有效的一步）：
  1. 完全退出微信 —— 右下角托盘图标上【右键 → 退出】
     （点窗口右上角的 X 只是收进托盘，不算退出）
  2. 重新打开微信，等它完全登录、显示出聊天列表
  3. 重新运行本程序

自检命令：  .venv\\Scripts\\python.exe tools\\wx_health.py
""".strip()


MAIN_TITLES = ("微信", "Weixin", "WeChat")
# 主界面上真实存在的控件关键词。命中说明 UIA 树是活的。
UI_HINTS = (
    "搜索", "Search", "聊天", "通讯录", "收藏", "朋友圈", "看一看", "搜一搜",
    "文件传输助手", "订阅号", "微信团队", "设置",
)


def _find_main_window() -> tuple[int, str, int, int]:
    """找微信窗口，返回 (hwnd, title, w, h)。找不到返回 (0, "", 0, 0)。

    实现上**直接复用 wx4py 的 find_wechat_window()**，不再自己按尺寸筛。

    为什么改（2026-10-09 踩坑）：
        早先我自己 EnumWindows，用"标题匹配 + 宽>=600/高>=400"挑主窗口。
        但微信登录窗口只有 294x387（还随 DPI 缩放变化），宽度直接被筛掉，
        体检于是报出"没找到主窗口。微信在运行吗？"这种误导性结论 ——
        明明窗口就在那儿，UIA 树也是好好的（28 个节点）。
        尺寸阈值本质不可靠：用户手动把窗口缩小也会触发同样的问题。
        wx4py 的查找是按"进程名 + 类名 + 标题"打分，比尺寸稳得多。
    """
    try:
        from wx4py.core.win32 import find_wechat_window

        hwnd = find_wechat_window() or 0
    except Exception:  # noqa: BLE001
        hwnd = 0

    if not hwnd:
        return 0, "", 0, 0
    try:
        import win32gui

        title = win32gui.GetWindowText(hwnd) or ""
        rect = win32gui.GetWindowRect(hwnd)
        return hwnd, title, rect[2] - rect[0], rect[3] - rect[1]
    except Exception:  # noqa: BLE001
        return hwnd, "", 0, 0


def _collect_uia_names(hwnd: int, max_depth: int = 6, limit: int = 80) -> list[str]:
    try:
        from wx4py.core import uiautomation as uia

        root = uia.ControlFromHandle(hwnd)
    except Exception:  # noqa: BLE001
        return []
    if not root:
        return []

    out: list[str] = []
    stack = [(root, 0)]
    while stack and len(out) < limit:
        node, depth = stack.pop()
        try:
            name = str(getattr(node, "Name", "") or "").strip()
            ctype = str(getattr(node, "ControlTypeName", "") or "").strip()
            out.append(name or (f"<{ctype}>" if ctype else ""))
        except Exception:  # noqa: BLE001
            pass
        if depth >= max_depth:
            continue
        try:
            for child in node.GetChildren():
                stack.append((child, depth + 1))
        except Exception:  # noqa: BLE001
            pass
    return [n for n in out if n]


def run_wx_health() -> int:
    """微信接口体检。只调 ShowWindow，不点击、不输入、不发消息。"""
    print("=" * 84)
    print("微信自动化接口健康检查")
    print("=" * 84)

    print("\n[1/4] 注册表 RunningState（微信启动时据此决定是否暴露 UIA 树）")
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Narrator\NoRoam",
            0,
            winreg.KEY_READ,
        )
        try:
            val, _ = winreg.QueryValueEx(key, "RunningState")
        finally:
            winreg.CloseKey(key)
        print(f"      RunningState = {val}" + ("  ✓ 已就绪" if val == 1 else "  ✗ 应为 1"))
    except FileNotFoundError:
        print("      RunningState 不存在  ✗（跑一次 --probe-wx 会自动创建）")
    except Exception as exc:  # noqa: BLE001
        print(f"      读取失败：{exc}")

    print("\n[2/4] 查找微信主窗口")
    hwnd, title, w, h = _find_main_window()
    if not hwnd:
        print("      没找到主窗口。微信在运行吗？")
        return 1
    print(f"      HWND={hwnd}  标题=«{title}»  尺寸={w}x{h}")
    print(f"      可见 = {'是' if __import__('win32gui').IsWindowVisible(hwnd) else '否'}")

    print("\n[3/4] 确保窗口可见")
    visible, why = ensure_wechat_window_visible()
    print(f"      {why}" + ("  ✓" if visible else "  ✗"))

    print("\n[4/4] 检查 UIA 控件树")
    names = _collect_uia_names(hwnd)
    hits = [n for n in names if any(hint in n for hint in UI_HINTS)]
    print(f"      节点数 = {len(names)}   主界面特征命中 = {len(hits)}")
    if names:
        print(f"      内容预览：{', '.join(names[:16])}")

    print("\n" + "=" * 84)
    if len(names) >= 5 and hits:
        print("✓ 微信已准备好，可以被自动化了。")
        print("  下一步：python -m qq2wx --probe-wx")
        return 0
    if len(names) >= 5:
        print("△ UIA 树有内容，但没认出主界面特征。可能微信还没加载完，稍等再试。")
        return 3
    print("✗ UIA 控件树是空的 —— 微信没有暴露辅助功能接口。")
    print()
    print(UIA_EMPTY_HINT)
    print("=" * 84)
    return 2


def run_wx_gate() -> int:
    """热激活微信的 Qt 无障碍开关，并报告是否真的让 UIA 树可用。

    只写微信进程内存里的 1 个字节；写入前后都有校验，失败自动回滚。
    """
    from . import wxgate

    print("=" * 84)
    print("微信 UIA 无障碍开关（Qt accessibility gate）热激活")
    print("=" * 84)
    print("\n说明：微信 4.1.x 界面自绘，默认不给 UIAutomation 暴露内部控件。")
    print("      这个开关是 Weixin.dll 里的一个字节，需要在运行中的进程内存里写成 1。")
    print("      注册表设置和重启微信都改不了它。\n")

    print("[1/2] 激活前状态")
    ok, why = wxgate.gate_health()
    print(f"      {'可用' if ok else '不可用'}：{why}\n")

    print("[2/2] 执行热激活")
    res = wxgate.prepare_wechat()
    print(res.summary())

    print("\n" + "=" * 84)
    if res.ok:
        final_ok, final_why = wxgate.gate_health()
        # gate 生效 ≠ 能用。必须确认树里真的长出了**内部**控件。
        # 实测本机：根节点是 mmui::MainWindow，但内部只有一个空的
        # MMUIRenderSubWindowHW —— 这种情况下 wx4py 依然找不到搜索框。
        hwnd = wxgate.find_wechat_hwnd()
        mmui, total = wxgate.mmui_node_count(hwnd) if hwnd else (0, 0)
        print(f"gate 状态 : {final_why}")
        print(f"节点统计 : mmui {mmui} 个 / 总 {total} 个")

        if final_ok and total > 3 and mmui >= 2:
            print("\n✓ UIA 树已可用，可以进行微信操作。")
            print("  下一步：python -m qq2wx --probe-wx")
            return 0

        print("\n✗ gate 写入成功，但 UIA 控件树内部仍然是空的 —— 微信操作依然不可用。")
        _print_gate_limitation()
        return 3

    print("✗ 激活失败。")
    _print_gate_limitation()
    return 2


def _print_gate_limitation() -> None:
    print("""
已知情况（本机微信 4.1.13.65 实测）：
  写入 gate 后根节点确实变成了 mmui::MainWindow，但控件树内部
  始终只有 MMUIRenderSubWindowHW 一个空节点 —— 搜索框、输入框都不存在。

  也就是说：**这个微信版本无法用界面自动化驱动。**
  想发到普通微信群，只能把微信降到 wx4py 实测过的 4.1.8.29。

  更省事的替代方案：改用 wechat.driver = "webhook" 发到企业微信群。
  详见项目里的「微信侧方案说明.md」。""")


def probe_wx4py() -> int:
    """自检：连一次微信，打印可用的发送方法，并给文件传输助手发一条测试消息。"""
    print("== wx4py 自检 ==")
    try:
        import wx4py  # type: ignore
    except ImportError:
        print("[x] 没装 wx4py。先执行：pip install wx4py")
        return 2

    print(f"[+] wx4py 已安装：{getattr(wx4py, '__version__', '未知版本')}")
    try:
        client_cls = wx4py.WeChatClient
    except AttributeError:
        print(f"[x] 模块里没有 WeChatClient，实际导出：{dir(wx4py)}")
        return 2

    client = client_cls()
    try:
        if hasattr(client, "connect"):
            ok = client.connect()
            print(f"[+] connect() -> {ok}")
            if ok is False:
                print("[x] 连接失败：确认微信已登录，且窗口没有被最小化")
                return 3

        chat_window = client.chat_window
        methods = [
            n for n in dir(chat_window) if not n.startswith("_") and callable(getattr(chat_window, n, None))
        ]
        print(f"[+] chat_window 可用方法：{methods}")

        # 关键检查：UIA 控件树是否为空。为空的话后面必然找不到搜索框。
        hwnd = 0
        try:
            from wx4py.core.win32 import find_wechat_window

            hwnd = find_wechat_window() or 0
        except Exception:  # noqa: BLE001
            pass
        if hwnd:
            nodes = wechat_uia_node_count(hwnd)
            print(f"[i] 微信主窗口 HWND={hwnd}，UIA 控件树节点数 = {nodes}")
            if 0 <= nodes < 5:
                print()
                print(UIA_EMPTY_HINT)
                return 6

        try:
            call = _resolve_send_callable(chat_window, "contact")
            print(f"[+] 选定发送接口：{call.__name__}")
        except SendError as exc:
            print(f"[x] {exc}")
            return 4

        target = "文件传输助手"
        print(f"[+] 尝试给「{target}」发一条测试消息…")
        result = call(target, "qq2wx-relay 自检消息，收到即表示微信通道可用")
        print(f"[+] 发送返回：{result!r}")
        print("[✓] 微信通道自检通过" if result is not False else "[x] 发送返回 False")
        return 0 if result is not False else 5
    finally:
        try:
            if hasattr(client, "disconnect"):
                client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def build_sender(cfg: AppConfig) -> Sender:
    driver = cfg.wechat.driver
    if driver == "wx4py":
        return Wx4pySender(cfg)
    if driver == "webhook":
        return WebhookSender(cfg.wechat.webhook)
    if driver == "push":
        from .push import PushSender

        return PushSender(cfg.wechat.push)
    if driver == "dry-run":
        return DryRunSender("logs/dry-run.log")
    raise SendError(f"未知的 wechat.driver：{driver}")
