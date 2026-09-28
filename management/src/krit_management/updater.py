from __future__ import annotations

import argparse
import ctypes
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path


class ApplyUpdateError(RuntimeError):
    pass


def wait_for_process(pid: int, timeout: int = 120) -> None:
    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return
    try:
        result = ctypes.windll.kernel32.WaitForSingleObject(handle, timeout * 1000)
        if result == 0x00000102:
            raise ApplyUpdateError("Программа управления не завершилась вовремя")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def safe_extract(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            if item.external_attr >> 16 & 0o170000 == 0o120000:
                raise ApplyUpdateError("Архив содержит символическую ссылку")
            target = (destination / item.filename).resolve()
            if target != root and root not in target.parents:
                raise ApplyUpdateError("Архив содержит опасный путь")
        package.extractall(destination)


def payload_root(staging: Path, executable: str) -> Path:
    if (staging / executable).is_file():
        return staging
    children = [item for item in staging.iterdir() if item.is_dir()]
    if len(children) == 1 and (children[0] / executable).is_file():
        return children[0]
    raise ApplyUpdateError("В архиве нет исполняемого файла")


def apply_update(archive: Path, install_dir: Path, pid: int, executable: str) -> None:
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise ApplyUpdateError("Архив обновления повреждён")
    if not (install_dir / executable).is_file() or (install_dir / ".git").exists():
        raise ApplyUpdateError("Каталог установки не прошёл проверку")
    wait_for_process(pid)
    suffix = uuid.uuid4().hex[:10]
    staging = install_dir.parent / f".krit-update-{suffix}"
    backup = install_dir.parent / f".krit-backup-{suffix}"
    try:
        staging.mkdir()
        safe_extract(archive, staging)
        payload = payload_root(staging, executable)
        install_dir.rename(backup)
        try:
            payload.rename(install_dir)
            subprocess.Popen([str(install_dir / executable)], cwd=install_dir, close_fds=True)
        except Exception:
            if install_dir.exists():
                shutil.rmtree(install_dir, ignore_errors=True)
            backup.rename(install_dir)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        archive.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--install-dir", required=True, type=Path)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--executable", required=True)
    args = parser.parse_args()
    try:
        time.sleep(0.25)
        apply_update(args.archive.resolve(), args.install_dir.resolve(), args.pid, args.executable)
        return 0
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(None, str(exc), "Ошибка обновления КРиТ", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
