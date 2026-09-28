from __future__ import annotations

import zipfile

import pytest

from krit_management.updater import ApplyUpdateError, safe_extract
from krit_management.updates import version_tuple


def test_semantic_version_comparison() -> None:
    assert version_tuple("v1.12.0") > version_tuple("1.9.9")


def test_safe_extract_rejects_parent_traversal(tmp_path) -> None:
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../outside.txt", "unsafe")

    with pytest.raises(ApplyUpdateError, match="опасный путь"):
        safe_extract(archive, tmp_path / "staging")

    assert not (tmp_path / "outside.txt").exists()
