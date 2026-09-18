#!/usr/bin/env python3
"""Start ohne Installation:  python3 run.py [Befehl ...]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atprog.cli import main   # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
