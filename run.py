"""给 .bat 用的入口：把项目根目录塞进 sys.path，然后调用 qq2wx 的 CLI。

为什么不直接在 .bat 里跑 qq2wx\\__main__.py：
    直接跑包内的 __main__.py 会触发 "attempted relative import with
    no known parent package"，因为此时它不是以包的形式被加载的。
为什么不用 python -m qq2wx：
    -m 在部分环境下会去改 sys.path[0]，配合含中文的项目路径容易出岔子，
    这里显式插入路径最稳。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qq2wx.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
