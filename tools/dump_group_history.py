#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
按时间段采集某个 QQ 群的聊天记录，落盘成可读文档。

用法：
    # 采集今天 18:25 之后的消息
    python tools\\dump_group_history.py --group 123456789 --since "18:25"

    # 增量采集：接着上次停的地方继续（日常用这个）
    python tools\\dump_group_history.py --group 123456789 --since-last

    # 跟配置文件里第一个监听群，采集最近 300 条
    python tools\\dump_group_history.py --count 300

    # 不下载群文件
    python tools\\dump_group_history.py --since "18:25" --no-media

产物（默认落在 notes/group-dump-<群号>-<时间戳>/）：
    messages.jsonl   原始 OneBot 事件，一行一条，可回放
    messages.md      可读文档（含合并转发展开）
    media/           群文件 / 图片附件
    SUMMARY.md       概览：条数、时间范围、发言人、文件清单

依赖 NapCat 在线（HTTP 接口 :3000）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qq2wx.config import AppConfig  # noqa: E402
from qq2wx.qq import OneBotHttpClient  # noqa: E402

MEDIA_TYPES = {"image", "file", "video", "record"}


def iter_media_segments(msgs: list[dict], default_gid: int):
    """遍历**顶层 + 合并转发内部**的所有媒体段。

    产出 (段类型, 段数据, 该用哪个群号取直链, 位置信息 ctx)。

    踩过的坑：一开始只看了顶层 message，结果合并转发里的 PPTX / PDF
    全部漏掉 —— 而那恰恰是最重要的那几个文件。
    """
    def walk(segs, gid, ctx):
        if not isinstance(segs, list):
            return
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            t = str(seg.get("type") or "")
            if t in MEDIA_TYPES:
                yield t, (seg.get("data") or {}), gid, ctx
            elif t == "forward":
                for sub in (seg.get("_forward") or []):
                    # 合并转发的内容来自**别的群**，子消息自带原 group_id。
                    # 附件直链必须用原群号去问，用本群号会取不到（实测踩过）。
                    inner = {
                        "src_group": sub.get("group_id") or gid,
                        "src_sender": sender_of(sub),
                        "src_group_name": sub.get("group_name") or "",
                        "src_time": sub.get("time"),
                        "outer_group": ctx.get("outer_group"),
                        "outer_sender": ctx.get("outer_sender"),
                        "outer_time": ctx.get("outer_time"),
                    }
                    yield from walk(sub.get("message"),
                                    sub.get("group_id") or gid, inner)

    for m in msgs:
        gid = m.get("group_id") or default_gid
        who = sender_of(m)
        when = m.get("time")
        ctx = {
            "src_group": gid, "src_sender": who, "src_time": when,
            # 顶层媒体没有"转发链"，outer 就是它自己
            "outer_group": gid, "outer_sender": who, "outer_time": when,
        }
        yield from walk(m.get("message"), gid, ctx)


def build_media_item(http, seg_type: str, data: dict, gid: int):
    """把消息段变成可下载的 MediaItem。

    返回 (item, 失败原因)。成功时原因是 None。
    群文件的段里**没有 url**，只有 file_id，必须用 OneBot 的
    get_group_file_url 现取直链（直链会过期，所以每次都得现取）。
    """
    from qq2wx.media import MediaItem

    raw_file = str(data.get("file") or "").strip()
    file_id = str(data.get("file_id") or "").strip()
    name = str(data.get("name") or data.get("file_name") or "").strip()

    url = ""
    for key in ("url", "path"):
        val = str(data.get(key) or "").strip()
        if val.startswith(("http://", "https://", "file://")):
            url = val
            break
    if not url and raw_file.startswith(("http://", "https://", "file://")):
        url = raw_file
    if not name and raw_file and not raw_file.startswith(
            ("http://", "https://", "file://")):
        name = raw_file

    if not url and seg_type == "file" and file_id:
        # 短时间内连续取直链会被限流：接口不报错但返回**空直链**。
        # 实测等一会儿再取就好了，所以这里自己重试，别让用户干等。
        for attempt in range(1, 4):
            try:
                res = http.call("get_group_file_url",
                                {"group_id": gid, "file_id": file_id})
                url = str(((res or {}).get("data") or {}).get("url") or "")
            except Exception as exc:  # noqa: BLE001
                return None, f"取直链被拒：{exc}"
            if url:
                break
            if attempt < 3:
                print(f"      （{name or file_id} 直链为空，等 15 秒重试 "
                      f"{attempt}/2）")
                time.sleep(15)
        if not url:
            return None, "接口连续返回空直链（多为限流，或文件已失效）"

    if not url:
        return None, "没有可用的下载地址"

    return MediaItem(kind=seg_type, url=url, filename=name, file_id=file_id), None


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def parse_since(s: str) -> float:
    """把 '18:25' / '2026-10-10 18:25' / '2026-10-10 18:25:30' 解析成时间戳。

    只有时分秒时，默认按**今天**算。
    """
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:                     # 只有时间 → 用今天
            today = datetime.now()
            dt = dt.replace(year=today.year, month=today.month, day=today.day)
        return dt.timestamp()
    raise SystemExit(f"[x] 时间格式看不懂：{s!r}（试试 18:25 或 '2026-10-10 18:25'）")


# --------------------------------------------------------------------------- #
# 拉取
# --------------------------------------------------------------------------- #
def fetch_history(http: OneBotHttpClient, group_id: int, since: float,
                  page_size: int, max_pages: int) -> list[dict]:
    """往后翻页拉群历史，直到早于 since 或拉不动为止。

    NapCat 的 get_group_msg_history 默认返回**最新的一批**，
    传 message_seq（用最旧那条的 message_id）可以继续往前翻。
    """
    collected: list[dict] = []
    seen: set = set()
    seq = None

    for page in range(1, max_pages + 1):
        params: dict = {"group_id": group_id, "count": page_size}
        if seq is not None:
            params["message_seq"] = seq
        try:
            data = http.call("get_group_msg_history", params)
        except Exception as exc:  # noqa: BLE001
            print(f"  [x] 第 {page} 页拉取失败：{exc}")
            break

        msgs = (data or {}).get("messages") if isinstance(data, dict) else data
        if not msgs:
            print(f"  第 {page} 页：没有更多消息了")
            break

        fresh = [m for m in msgs if m.get("message_id") not in seen]
        for m in fresh:
            seen.add(m.get("message_id"))
        collected.extend(fresh)

        times = [int(m.get("time") or 0) for m in fresh]
        oldest = min(times) if times else 0
        newest = max(times) if times else 0
        print(f"  第 {page} 页：{len(fresh)} 条新消息"
              f"（{fmt_time(oldest)} ~ {fmt_time(newest)}）")

        seq = min((m.get("message_id") for m in fresh if m.get("message_id")),
                  default=None)
        if seq is None or oldest <= since:
            break
        time.sleep(0.4)                          # 别把接口打太急

    return collected


def expand_forwards(http: OneBotHttpClient, msgs: list[dict]) -> int:
    """把合并转发（forward 段）展开，塞回 msg['_forward']。返回展开了几条。"""
    done = 0
    for m in msgs:
        segs = m.get("message")
        if not isinstance(segs, list):
            continue
        for seg in segs:
            if seg.get("type") != "forward":
                continue
            fid = (seg.get("data") or {}).get("id")
            if not fid:
                continue
            try:
                res = http.call("get_forward_msg", {"message_id": fid})
            except Exception as exc:  # noqa: BLE001
                seg["_forward_error"] = str(exc)
                continue
            inner = (res or {}).get("messages") if isinstance(res, dict) else res
            seg["_forward"] = inner or []
            done += 1
            print(f"    ↳ 展开一条合并转发：{len(seg['_forward'])} 条子消息")
            time.sleep(0.3)
    return done


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def fmt_time(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "?"


def sender_of(m: dict) -> str:
    s = m.get("sender") or {}
    return s.get("card") or s.get("nickname") or str(s.get("user_id") or "?")


def render_segments(segs, indent: str = "") -> list[str]:
    """把消息段渲染成文本行。"""
    out: list[str] = []
    if not isinstance(segs, list):
        return [f"{indent}{segs}"]
    for seg in segs:
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            for line in str(d.get("text", "")).splitlines() or [""]:
                out.append(f"{indent}{line}")
        elif t == "at":
            out.append(f"{indent}@{d.get('qq')}")
        elif t == "image":
            name = d.get("file") or d.get("filename") or "图片"
            out.append(f"{indent}[图片: {name}]")
        elif t == "file":
            out.append(f"{indent}[文件: {d.get('name') or d.get('file')}]")
        elif t == "video":
            out.append(f"{indent}[视频]")
        elif t == "record":
            out.append(f"{indent}[语音]")
        elif t == "face":
            out.append(f"{indent}[表情]")
        elif t == "forward":
            inner = seg.get("_forward")
            out.append(f"{indent}[合并转发]")
            if seg.get("_forward_error"):
                out.append(f"{indent}  ⚠️ 展开失败：{seg['_forward_error']}")
            elif not inner:
                out.append(f"{indent}  ⚠️ 内容为空（可能已过期）")
            else:
                for sub in inner:
                    sub_sender = sender_of(sub)
                    src = sub.get("group_name") or sub.get("group_id") or ""
                    head = f"{indent}  ┌ [{fmt_time(sub.get('time'))}] {sub_sender}"
                    if src:
                        head += f"  〔来自: {src}〕"
                    out.append(head)
                    for line in render_segments(sub.get("message"), indent + "  │ "):
                        out.append(line)
                out.append(f"{indent}  └ （共 {len(inner)} 条）")
        elif t == "reply":
            out.append(f"{indent}[引用 {d.get('id')}]")
        elif t == "json":
            out.append(f"{indent}[卡片消息]")
        else:
            out.append(f"{indent}[{t}]")
    return out


def render_markdown(group_id: int, group_name: str, msgs: list[dict],
                    since: float) -> str:
    lines = [
        f"# QQ 群聊天记录 · {group_name}（{group_id}）",
        "",
        f"- 采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 筛选条件：{fmt_time(since)} 之后",
        f"- 消息条数：**{len(msgs)}**",
    ]
    if msgs:
        lines.append(f"- 实际范围：{fmt_time(msgs[0].get('time'))} ~ "
                     f"{fmt_time(msgs[-1].get('time'))}")
    lines += ["", "---", ""]

    for m in msgs:
        lines.append(f"### [{fmt_time(m.get('time'))}] {sender_of(m)}")
        lines.append("")
        lines.extend(render_segments(m.get("message")))
        lines.append("")
    return "\n".join(lines) + "\n"


def render_summary(group_id: int, group_name: str, msgs: list[dict],
                   since: float, media_files: list[Path]) -> str:
    senders: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for m in msgs:
        senders[sender_of(m)] = senders.get(sender_of(m), 0) + 1
        for seg in (m.get("message") or []):
            if isinstance(seg, dict):
                k = str(seg.get("type"))
                kinds[k] = kinds.get(k, 0) + 1

    lines = [
        f"# 采集概览 · {group_name}（{group_id}）",
        "",
        f"- 采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 筛选：{fmt_time(since)} 之后",
        f"- 消息条数：{len(msgs)}",
        "",
        "## 发言人排行",
        "",
    ]
    for name, n in sorted(senders.items(), key=lambda x: -x[1]):
        lines.append(f"- {name}：{n} 条")
    lines += ["", "## 消息类型分布", ""]
    for k, n in sorted(kinds.items(), key=lambda x: -x[1]):
        lines.append(f"- {k}：{n} 个")
    lines += ["", "## 已下载的群文件 / 图片", ""]
    if media_files:
        for f in media_files:
            lines.append(f"- `{f.name}`（{f.stat().st_size / 1024:.0f} KB）")
    else:
        lines.append("- （无）")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def fmt_clock(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "?"


def render_missing(group_id: int, group_name: str, missing: list[dict]) -> str:
    """把下不来的附件整理成「去哪下」的坐标说明。"""
    lines = [
        f"# 下不来的附件 · {group_name}（{group_id}）",
        "",
        f"共 **{len(missing)}** 个。机器人取不到直链，但你在 QQ 里点开就能下。",
        "",
    ]
    for i, m in enumerate(missing, 1):
        size = f"{m['size'] / 1024:.0f} KB" if m.get("size") else "大小未知"
        cross = bool(m.get("outer_time") and m.get("src_time")
                     and m.get("outer_group") != m.get("src_group"))
        lines += [
            f"## {i}. {m['name']}",
            "",
            f"- 大小：{size}",
            f"- 取不到的原因：{m.get('reason') or '未知'}",
            "",
            "**去哪下：**",
            "",
        ]
        if cross:
            lines += [
                f"1. 打开群 **{group_id}**（{group_name}）",
                f"2. 找 **{m.get('outer_sender')}** 在 "
                f"**{fmt_clock(m.get('outer_time'))}** 发的那条**合并转发**，点开它",
                f"3. 里面 **{m.get('src_sender')}** 于 "
                f"**{fmt_clock(m.get('src_time'))}** 发的消息带的就是这个文件",
                "",
                f"> 原件在群 **{m.get('src_group')}**"
                + (f"（{m.get('src_group_name')}）" if m.get("src_group_name") else "")
                + "。机器人不在那个群里，QQ 会报「非群成员」或「文件不存在」，"
                  "所以取不到直链；但**你自己在群里点开那条转发就能下**。",
                "",
            ]
        else:
            lines += [
                f"1. 打开群 **{group_id}**（{group_name}）",
                f"2. 找 **{m.get('src_sender')}** 在 "
                f"**{fmt_clock(m.get('src_time'))}** 发的消息",
                "",
            ]
    lines += [
        "---",
        "",
        "> 想以后自动下：把机器人拉进上面提到的来源群，再跑一次采集即可。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", type=int, default=None, help="群号，默认取配置里第一个")
    ap.add_argument("--since", default=None, help="起始时间，如 18:25 或 '2026-10-10 18:25'")
    ap.add_argument("--since-last", action="store_true",
                    help="增量：接着上次采集的位置继续（状态存 notes/collect-state.json）")
    ap.add_argument("--state", default=None, help="增量状态文件路径")
    ap.add_argument("--count", type=int, default=200, help="每页条数，默认 200")
    ap.add_argument("--max-pages", type=int, default=10, help="最多翻几页")
    ap.add_argument("--out", default=None, help="输出目录")
    ap.add_argument("--no-media", action="store_true", help="不下载群文件/图片")
    ap.add_argument("--http", default=None, help="OneBot HTTP 地址，默认取配置")
    args = ap.parse_args()

    cfg = AppConfig.load("config.yaml")
    http = OneBotHttpClient(args.http or cfg.qq.http_url, cfg.qq.access_token)

    # ---- 确认在线
    try:
        info = http.call("get_login_info")
        nick = (info or {}).get("nickname")
        print(f"NapCat 已连接：{nick}（{(info or {}).get('user_id')}）")
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 连不上 NapCat：{exc}")
        print("    先用扫码登录QQ机器人.bat 把 NapCat 拉起来。")
        return 2

    group_id = args.group or next(iter(cfg.qq_groups), None)
    if not group_id:
        print("[x] 没指定 --group，配置里也没有监听群")
        return 2

    group_name = str(group_id)
    try:
        groups = http.group_list() or {}
        # OneBotHttpClient.group_list() 返回 {群号: 群名}；
        # 但别的实现可能返回 [{"group_id":..,"group_name":..}]，两种都认。
        if isinstance(groups, dict):
            group_name = (groups.get(group_id)
                          or groups.get(str(group_id))
                          or group_name)
        else:
            for g in groups:
                if int(g.get("group_id", 0)) == int(group_id):
                    group_name = g.get("group_name") or group_name
                    break
    except Exception as exc:  # noqa: BLE001
        print(f"  [警告] 群名没解析出来（{exc}），文档里就用群号代替")
    print(f"目标群：{group_name}（{group_id}）")

    state_path = Path(args.state) if args.state else ROOT / "notes" / "collect-state.json"
    state: dict = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}

    if args.since:
        since = parse_since(args.since)
        print(f"起始时间：{fmt_time(since)}（命令行指定）")
    elif args.since_last:
        rec = state.get(str(group_id)) or {}
        last = float(rec.get("last_time") or 0)
        if last:
            since = last
            print(f"起始时间：{fmt_time(since)}"
                  f"（上次采集于 {rec.get('last_run', '?')}）")
        else:
            since = time.time() - 86400
            print(f"起始时间：{fmt_time(since)}（没有历史位置，默认最近 24 小时）")
    else:
        since = 0.0
        print("起始时间：不限（拉到的全都算）")

    # ---- 拉取
    print("\n[1/4] 拉取历史消息")
    msgs = fetch_history(http, group_id, since, args.count, args.max_pages)
    # 增量模式下，上次采到的那几条（时间戳与边界同秒）按 message_id 精确跳过，
    # 否则每次都会重复采到边界那条。
    skip_ids: set = set()
    if args.since_last:
        skip_ids = set((state.get(str(group_id)) or {}).get("last_ids") or [])

    msgs = [m for m in msgs if m.get("post_type") == "message"
            and int(m.get("time") or 0) >= since
            and m.get("message_id") not in skip_ids]
    msgs.sort(key=lambda m: int(m.get("time") or 0))
    print(f"  → 符合条件 {len(msgs)} 条")
    if not msgs:
        print("  没有符合时间条件的消息，结束。")
        return 0

    # ---- 展开合并转发
    print("\n[2/4] 展开合并转发消息")
    n_fwd = expand_forwards(http, msgs)
    print(f"  → 展开 {n_fwd} 条")

    # ---- 输出目录
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else ROOT / "notes" / f"group-dump-{group_id}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    # ---- 下载媒体
    media_files: list[Path] = []
    missing: list[dict] = []
    if not args.no_media:
        print("\n[3/4] 下载群文件 / 图片（含合并转发内部的）")
        from qq2wx.media import MediaDownloader

        flt = cfg.filters
        wanted = {
            "image": getattr(flt, "forward_images", True),
            "file": getattr(flt, "forward_files", True),
            "video": getattr(flt, "forward_videos", True),
            "record": False,
        }

        dl = MediaDownloader(cfg, http_call=http.call)
        dl.media_dir = out / "media"
        dl.media_dir.mkdir(parents=True, exist_ok=True)

        done: set = set()
        got_names: set = set()
        for seg_type, data, gid, ctx in iter_media_segments(msgs, group_id):
            if not wanted.get(seg_type, False):
                continue
            keys = []
            if data.get("file_id"):
                keys.append(str(data["file_id"]))
            if data.get("url"):
                keys.append(str(data["url"]))
            if seg_type == "file":
                nm = str(data.get("file") or data.get("name") or "").strip()
                if nm:
                    keys.append("name:" + nm)     # 同文件转发前后各出现一次
            if keys and any(k in done for k in keys):
                continue

            label = str(data.get("file") or data.get("name") or "?")
            item, reason = build_media_item(http, seg_type, data, gid)
            if item is None:
                # 不标记 done：同一个文件可能在顶层和转发里各有一份，
                # 转发那份属于别的群取不到，顶层那份还是能下的。
                missing.append({"name": label, "kind": seg_type,
                                "size": int(data.get("file_size") or 0),
                                "file_id": str(data.get("file_id") or ""),
                                "reason": reason, **ctx})
                print(f"  [x] {label} → {reason}")
                continue

            item = dl.download(item)
            done.update(keys)            # 同一个 id / 同名文件不用再试
            if item.ok:
                media_files.append(Path(item.local_path))
                got_names.add(label)
                print(f"  ↓ {Path(item.local_path).name}"
                      f"  ({item.size / 1024:.0f} KB)")
            else:
                missing.append({"name": label, "kind": seg_type,
                                "size": int(data.get("file_size") or 0),
                                "file_id": str(data.get("file_id") or ""),
                                "reason": f"下载失败：{item.error}", **ctx})
                print(f"  [x] {label} → 下载失败：{item.error}")
            time.sleep(0.3)
        # 同一个文件可能转发那份失败、顶层那份成功。已经拿到手的就不算"下不来"。
        missing = [m for m in missing if m["name"] not in got_names]
        print(f"  → 成功 {len(media_files)} 个"
              + (f"，{len(missing)} 个下不来（坐标写进 MISSING.md）"
                 if missing else ""))
    else:
        print("\n[3/4] 跳过媒体下载（--no-media）")

    # ---- 落盘
    print("\n[4/4] 写文件")
    raw = out / "messages.jsonl"
    with raw.open("w", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    (out / "messages.md").write_text(
        render_markdown(group_id, group_name, msgs, since), encoding="utf-8")
    (out / "SUMMARY.md").write_text(
        render_summary(group_id, group_name, msgs, since, media_files),
        encoding="utf-8")

    # 下不来的附件：写清楚"去哪下"，交给用户自己拿
    if missing:
        (out / "MISSING.md").write_text(
            render_missing(group_id, group_name, missing), encoding="utf-8")

    # 记下增量位置，下次 --since-last 从这里接着走
    if msgs:
        rec = state.setdefault(str(group_id), {})
        rec["group_name"] = group_name
        newest = max(int(m.get("time") or 0) for m in msgs)
        rec["last_time"] = newest
        # 边界同秒的消息 id 也要记住，否则下次会重复采到
        rec["last_ids"] = [m.get("message_id") for m in msgs
                           if int(m.get("time") or 0) == newest]
        rec["last_count"] = len(msgs)
        rec["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"增量位置已记录：{fmt_time(rec['last_time'])}")
        except OSError as exc:
            print(f"[警告] 增量状态没写成功：{exc}")

    print(f"\n完成，产物在：{out}")
    for f in sorted(out.iterdir()):
        if f.is_file():
            print(f"  {f.name}  ({f.stat().st_size / 1024:.1f} KB)")
        else:
            n = len(list(f.iterdir()))
            print(f"  {f.name}/  ({n} 个文件)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
