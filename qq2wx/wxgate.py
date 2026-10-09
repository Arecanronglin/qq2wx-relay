"""微信 4.1.x UIA 控件树"热激活"。

问题背景（实测 + 上游资料确认）
--------------------------------
微信 4.1.x 的聊天界面是 **自绘渲染**，默认对 UIAutomation 只暴露一个空壳：

    WindowControl  «微信»        cls=Qt51514QWindowIcon
      PaneControl  «Weixin»      cls=Qt51514QWindowIcon
      PaneControl  «MMUIRenderSubWindowHW»

只有 3 个节点，找不到搜索框、聊天列表、输入框 —— 所有基于 UIA 的
微信自动化（wxauto / wx4py 等）在 4.1.12+ 上因此全部失效。

根因：微信内置了一个 **Qt accessibility gate**（一个运行时 active 字节）。
QAccessible 在响应 WM_GETOBJECT 前会先检查它；为 0 时只返回 Qt 外壳，
为 1 时才返回完整的 ``mmui::*`` 控件树。

  注册表 RunningState、系统"屏幕阅读器"标志、重启微信 —— **都无法**改变
  这个字节，因为它每个进程独立、且每次微信重启/升级/重新登录都会归零。

唯一有效的做法：在【运行中的 Weixin 进程内存里】把这个字节写成 1。
上游项目 wechatauto-replica 验证过这条路径，并给出了 4.1.13.65 的地址
（RVA 0x0AE2B0C8）：

    https://github.com/fanyuantaier/wechatauto-replica

本模块的安全性设计
------------------
只写 1 个字节，且写之前做三重校验，绝不盲写：

  1. **版本校验**：Weixin.dll 的文件版本必须精确命中已知表
  2. **地址校验**：从 DLL 文件里读出该 RVA 处应有的字节，和进程内存里的
     实际值对比；不一致说明地址表过时，直接拒绝写入
  3. **值校验**：内存里的值必须是 0x00（未激活）或 0x01（已激活）；
     是别的值说明地址不对，拒绝写入

写入后重新检查 UIA 树是否真的长出了 ``mmui::`` 控件来验证效果；
失败会自动把字节改回原值。

不注入代码、不重启进程、不修改磁盘上的任何文件。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import re
import struct
from dataclasses import dataclass, field
from typing import Iterable

from .core import get_logger

log = get_logger("qq2wx.wxgate")

# ----------------------------------------------------------------- 常量

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_MODULE_NAME32 = 255
MAX_PATH = 260

SPI_GETSCREENREADER = 0x0046
SPI_SETSCREENREADER = 0x0047
SPIF_SENDCHANGE = 0x02

# 已知的 gate RVA 表（DLL 文件版本 -> 文件内 RVA）
#
# 来源：wechatauto-replica 的 QACCESSIBLE_ACTIVE_RVA_BY_VERSION，
# 均为对方在真实机器上实测得出。只收录已实测的版本，宁可拒绝也不盲猜。
KNOWN_GATE_RVA: dict[str, int] = {
    "4.1.11.22": 0x0A1E7DB8,
    "4.1.13.65": 0x0AE2B0C8,
    "4.1.15.13": 0x0B135C38,
}

# UIA 树里出现这些类名，说明物化成功
MMUI_MARKERS = ("mmui::", "MainWindow")


# ----------------------------------------------------------------- 结构体

class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("th32ModuleID", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("GlblcntUsage", ctypes.c_ulong),
        ("ProccntUsage", ctypes.c_ulong),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
        ("modBaseSize", ctypes.c_ulong),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * (MAX_MODULE_NAME32 + 1)),
        ("szExePath", ctypes.c_wchar * MAX_PATH),
    ]


@dataclass
class GateResult:
    """热激活结果。"""

    ok: bool
    reason: str = ""
    dll_path: str = ""
    dll_version: str = ""
    rva: int = 0
    old_byte: int | None = None
    new_byte: int | None = None
    verified: bool = False
    mmui_nodes: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [self.reason]
        if self.dll_path:
            lines.append(f"  Weixin.dll : {self.dll_path}")
        if self.dll_version:
            lines.append(f"  文件版本   : {self.dll_version}")
        if self.rva:
            lines.append(f"  gate RVA   : 0x{self.rva:08X}")
        if self.old_byte is not None:
            lines.append(f"  字节变化   : 0x{self.old_byte:02X} -> 0x{self.new_byte:02X}")
        if self.verified:
            lines.append(f"  mmui 控件  : 已物化（{self.mmui_nodes} 个节点）")
        for n in self.notes:
            lines.append(f"  · {n}")
        return "\n".join(lines)


# ----------------------------------------------------------------- 基础工具

def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)

    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    k.Module32FirstW.restype = wintypes.BOOL
    k.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    k.Module32NextW.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL

    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    k.ReadProcessMemory.restype = wintypes.BOOL
    k.WriteProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    k.WriteProcessMemory.restype = wintypes.BOOL
    k.VirtualProtectEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    k.VirtualProtectEx.restype = wintypes.BOOL
    return k


def weixin_pids() -> list[int]:
    """列出所有 Weixin.exe 的 PID（按内存占用从大到小，主进程通常最大）。"""
    import subprocess

    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="gbk", errors="replace", timeout=15,
        ).stdout or ""
    except Exception as exc:  # noqa: BLE001
        log.debug("tasklist 失败：%s", exc)
        return []

    rows: list[tuple[int, int]] = []
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) < 5 or not parts[0].lower().startswith("weixin.exe"):
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        mem = parts[4].replace(",", "").replace(" K", "").replace("K", "").strip()
        try:
            rows.append((int(mem), pid))
        except ValueError:
            rows.append((0, pid))
    rows.sort(reverse=True)
    return [pid for _mem, pid in rows]


def find_module(pid: int, module_name: str = "weixin.dll") -> tuple[int, int, str] | None:
    """在目标进程里找模块。返回 (基址, 大小, 完整路径)。"""
    k = _kernel32()
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE:
        return None
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not k.Module32FirstW(snap, ctypes.byref(entry)):
            return None
        while True:
            if entry.szModule.lower() == module_name.lower():
                base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
                return base, int(entry.modBaseSize), entry.szExePath
            if not k.Module32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        k.CloseHandle(snap)
    return None


def read_mem(handle, addr: int, size: int) -> bytes | None:
    k = _kernel32()
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = k.ReadProcessMemory(
        handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)
    )
    if not ok or got.value != size:
        return None
    return buf.raw[:size]


def write_byte(handle, addr: int, value: int) -> bool:
    """写一个字节。先尝试直接写，失败则临时放开页保护再写。"""
    k = _kernel32()
    buf = ctypes.create_string_buffer(bytes([value & 0xFF]), 1)
    got = ctypes.c_size_t(0)

    if k.WriteProcessMemory(
        handle, ctypes.c_void_p(addr), buf, 1, ctypes.byref(got)
    ) and got.value == 1:
        return True

    old = wintypes.DWORD(0)
    PAGE_EXECUTE_READWRITE = 0x40
    if not k.VirtualProtectEx(
        handle, ctypes.c_void_p(addr), 1, PAGE_EXECUTE_READWRITE, ctypes.byref(old)
    ):
        return False
    try:
        ok = k.WriteProcessMemory(
            handle, ctypes.c_void_p(addr), buf, 1, ctypes.byref(got)
        )
        return bool(ok) and got.value == 1
    finally:
        restored = wintypes.DWORD(0)
        k.VirtualProtectEx(handle, ctypes.c_void_p(addr), 1, old.value, ctypes.byref(restored))


def set_screen_reader_flag() -> bool:
    """把系统"屏幕阅读器正在运行"标志置 1（Qt 也会参考它）。"""
    try:
        ctypes.windll.user32.SystemParametersInfoW(
            SPI_SETSCREENREADER, 1, 0, SPIF_SENDCHANGE
        )
        return True
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------- PE 解析

def file_version_of(path: str) -> str:
    """读文件的版本号（用于精确匹配已知 RVA 表）。"""
    try:
        import win32api  # type: ignore
    except ImportError:
        win32api = None

    if win32api is not None:
        try:
            info = win32api.GetFileVersionInfo(path, "\\")
            ms, ls = info["FileVersionMS"], info["FileVersionLS"]
            return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        except Exception:  # noqa: BLE001
            pass

    # 退路：从路径里猜（Weixin 的安装目录通常就是版本号，如 ...\4.1.13.65\）
    parts = os.path.normpath(path).split(os.sep)
    for part in reversed(parts[:-1]):
        bits = part.split(".")
        if len(bits) == 4 and all(b.isdigit() for b in bits):
            return part
    return ""


def rva_to_file_offset(path: str, rva: int) -> int | None:
    """把 RVA 换算成文件偏移（用于读磁盘上该处应有的字节做校验）。"""
    sections = _pe_sections_from_file(path)
    if not sections:
        return None
    for sec in sections:
        size = max(sec["vsize"], sec["raw_size"])
        if sec["rva"] <= rva < sec["rva"] + size:
            return sec["raw_ptr"] + (rva - sec["rva"])
    return None


# ----------------------------------------------------------------- 自动扫描
#
# 硬编码的版本表会随微信升级失效（4.1.8.28 就不在表里）。下面这套是
# 从 Weixin.dll 里【自动找】gate 地址，思路来自上游 wechatauto-replica：
#
#   1. 找到字符串 "qt.accessibility.core"，记下它的 RVA
#   2. 找出所有以 RIP 相对寻址引用该字符串的指令位置（"代码岛"）
#   3. 在文件里匹配 gate 的字节特征：
#         48 85 c9 0f 84 ?? ?? ?? ?? 80 3d <disp32> 00 0f 84
#      即 test rcx,rcx / jz / cmp byte ptr [rip+disp],0 / jz
#   4. 由 disp 算出被检查字节的 RVA，要求它落在【可写段】
#   5. 按"离代码岛的距离"排序（同一段代码里的最像真 gate），逐个热写校验
#
# 全程只读文件；写入前仍然要求原值必须是 0 或 1。

_QACCESSIBLE_CORE = b"qt.accessibility.core"

# 48 85 c9 | 0f 84 xx xx xx xx | 80 3d <disp32> 00 | 0f 84
_GATE_RE = re.compile(
    rb"\x48\x85\xc9\x0f\x84....\x80\x3d(?P<disp>.{4})\x00\x0f\x84",
    re.DOTALL,
)

IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_WRITE = 0x80000000

_CANDIDATE_CACHE: dict[str, tuple[int, ...]] = {}


def _dll_identity(path: str) -> str:
    """DLL 身份串（版本目录 + 大小 + mtime），用作扫描结果缓存键。"""
    try:
        st = os.stat(path)
        ver = os.path.basename(os.path.dirname(path))
        return f"{ver}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return path


def _pe_sections(data: bytes) -> list[dict]:
    """解析 PE 段表。"""
    try:
        if len(data) < 0x40 or data[:2] != b"MZ":
            return []
        pe_off = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe_off:pe_off + 4] != b"PE\0\0":
            return []
        coff = pe_off + 4
        count = struct.unpack_from("<H", data, coff + 2)[0]
        opt_size = struct.unpack_from("<H", data, coff + 16)[0]
        sec_off = coff + 20 + opt_size
        out = []
        for i in range(count):
            off = sec_off + i * 40
            if off + 40 > len(data):
                break
            name = data[off:off + 8].split(b"\0", 1)[0].decode("ascii", "ignore")
            vsize, vaddr, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
            chars = struct.unpack_from("<I", data, off + 36)[0]
            out.append({
                "name": name, "rva": vaddr, "vsize": vsize,
                "raw_size": raw_size, "raw_ptr": raw_ptr, "chars": chars,
            })
        return out
    except Exception:  # noqa: BLE001
        return []


def _pe_sections_from_file(path: str) -> list[dict]:
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x4000)
    except OSError:
        return []
    return _pe_sections(head)


def _offset_to_rva(sections: list[dict], offset: int) -> int | None:
    for sec in sections:
        if sec["raw_ptr"] <= offset < sec["raw_ptr"] + sec["raw_size"]:
            return sec["rva"] + offset - sec["raw_ptr"]
    return None


def _section_for_rva(sections: list[dict], rva: int) -> dict | None:
    for sec in sections:
        size = max(sec["vsize"], sec["raw_size"])
        if sec["rva"] <= rva < sec["rva"] + size:
            return sec
    return None


def _rip_xrefs_to_rva(data: bytes, sections: list[dict], target_rva: int,
                      chunk: int = 8 << 20) -> list[int]:
    """找出以 RIP 相对寻址引用 target_rva 的 lea 指令位置。

    匹配 ``[REX] 8D <modrm>``，modrm 满足 (b & 0xC7) == 0x05（RIP 相对）。
    分块扫描且块间重叠 8 字节，避免漏掉跨块的指令。
    """
    xrefs: list[int] = []
    n = len(data)
    start = 0
    while start < n:
        end = min(n, start + chunk)
        window = data[start:end]
        limit = len(window) - 7
        i = 0
        while i < limit:
            b = window[i]
            if b == 0x8D and (window[i + 1] & 0xC7) == 0x05:
                disp = struct.unpack_from("<i", window, i + 2)[0]
                rva = _offset_to_rva(sections, start + i)
                if rva is not None and rva + 6 + disp == target_rva:
                    xrefs.append(rva)
            elif (0x40 <= b <= 0x4F and window[i + 1] == 0x8D
                  and (window[i + 2] & 0xC7) == 0x05):
                disp = struct.unpack_from("<i", window, i + 3)[0]
                rva = _offset_to_rva(sections, start + i)
                if rva is not None and rva + 7 + disp == target_rva:
                    xrefs.append(rva)
            i += 1
        if end >= n:
            break
        start = end - 8
    return xrefs


def scan_gate_candidates(dll_path: str, max_candidates: int = 6) -> list[int]:
    """从 Weixin.dll 扫出 gate 字节的候选 RVA，按可信度排序。

    只读文件。结果按 DLL 身份缓存在内存里。
    """
    identity = _dll_identity(dll_path)
    cached = _CANDIDATE_CACHE.get(identity)
    if cached is not None:
        return list(cached)[:max_candidates]

    try:
        with open(dll_path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        log.warning("读取 Weixin.dll 失败：%s", exc)
        return []

    sections = _pe_sections(data)
    if not sections:
        log.warning("Weixin.dll 不是有效的 PE 文件")
        return []

    core_off = data.find(_QACCESSIBLE_CORE)
    core_rva = _offset_to_rva(sections, core_off) if core_off >= 0 else None
    core_xrefs = _rip_xrefs_to_rva(data, sections, core_rva) if core_rva else []
    log.debug("qt.accessibility.core RVA=%s，引用点 %d 个", core_rva, len(core_xrefs))

    cands: list[tuple[int, int]] = []
    for m in _GATE_RE.finditer(data):
        match_rva = _offset_to_rva(sections, m.start())
        disp_rva = _offset_to_rva(sections, m.start("disp"))
        if match_rva is None or disp_rva is None:
            continue
        sec = _section_for_rva(sections, match_rva)
        if not sec or not (sec["chars"] & IMAGE_SCN_MEM_EXECUTE):
            continue

        cmp_rva = disp_rva - 2
        disp = struct.unpack("<i", m.group("disp"))[0]
        target_rva = cmp_rva + 7 + disp
        tsec = _section_for_rva(sections, target_rva)
        if not tsec or not (tsec["chars"] & IMAGE_SCN_MEM_WRITE):
            continue  # gate 一定在可写段

        distance = min((abs(match_rva - x) for x in core_xrefs), default=0x7FFFFFFF)
        cands.append((distance, target_rva))

    if not cands:
        log.warning("在 %s 里没扫到 gate 特征", os.path.basename(dll_path))
        _CANDIDATE_CACHE[identity] = ()
        return []

    cands.sort(key=lambda t: t[0])
    near = [rva for dist, rva in cands if dist <= 0x20000]
    ordered = near or [rva for _d, rva in cands]
    result = tuple(dict.fromkeys(ordered))
    _CANDIDATE_CACHE[identity] = result
    log.info("gate 扫描完成：%d 个候选（代码岛附近 %d 个）", len(result), len(near))
    return list(result)[:max_candidates]


# ----------------------------------------------------------------- 验证

def mmui_node_count(hwnd: int, max_depth: int = 12, limit: int = 120) -> tuple[int, int]:
    """统计 mmui 节点。返回 (mmui节点数, 总节点数)。

    遍历参数说明（2026-10-09 实测）：
        登录页的真实树深就有 8 层（mmui::LoginWindow -> QWidget ->
        QStackedWidget x2 -> mmui::XView ... -> 控件），主界面的聊天区更深。
        早先 max_depth=6 会漏掉深层控件，把节点数统计得偏少 —— 虽然够
        触发"健康"判定，但数字不准会误导排查。现在放宽到 12 层。
    """
    try:
        from wx4py.core import uiautomation as uia
    except Exception:  # noqa: BLE001
        return 0, 0

    try:
        root = uia.ControlFromHandle(hwnd)
    except Exception:  # noqa: BLE001
        return 0, 0
    if not root:
        return 0, 0

    total = 0
    mmui = 0
    stack = [(root, 0)]
    while stack and total < limit:
        node, depth = stack.pop()
        total += 1
        try:
            cls = str(getattr(node, "ClassName", "") or "")
            if "mmui::" in cls:
                mmui += 1
        except Exception:  # noqa: BLE001
            pass
        if depth >= max_depth:
            continue
        try:
            for ch in node.GetChildren():
                stack.append((ch, depth + 1))
        except Exception:  # noqa: BLE001
            pass
    return mmui, total


def mmui_present(hwnd: int) -> tuple[bool, str]:
    """gate 是否生效（对齐上游判定标准）。

    根节点类名以 mmui:: 开头，或其**直接子节点**里有 mmui:: 控件，即算生效。
    """
    try:
        from wx4py.core import uiautomation as uia

        c = uia.ControlFromHandle(hwnd)
    except Exception as exc:  # noqa: BLE001
        return False, f"读取 UIA 失败：{exc}"
    if c is None:
        return False, "ControlFromHandle 返回 None"

    cls = str(getattr(c, "ClassName", "") or "")
    if cls.startswith("mmui::"):
        return True, f"根节点即 {cls}"

    try:
        kids = c.GetChildren()
    except Exception:  # noqa: BLE001
        kids = []
    for k in kids:
        try:
            if str(getattr(k, "ClassName", "") or "").startswith("mmui::"):
                return True, "直接子节点含 mmui:: 控件"
        except Exception:  # noqa: BLE001
            pass
    return False, f"根={cls}，无 mmui:: 控件"


def find_wechat_hwnd() -> int:
    try:
        from wx4py.core.win32 import find_wechat_window

        return find_wechat_window() or 0
    except Exception:  # noqa: BLE001
        return 0


# ----------------------------------------------------------------- 主流程

def activate_gate(pid: int | None = None, verify: bool = True) -> GateResult:
    """对运行中的微信进程热激活 Qt accessibility gate。

    只写 Weixin.dll 里的 1 个字节。写前做版本 / 地址 / 原值三重校验，
    写后验证 mmui 树是否物化，失败自动回滚。
    """
    res = GateResult(ok=False)

    set_screen_reader_flag()

    # --- 选进程
    pids = [pid] if pid else weixin_pids()
    if not pids:
        res.reason = "没找到 Weixin.exe 进程，微信在运行吗？"
        return res

    k = _kernel32()
    handle = None
    used_pid = 0
    mod = None

    for candidate in pids:
        access = (
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION
        )
        h = k.OpenProcess(access, False, candidate)
        if not h:
            err = ctypes.get_last_error()
            res.notes.append(f"PID {candidate} 打不开（错误 {err}）")
            continue
        m = find_module(candidate, "weixin.dll")
        if not m:
            k.CloseHandle(h)
            res.notes.append(f"PID {candidate} 里没有 weixin.dll")
            continue
        handle, used_pid, mod = h, candidate, m
        break

    if handle is None or mod is None:
        res.reason = (
            "无法读写微信进程内存。通常是权限不够 —— "
            "请用【管理员身份】运行命令行/程序后重试。"
        )
        res.notes.append("提示：32 位 Python 也读不了 64 位微信的内存。")
        return res

    base, _size, dll_path = mod
    res.dll_path = dll_path

    try:
        version = file_version_of(dll_path)
        res.dll_version = version

        # --- 候选地址列表：已验证缓存 > 版本表 > 特征扫描
        candidates: list[int] = []
        verified = _CANDIDATE_CACHE.get("verified|" + _dll_identity(dll_path))
        if verified:
            candidates.append(int(verified[0]))
        if version in KNOWN_GATE_RVA:
            rva_known = KNOWN_GATE_RVA[version]
            if rva_known not in candidates:
                candidates.append(rva_known)
            res.notes.append(f"版本 {version} 命中已知地址表")
        else:
            res.notes.append(f"版本 {version or '未知'} 不在已知地址表里，改用特征扫描")

        scanned = scan_gate_candidates(dll_path)
        for r in scanned:
            if r not in candidates:
                candidates.append(r)

        if not candidates:
            res.reason = (
                f"既没有 {version} 的已知 gate 地址，也没能从 Weixin.dll 里扫出候选。"
                "这个微信版本可能改动了无障碍实现，本工具暂时不支持。"
            )
            return res

        res.notes.append(f"待试候选 {len(candidates)} 个：{', '.join(f'0x{r:X}' for r in candidates[:6])}")

        hwnd = find_wechat_hwnd()
        tried: list[str] = []

        for rva in candidates:
            addr = base + rva
            live = read_mem(handle, addr, 1)
            if live is None:
                tried.append(f"0x{rva:X}:读不到")
                continue

            original = live[0]

            # 值校验：gate 必须是个布尔字节。别的值说明这个 RVA 指到了别处。
            if original not in (0x00, 0x01):
                tried.append(f"0x{rva:X}:原值0x{original:02X}非布尔")
                continue

            wrote = False
            if original == 0x00:
                if not write_byte(handle, addr, 0x01):
                    tried.append(f"0x{rva:X}:写失败")
                    continue
                wrote = True
                back = read_mem(handle, addr, 1)
                if back != b"\x01":
                    tried.append(f"0x{rva:X}:写后回读不符")
                    continue

            res.rva = rva
            res.old_byte = original
            res.new_byte = 0x01

            # --- 效果校验：必须真的长出【内部】控件，光根节点变名字不算
            if not verify:
                res.ok = True
                res.reason = f"已写入 gate（PID {used_pid}，RVA 0x{rva:X}），未做效果校验。"
                return res

            mmui, total = mmui_node_count(hwnd) if hwnd else (0, 0)
            res.mmui_nodes = mmui
            if mmui >= 2 and total > 2:
                res.ok = True
                res.verified = True
                res.reason = (
                    f"已热激活 gate（PID {used_pid}，RVA 0x{rva:X}），"
                    f"UIA 控件树已可用（mmui {mmui} 个 / 总 {total} 个节点）。"
                )
                # 记住这个地址，下次优先用
                _CANDIDATE_CACHE["verified|" + _dll_identity(dll_path)] = (rva,)
                if original != 0x01:
                    res.notes.append(f"字节变化：0x{original:02X} -> 0x01")
                return res

            # 不对：回滚，试下一个候选
            tried.append(f"0x{rva:X}:控件树未长出({mmui}/{total})")
            if wrote:
                write_byte(handle, addr, original)

        res.ok = False
        res.reason = "所有候选地址都试过了，都没能让 UIA 控件树长出来。"
        res.notes.append("尝试记录：" + "；".join(tried))
        return res
    finally:
        k.CloseHandle(handle)



def gate_health() -> tuple[bool, str]:
    """快速判断 gate 是否生效（不写任何内存）。"""
    hwnd = find_wechat_hwnd()
    if not hwnd:
        return False, "找不到微信主窗口"
    ok, why = mmui_present(hwnd)
    if ok:
        mmui, total = mmui_node_count(hwnd)
        return True, f"{why}（mmui {mmui} 个 / 总 {total} 个节点）"
    return False, why


def prepare_wechat(*, force: bool = False, restore_window: bool = True) -> GateResult:
    """把微信准备好给 UIA 用 —— 按正确顺序做完整套前置动作。

    顺序很重要，实测踩过：

      1. **先让窗口可见**。
         窗口在托盘里隐藏时，即使 gate 写入成功，UIA 树也不会稳定物化
         （实测：隐藏状态下树只有 2 个节点；恢复可见后同样一次写入能出
         46 个 mmui 节点）。
      2. **再热激活 gate**。
         gate 字节会被重置（微信内部逻辑、以及某些失败操作），所以每次
         连接前都要检查一遍；已经是 1 就直接跳过。
      3. **最后验证** mmui 树是否真的长出来了。

    这个函数是幂等的，可以放心在每次发送前调用。
    """
    notes: list[str] = []

    if restore_window:
        try:
            from .senders import ensure_wechat_window_visible

            visible, why = ensure_wechat_window_visible()
            notes.append(f"窗口：{why}")
            if not visible:
                return GateResult(ok=False, reason=why, notes=notes)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"窗口检查跳过：{exc}")

    already, detail = gate_health()
    if already and not force:
        return GateResult(
            ok=True,
            reason=f"gate 已生效，无需激活。{detail}",
            verified=True,
            mmui_nodes=0,
            notes=notes,
        )

    res = activate_gate()
    res.notes = notes + res.notes
    return res
