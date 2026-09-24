"""Run the Adso web UI as an always-on macOS background service (launchd).

`adso service install` writes a per-user LaunchAgent that starts `adso serve`
at login and restarts it if it dies, so the web UI is simply always there.
The agent runs whichever Python interpreter performed the install, so a pinned
install (e.g. `uv tool install` / `pipx`) keeps the everyday service isolated
from a development checkout.
"""

from __future__ import annotations

import os
import plistlib
import socket
import subprocess
import sys
from pathlib import Path

from .errors import AdsoError

LABEL = "com.davidwhipps.adso"
DEFAULT_PORT = 8420


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "adso"


def service_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def build_plist(
    *,
    db_path: str | Path,
    port: int,
    working_dir: str | Path,
    python: str | None = None,
    logs: Path | None = None,
) -> dict:
    """Return the LaunchAgent definition as a plist-ready dict."""
    logs = logs or log_dir()
    return {
        "Label": LABEL,
        "ProgramArguments": [
            python or sys.executable,
            "-m",
            "adso.cli",
            "--db",
            str(Path(db_path).expanduser().resolve()),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-browser",
        ],
        # Project-level adso.ini is read from the working directory.
        "WorkingDirectory": str(Path(working_dir).resolve()),
        "RunAtLoad": True,
        "KeepAlive": True,
        # Don't hammer a crash loop (e.g. port already taken).
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "StandardOutPath": str(logs / "serve.log"),
        "StandardErrorPath": str(logs / "serve.log"),
    }


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise AdsoError(
            "`adso service` manages a macOS LaunchAgent and only works on macOS.",
            hint="Elsewhere, run `adso serve` directly or use your OS's service manager.",
        )


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AdsoError(f"launchctl {args[0]} failed: {detail or f'exit {result.returncode}'}")
    return result


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def is_loaded(label: str = LABEL) -> bool:
    return _launchctl("print", f"{_domain()}/{label}", check=False).returncode == 0


def install(*, db_path: str | Path, port: int = DEFAULT_PORT, working_dir: str | Path) -> str:
    """Write the LaunchAgent and (re)load it. Returns the service URL."""
    _require_macos()
    try:
        import uvicorn  # noqa: F401
    except ModuleNotFoundError as exc:
        raise AdsoError(
            "The web UI needs extra dependencies.",
            hint="Install them with: pip install -e '.[web]'",
        ) from exc

    log_dir().mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if is_loaded():
        _launchctl("bootout", f"{_domain()}/{LABEL}", check=False)
    elif _port_in_use(port):
        # Checked only when our own service isn't the one holding the port.
        raise AdsoError(
            f"Port {port} is already in use by another program.",
            hint="Pick a free one with `adso service install --port <n>`.",
        )

    with path.open("wb") as fh:
        plistlib.dump(build_plist(db_path=db_path, port=port, working_dir=working_dir), fh)

    _launchctl("bootstrap", _domain(), str(path))
    return service_url(port)


def uninstall() -> bool:
    """Stop the service and remove the LaunchAgent. Returns True if anything was removed."""
    _require_macos()
    removed = False
    if is_loaded():
        _launchctl("bootout", f"{_domain()}/{LABEL}", check=False)
        removed = True
    path = plist_path()
    if path.exists():
        path.unlink()
        removed = True
    return removed


def restart() -> None:
    """Restart the running service (picks up code changes; the server doesn't hot-reload)."""
    _require_macos()
    if not is_loaded():
        raise AdsoError("The Adso service isn't installed.", hint="Run `adso service install`.")
    _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")


def status() -> dict:
    """Describe the installed service, if any."""
    _require_macos()
    path = plist_path()
    info: dict = {"installed": path.exists(), "loaded": is_loaded(), "plist": str(path)}
    if path.exists():
        with path.open("rb") as fh:
            spec = plistlib.load(fh)
        argv = spec.get("ProgramArguments", [])
        info["python"] = argv[0] if argv else None
        info["db"] = _arg_after(argv, "--db")
        port = _arg_after(argv, "--port")
        info["url"] = service_url(int(port)) if port else None
        info["log"] = spec.get("StandardOutPath")
    if info["loaded"]:
        out = _launchctl("print", f"{_domain()}/{LABEL}", check=False).stdout
        info["pid"] = next(
            (
                line.split("=", 1)[1].strip()
                for line in out.splitlines()
                if line.strip().startswith("pid =")
            ),
            None,
        )
    return info


def _arg_after(argv: list[str], flag: str) -> str | None:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None



# --- Goodreads auto-sync ------------------------------------------------------
# Two small LaunchAgents, neither kept alive:
#   * the watcher runs `adso goodreads ingest` whenever the watch folder changes,
#     so a freshly downloaded export is synced within seconds;
#   * the reminder runs `adso goodreads remind` weekly to prompt the one manual
#     step (clicking "Export Library" — Goodreads blocks headless browsers).

WATCH_LABEL = f"{LABEL}.goodreads-watch"
REMIND_LABEL = f"{LABEL}.goodreads-remind"
WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def agent_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _adso_argv(python: str | None, db_path: str | Path, *command: str) -> list[str]:
    return [
        python or sys.executable,
        "-m",
        "adso.cli",
        "--db",
        str(Path(db_path).expanduser().resolve()),
        *command,
    ]


def build_watch_plist(
    *,
    db_path: str | Path,
    working_dir: str | Path,
    watch_dir: str | Path,
    python: str | None = None,
    logs: Path | None = None,
) -> dict:
    """LaunchAgent that syncs a Goodreads export as soon as it lands in `watch_dir`."""
    logs = logs or log_dir()
    watch = str(Path(watch_dir).expanduser().resolve())
    return {
        "Label": WATCH_LABEL,
        "ProgramArguments": _adso_argv(
            python, db_path, "goodreads", "ingest", "--watch-dir", watch, "--notify"
        ),
        "WorkingDirectory": str(Path(working_dir).resolve()),
        "WatchPaths": [watch],
        "RunAtLoad": True,  # pick up anything downloaded while the agent was off
        # Downloads changes constantly; don't re-run more than once a minute.
        "ThrottleInterval": 60,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "goodreads-sync.log"),
        "StandardErrorPath": str(logs / "goodreads-sync.log"),
    }


def build_remind_plist(
    *,
    db_path: str | Path,
    working_dir: str | Path,
    day: str = "sun",
    hour: int = 9,
    python: str | None = None,
    logs: Path | None = None,
) -> dict:
    """LaunchAgent that prompts for a Goodreads export once a week."""
    logs = logs or log_dir()
    return {
        "Label": REMIND_LABEL,
        "ProgramArguments": _adso_argv(python, db_path, "goodreads", "remind"),
        "WorkingDirectory": str(Path(working_dir).resolve()),
        "StartCalendarInterval": {"Weekday": WEEKDAYS[day], "Hour": hour, "Minute": 0},
        "RunAtLoad": False,
        "ProcessType": "Interactive",
        "StandardOutPath": str(logs / "goodreads-sync.log"),
        "StandardErrorPath": str(logs / "goodreads-sync.log"),
    }


def _install_agent(label: str, spec: dict) -> None:
    path = agent_plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_loaded(label):
        _launchctl("bootout", f"{_domain()}/{label}", check=False)
    with path.open("wb") as fh:
        plistlib.dump(spec, fh)
    _launchctl("bootstrap", _domain(), str(path))


def _uninstall_agent(label: str) -> bool:
    removed = False
    if is_loaded(label):
        _launchctl("bootout", f"{_domain()}/{label}", check=False)
        removed = True
    path = agent_plist_path(label)
    if path.exists():
        path.unlink()
        removed = True
    return removed


def install_sync(
    *,
    db_path: str | Path,
    working_dir: str | Path,
    watch_dir: str | Path,
    day: str = "sun",
    hour: int = 9,
    reminder: bool = True,
) -> None:
    """Install the Downloads watcher and (optionally) the weekly reminder."""
    _require_macos()
    log_dir().mkdir(parents=True, exist_ok=True)
    _install_agent(
        WATCH_LABEL, build_watch_plist(db_path=db_path, working_dir=working_dir, watch_dir=watch_dir)
    )
    if reminder:
        _install_agent(
            REMIND_LABEL,
            build_remind_plist(db_path=db_path, working_dir=working_dir, day=day, hour=hour),
        )
    else:
        _uninstall_agent(REMIND_LABEL)


def uninstall_sync() -> bool:
    """Remove the watcher and reminder. Returns True if anything was removed."""
    _require_macos()
    removed_watch = _uninstall_agent(WATCH_LABEL)
    removed_remind = _uninstall_agent(REMIND_LABEL)
    return removed_watch or removed_remind


def sync_status() -> dict:
    """Describe the Goodreads auto-sync agents, if installed."""
    _require_macos()
    watch_path = agent_plist_path(WATCH_LABEL)
    remind_path = agent_plist_path(REMIND_LABEL)
    info: dict = {
        "installed": watch_path.exists(),
        "loaded": is_loaded(WATCH_LABEL),
        "reminder": None,
    }
    if watch_path.exists():
        with watch_path.open("rb") as fh:
            spec = plistlib.load(fh)
        argv = spec.get("ProgramArguments", [])
        info["watch_dir"] = _arg_after(argv, "--watch-dir")
        info["db"] = _arg_after(argv, "--db")
        info["log"] = spec.get("StandardOutPath")
    if remind_path.exists():
        with remind_path.open("rb") as fh:
            when = plistlib.load(fh).get("StartCalendarInterval", {})
        names = {v: k for k, v in WEEKDAYS.items()}
        info["reminder"] = (
            f"weekly, {names.get(when.get('Weekday'), '?').capitalize()} "
            f"{when.get('Hour', 0):02d}:{when.get('Minute', 0):02d}"
        )
    return info
