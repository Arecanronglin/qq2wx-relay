#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
提交前敏感信息扫描器  ——  防止把真实凭据 / 个人信息 / 聊天记录提进仓库。

设计上的一个要点：**这个脚本本身不含任何真实凭据。**
它只用通用特征做启发式判断，所以可以安全地公开。
你本地的真实值清单写在 `notes/secret_patterns.txt`（已被 .gitignore 挡住），
存在的话会被自动加载。

用法：
    python tools/check_secrets.py              # 扫描被 git 跟踪的文件
    python tools/check_secrets.py --staged     # 只扫暂存区（pre-commit 用）
    python tools/check_secrets.py --all        # 不管 git，扫整个工作区

退出码：0 = 干净，1 = 有命中（会阻断提交）。

误报怎么办？在那一行加注释 `# secrets-allow` 或 `<!-- secrets-allow -->`。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Windows 上 python 默认用 GBK 写 stdout，输出里的 emoji / 中文会直接抛
# UnicodeEncodeError 把 pre-commit hook 搞崩。这里强制切到 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".venv", "venv", ".git", "logs", "media", "notes",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "_extract", "node_modules",
}

TEXT_SUFFIXES = {
    ".py", ".mjs", ".js", ".cjs", ".bat", ".cmd", ".ps1", ".sh",
    ".yaml", ".yml", ".json", ".jsonl", ".md", ".txt",
    ".cfg", ".ini", ".toml", ".html", ".css",
}
TEXT_NAMES = {".gitignore", ".gitattributes", "requirements.txt"}

MAX_BYTES = 4 * 1024 * 1024

ALLOW_MARKERS = ("secrets-allow", "check-secrets:ignore")

# --- 通用特征（不含任何真实值）--------------------------------------------
RULES: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "private-key",
        "疑似私钥",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    (
        "long-hex",
        "长十六进制串（可能是 token / 密钥 / 哈希）",
        re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])"),
    ),
    (
        "assigned-secret",
        "赋值给 token/secret/password/key 的长字符串",
        re.compile(
            r"""(?ix)
            \b(token|secret|password|passwd|pwd|api[_-]?key|
               access[_-]?key|webhook[_-]?key|credential)\b
            \s*[:=]\s*
            ["']?
            (?P<val>[A-Za-z0-9_\-+/=]{12,})
            """
        ),
    ),
    (
        "windows-user-path",
        "Windows 绝对路径里的真实用户名",
        re.compile(r"[A-Za-z]:\\Users\\(?!(?:YOUR_USERNAME|<|\$|%|username|user)\b)[^\\\s\"']+"),
    ),
    (
        "posix-home-path",
        "POSIX home 目录里的真实用户名",
        re.compile(r"/(?:home|Users)/(?!(?:user|username|your[_-]?user)\b)[a-z_][a-z0-9_-]{2,}"),
    ),
    (
        "qq-context",
        "上下文里的真实 QQ / 群号",
        re.compile(
            r"(?i)\b(?:qq|uin|bot[_-]?id|group|group[_-]?id|user[_-]?id|self[_-]?id"
            r"|群号|群|机器人QQ)"
            r"\s*[:=「\"]?\s*\"?(\d{6,12})\"?"
        ),
    ),
    (
        "email",
        "邮箱地址",
        re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org)|test\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
]

# 看起来就是占位符的值，放过
PLACEHOLDER = re.compile(
    r"""(?x)
    ^(?:
        YOUR_[A-Z_]+ |
        [A-Z][A-Z0-9_]{4,} |
        <[^>]*> |
        \$\{[^}]*\} |
        %[A-Za-z_]+% |
        x{3,} | X{3,} | \*{3,} |
        changeme | placeholder | your[_-]?token | your[_-]?key |
        none | null | true | false | empty
    )$
    """
)

ID_PLACEHOLDERS = {
    "123456789", "987654321", "111222333", "444555666", "777888999",
    "999999999", "100000000",
}


def load_extra_patterns() -> list[tuple[str, str, re.Pattern[str]]]:
    """从 notes/secret_patterns.txt 加载本地真实值（每行一个，忽略 # 注释）。"""
    f = ROOT / "notes" / "secret_patterns.txt"
    if not f.is_file():
        return []
    out = []
    for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        try:
            out.append(("local-pattern", f"本地清单命中：{s[:24]}…", re.compile(re.escape(s))))
        except re.error:
            continue
    return out


def staged_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [ROOT / p.decode("utf-8", "replace") for p in out.split(b"\x00") if p]


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [ROOT / p.decode("utf-8", "replace") for p in out.split(b"\x00") if p]


def walk_files() -> list[Path]:
    return [p for p in sorted(ROOT.rglob("*")) if p.is_file()]


def candidate(path: Path) -> bool:
    """这个文件值不值得扫（跳过目录、后缀、超大文件、这个脚本自己）。"""
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        return False
    parts = {p.lower() for p in rel.parts[:-1]}
    if parts & SKIP_DIRS:
        return False
    if path.name in {"check_secrets.py"}:
        return False
    if not (path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES):
        return False
    try:
        return path.stat().st_size <= MAX_BYTES
    except OSError:
        return False


def scan(path: Path, rules) -> list[tuple[int, str, str, str]]:
    rel = path.relative_to(ROOT)
    if not candidate(path):
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "\x00" in text[:4096]:
        return []

    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(m in line for m in ALLOW_MARKERS):
            continue
        for rule_id, desc, rx in rules:
            for m in rx.finditer(line):
                val = m.groupdict().get("val") or (m.group(1) if m.groups() else m.group(0))
                val = (val or "").strip().strip("\"'")
                if val and PLACEHOLDER.match(val):
                    continue
                if val in ID_PLACEHOLDERS:
                    continue
                if rule_id == "long-hex" and (
                    PLACEHOLDER.match(val) or set(val) <= set("0")
                ):
                    continue
                findings.append((lineno, rule_id, desc, line.strip()[:120]))
                break
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true", help="只扫暂存区")
    g.add_argument("--all", action="store_true", help="忽略 git，扫整个工作区")
    args = ap.parse_args()

    rules = RULES + load_extra_patterns()

    if args.staged:
        files, scope = staged_files(), "暂存区"
    elif args.all:
        files, scope = walk_files(), "整个工作区"
    else:
        files = tracked_files()
        scope = "已跟踪文件" if files else "整个工作区"
        if not files:
            files = walk_files()

    total = 0
    scanned = 0
    for f in files:
        if not f.is_file() or not candidate(f):
            continue
        scanned += 1
        hits = scan(f, rules)
        if not hits:
            continue
        print(f"\n❌ {f.relative_to(ROOT)}")
        for lineno, rule_id, desc, snippet in hits:
            print(f"   {lineno:>5}  [{rule_id}] {desc}")
            print(f"          {snippet}")
        total += len(hits)

    print()
    if total:
        print(f"🛑 扫到 {total} 处可疑内容（范围：{scope}，扫了 {scanned} 个文件）。提交已阻断。")
        print("   真的是误报，在那一行加 `# secrets-allow` 注释即可放过。")
        print("   要放过整批： git commit --no-verify")
        return 1
    print(f"✅ 没有扫到可疑内容（范围：{scope}，扫了 {scanned} 个文件）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
