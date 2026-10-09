"""一键完成：降级微信到 4.1.8.28 + 彻底封锁自动更新。

为什么需要这个脚本
------------------
微信会在后台自动升级，把 4.1.8.28 覆盖成新版；而新版（4.1.13+）不向
Windows 暴露 UIA 控件树，会导致 QQ→微信 转发完全失效。
2026-10-09 实测被升级过一次（4.1.8.28 目录直接被删），所以必须封锁。

封锁手段（三层，都不需要改注册表策略）
------------------------------------
1. 把每个版本目录下的 WeixinUpdate.exe 替换成空的只读文件
   —— 这是微信执行升级的入口程序
2. 对这两个文件 icacls /deny (X)，拒绝执行
3. 把备份的原始文件留成 .orig，随时可还原

用法（需要管理员权限）
--------------------
    python tools\\fix_wechat_version.py --install     # 静默装 4.1.8.28 并封锁
    python tools\\fix_wechat_version.py --block       # 只封锁更新（不装）
    python tools\\fix_wechat_version.py --status      # 只看状态
    python tools\\fix_wechat_version.py --unblock     # 解除封锁
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = Path(r"D:\wechat-4.1.8.28\WeChatWin_4.1.8.28.exe")
# 微信会装到这两个目录之一，两个都要处理
WECHAT_DIRS = [Path(r"D:\wechat418\Weixin"), Path(r"D:\wechat\Weixin")]
TARGET_VERSION = "4.1.8.28"


# ------------------------------------------------------------------ 基础

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="gbk", errors="replace", **kw)


def wechat_pids() -> list[int]:
    out = run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"]).stdout or ""
    pids = []
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("weixin.exe"):
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def versions_in(base: Path) -> list[str]:
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and p.name[:1].isdigit())


# ------------------------------------------------------------------ 封锁

def block_updater(version_dir: Path, verbose: bool = True) -> list[str]:
    """把某个版本目录里的更新程序废掉。返回操作说明。"""
    notes: list[str] = []
    upd = version_dir / "WeixinUpdate.exe"
    bak = version_dir / "WeixinUpdate.exe.orig"

    if not upd.exists():
        # 可能已经封锁过（原文件被换名了）
        if bak.exists():
            notes.append(f"{version_dir.name}: 已封锁过（存在 .orig 备份）")
        else:
            notes.append(f"{version_dir.name}: 没有 WeixinUpdate.exe，跳过")
        return notes

    size = upd.stat().st_size

    # 1. 备份原文件
    if not bak.exists():
        try:
            shutil.copy2(upd, bak)
            notes.append(f"{version_dir.name}: 已备份原更新程序（{size} bytes）")
        except OSError as exc:
            notes.append(f"{version_dir.name}: 备份失败 {exc}")
            return notes
    else:
        notes.append(f"{version_dir.name}: 备份已存在，保留")

    # 2. 去只读 -> 置空 -> 设只读
    try:
        os.chmod(upd, 0o666)
    except OSError:
        pass
    try:
        upd.write_bytes(b"")
    except OSError as exc:
        notes.append(f"{version_dir.name}: 置空失败 {exc}（可能被占用或权限不足）")
        return notes
    try:
        os.chmod(upd, 0o444)
    except OSError:
        pass

    # 3. 拒绝执行
    user = os.environ.get("USERNAME", "")
    if user:
        r = run(["icacls", str(upd), "/deny", f"{user}:(X)"])
        if r.returncode == 0:
            notes.append(f"{version_dir.name}: 已拒绝执行权限")
        else:
            notes.append(f"{version_dir.name}: icacls 失败 -> {(r.stderr or r.stdout).strip()[:90]}")

    notes.append(f"{version_dir.name}: 更新程序已废（0 bytes）")
    return notes


def unblock_updater(version_dir: Path) -> list[str]:
    notes: list[str] = []
    upd = version_dir / "WeixinUpdate.exe"
    bak = version_dir / "WeixinUpdate.exe.orig"
    if not bak.exists():
        notes.append(f"{version_dir.name}: 没有 .orig 备份，无法还原")
        return notes
    user = os.environ.get("USERNAME", "")
    if user and upd.exists():
        run(["icacls", str(upd), "/remove:d", user])
    try:
        if upd.exists():
            os.chmod(upd, 0o666)
            upd.unlink()
        shutil.copy2(bak, upd)
        notes.append(f"{version_dir.name}: 已还原原始更新程序")
    except OSError as exc:
        notes.append(f"{version_dir.name}: 还原失败 {exc}")
    return notes


# ------------------------------------------------------------------ 状态

def show_status() -> None:
    print("=" * 78)
    print("微信版本与更新封锁状态")
    print("=" * 78)
    print(f"管理员权限: {'是' if is_admin() else '否（安装/封锁需要管理员）'}")
    print()

    for base in WECHAT_DIRS:
        print(f"【{base}】")
        if not base.exists():
            print("  目录不存在")
            print()
            continue
        vs = versions_in(base)
        if not vs:
            print("  没有任何版本目录")
        for v in vs:
            vd = base / v
            upd = vd / "WeixinUpdate.exe"
            bak = vd / "WeixinUpdate.exe.orig"
            if upd.exists():
                sz = upd.stat().st_size
                state = "已废（0字节）✓" if sz == 0 else f"原始（{sz} bytes）✗ 未封锁"
            else:
                state = "文件不存在"
            print(f"  版本 {v:<14} 更新程序: {state}"
                  + ("  [有备份]" if bak.exists() else ""))
        print()

    # 正在跑的进程（Win11 已移除 wmic，改用 PowerShell CIM）
    pids = wechat_pids()
    print(f"正在运行的 Weixin.exe: {len(pids)} 个")
    if pids:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='Weixin.exe'\" | "
            "Select-Object ProcessId,ExecutablePath | "
            "ForEach-Object { \"$($_.ProcessId)=$($_.ExecutablePath)\" }"
        )
        r = run(["powershell", "-NoProfile", "-Command", ps])
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if "=" in line and line.split("=")[0].isdigit():
                pid, _, path = line.partition("=")
                print(f"  PID {pid}: {path}")
    print()

    # 更新暂存
    upd_stage = Path(os.environ["APPDATA"]) / "Tencent" / "xwechat" / "update"
    if upd_stage.exists():
        print(f"更新暂存目录: {upd_stage}")
        for sub in ("download", "patch"):
            p = upd_stage / sub
            if p.exists():
                files = list(p.rglob("*"))
                print(f"  {sub}/: {len(files)} 项")
    print()


# ------------------------------------------------------------------ 安装

def install_41828() -> int:
    if not INSTALLER.exists():
        print(f"[x] 找不到安装包：{INSTALLER}")
        return 2
    if not is_admin():
        print("[x] 需要管理员权限才能静默安装")
        return 3

    # 先关掉微信
    pids = wechat_pids()
    if pids:
        print(f"[1/4] 结束 {len(pids)} 个微信进程…")
        run(["taskkill", "/F", "/IM", "Weixin.exe"])
        for _ in range(20):
            time.sleep(0.5)
            if not wechat_pids():
                break
        if wechat_pids():
            print("      [x] 微信仍未退出，请手动退出后重试")
            return 4
        print("      已全部退出")
    else:
        print("[1/4] 微信未运行，直接继续")

    print(f"[2/4] 静默安装 4.1.8.28 …（约 1-2 分钟）")
    t0 = time.time()
    r = run([str(INSTALLER), "/S"], timeout=600)
    print(f"      安装器退出码 {r.returncode}，耗时 {time.time()-t0:.0f}s")

    # NSIS /S 不等待；轮询等目标版本目录出现
    print("[3/4] 等待安装完成…")
    found = None
    for _ in range(60):
        time.sleep(2)
        for base in WECHAT_DIRS:
            if TARGET_VERSION in versions_in(base):
                found = base / TARGET_VERSION
                break
        if found:
            break

    if not found:
        print(f"      [x] 没等到 {TARGET_VERSION} 目录出现。安装可能失败或装到了别处。")
        print("          当前状态：")
        for base in WECHAT_DIRS:
            print(f"            {base}: {versions_in(base)}")
        return 5
    print(f"      已安装：{found}")

    print("[4/4] 封锁自动更新…")
    for note in block_updater(found):
        print(f"      {note}")

    print()
    print("=" * 78)
    print("完成。请注意：")
    print("  1. 现在打开微信，应该会要求重新登录")
    print("  2. 登录后跑体检：.venv\\Scripts\\python.exe -m qq2wx --wx-health")
    print("  3. 看到「✓ 微信已经准备好」就说明可以用了")
    print("=" * 78)
    return 0


def block_all() -> int:
    if not is_admin():
        print("[!] 非管理员运行：能读不能改的话会失败。建议用管理员权限。")
    total = 0
    for base in WECHAT_DIRS:
        for v in versions_in(base):
            for note in block_updater(base / v):
                print(f"  {note}")
                total += 1
    if total == 0:
        print("  没有任何版本目录需要处理")
    return 0


def unblock_all() -> int:
    for base in WECHAT_DIRS:
        for v in versions_in(base):
            for note in unblock_updater(base / v):
                print(f"  {note}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="微信降级 + 封锁自动更新")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true", help="静默安装 4.1.8.28 并封锁更新")
    g.add_argument("--block", action="store_true", help="只封锁更新（不安装）")
    g.add_argument("--unblock", action="store_true", help="解除封锁，还原原始更新程序")
    g.add_argument("--status", action="store_true", help="只看状态")
    args = ap.parse_args()

    if args.install:
        return install_41828()
    if args.block:
        return block_all()
    if args.unblock:
        return unblock_all()
    show_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
