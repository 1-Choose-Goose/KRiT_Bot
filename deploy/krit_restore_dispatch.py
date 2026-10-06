from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

RESTORE_ROOT = Path("/var/lib/krit/restore")
SYSTEMCTL = "/usr/bin/systemctl"


def dispatch_restore(
    operation_id: str,
    *,
    restore_root: Path = RESTORE_ROOT,
    runner: Callable[..., object] = subprocess.run,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("invalid restore operation id")
    if not (restore_root / operation_id / "state.json").is_file():
        raise FileNotFoundError(operation_id)
    runner(
        [
            SYSTEMCTL,
            "start",
            "--no-block",
            f"krit-restore@{operation_id}.service",
        ],
        check=True,
    )


def main() -> None:
    operation_id = sys.stdin.read(65).strip()
    dispatch_restore(operation_id)


if __name__ == "__main__":
    main()
