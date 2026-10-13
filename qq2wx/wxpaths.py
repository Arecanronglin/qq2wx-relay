# -*- coding: utf-8 -*-
"""微信安装位置与安装包的自动发现。

为什么需要这个模块
------------------
早期版本把 `D:\\wechat418\\Weixin` 这种路径**写死在代码里**，
换台机器就得改源码。现在改成运行时发现：

1. 环境变量 `WECHAT_DIRS` / `WECHAT_INSTALLER`（用 `;` 分隔）—— 优先级最高
2. 注册表里腾讯登记的安装路径
3. 扫描各盘符下名字含 `wechat` / `weixin` 的目录（只看两层，不会很慢）

自己发现不准的时候，用命令行参数覆盖即可，例如：

    python tools\\fix_wechat_version.py --wechat-dir "E:\\MyWeChat\\Weixin"
    python tools\\fix_wechat_version.py --installer "D:\\dl\\WeChatWin_4.1.8.28.exe"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")

# 应用主程序名（微信 4.x 是 Weixin.exe，3.x 是 WeChat.exe）
EXE_NAMES = ("Weixin.exe", "WeChat.exe")

# 安装包常见文件名
INSTALLER_GLOBS = ("WeChatWin*.exe", "Weixin*.exe", "WeChat*.exe")

# 注册表里可能登记安装路径的位置
_REGISTRY_KEYS = (
    (r"Software\Tencent\Weixin", "InstallPath"),
    (r"Software\Tencent\WeChat", "InstallPath"),
    (r"Software\Tencent\Weixin", "InstallDir"),
    (r"Software\WOW6432Node\Tencent\Weixin", "InstallPath"),
)

_DRIVE_LETTERS = "CDEFGH"


def _has_exe(d: Path) -> bool:
    try:
        return d.is_dir() and any((d / n).is_file() for n in EXE_NAMES)
    except OSError:
        return False


def _from_env() -> list[Path]:
    raw = os.environ.get("WECHAT_DIRS", "")
    return [Path(p.strip()) for p in raw.split(";") if p.strip()]


def _from_registry() -> list[Path]:
    if not IS_WINDOWS:
        return []
    out: list[Path] = []
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return out
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for subkey, value in _REGISTRY_KEYS:
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    p, _ = winreg.QueryValueEx(k, value)
                if p:
                    cand = Path(str(p))
                    # 登记的可能就是安装目录，也可能是它的上一级
                    for c in (cand, cand / "Weixin", cand.parent):
                        if _has_exe(c) and c not in out:
                            out.append(c)
            except OSError:
                continue
    return out


def _scan_drives() -> list[Path]:
    """扫各盘符下名字含 wechat/weixin 的目录，看里面有没有主程序。

    只往下看两层（`D:\\wechat418\\Weixin`、`D:\\wechat\\Weixin` 这种结构），
    所以不会遍历整块盘。
    """
    out: list[Path] = []
    for letter in _DRIVE_LETTERS:
        root = Path(f"{letter}:\\")
        if not root.exists():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            name = child.name.lower()
            if "wechat" not in name and "weixin" not in name:
                continue
            if _has_exe(child):
                out.append(child)
                continue
            try:
                for sub in child.iterdir():
                    if _has_exe(sub):
                        out.append(sub)
            except OSError:
                continue
    return out


def default_wechat_dirs() -> list[Path]:
    """返回所有可能装有微信客户端的目录（去重、保序）。"""
    found: list[Path] = []
    for group in (_from_env(), _from_registry(), _scan_drives()):
        for p in group:
            if p not in found:
                found.append(p)
    return found


def find_installer() -> Path | None:
    """找一个微信安装包（WeChatWin*.exe）。找不到返回 None。"""
    env = os.environ.get("WECHAT_INSTALLER", "").strip()
    if env and Path(env).is_file():
        return Path(env)

    roots: list[Path] = [Path(__file__).resolve().parent.parent]
    roots += [Path.home() / "Downloads", Path.cwd()]

    # 安装包一般和版本目录放在一起，顺手用同一批目录找
    for d in default_wechat_dirs():
        roots += [d, d.parent]

    for letter in _DRIVE_LETTERS:
        roots.append(Path(f"{letter}:\\"))

    seen: set[Path] = set()
    for root in roots:
        if root in seen or not root.exists():
            continue
        seen.add(root)
        for pattern in INSTALLER_GLOBS:
            try:
                for hit in root.glob(pattern):
                    if hit.is_file() and hit.stat().st_size > 10 * 1024 * 1024:
                        return hit          # 太小的肯定不是安装包
            except OSError:
                continue
    return None


def describe(dirs: list[Path]) -> str:
    """给人看的摘要。"""
    if not dirs:
        return "（没找到任何微信安装目录）"
    return "\n".join(f"  - {d}" for d in dirs)


if __name__ == "__main__":  # 手动排查用：python -m qq2wx.wxpaths
    print("微信安装目录：")
    print(describe(default_wechat_dirs()))
    print("安装包：", find_installer() or "（没找到）")
