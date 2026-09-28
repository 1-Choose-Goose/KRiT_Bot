from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import certifi

from .version import APP_VERSION

LATEST_RELEASE_API = "https://api.github.com/repos/1-Choose-Goose/KRiT_Bot/releases/latest"
WINDOWS_ASSET_NAME = "KRiT-Management-Windows-x64.zip"
UPDATER_NAME = "KRiTManagementUpdater.exe"
UPDATER_RELATIVE_PATH = Path("_internal") / UPDATER_NAME
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
SHA256_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$")


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    version: str
    download_url: str
    sha256: str
    size: int
    notes: str
    page_url: str


def version_tuple(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Unsupported version: {value}")
    return tuple(int(part) for part in match.groups())


def updates_supported() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def _request(url: str, accept: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": f"KRiT-Management/{APP_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def check_for_update() -> UpdateInfo | None:
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urlopen(
            _request(LATEST_RELEASE_API, "application/vnd.github+json"),
            timeout=15,
            context=context,
        ) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise UpdateError(f"Сервер обновлений вернул HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise UpdateError("Не удалось проверить обновления") from exc

    tag = str(payload.get("tag_name") or "")
    try:
        if version_tuple(tag) <= version_tuple(APP_VERSION):
            return None
    except ValueError as exc:
        raise UpdateError("Сервер вернул некорректную версию") from exc
    asset = next(
        (item for item in payload.get("assets", ()) if item.get("name") == WINDOWS_ASSET_NAME),
        None,
    )
    if asset is None:
        raise UpdateError("В релизе нет сборки программы управления для Windows")
    digest = SHA256_RE.fullmatch(str(asset.get("digest") or ""))
    if digest is None:
        raise UpdateError("У файла обновления отсутствует SHA-256")
    url = str(asset.get("browser_download_url") or "")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise UpdateError("Ссылка на обновление не прошла проверку безопасности")
    return UpdateInfo(
        version=tag.removeprefix("v"),
        download_url=url,
        sha256=digest.group(1).lower(),
        size=max(0, int(asset.get("size") or 0)),
        notes=str(payload.get("body") or "").strip(),
        page_url=str(payload.get("html_url") or ""),
    )


def download_update(update: UpdateInfo) -> Path:
    folder = Path(tempfile.mkdtemp(prefix=f"KRiT-Management-{update.version}-"))
    destination = folder / WINDOWS_ASSET_NAME
    partial = destination.with_suffix(".zip.part")
    digest = hashlib.sha256()
    downloaded = 0
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with (
            urlopen(
                _request(update.download_url, "application/octet-stream"),
                timeout=120,
                context=context,
            ) as response,
            partial.open("wb") as stream,
        ):
            while block := response.read(1024 * 1024):
                stream.write(block)
                digest.update(block)
                downloaded += len(block)
        if update.size and downloaded != update.size:
            raise UpdateError("Файл обновления загружен не полностью")
        if digest.hexdigest() != update.sha256:
            raise UpdateError("Контрольная сумма обновления не совпала")
        partial.replace(destination)
        return destination
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def launch_updater(archive: Path) -> None:
    if not updates_supported():
        raise UpdateError("Установка обновлений доступна после сборки Windows-приложения")
    install_dir = Path(sys.executable).resolve().parent
    source = install_dir / UPDATER_RELATIVE_PATH
    if not source.is_file() or (install_dir / ".git").exists():
        raise UpdateError("Не найден безопасный модуль обновления")
    updater_dir = Path(tempfile.mkdtemp(prefix="KRiT-Management-updater-"))
    updater = updater_dir / UPDATER_NAME
    shutil.copy2(source, updater)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(
            [
                str(updater),
                "--archive",
                str(archive.resolve()),
                "--install-dir",
                str(install_dir),
                "--pid",
                str(os.getpid()),
                "--executable",
                Path(sys.executable).name,
            ],
            close_fds=True,
            creationflags=flags,
            cwd=updater_dir,
        )
    except OSError as exc:
        shutil.rmtree(updater_dir, ignore_errors=True)
        raise UpdateError("Не удалось запустить установщик обновления") from exc
