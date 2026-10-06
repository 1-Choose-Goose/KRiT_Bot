from __future__ import annotations

import subprocess

import pytest
from deploy.krit_restore_dispatch import dispatch_restore


def test_dispatch_restore_starts_only_valid_existing_operation(tmp_path) -> None:
    operation_id = "a" * 32
    operation = tmp_path / operation_id
    operation.mkdir()
    (operation / "state.json").write_text("{}", encoding="utf-8")
    calls: list[tuple[list[str], bool]] = []

    dispatch_restore(
        operation_id,
        restore_root=tmp_path,
        runner=lambda argv, check: calls.append((argv, check)),
    )

    assert calls == [
        (
            [
                "/usr/bin/systemctl",
                "start",
                "--no-block",
                f"krit-restore@{operation_id}.service",
            ],
            True,
        )
    ]


@pytest.mark.parametrize("operation_id", ["", "../krit", "g" * 32, "a" * 33])
def test_dispatch_restore_rejects_invalid_operation_id(tmp_path, operation_id) -> None:
    with pytest.raises(ValueError, match="operation id"):
        dispatch_restore(operation_id, restore_root=tmp_path)


def test_dispatch_restore_rejects_missing_operation(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        dispatch_restore(
            "b" * 32,
            restore_root=tmp_path,
            runner=subprocess.run,
        )
