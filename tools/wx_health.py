"""微信接口体检（独立脚本版）。

真正实现在 qq2wx/senders.py 里，这样命令行和脚本共用同一份逻辑。
等价于：python -m qq2wx --wx-health
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qq2wx.senders import run_wx_health  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run_wx_health())
