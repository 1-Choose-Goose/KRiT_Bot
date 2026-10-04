from __future__ import annotations

import argparse
import ctypes
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path


class ApplyUpdateError(RuntimeError):
    pass


def wait_for_process(pid: int, timeout: int = 120) -> None:
    if sys.platform != "win32":
        return
    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return
    try:
        result = ctypes.windll.kernel32.WaitForSingleObject(handle, timeout * 1000)
        if result == 0x00000102:
            raise ApplyUpdateError("Программа управления не завершилась вовремя")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


ProgressCallback = Callable[[str, int], None]


def updater_icon_path() -> Path | None:
    bundle_root = getattr(sys, "_MEIPASS", None)
    candidates = [Path(__file__).with_name("assets") / "app_icon.ico"]
    if bundle_root:
        candidates.insert(
            0,
            Path(bundle_root) / "krit_management" / "assets" / "app_icon.ico",
        )
    return next((path for path in candidates if path.is_file()), None)


def set_window_icon(root: object) -> None:
    icon = updater_icon_path()
    if icon is None:
        return
    try:
        root.iconbitmap(default=str(icon))  # type: ignore[attr-defined]
    except Exception:
        # The update must remain usable even if a particular Tk build rejects
        # a valid Windows icon resource.
        return


def safe_extract(
    archive: Path,
    destination: Path,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        items = package.infolist()
        for item in items:
            if item.external_attr >> 16 & 0o170000 == 0o120000:
                raise ApplyUpdateError("Архив содержит символическую ссылку")
            target = (destination / item.filename).resolve()
            if target != root and root not in target.parents:
                raise ApplyUpdateError("Архив содержит опасный путь")
        total = max(1, len(items))
        for index, item in enumerate(items, 1):
            package.extract(item, destination)
            if progress is not None:
                progress(index, total)


def payload_root(staging: Path, executable: str) -> Path:
    if (staging / executable).is_file():
        return staging
    children = [item for item in staging.iterdir() if item.is_dir()]
    if len(children) == 1 and (children[0] / executable).is_file():
        return children[0]
    raise ApplyUpdateError("В архиве нет исполняемого файла")


def apply_update(
    archive: Path,
    install_dir: Path,
    pid: int,
    executable: str,
    progress: ProgressCallback | None = None,
) -> None:
    report = progress or (lambda _stage, _percent: None)
    report("Проверка пакета обновления…", 5)
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise ApplyUpdateError("Архив обновления повреждён")
    if not (install_dir / executable).is_file() or (install_dir / ".git").exists():
        raise ApplyUpdateError("Каталог установки не прошёл проверку")
    report("Ожидание закрытия программы…", 12)
    wait_for_process(pid)
    suffix = uuid.uuid4().hex[:10]
    staging = install_dir.parent / f".krit-update-{suffix}"
    backup = install_dir.parent / f".krit-backup-{suffix}"
    try:
        staging.mkdir()
        report("Распаковка обновления…", 20)
        safe_extract(
            archive,
            staging,
            lambda current, total: report("Распаковка обновления…", 20 + int(current * 40 / total)),
        )
        report("Проверка файлов обновления…", 65)
        payload = payload_root(staging, executable)
        report("Создание резервной копии…", 72)
        install_dir.rename(backup)
        try:
            report("Установка новой версии…", 84)
            payload.rename(install_dir)
            report("Запуск обновлённой программы…", 96)
            subprocess.Popen([str(install_dir / executable)], cwd=install_dir, close_fds=True)
        except Exception:
            if install_dir.exists():
                shutil.rmtree(install_dir, ignore_errors=True)
            backup.rename(install_dir)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        report("Обновление установлено", 100)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        archive.unlink(missing_ok=True)


def run_with_window(archive: Path, install_dir: Path, pid: int, executable: str) -> int:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Обновление КРиТ")
    set_window_icon(root)
    root.geometry("500x210")
    root.resizable(False, False)
    root.configure(background="#f5f7fb")
    root.protocol("WM_DELETE_WINDOW", lambda: None)

    style = ttk.Style(root)
    style.configure(
        "KRiT.Horizontal.TProgressbar",
        troughcolor="#dfe6f1",
        background="#1b63db",
        bordercolor="#dfe6f1",
        lightcolor="#1b63db",
        darkcolor="#1b63db",
        thickness=14,
    )
    title = tk.Label(
        root,
        text="Установка обновления",
        background="#f5f7fb",
        foreground="#15213a",
        font=("Segoe UI", 16, "bold"),
    )
    title.pack(anchor="w", padx=28, pady=(24, 8))
    status = tk.StringVar(value="Подготовка к установке…")
    label = tk.Label(
        root,
        textvariable=status,
        background="#f5f7fb",
        foreground="#445066",
        font=("Segoe UI", 10),
    )
    label.pack(anchor="w", padx=28, pady=(0, 12))
    value = tk.IntVar(value=0)
    bar = ttk.Progressbar(
        root,
        variable=value,
        maximum=100,
        length=444,
        style="KRiT.Horizontal.TProgressbar",
    )
    bar.pack(padx=28)
    percent = tk.StringVar(value="0 %")
    percent_label = tk.Label(
        root,
        textvariable=percent,
        background="#f5f7fb",
        foreground="#687386",
        font=("Segoe UI", 9),
    )
    percent_label.pack(anchor="e", padx=28, pady=(7, 0))

    events: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
    result = {"code": 1}

    def report(stage: str, progress_value: int) -> None:
        events.put(("progress", (stage, progress_value)))

    def install() -> None:
        try:
            apply_update(archive, install_dir, pid, executable, report)
        except Exception as exc:
            events.put(("error", str(exc)))
        else:
            events.put(("done", None))

    def close_after_success() -> None:
        result["code"] = 0
        root.destroy()

    def poll() -> None:
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "progress":
                    stage, progress_value = payload  # type: ignore[misc]
                    status.set(str(stage))
                    value.set(int(progress_value))
                    percent.set(f"{int(progress_value)} %")
                elif kind == "done":
                    status.set("Обновление установлено. Программа запускается…")
                    value.set(100)
                    percent.set("100 %")
                    root.after(1200, close_after_success)
                elif kind == "error":
                    status.set(f"Не удалось установить обновление: {payload}")
                    percent.set("Ошибка")
                    root.protocol("WM_DELETE_WINDOW", root.destroy)
                    button = tk.Button(
                        root,
                        text="Закрыть",
                        command=root.destroy,
                        background="#1b63db",
                        foreground="white",
                        activebackground="#164fb5",
                        activeforeground="white",
                        relief="flat",
                        padx=18,
                        pady=6,
                        font=("Segoe UI", 9, "bold"),
                    )
                    button.pack(anchor="e", padx=28, pady=12)
        except queue.Empty:
            pass
        if root.winfo_exists():
            root.after(80, poll)

    threading.Thread(target=install, name="krit-update-installer", daemon=True).start()
    root.after(80, poll)
    root.mainloop()
    return result["code"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--install-dir", required=True, type=Path)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--executable", required=True)
    args = parser.parse_args()
    time.sleep(0.25)
    try:
        return run_with_window(
            args.archive.resolve(), args.install_dir.resolve(), args.pid, args.executable
        )
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(None, str(exc), "Ошибка обновления КРиТ", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
