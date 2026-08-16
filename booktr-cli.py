"""booktr 启动脚本（位于 src/ 内，从任意 cwd 运行均可）。

用法:
    python booktr-cli.py init
    python booktr-cli.py scan
    python booktr-cli.py plan
    python booktr-cli.py translate --pages today/today45.html
    ...
与 `python -m booktr <cmd>` 等价（需在 src/ 下运行）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from booktr.pipeline import main

if __name__ == "__main__":
    sys.exit(main())
