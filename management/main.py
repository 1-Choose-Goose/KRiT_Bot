"""Точка запуска программы управления из PyCharm."""

import sys
from importlib import import_module
from pathlib import Path

SOURCE_DIR = Path(__file__).parent / "src"
sys.path.insert(0, str(SOURCE_DIR))
if __name__ == "__main__":
    run = import_module("krit_management.main").run
    run()
