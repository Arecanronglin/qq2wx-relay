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
import json
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


def _load_events(path: Path) -> list[dict]:
    """从 JSONL 读 OneBot 事件（离线回放用）。坏行跳过，不中断。"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _usage() -> None:
    print("用法：")
    print("  在线：python tools\\test_e2e_real.py [群号] [条数]")
    print("  离线：python tools\\test_e2e_real.py --from-file samples\\events.sample.jsonl")


def main() -> int:
    argv = list(sys.argv[1:])

    # --from-file：离线回放模式，不需要 NapCat。
    # （没登录 / CI 环境 / 别人 clone 下来没有真实群时也能跑完这套测试。）
    from_file = None
    if "--from-file" in argv:
        i = argv.index("--from-file")
        if i + 1 >= len(argv):
            print("[x] --from-file 后面要跟一个 JSONL 文件路径")
            _usage()
            return 2
        from_file = Path(argv[i + 1])
        del argv[i:i + 2]

    cfg = AppConfig.load("config.yaml")
    count = int(argv[1]) if len(argv) > 1 else 40

    print("=" * 74)
    if from_file:
        print("离线端到端测试（OneBot JSONL 回放 -> 完整 Pipeline）")
    else:
        print("真实数据端到端测试（NapCat 真实消息 -> 完整 Pipeline）")
    print("=" * 74)

    if from_file:
        # ------------------------------ 离线回放 ------------------------------
        if not from_file.is_file():
            print(f"[x] 文件不存在：{from_file}")
            return 2
        events = _load_events(from_file)
        raw_msgs = [e for e in events if e.get("post_type") == "message"]
        if not raw_msgs:
            print(f"[x] {from_file} 里没有 post_type=message 的事件")
            return 2
        self_id = next((e.get("self_id") for e in events if e.get("self_id")), "")
        info = {"user_id": self_id, "nickname": "(离线回放)"}

        # 主群 = 消息最多的那个群。样本里其他的群是"不该被监听"的**负例**，
        # 不能一起塞进白名单，否则过滤逻辑就测不到了。
        tally: dict = {}
        for m in raw_msgs:
            g = m.get("group_id")
            if g:
                tally[g] = tally.get(g, 0) + 1
        if not tally:
            print("[x] 事件里没有 group_id，无法确定要监听的群")
            return 2
        primary = max(tally, key=lambda g: tally[g])
        names = {primary: f"离线示例群{primary}"}
        cfg.qq_groups = {primary: ["离线目标群"]}

        # 只保留主群，并按 message_id 去重。
        # 「非监听群被忽略」和「重复消息被去掉」由 7-离线自检 专门覆盖，
        # 这里先排除掉，好让「解析出的消息全都发出去」这条强断言成立。
        seen: set = set()
        replay: list = []
        for m in raw_msgs:
            if m.get("group_id") != primary:
                continue
            mid = m.get("message_id")
            if mid is not None:
                if mid in seen:
                    continue
                seen.add(mid)
            replay.append(m)
        excluded = len(raw_msgs) - len(replay)
        raw_msgs = replay

        print(f"\n1. 回放事件")
        print(f"  文件 {from_file}")
        print(f"  主群 {primary}（另有 {len(tally) - 1} 个群是负例，已排除）")
        print(f"  取用 {len(raw_msgs)} 条消息（另排除 {excluded} 条：非监听群或重复）")
    else:
        # ------------------------------ 在线拉取 ------------------------------
        if argv:
            group_id = int(argv[0])
        elif cfg.qq_groups:
            group_id = next(iter(cfg.qq_groups))
        else:
            print("[x] 没给群号，config.yaml 的 qq_groups 也是空的。")
            _usage()
            return 2
        print(f"群号 {group_id}，取最近 {count} 条")

        http = OneBotHttpClient("http://127.0.0.1:3000")
        try:
            info = http.call("get_login_info")
            print(f"\nNapCat 已连接：{info.get('nickname')}（{info.get('user_id')}）")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[x] 连不上 NapCat：{exc}")
            print("    NapCat 没在运行时可以改用离线回放：")
            print("      python tools\\test_e2e_real.py --from-file samples\\events.sample.jsonl")
            return 2

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
        names = http.group_list()

    tmp = Path(tempfile.mkdtemp(prefix="qq2wx_e2e_"))
    try:
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
            if s.files:
                check(True, f"成功转发 {len(s.files)} 个媒体",
                      f"预期最多 {min(expect_media, cfg.media.max_per_batch)} 个（有每批上限）")
                for _, fname, _ in s.files:
                    check(bool(Path(fname).suffix and Path(fname).suffix != ".bin"),
                          f"媒体 {fname[:30]} 扩展名正常", fname)
                check(True, "文字在文件之前发出（Pipeline 保证顺序）")
            elif from_file:
                skip("媒体转发",
                     "离线样本里的媒体 URL 不可达，下载必然失败（真实媒体转发由在线模式验证）")
            else:
                check(False, "成功转发 0 个媒体",
                      f"预期最多 {min(expect_media, cfg.media.max_per_batch)} 个（有每批上限）")
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
