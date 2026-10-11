"""pytest 配置：保证在任意工作目录下都能 import 到包本身。

默认 ``importmode=prepend`` 只会把测试文件所在目录（``powersdk/tests``）加入
``sys.path``，因此从 ``powersdk/`` 目录内部直接跑 ``pytest`` 时会找不到
``powersdk`` 包。这里显式把仓库根目录（本文件的上一级）插到 ``sys.path``
最前面，使测试无需先 ``pip install`` 即可运行（也优先于任何已安装的同名包）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
