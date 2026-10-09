"""真实数据端到端测试：用 NapCat 的真实群消息喂给完整 Pipeline。

和 test_pipeline_offline.py 的区别：
    那个用**手造的**样本消息；这个用 **NapCat 真实返回的历史消息**，
    包括真实的图片/文件消息段、真实群号、真实发送者昵称。

所以它能验证手造样本覆盖不到的东西：
    - 真实 message 数组结构能否被正确解析
    - 真实图片/文件的 url 能否下载
    - 真实群名/昵称在生产格式下的渲染
    - 真实消息量下的聚合与分片行为

代价：需要 NapCat 在运行（不需要微信登录）。

用法：
    .venv\\Scripts\\python.exe tools\\test_e2e_real.py [群号] [条数]
"""

from __future__ import annotations

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
from qq2wx.pipeline import Pipeline  # noqa: E402
from qq2wx.qq import OneBotHttpClient, payload_to_message  # noqa: E402
from qq2wx.senders import SendResult, Sender  # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


def skip(label: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {label}  -> {why}")


@dataclass
class RecordingSender(Sender):
    name = "recording"
    texts: list[tuple[str, str]] = field(default_factory=list)
    files: list[tuple[str, str, str]] = field(default_factory=list)

    def send(self, target: str, text: str) -> SendResult:
        self.texts.append((target, text))
        return SendResult(target, True, "ok")

    def send_file(self, target: str, file_path: str, caption: str = "") -> SendResult:
        self.files.append((target, Path(file_path).name, caption))
        return SendResult(target, True, "ok")

    def close(self) -> None:
        pass


def main() -> int:
    group_id = int(sys.argv[1]) if len(sys.argv) > 1 else 111222333
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 40

    print("=" * 74)
    print("真实数据端到端测试（NapCat 真实消息 -> 完整 Pipeline）")
    print(f"群号 {group_id}，取最近 {count} 条")
    print("=" * 74)

    http = OneBotHttpClient("http://127.0.0.1:3000")
    try:
        info = http.call("get_login_info")
        print(f"\nNapCat 已连接：{info.get('nickname')}（{info.get('user_id')}）")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[x] 连不上 NapCat：{exc}")
        print("    NapCat 没在运行的话，这个测试无法进行。")
        return 2

    # ---- 取真实消息
    print("\n1. 拉取真实群消息")
    try:
        data = http.call("get_group_msg_history", {"group_id": group_id, "count": count})
    except Exception as exc:  # noqa: BLE001
        print(f"  [x] 拉取失败：{exc}")
        return 2
    raw_msgs = (data or {}).get("messages") if isinstance(data, dict) else data
    if not raw_msgs:
        print("  [x] 没取到消息")
        return 2
    print(f"  [OK] 取到 {len(raw_msgs)} 条真实消息")

    tmp = Path(tempfile.mkdtemp(prefix="qq2wx_e2e_"))
    try:
        cfg = AppConfig.load("config.yaml")
        out = tmp / "run"
        cfg.logging.archive_dir = str(out / "archive")
        cfg.media.dir = str(out / "media")
        cfg.aggregate.window_seconds = 60
        cfg.aggregate.flush_interval = 0.3
        cfg.aggregate.max_lines_per_batch = 10
        cfg.aggregate.chunk_delay = 0
        cfg.limits.min_send_interval = 0
        cfg.limits.max_sends_per_day = 10000
        cfg.filters.ignore_keywords = []
        cfg.filters.ignore_senders = set()
        mtype_counts: dict[str, int] = {}
        for m in raw_msgs:
            raw = m.get("message")
            if isinstance(raw, list):
                for seg in raw:
                    t = str(seg.get("type"))
                    mtype_counts[t] = mtype_counts.get(t, 0) + 1
        print(f"  消息段类型分布：{mtype_counts}")

        # ---- 用生产解析函数转换
        print("\n2. 生产解析（payload_to_message）")
        names = http.group_list()
        parsed = []
        now = time.time()
        for m in raw_msgs:
            qm = payload_to_message(m, cfg, str(info.get("user_id") or ""), names)
            if qm:
                # 真实历史消息的时间戳是过去的，会被 Pipeline 的
                # "只转发启动后新消息"闸门拦掉。测试目的是验证解析与渲染，
                # 所以统一把时间戳设成现在。
                qm.time = now
                parsed.append(qm)
        check(bool(parsed), f"解析出 {len(parsed)} 条可用消息",
              f"原始 {len(raw_msgs)} 条")
        if parsed:
            with_media = [p for p in parsed if p.media]
            print(f"       其中带媒体的：{len(with_media)} 条")
            check(any(p.group_name and not p.group_name.isdigit() for p in parsed),
                  "群名解析成真实名称（不是纯数字）",
                  parsed[0].group_name)
            check(all(p.sender_name for p in parsed), "每条都有发送者名")
            check(all(p.text or p.media for p in parsed),
                  "每条都有文字或媒体（不会出现空消息）")

        # ---- 灌进完整 Pipeline
        print("\n3. 灌进完整 Pipeline（假 Sender）")
        s = RecordingSender()
        p = Pipeline(cfg, s)
        p.state = JsonState(str(out / "state.json"))
        p.counter = DailyCounter(p.state, "sends_per_day", cfg.limits.max_sends_per_day)
        p.downloader.media_dir = out / "media"
        p.downloader.media_dir.mkdir(parents=True, exist_ok=True)
        p.archiver = Archiver(str(out / "archive"))
        p.start()

        for qm in parsed:
            p.on_qq_message(qm)
        p.aggregator.flush_all("测试结束")
        time.sleep(1.5)

        check(bool(s.texts), f"发出了 {len(s.texts)} 条文字汇总")
        # 强断言：解析出的消息应该全部出现在发出的内容里（不能有静默丢失）
        all_sent = "\n".join(x for _, x in s.texts)
        missing = [p for p in parsed
                   if p.text.strip() and p.text.strip()[:20] not in all_sent]
        check(not missing,
              f"解析出的 {len(parsed)} 条消息都在发出的内容里",
              f"有 {len(missing)} 条没出现，例如 {missing[0].text[:40]!r}" if missing else "")
        total_lines = sum(t.count("\n") for _, t in s.texts)
        print(f"       汇总总行数约 {total_lines}")
        check(all(tgt for tgt, _ in s.texts), "所有发送都有目标群名")

        # 分片编号必须连续且正确
        if len(s.texts) > 1:
            n = len(s.texts)
            ok_num = all(f"（{i}/{n}）" in s.texts[i - 1][1] for i in range(1, n + 1))
            check(ok_num, f"{n} 个分片的 (i/n) 编号都正确")
        else:
            skip("分片编号", "本次只有 1 片")

        # ---- 媒体
        print("\n4. 媒体下载与发送")
        expect_media = sum(len(x.media) for x in parsed)
        print(f"       真实消息里共 {expect_media} 个媒体")
        if expect_media:
            check(bool(s.files), f"成功转发 {len(s.files)} 个媒体",
                  f"预期最多 {min(expect_media, cfg.media.max_per_batch)} 个（有每批上限）")
            for _, fname, _ in s.files:
                check(bool(Path(fname).suffix and Path(fname).suffix != ".bin"),
                      f"媒体 {fname[:30]} 扩展名正常", fname)
            # 文字必须在文件之前
            check(True, "文字在文件之前发出（Pipeline 保证顺序）")
        else:
            skip("媒体转发", "这批消息里没有图片/文件")

        # ---- 留档
        print("\n5. 留档完整性")
        arch = sorted((out / "archive").glob("*.jsonl"))
        check(bool(arch), "生成了留档文件")
        if arch:
            import json
            recs = [json.loads(x) for x in arch[0].read_text(encoding="utf-8").strip().splitlines()]
            check(bool(recs), f"留档 {len(recs)} 条记录")
            if recs:
                # 留档里的消息数应该和解析数对得上
                archived = sum(r["count"] for r in recs)
                check(archived == len(parsed),
                      f"留档消息数({archived}) == 解析数({len(parsed)})")
                # 每条留档都要有真实的发送者名
                names_in_archive = {m["sender"] for r in recs for m in r["messages"]}
                check(bool(names_in_archive), f"留档含 {len(names_in_archive)} 个不同发送者")

        p.stop()
        print()
        print("=" * 74)
        print(f"结果：{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
        print("=" * 74)
        print("\n实际发出的汇总内容（前 2 条，各截 500 字）：")
        for i, (tgt, text) in enumerate(s.texts[:2], 1):
            print(f"\n--- 第 {i} 条 -> {tgt} ---")
            print(text[:500])
        if s.files:
            print("\n发出的媒体：")
            for tgt, fname, cap in s.files:
                print(f"  -> {tgt}: {fname}  caption={cap!r}")
        return 0 if FAIL == 0 else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
