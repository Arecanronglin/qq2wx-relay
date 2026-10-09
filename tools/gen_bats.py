"""生成用户双击用的 .bat 启动器。

设计原则（踩过坑之后定下来的）：
  1. .bat 文件本身**只允许 ASCII 字符**。
     cmd.exe 在中文 Windows 上是 GBK(936) 码页。.bat 里如果出现中文，
     存 UTF-8 会被 cmd 按字节切坏（报 "'xx' 不是内部或外部命令"），
     存 GBK 又会在别的工具/编辑器里显示成乱码。干脆不放中文。
  2. 所有中文提示由 Python 打印。
     Python 侧已经有 harden_console_encoding()，输出中文完全没问题，
     实测显示正常。
  3. 启动器用 run.py 而不是 qq2wx\\__main__.py。
     直接跑包内 __main__.py 会 "attempted relative import with no known
     parent package"；run.py 会把项目根塞进 sys.path。
  4. 在 .bat 里设置 PYTHONUTF8=1 和 PYTHONIOENCODING=utf-8。
     项目路径含中文（新建文件夹），不设的话 Python 拿到的 argv 会乱码。

生成的 .bat 统一写成 GBK + CRLF：GBK 是 cmd 的原生编码，CRLF 最保险。
ASCII 内容在 GBK 下与 ASCII 完全一致，所以这一步只是为了稳妥。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BAT_ENCODING = "gbk"

BANNER = "=" * 60

# (生成的 .bat 文件名, 传给程序的参数, 启动器自己的英文说明行)
LAUNCHERS = [
    (
        "1-校验配置.bat",
        "--check",
        [
            "qq2wx-relay - config check",
            "No connections are made. Safe to run anytime.",
        ],
    ),
    (
        "2-测试QQ链路.bat",
        "--driver dry-run",
        [
            "Stage 1: QQ group  ->  relay  (dry-run)",
            "Nothing is sent to WeChat; output is printed here only.",
            "",
            "NapCat must already be running and QQ logged in.",
            "Ask ANOTHER QQ account to send messages in the group.",
            "Wait up to 60 seconds for the summary to appear.",
            "",
            "To stop: press Ctrl+C",
        ],
    ),
    (
        "3-自检微信.bat",
        "--probe-wx",
        [
            "WeChat channel self-test",
            "",
            "Connects to WeChat and sends ONE test message to",
            "the File Transfer Helper. No group is disturbed.",
            "",
            "!! From now on, DO NOT touch the mouse or keyboard !!",
            "It simulates clicks on the WeChat window.",
        ],
    ),
    (
        "4-正式运行.bat",
        "--live",
        [
            "LIVE RUN:  QQ groups  ->  WeChat groups",
            "",
            "Before running, make sure:",
            "  1. NapCat is running and QQ is logged in",
            "  2. WeChat is logged in and its window is NOT minimized",
            "  3. config.yaml has  driver: wx4py",
            "",
            "Avoid using the mouse/keyboard on the WeChat window.",
            "",
            "To stop: press Ctrl+C",
        ],
    ),
    (
        "0-微信体检.bat",
        "--wx-health",
        [
            "WeChat automation health check",
            "",
            "Checks the registry flag, the main window, its visibility,",
            "and whether the UIA control tree is actually exposed.",
            "",
            "Safe: only ShowWindow is called. No clicks, no typing,",
            "no messages sent, no memory writes.",
            "",
            "Run this FIRST whenever the relay stops working.",
        ],
    ),
    (
        "5-推送自检.bat",
        "--probe-push",
        [
            "Push channel self-test (send to your PHONE)",
            "",
            "Sends one test message using the push settings in config.yaml.",
            "This does NOT touch the WeChat desktop client at all.",
            "",
            "Setup (3 minutes, one time):",
            "  1. Scan the QR at https://www.pushplus.plus/ with WeChat",
            "  2. Copy your token from the web page",
            "  3. Put it in config.yaml under wechat.push.token",
            "  4. Set wechat.driver to push",
            "",
            "Then run this and check your phone.",
        ],
    ),
    (
        "6-全面诊断.bat",
        "--doctor",
        [
            "Full diagnostic (one-stop health check)",
            "",
            "Checks ALL of these at once and tells you how to fix each:",
            "  - config file, send channel, watched groups",
            "  - NapCat / QQ login, group membership, WebSocket port",
            "  - WeChat version, window, login state, UIA tree",
            "  - whether the target WeChat group name actually exists",
            "",
            "Run this whenever forwarding stops working.",
            "Read-only except for ShowWindow (restores a hidden window).",
        ],
    ),
]
TEMPLATE_BATS = [
    ("tools/tpl_wechat_downgrade.txt", "降级微信并封锁更新.bat"),
]

# 直接跑某个 python 脚本的启动器：(生成的 .bat 名, 脚本相对路径, 说明行)
SCRIPT_BATS = [
    (
        "就绪检查.bat",
        r"tools\ready_check.py",
        [
            "READINESS CHECK - run this first",
            "",
            "Tells you in one glance whether the relay can start, and",
            "exactly what is still missing if it cannot.",
            "",
            "Checks: WeChat version, update blocking, window visibility,",
            "login state, UIA tree, NapCat connection, group membership,",
            "WebSocket port, send channel, forwarding rules.",
            "",
            "Expect:  all green once you have logged in to WeChat.",
        ],
    ),
    (
        "7-离线自检.bat",
        r"tools\test_pipeline_offline.py",
        [
            "Offline self-test of the whole pipeline",
            "",
            "Tests filtering, de-duplication, aggregation, chunking,",
            "rate limiting, media handling and archiving - using a FAKE",
            "sender. It does NOT touch WeChat or QQ, so it is safe to run",
            "anytime, even before logging in.",
            "",
            "Expect:  36 passed / 0 failed",
        ],
    ),
    (
        "8-真实数据自检.bat",
        r"tools\test_e2e_real.py",
        [
            "Real-data end-to-end test",
            "",
            "Pulls REAL messages from NapCat (includes real image/file",
            "segments) and runs them through the whole pipeline with a",
            "fake sender. Downloads the real media too.",
            "",
            "Needs NapCat running. Does NOT need WeChat login.",
            "Expect:  about 18 passed / 0 failed",
        ],
    ),
    (
        "9-接口绑定自检.bat",
        r"tools\test_send_binding.py",
        [
            "Send-API binding check (static, sends nothing)",
            "",
            "Verifies the parameters we pass to wx4py really match its",
            "method signatures - catches the kind of TypeError that would",
            "otherwise only appear on the first real send.",
            "",
            "Also checks fallback handling for older wxauto-style names",
            "(send_msg / send_files taking who/msg).",
            "",
            "Expect:  30 passed / 0 failed",
        ],
    ),
]


def build_script_launcher(script: str, lines: list[str]) -> str:
    """生成"跑某个独立脚本"的启动器（不走 run.py，直接执行脚本）。"""
    out = [
        "@echo off",
        "rem ----------------------------------------------",
        "rem  Auto-generated script launcher. ASCII only, by design.",
        "rem  Regenerate with:  python tools\\gen_bats.py",
        "rem ----------------------------------------------",
        "chcp 65001 >nul 2>&1",
        'set "PY=%~dp0.venv\\Scripts\\python.exe"',
        'set "PYTHONUTF8=1"',
        'set "PYTHONIOENCODING=utf-8"',
        "echo " + BANNER,
    ]
    for line in lines:
        if not line.strip():
            out.append("echo.")
            continue
        safe = line.replace("^", "^^")
        for ch in (">", "<", "|", "&"):
            safe = safe.replace(ch, "^" + ch)
        out.append(f"echo {safe}".rstrip())
    out.append("echo " + BANNER)
    out.append("echo.")
    out.append("if not exist \"%PY%\" (")
    out.append("    echo [ERROR] Python venv not found: %PY%")
    out.append("    pause")
    out.append("    exit /b 2")
    out.append(")")
    out.append(f'"%PY%" "%~dp0{script}" %*')
    out.append('set "RC=%ERRORLEVEL%"')
    out.append("echo.")
    out.append("echo Exit code: %RC%   (0 = all passed)")
    out.append("pause")
    out.append("exit /b %RC%")
    out.append("")
    return "\r\n".join(out)


def build_launcher(args: str, lines: list[str], interactive: bool = True) -> str:
    out = [
        "@echo off",
        "rem ----------------------------------------------",
        "rem  Auto-generated launcher. ASCII only, by design.",
        "rem  Chinese UI text is printed by Python instead.",
        "rem  Regenerate with:  python tools\\gen_bats.py",
        "rem ----------------------------------------------",
        "rem Switch console to UTF-8 so it matches Python's output encoding.",
        "rem Safe here because this file is pure ASCII.",
        "chcp 65001 >nul 2>&1",
        "echo " + BANNER,
    ]
    for line in lines:
        if not line.strip():
            # 空行必须用 "echo." 形式，否则 cmd 会打印 "ECHO is off."
            out.append("echo.")
            continue
        # 关键：echo 后面的 '>' '<' '|' '&' 会被 cmd 当成重定向/管道，
        # 结果不是打印文字，而是悄悄生成一个文件（踩过：横幅里的 "->"
        # 生成了名为 relay 和 WeChat 的垃圾文件）。必须转义。
        safe = line.replace("^", "^^")
        for ch in (">", "<", "|", "&"):
            safe = safe.replace(ch, "^" + ch)
        out.append(f"echo {safe}".rstrip())
    out.append("echo " + BANNER)
    out.append("echo.")
    if interactive:
        out.append("pause")
    out.append(f'call "%~dp0_runpy.bat" {args}'.rstrip())
    out.append('set "RC=%ERRORLEVEL%"')
    out.append("echo.")
    out.append("echo Exit code: %RC%")
    if interactive:
        out.append("pause")
    out.append("exit /b %RC%")
    out.append("")
    return "\r\n".join(out)


def build_runpy() -> str:
    """共用的 Python 调用包装：检查环境 + 设置 UTF-8。"""
    return "\r\n".join(
        [
            "@echo off",
            "rem ============================================================",
            "rem  Shared helper: check environment, then run qq2wx via run.py",
            "rem  Usage from other .bat files:  call \"%~dp0_runpy.bat\" [args]",
            "rem",
            "rem  Keep this file ASCII-only. cmd runs on code page 936 here,",
            "rem  so non-ASCII bytes in a .bat get mis-parsed.",
            "rem ============================================================",
            "setlocal",
            "set \"PY=%~dp0.venv\\Scripts\\python.exe\"",
            "set \"ENTRY=%~dp0run.py\"",
            "rem Force UTF-8 for stdio AND for decoding the command line, so the",
            "rem Chinese folder name in this path survives.",
            "set \"PYTHONUTF8=1\"",
            "set \"PYTHONIOENCODING=utf-8\"",
            "if not exist \"%PY%\" goto :noenv",
            "if not exist \"%ENTRY%\" goto :noentry",
            "\"%PY%\" \"%ENTRY%\" %*",
            "set \"RC=%ERRORLEVEL%\"",
            "endlocal & exit /b %RC%",
            "",
            ":noenv",
            "echo.",
            "echo [ERROR] Python venv not found:",
            "echo         %PY%",
            "echo.",
            "echo The \".venv\" folder is missing. Run these once in this folder:",
            "echo         python -m venv .venv",
            "echo         .venv\\Scripts\\python.exe -m pip install -r requirements.txt",
            "echo.",
            "endlocal & exit /b 2",
            "",
            ":noentry",
            "echo.",
            "echo [ERROR] Cannot find run.py under:",
            "echo         %~dp0",
            "echo         Make sure you run this from the qq2wx-relay folder.",
            "echo.",
            "endlocal & exit /b 2",
            "",
        ]
    )


def main() -> int:
    ok = True

    for name, args, lines in LAUNCHERS:
        text = build_launcher(args, lines)
        ok = _write(name, text) and ok

    # 从 .txt 模板直接生成的 .bat（内容已经是完整批处理）
    for tpl_rel, out_name in TEMPLATE_BATS:
        tpl = ROOT / tpl_rel
        if not tpl.exists():
            print(f"[warn] 模板不存在：{tpl_rel}")
            ok = False
            continue
        ok = _write(out_name, tpl.read_text(encoding="utf-8")) and ok

    # 直接执行某个脚本的启动器
    for out_name, script, lines in SCRIPT_BATS:
        if not (ROOT / script).exists():
            print(f"[warn] 脚本不存在：{script}")
            ok = False
            continue
        ok = _write(out_name, build_script_launcher(script, lines)) and ok

    ok = _write("_runpy.bat", build_runpy()) and ok

    # 清掉不再需要的临时/中间文件
    for junk in ("_tpl_check.bat", "_tpl_qq.bat", "_tpl_wx.bat", "_tpl_run.bat",
                 "_runpython.bat", "1-check.bat"):
        p = ROOT / junk
        if p.exists():
            p.unlink()
            print(f"[del] {junk}")

    if ok:
        print("\n完成。所有 .bat 都是纯 ASCII，中文提示由 Python 打印。")
    return 0 if ok else 1


def _write(name: str, text: str) -> bool:
    if not text.isascii():
        bad = [c for c in text if not c.isascii()][:5]
        print(f"[error] {name} 里出现了非 ASCII 字符：{bad}")
        return False
    try:
        data = text.encode(BAT_ENCODING)
    except UnicodeEncodeError as exc:
        print(f"[error] {name} 编码失败：{exc}")
        return False
    (ROOT / name).write_bytes(data)
    print(f"[ok] {name}  ({len(data)} bytes)")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
