from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from krit_management import updater, updates
from krit_management.updater import ApplyUpdateError, apply_update, safe_extract
from krit_management.updates import (
    UpdateInfo,
    download_update,
    release_notes_html,
    version_tuple,
)


def test_semantic_version_comparison() -> None:
    assert version_tuple("v1.12.0") > version_tuple("1.9.9")


def test_release_notes_are_rendered_as_safe_readable_list() -> None:
    rendered = release_notes_html(
        "- Первый пункт\\n- Второй <важный> пункт\n\nДополнительная информация"
    )

    assert "<ul" in rendered
    assert "<li>Первый пункт</li>" in rendered
    assert "<li>Второй &lt;важный&gt; пункт</li>" in rendered
    assert "<p>Дополнительная информация</p>" in rendered
    assert "\\n" not in rendered


def test_updater_window_uses_bundled_brand_icon(tmp_path, monkeypatch) -> None:
    icon = tmp_path / "app_icon.ico"
    icon.write_bytes(b"icon")
    calls: list[str] = []

    class FakeRoot:
        def iconbitmap(self, *, default: str) -> None:
            calls.append(default)

    monkeypatch.setattr(updater, "updater_icon_path", lambda: icon)
    updater.set_window_icon(FakeRoot())

    assert calls == [str(icon)]
    build_script = Path(__file__).resolve().parents[1] / "build_windows.ps1"
    installer_script = (
        Path(__file__).resolve().parents[1] / "packaging" / "windows-installer.iss"
    )
    assert build_script.exists()
    assert installer_script.exists()
    assert '--add-data "$assets\\app_icon.ico;krit_management\\assets"' in (
        build_script.read_text(encoding="utf-8-sig")
    )
    assert '"SetupKrit.exe"' in build_script.read_text(encoding="utf-8-sig")
    assert '"RESTORE_DATABASES.txt"' in build_script.read_text(encoding="utf-8-sig")
    assert "OutputBaseFilename=SetupKrit" in installer_script.read_text(
        encoding="utf-8-sig"
    )


def test_safe_extract_rejects_parent_traversal(tmp_path) -> None:
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../outside.txt", "unsafe")

    with pytest.raises(ApplyUpdateError, match="опасный путь"):
        safe_extract(archive, tmp_path / "staging")

    assert not (tmp_path / "outside.txt").exists()


def test_download_reports_visible_progress(tmp_path, monkeypatch) -> None:
    content = b"new-version" * 200_000
    folder = tmp_path / "download"
    folder.mkdir()

    class FakeResponse:
        headers = {"Content-Length": str(len(content))}

        def __init__(self) -> None:
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def read(self, size: int) -> bytes:
            block = content[self.offset : self.offset + size]
            self.offset += len(block)
            return block

    monkeypatch.setattr(updates, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    monkeypatch.setattr(updates.tempfile, "mkdtemp", lambda **_kwargs: str(folder))
    info = UpdateInfo(
        version="0.2.1",
        download_url="https://github.com/example/update.zip",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        notes="",
        page_url="",
    )
    progress = []

    archive = download_update(info, progress.append)

    assert archive.read_bytes() == content
    assert progress[0].stage == "Подключение к серверу обновлений…"
    assert any(item.stage == "Загрузка обновления…" for item in progress)
    assert progress[-1].percent == 100


def test_installer_reports_each_stage_and_replaces_application(tmp_path, monkeypatch) -> None:
    install_dir = tmp_path / "KRiTManagement"
    install_dir.mkdir()
    executable = "KRiTManagement.exe"
    (install_dir / executable).write_text("old", encoding="utf-8")
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(executable, "new")
        package.writestr("_internal/library.dat", "payload")

    launched: list[tuple[object, object]] = []
    monkeypatch.setattr(
        updater.subprocess,
        "Popen",
        lambda command, cwd, close_fds: launched.append((command, cwd)),
    )
    stages: list[tuple[str, int]] = []

    apply_update(
        archive, install_dir, 0, executable, lambda text, value: stages.append((text, value))
    )

    assert (install_dir / executable).read_text(encoding="utf-8") == "new"
    assert (install_dir / "_internal/library.dat").read_text(encoding="utf-8") == "payload"
    assert launched
    assert stages[-1] == ("Обновление установлено", 100)
    assert {value for _, value in stages} >= {5, 12, 20, 65, 72, 84, 96, 100}
