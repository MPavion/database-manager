from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

APP_TITLE = "Database Manager Launcher"


def show_message(message: str, title: str = APP_TITLE, error: bool = False) -> None:
    flags = 0x10 if error else 0x40
    try:
        ctypes.windll.user32.MessageBoxW(None, str(message), str(title), flags)
    except Exception:
        pass


def find_app_dir() -> Path | None:
    base_dir = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    candidates = [
        base_dir,
        base_dir / "NotionLocalSync",
        base_dir.parent,
        base_dir.parent / "NotionLocalSync",
    ]

    for candidate in candidates:
        if (candidate / "setup_and_run.bat").exists():
            return candidate
    return None


def build_launch_command(app_dir: Path) -> list[str]:
    setup_launch = app_dir / "setup_and_run.bat"
    pythonw_exe = app_dir / "venv" / "Scripts" / "pythonw.exe"
    python_exe = app_dir / "venv" / "Scripts" / "python.exe"
    bootstrap_flag = app_dir / ".bootstrap_complete"

    if bootstrap_flag.exists() and pythonw_exe.exists():
        return [str(pythonw_exe), "-m", "src.main"]

    if bootstrap_flag.exists() and python_exe.exists():
        return [str(python_exe), "-m", "src.main"]

    return ["cmd.exe", "/c", str(setup_launch)]


def main() -> int:
    app_dir = find_app_dir()
    if not app_dir:
        show_message(
            "I couldn't find the NotionLocalSync app folder next to this launcher.\n\n"
            "Keep this .exe in the project folder, or in a subfolder inside it.",
            error=True,
        )
        return 1

    command = build_launch_command(app_dir)
    target_path = Path(command[-1]) if command else None
    if target_path and target_path.suffix.lower() == ".bat" and not target_path.exists():
        show_message(
            f"The launcher file was not found:\n{target_path}",
            error=True,
        )
        return 1

    creationflags = 0
    for flag_name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= getattr(subprocess, flag_name, 0)

    try:
        subprocess.Popen(
            command,
            cwd=str(app_dir),
            close_fds=True,
            creationflags=creationflags,
        )
        return 0
    except Exception as exc:
        show_message(f"The app could not be launched.\n\n{exc}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
