"""手机推送通道：QQ 群消息直接推到你手机的微信上。

为什么需要它
------------
桌面微信客户端这条路有三个叠加的坑（2026-10-09 实测）：

1. 微信会**自动升级**，把能用的 4.1.8.28 覆盖成 4.1.13+；
2. 4.1.13+ 不向 Windows 暴露 UIA 控件树，自动化**完全失效**；
3. 就算停在 4.1.8.28，窗口一旦被最小化，控件树也会**塌陷**成 1 个节点，
   恢复窗口不会自己长回来，而且反复操作会把微信搞成白屏崩溃。

推送通道完全绕开这些：不装客户端、不碰窗口、不需要微信在前台，
也就没有自动更新和崩溃的问题。

支持的推送服务
--------------
- **PushPlus**  https://www.pushplus.plus/   微信扫码即可，免费额度够个人用
- **Server酱**  https://sct.ftqq.com/        同样走微信服务号
- **通用 Webhook**  自定义 URL + JSON 模板

三者都只需要一个 token/URL，配置进 config.yaml 即可。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .core import get_logger

log = get_logger("qq2wx.push")


class PushError(RuntimeError):
    pass


# ---------------------------------------------------------------- 响应解析

def _explain_body(raw: str, limit: int = 200) -> str:
    """把响应体压成一行，方便打日志。"""
    text = (raw or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _looks_failed(data: dict) -> str | None:
    """从响应 JSON 里判断是否失败。返回失败原因或 None。

    各家字段名不一样，都认一遍：
      PushPlus:   code (200 成功)
      Server酱:   code (0 成功)
      其它:       errcode / status / success
    """
    if not isinstance(data, dict):
        return None

    for key, ok_values in (
        ("code", (0, 200)),
        ("errcode", (0,)),
        ("status", (0, 200, "ok", "success")),
        ("success", (True, "true", 1)),
    ):
        if key in data:
            val = data[key]
            if val not in ok_values:
                return f"{key}={val} msg={data.get('msg') or data.get('message') or ''}"
            return None
    return None


# ---------------------------------------------------------------- 通道

class PushSender:
    """把消息推送到手机（微信服务号 / 自定义 webhook）。

    不继承 Sender 是有意的：它不是"微信客户端"通道，但接口形状一致，
    所以 Pipeline 可以直接替换使用。
    """

    name = "push"

    def __init__(self, conf: dict) -> None:
        self.kind = str(conf.get("kind") or "pushplus").strip().lower()
        self.token = str(conf.get("token") or "").strip()
        self.url = str(conf.get("url") or "").strip()
        self.title = str(conf.get("title") or "QQ群消息").strip()
        self.timeout = float(conf.get("timeout") or 15)
        self.template = str(conf.get("template") or "").strip()
        self._lock = threading.Lock()
        self._counter = {"ok": 0, "fail": 0}

        if self.kind == "pushplus" and not self.token:
            raise PushError("pushplus 需要填 push.token（在 pushplus.plus 扫码后获取）")
        if self.kind == "serverchan" and not self.token:
            raise PushError("serverchan 需要填 push.token（形如 SCTxxxxx）")
        if self.kind == "webhook" and not self.url:
            raise PushError("webhook 需要填 push.url")

    # ------------------------------------------------------------ 构造请求

    def _build(self, text: str) -> tuple[str, bytes, dict]:
        """返回 (url, body, headers)。"""
        if self.kind == "pushplus":
            payload = {
                "token": self.token,
                "title": self.title,
                "content": text,
                "template": "txt",
            }
            return (
                "https://www.pushplus.plus/send",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                {"Content-Type": "application/json"},
            )

        if self.kind == "serverchan":
            # Server酱支持 GET/POST，POST 表单更稳（内容长时不会被截断）
            form = urllib.parse.urlencode({"title": self.title, "desp": text}).encode()
            return (
                f"https://sctapi.ftqq.com/{self.token}.send",
                form,
                {"Content-Type": "application/x-www-form-urlencoded"},
            )

        if self.kind == "webhook":
            tpl = self.template or '{"title": "{title}", "text": "{text}"}'
            body = tpl.replace("{title}", _json_escape(self.title)) \
                      .replace("{text}", _json_escape(text))
            return (
                self.url,
                body.encode("utf-8"),
                {"Content-Type": "application/json"},
            )

        raise PushError(f"不认识的推送类型：{self.kind}")

    def send(self, target: str, text: str):
        """发送。返回对象形状与 senders.SendResult 一致。"""
        from .senders import SendResult

        with self._lock:
            try:
                url, body, headers = self._build(text)
            except PushError as exc:
                self._counter["fail"] += 1
                return SendResult(target, False, str(exc))

            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    status = resp.status
            except urllib.error.HTTPError as exc:
                self._counter["fail"] += 1
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    pass
                return SendResult(target, False, f"HTTP {exc.code} {_explain_body(detail)}")
            except urllib.error.URLError as exc:
                self._counter["fail"] += 1
                return SendResult(target, False, f"网络错误：{exc.reason}")
            except OSError as exc:
                self._counter["fail"] += 1
                return SendResult(target, False, f"请求失败：{exc}")

            # 解析响应判断业务是否成功
            reason = None
            try:
                data = json.loads(raw or "{}")
                reason = _looks_failed(data)
            except ValueError:
                data = None

            if reason:
                self._counter["fail"] += 1
                return SendResult(target, False, f"接口返回失败：{reason}")
            if status != 200:
                self._counter["fail"] += 1
                return SendResult(target, False, f"HTTP {status} {_explain_body(raw)}")

            self._counter["ok"] += 1
            return SendResult(target, True, _explain_body(raw, 80) or "ok")

    def send_file(self, target: str, file_path: str, caption: str = ""):
        """推送服务只能发文字，附带一条文件名提示。"""
        import os

        from .senders import SendResult

        name = os.path.basename(file_path)
        size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        note = f"（{caption}）" if caption else ""
        result = self.send(target, f"[附件] {name}  {size/1024:.1f} KB{note}")
        # 明确标记为"未真正发送文件"，免得上层以为成功了
        return SendResult(target, False, f"推送通道不能发文件，已发文字提示：{result.detail}")

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return f"push({self.kind})"

    def stats(self) -> dict[str, int]:
        return dict(self._counter)


def _json_escape(s: str) -> str:
    """把字符串转义成能安全嵌进 JSON 模板的形式（不含首尾引号）。"""
    return json.dumps(s, ensure_ascii=False)[1:-1]


# ---------------------------------------------------------------- 自检

def probe_push(conf: dict) -> int:
    """发一条测试推送，验证 token 是否有效。"""
    print("=" * 76)
    print("推送通道自检")
    print("=" * 76)
    try:
        sender = PushSender(conf)
    except PushError as exc:
        print(f"[x] {exc}")
        return 2

    print(f"[+] 类型：{sender.describe()}")
    text = (
        f"qq2wx-relay 推送测试\n"
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"如果你在手机上看到这条消息，说明推送通道已经通了。"
    )
    print("[+] 正在发送测试消息…")
    result = sender.send("test", text)
    if result.ok:
        print(f"[✓] 发送成功：{result.detail}")
        print("    去手机上看一眼，收到就说明配置正确。")
        return 0
    print(f"[x] 发送失败：{result.detail}")
    print()
    print("常见原因：")
    print("  · token 填错或已失效 —— 去 pushplus.plus 重新获取")
    print("  · 免费额度用完（推送服务一般每天有限量）")
    print("  · 没关注对应的微信服务号 —— pushplus 需要微信扫码关注后才有 token")
    return 1
