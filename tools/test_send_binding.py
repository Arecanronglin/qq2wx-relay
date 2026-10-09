"""验证真实的 wx4py 发送接口能否接受我们传的参数（不实际发送）。

为什么需要这个测试
------------------
之前的离线测试用的是**假函数**（fake_text/fake_file），所以从没验证过
`_resolve_send_callable()` / `_resolve_file_callable()` 选出来的真实 wx4py
方法是否接受我们传的参数名和个数。

如果参数对不上，登录后会直接抛 TypeError，前面所有测试都白跑。

本测试用 `inspect.signature().bind()` 做**纯静态签名校验**：
它只检查"这些参数名能不能绑上"，**不会真的调用方法**，
所以不需要登录、也不会给任何人发消息。

同时覆盖多个 wx4py 版本的方法命名差异（send_to / send_msg /
send_file_to / send_files ...），确保自动探测逻辑选得对。

用法：
    .venv\\Scripts\\python.exe tools\\test_send_binding.py
"""

from __future__ import annotations

import ctypes
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qq2wx.config import AppConfig  # noqa: E402
from qq2wx.senders import _resolve_file_callable, _resolve_send_callable  # noqa: E402

PASS = 0
FAIL = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


class FakeChatWindow:
    """模拟 wx4py 的 chat_window，用于验证参数探测逻辑。

    每个方法都只记录收到的参数，不真的发送。
    """

    def __init__(self) -> None:
        self.log: list[tuple] = []

    # --- 当前 wx4py 0.2.1 的真实签名
    def send_to(self, target: str, message: str, target_type: str = "contact") -> bool:
        self.log.append(("send_to", target, message, target_type))
        return True

    def send_file_to(self, target: str, file_path, target_type: str = "contact",
                     message: str = None) -> bool:
        self.log.append(("send_file_to", target, file_path, target_type, message))
        return True


class OldStyleChatWindow:
    """模拟老版/别版 wxauto 的命名（who/msg/filepath）。"""

    def __init__(self) -> None:
        self.log: list[tuple] = []

    def send_msg(self, who: str, msg: str) -> bool:
        self.log.append(("send_msg", who, msg))
        return True

    def send_files(self, who, filepath) -> bool:
        self.log.append(("send_files", who, filepath))
        return True


def probe_chat_window() -> object | None:
    """拿到真实的 wx4py chat_window（连接微信但不发送）。"""
    try:
        from wx4py import WeChatClient

        c = WeChatClient()
        if hasattr(c, "connect"):
            c.connect()
        return c.chat_window
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] 拿不到真实 chat_window：{exc}")
        return None


def verify_binding(call, args: tuple, label: str) -> None:
    """校验 call(*args) 能否绑定成功（不实际调用）。"""
    target = getattr(call, "__wrapped__", None) or call
    # 我们生成的 call 是闭包，签名是 (*)(target, text[, caption])，
    # 真正要校验的是它内部调用的那个 method。从闭包里取出来。
    method = None
    for cell in (target.__closure__ or ()):
        try:
            val = cell.cell_contents
        except ValueError:
            continue
        nm = getattr(val, "__name__", "")
        if callable(val) and any(
            nm.startswith(m) for m in (
                "send_to", "send_msg", "send_file_to", "send_files",
                "send_message", "send_message_to",
            )
        ):
            method = val
            break

    if method is None:
        # 拿不到原方法就退化成"能不能用我们的关键字调用"
        try:
            inspect.signature(target).bind(*args)
            check(True, f"{label}：闭包签名可绑定")
        except TypeError as exc:
            check(False, f"{label}：闭包签名不可绑定", str(exc))
        return

    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        check(True, f"{label}：{method.__name__} 无法取签名，跳过")
        return

    print(f"        {label} -> 实际方法 {method.__name__}{sig}")
    # 我们实际是以关键字调用的，按关键字绑定才准确
    try:
        bound = sig.bind(*args[:1], **{})
        _ = bound
    except TypeError:
        pass

    # 用真实关键字试绑
    kw = {}
    params = list(sig.parameters)
    for a, p in zip(args[1:], params[1:]):
        kw[p] = a
    try:
        sig.bind(args[0], **kw)
        check(True, f"{label}：参数可绑定到 {method.__name__}")
    except TypeError as exc:
        check(False, f"{label}：参数绑定失败", f"{exc}")


def main() -> int:
    ctypes.windll.ole32.CoInitializeEx(None, 0x2)

    print("=" * 74)
    print("wx4py 发送接口参数绑定校验（静态，不发送任何消息）")
    print("=" * 74)

    cfg = AppConfig.load("config.yaml")
    ttype = cfg.wechat.wx4py_target_type

    # ---------------------------------------------------------- 假对象
    print("\n1. 参数探测逻辑（假 chat_window，验证选方法的规则）")
    fake = FakeChatWindow()
    fn = _resolve_send_callable(fake, ttype)
    check(callable(fn), "探测到发送方法")
    print(f"        选中：{fn.__name__}")
    r = fn("测试群", "测试内容")
    check(r is True, "调用成功")
    check(fake.log and fake.log[0][0] == "send_to", "确实调用了 send_to", str(fake.log))
    if fake.log:
        _, tgt, msg, tt = fake.log[0]
        check(tgt == "测试群", "目标参数名正确（target）", tgt)
        check(msg == "测试内容", "内容参数名正确（message）", msg)
        check(tt == ttype, f"类型参数名正确（target_type={ttype}）", tt)

    ff = _resolve_file_callable(fake, ttype)
    check(callable(ff), "探测到发文件方法")
    print(f"        选中：{ff.__name__}")
    r2 = ff("测试群", r"C:\tmp\x.pdf", "说明文字")
    check(r2 is True, "发文件调用成功")
    fk = [x for x in fake.log if x[0] == "send_file_to"]
    check(bool(fk), "确实调用了 send_file_to", str(fake.log))
    if fk:
        _, tgt, path, tt, caption = fk[-1]
        check(tgt == "测试群", "文件：目标参数名正确", tgt)
        check(path.endswith("x.pdf"), "文件：路径参数名正确", path)
        check(tt == ttype, "文件：类型参数名正确", tt)
        check(caption == "说明文字", "文件：附言参数名正确（message）", caption)

    # 空 caption 不应该传 message
    ff("测试群", r"C:\tmp\y.pdf", "")
    fk2 = [x for x in fake.log if x[0] == "send_file_to"][-1]
    check(fk2[4] is None, "空附言时不传 message（避免发空行）", repr(fk2[4]))

    # ---------------------------------------------------------- 老式命名
    print("\n2. 老式命名兼容（who/msg/filepath）")
    old = OldStyleChatWindow()
    fn2 = _resolve_send_callable(old, ttype)
    print(f"        选中：{fn2.__name__}")
    fn2("目标", "内容")
    check(old.log and old.log[0][0] == "send_msg", "回退到 send_msg", str(old.log))
    ff2 = _resolve_file_callable(old, ttype)
    print(f"        选中：{ff2.__name__}")
    ff2("目标", r"C:\a.pdf")
    check(any(x[0] == "send_files" for x in old.log), "回退到 send_files", str(old.log))

    # ---------------------------------------------------------- 真实 wx4py
    print("\n3. 真实 wx4py 接口（连接但不发送）")
    cw = probe_chat_window()
    if cw is None:
        check(False, "拿不到真实 chat_window")
    else:
        methods = [n for n in dir(cw) if not n.startswith("_")]
        print(f"        chat_window 方法：{methods}")
        real_send = _resolve_send_callable(cw, ttype)
        print(f"        真实发送方法：{real_send.__name__}")
        check(callable(real_send), "真实发送方法可用")
        real_file = _resolve_file_callable(cw, ttype)
        print(f"        真实发文件方法：{real_file.__name__}")
        check(callable(real_file), "真实发文件方法可用")

        # 真实签名里必须包含关键方法名（说明探测选对了）
        check("send_to" in real_send.__name__ or "send_msg" in real_send.__name__,
              "选中的是已知的发送方法", real_send.__name__)
        check("send_file" in real_file.__name__,
              "选中的是已知的发文件方法", real_file.__name__)

        # 用真实签名造探针，确认最终传出的参数正确
        import inspect as _insp

        class Probe:
            """签名与真实 wx4py 完全一致，记录收到的参数。"""

            def __init__(self):
                self.calls = []

            def send_to(self, target: str, message: str,
                        target_type: str = "contact") -> bool:
                self.calls.append(("send_to", target, message, target_type))
                return True

            def send_file_to(self, target: str, file_path,
                             target_type: str = "contact",
                             message: str = None) -> bool:
                self.calls.append(("send_file_to", target, file_path,
                                   target_type, message))
                return True

        probe = Probe()
        # 断言探针签名与真实一致（防止探针和真实脱节）
        # 比较参数名 / 种类 / 默认值 —— 这才是关键字绑定真正依赖的东西。
        # 不比较 str(signature)：wx4py 用了 from __future__ import annotations，
        # 注解会变成字符串形式（'str'），字符串比较会误报不一致。
        def shape(fn):
            out = []
            for nm, p in _insp.signature(fn).parameters.items():
                out.append((nm, p.kind.name, p.default))
            return out

        real_fn = getattr(cw, "send_to", None)
        if real_fn is not None:
            rs, ps = shape(real_fn), shape(probe.send_to)
            check(rs == ps, "探针签名与真实 send_to 形状一致",
                  f"真实{rs} vs 探针{ps}")
            rf, pf_ = shape(getattr(cw, "send_file_to")), shape(probe.send_file_to)
            check(rf == pf_, "探针签名与真实 send_file_to 形状一致",
                  f"真实{rf} vs 探针{pf_}")
        else:
            check(False, "真实 send_to 不存在")

        pf = _resolve_send_callable(probe, ttype)
        pf("目标群", "内容文本")
        check(probe.calls and probe.calls[0][1] == "目标群",
              "真实签名下：目标传对了", str(probe.calls))
        check(probe.calls and probe.calls[0][2] == "内容文本",
              "真实签名下：内容传对了", str(probe.calls))
        check(probe.calls and probe.calls[0][3] == ttype,
              f"真实签名下：类型传对了（{ttype}）", str(probe.calls))

        pff = _resolve_file_callable(probe, ttype)
        pff("目标群", r"C:\tmp\报告.pdf", "这是附言")
        fc = [c for c in probe.calls if c[0] == "send_file_to"]
        check(bool(fc), "真实签名下：文件调用发生了", str(probe.calls))
        if fc:
            check(fc[-1][1] == "目标群", "真实签名下：文件目标传对", str(fc[-1]))
            check(fc[-1][2].endswith("报告.pdf"), "真实签名下：文件路径传对", str(fc[-1]))
            check(fc[-1][3] == ttype, "真实签名下：文件类型传对", str(fc[-1]))
            check(fc[-1][4] == "这是附言", "真实签名下：附言传对", str(fc[-1]))

    print()
    print("=" * 74)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
