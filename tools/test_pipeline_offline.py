"""离线集成测试：用假 Sender 跑完整 Pipeline，不碰微信、不连 QQ。

为什么需要它
------------
真实发送必须先登录微信，没法在无人值守时验证。但 Pipeline 里除了"真正
调用微信"那一步，其余全是纯逻辑，完全可以离线测：

    消息进入 -> 过滤 -> 去重 -> 时间窗聚合 -> 渲染 -> 分片
             -> 限流 -> 媒体下载 -> 交给 Sender 发送 -> 留档

用假 Sender 就能把这整条链路跑通并断言结果，提前发现"登录后才会暴露"的
问题（比如分片顺序错、限流算错、媒体和文字顺序颠倒）。

用法：
    .venv\\Scripts\\python.exe tools\\test_pipeline_offline.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qq2wx.aggregate import Archiver  # noqa: E402
from qq2wx.config import AppConfig  # noqa: E402
from qq2wx.core import DailyCounter, JsonState  # noqa: E402
from qq2wx.media import MediaItem  # noqa: E402
from qq2wx.pipeline import Pipeline  # noqa: E402
from qq2wx.qq import QqMessage  # noqa: E402
from qq2wx.senders import SendResult, Sender  # noqa: E402

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


@dataclass
class FakeSender(Sender):
    """记录所有发送调用，不碰任何外部系统。"""

    name = "fake"
    text_calls: list[tuple[str, str]] = field(default_factory=list)
    file_calls: list[tuple[str, str, str]] = field(default_factory=list)
    fail_text: bool = False
    fail_file: bool = False

    def send(self, target: str, text: str) -> SendResult:
        self.text_calls.append((target, text))
        if self.fail_text:
            return SendResult(target, False, "模拟文字发送失败")
        return SendResult(target, True, "ok")

    def send_file(self, target: str, file_path: str, caption: str = "") -> SendResult:
        self.file_calls.append((target, Path(file_path).name, caption))
        if self.fail_file:
            return SendResult(target, False, "模拟文件发送失败")
        return SendResult(target, True, "ok")

    def close(self) -> None:
        pass


def make_cfg(tmp: Path, tag: str = "main") -> AppConfig:
    """构造测试配置。

    注意几个容易踩的点：
      - window_seconds 不能设 0：0 是"实时逐条转发"的语义，会一条一发，
        测不出聚合行为。这里设长一点，改用 flush_all() 主动触发，结果确定。
      - state.json 要重定向：DailyCounter 会把当天计数写进去，多个测试实例
        共用同一个文件会互相污染（第一次跑就踩到了）。
      - archive / media 目录也要隔离，别写进真实 logs/。
    """
    cfg = AppConfig.load("config.yaml")
    out = tmp / tag
    cfg.logging.archive_dir = str(out / "archive")
    cfg.media.dir = str(out / "media")
    cfg.aggregate.flush_interval = 0.3
    cfg.aggregate.window_seconds = 60         # 靠 flush_all() 主动发，结果确定
    cfg.aggregate.max_lines_per_batch = 10
    cfg.aggregate.chunk_delay = 0
    cfg.aggregate.max_line_chars = 200
    cfg.limits.min_send_interval = 0
    cfg.limits.max_sends_per_day = 1000
    cfg.wechat.wx4py["max_chars"] = 1800
    gid = list(cfg.qq_groups.keys())[0]
    cfg.qq_groups = {gid: ["测试微信群"]}
    return cfg


def make_pipeline(cfg: AppConfig, sender: Sender, tmp: Path, tag: str) -> Pipeline:
    """建 Pipeline 并把所有输出重定向到临时目录，避免测试间互相污染。

    注意：Archiver 是在 Pipeline.__init__ 里用当时的 cfg 建的，
    所以必须重新构造它 —— 光改 cfg 不够（踩过：留档写进了别的测试的目录）。
    """
    p = Pipeline(cfg, sender)
    out = tmp / tag
    p.state = JsonState(str(out / "state.json"))
    p.counter = DailyCounter(
        p.state, "sends_per_day", cfg.limits.max_sends_per_day
    )
    p.downloader.media_dir = out / "media"
    p.downloader.media_dir.mkdir(parents=True, exist_ok=True)
    p.archiver = Archiver(str(out / "archive"))
    return p


def msg(gid: int, i: int, text: str, media=None) -> QqMessage:
    # 时间必须用"现在"：Pipeline 有"只转发启动后新消息"的闸门，
    # 用过去的时间戳会被当成历史消息丢弃（这是刻意设计的行为）。
    return QqMessage(
        group_id=gid,
        group_name="测试群",
        sender_id=f"1000{i}",
        sender_name=f"同学{i}",
        text=text,
        time=time.time(),
        message_id=f"mid{i}",
        media=media or [],
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qq2wx_test_"))
    print("=" * 74)
    print("离线集成测试：完整 Pipeline（假 Sender，不碰微信/QQ）")
    print(f"临时目录：{tmp}")
    print("=" * 74)

    try:
        cfg = make_cfg(tmp)
        gid = list(cfg.qq_groups.keys())[0]

        # ---------------------------------------------------------- 1 基础转发
        print("\n1. 基础转发：3 条消息 -> 1 条汇总")
        tag = "t1"
        s = FakeSender()
        p = make_pipeline(cfg, s, tmp, tag)
        p.start()
        for i in range(1, 4):
            p.on_qq_message(msg(gid, i, f"第 {i} 条"))
        p.aggregator.flush_all("测试")
        time.sleep(0.4)
        check(len(s.text_calls) == 1, "只发出 1 条汇总",
              f"实际 {len(s.text_calls)} 条")
        if s.text_calls:
            target, text = s.text_calls[0]
            check(target == "测试微信群", "目标群名正确", target)
            check("第 1 条" in text and "第 3 条" in text, "3 条内容都在", text[:80])
            check("同学1" in text, "带发送者名", text[:80])
            check("[" in text and "]" in text, "带时间戳 [HH:MM]", text[:60])
        p.stop()

        # ---------------------------------------------------------- 2 分片
        print("\n2. 超过 10 条 -> 拆成多条发送")
        tag = "t2"
        s = FakeSender()
        p = make_pipeline(cfg, s, tmp, tag)
        p.start()
        for i in range(1, 24):                     # 23 条 -> 10+10+3
            p.on_qq_message(msg(gid, i, f"第 {i} 条"))
        p.aggregator.flush_all("测试")
        time.sleep(0.5)
        check(len(s.text_calls) == 3, "拆成 3 片", f"实际 {len(s.text_calls)}")
        if len(s.text_calls) == 3:
            t0, t1, t2 = (c[1] for c in s.text_calls)
            check("（1/3）" in t0, "第 1 片带 (1/3)", t0[-20:])
            check("（2/3）" in t1, "第 2 片带 (2/3)", t1[-20:])
            check("（3/3）" in t2, "第 3 片带 (3/3)", t2[-20:])
            check("10 条" in t0 and "10 条" in t1 and "3 条" in t2,
                  "每片条数正确 (10/10/3)",
                  f"{t0.splitlines()[0]} | {t1.splitlines()[0]} | {t2.splitlines()[0]}")
        p.stop()

        # ---------------------------------------------------------- 3 过滤
        print("\n3. 过滤：忽略发送者 / 关键词 / 非监听群")
        tag3 = "t3"
        cfg3 = make_cfg(tmp, tag3)
        cfg3.filters.ignore_keywords = ["广告"]
        cfg3.filters.ignore_senders = {"100099"}
        s = FakeSender()
        p = make_pipeline(cfg3, s, tmp, tag3)
        p.start()
        p.on_qq_message(msg(gid, 1, "正常消息"))
        p.on_qq_message(msg(gid, 2, "这是广告内容"))
        m = msg(gid, 3, "被忽略的人")
        m.sender_id = "100099"
        p.on_qq_message(m)
        p.on_qq_message(msg(999999999, 4, "别的群"))
        p.aggregator.flush_all("测试")
        time.sleep(0.4)
        total = "\n".join(t for _, t in s.text_calls)
        check("正常消息" in total, "正常消息被转发")
        check("广告" not in total, "关键词过滤生效")
        check("被忽略的人" not in total, "发送者黑名单生效")
        check("别的群" not in total, "非监听群被忽略")
        p.stop()

        # ---------------------------------------------------------- 4 去重
        print("\n4. 去重：相同 message_id 只算一次")
        tag = "t4"
        s = FakeSender()
        p = make_pipeline(cfg, s, tmp, tag)
        p.start()
        p.on_qq_message(msg(gid, 1, "唯一消息"))
        p.on_qq_message(msg(gid, 1, "唯一消息"))     # 同 mid1
        p.on_qq_message(msg(gid, 1, "唯一消息"))
        p.aggregator.flush_all("测试")
        time.sleep(0.4)
        body = s.text_calls[0][1] if s.text_calls else ""
        check(body.count("唯一消息") == 1, "重复消息只出现 1 次",
              f"出现 {body.count('唯一消息')} 次")
        check("1 条" in body, "汇总条数是 1", body.splitlines()[0] if body else "")
        p.stop()

        # ---------------------------------------------------------- 5 媒体
        print("\n5. 媒体：下载成功后单独发送，顺序在文字之后")
        tag = "t5"
        # 造一个本地文件当媒体源
        src = tmp / "报告.pdf"
        src.write_bytes(b"%PDF-1.4\n% fake pdf for test\n%%EOF\n")
        s = FakeSender()
        p = make_pipeline(cfg, s, tmp, tag)
        p.start()
        item = MediaItem(kind="file", url=src.as_uri(), filename="报告.pdf")
        p.on_qq_message(msg(gid, 1, "请查收 [文件: 报告.pdf]", media=[item]))
        p.aggregator.flush_all("测试")
        time.sleep(1.0)
        check(len(s.text_calls) == 1, "文字发出 1 条", str(len(s.text_calls)))
        check(len(s.file_calls) == 1, "文件发出 1 个", str(len(s.file_calls)))
        if s.file_calls:
            _, fname, caption = s.file_calls[0]
            check("报告" in fname, "文件名保留", fname)
            check(".pdf" in fname, "扩展名正确（不是 .bin）", fname)
        p.stop()

        # ---------------------------------------------------------- 6 媒体失败
        print("\n6. 媒体下载失败 -> 只发文字，不中断")
        tag = "t6"
        s = FakeSender()
        p = make_pipeline(cfg, s, tmp, tag)
        p.start()
        bad = MediaItem(kind="image", url="http://127.0.0.1:1/nope.png")
        p.on_qq_message(msg(gid, 1, "看这张 [图片]", media=[bad]))
        p.aggregator.flush_all("测试")
        time.sleep(0.8)
        check(len(s.text_calls) == 1, "文字仍然发出", str(len(s.text_calls)))
        check(len(s.file_calls) == 0, "没有文件被发送", str(len(s.file_calls)))
        p.stop()

        # ---------------------------------------------------------- 7 限流
        print("\n7. 限流：达到每日上限后停止发送")
        tag7 = "t7"
        cfg7 = make_cfg(tmp, tag7)
        cfg7.limits.max_sends_per_day = 2
        cfg7.aggregate.max_lines_per_batch = 1     # 每条消息 -> 1 片
        cfg7.logging.archive_dir = str(tmp / tag7 / "archive")
        s = FakeSender()
        p = make_pipeline(cfg7, s, tmp, tag7)
        p.start()
        for i in range(1, 6):
            p.on_qq_message(msg(gid, i, f"第 {i} 条"))
            p.aggregator.flush_all("测试")
            time.sleep(0.12)
        time.sleep(0.5)
        check(len(s.text_calls) == 2, "上限 2 条时只发出 2 条",
              f"实际 {len(s.text_calls)}")
        p.stop()

        # ---------------------------------------------------------- 8 留档
        print("\n8. 留档：JSONL 记录完整")
        arch = sorted((tmp / "t1" / "archive").glob("*.jsonl"))
        check(bool(arch), "生成了留档文件")
        if arch:
            lines = arch[0].read_text(encoding="utf-8").strip().splitlines()
            recs = [json.loads(x) for x in lines]
            check(bool(recs), "留档里有记录", str(len(recs)))
            if recs:
                r0 = recs[0]
                for key in ("time", "group_id", "group_name", "targets",
                            "count", "chunk_count", "chunk_counts", "chunks", "messages"):
                    check(key in r0, f"留档含字段 {key}")
                check(isinstance(r0.get("chunk_counts"), list),
                      "chunk_counts 是列表（每片条数）")
                check(isinstance(r0.get("chunks"), list),
                      "chunks 是列表（真实文本，不是数字）")
        print()
        print("=" * 74)
        print(f"结果：{PASS} 通过 / {FAIL} 失败")
        print("=" * 74)
        return 0 if FAIL == 0 else 1

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
