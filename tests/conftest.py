"""pytest fixtures for bid-gen tests.

把 scripts/ 加入 sys.path,所有 test_*.py 可以直接 import bid_*。
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
