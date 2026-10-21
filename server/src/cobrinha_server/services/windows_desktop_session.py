from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

from cobrinha_server.bridge.godot_bridge import godot_bridge
from cobrinha_server.core.settings import LOGS_DIR
from cobrinha_server.services import maintenance_mode, tiktok_monitor
from cobrinha_server.services.session_executor_plan import (
    build_game_plan,
    preferred_live_executable,
)


_LOCK = threading.RLock()
_PROCESS: subprocess.Popen[bytes] | None = None
_STATE: dict[str, Any] = {
    "state": "stopped",
    "pid": 0,
    "started_at": 0.0,
    "last_error": "",
}


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(
        getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        )
    )


def _refresh_locked() -> None:
    global _PROCESS

    if _PROCESS is None:
        return

    returncode = _PROCESS.poll()
    if returncode is None:
        return

    _STATE.update(
        {
            "state": "stopped",
            "pid": 0,
            "returncode": int(returncode),
        }
    )
    _PROCESS = None


def status() -> dict[str, Any]:
    with _LOCK:
        _refresh_locked()
        running = bool(
            _PROCESS is not None
            and _PROCESS.poll() is None
        )
        return {
            **_STATE,
            "available": os.name == "nt",
            "running": running,
            "capture_mode": "tiktok_live_studio_screen_capture",
            "godot": godot_bridge.status(),
        }


def start(
    *,
    executable: str,
    server_url: str,
    username: str,
    monitor_server: str,
) -> dict[str, Any]:
    global _PROCESS

    if maintenance_mode.status()["enabled"]:
        return maintenance_mode.blocked_result()

    if os.name != "nt":
        return {
            "ok": False,
            "state": "unsupported_platform",
            "message": "Sessao desktop disponivel somente no Windows.",
        }

    with _LOCK:
        _refresh_locked()
        if _PROCESS is not None and _PROCESS.poll() is None:
            return {
                "ok": True,
                "state": "running",
                "already_running": True,
                "message": "O jogo ja esta aberto.",
                "windows_desktop": status(),
            }

        executable_path = Path(
            preferred_live_executable(
                executable,
                "build",
            )
        ).expanduser().resolve()
        if not executable_path.is_file():
            return {
                "ok": False,
                "state": "game_not_found",
                "message": f"Executavel do jogo nao encontrado: {executable_path}",
            }

        plan = build_game_plan(
            session_id="windows-desktop",
            executable=str(executable_path),
            server_url=server_url,
            display_value="",
        )

        environment = os.environ.copy()
        environment.update(plan.environment)
        log_path = Path(LOGS_DIR) / "windows-game.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with log_path.open("ab", buffering=0) as log_file:
                _PROCESS = subprocess.Popen(
                    list(plan.argv),
                    cwd=plan.cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=_creation_flags(),
                )
        except Exception as exc:
            _PROCESS = None
            _STATE.update(
                {
                    "state": "error",
                    "pid": 0,
                    "last_error": f"{type(exc).__name__}:{exc}",
                }
            )
            return {
                "ok": False,
                "state": "error",
                "message": "Nao foi possivel abrir o jogo no Windows.",
                "error": _STATE["last_error"],
            }

        _STATE.update(
            {
                "state": "starting",
                "pid": int(_PROCESS.pid),
                "started_at": time.time(),
                "last_error": "",
                "returncode": None,
            }
        )

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        with _LOCK:
            if _PROCESS is None or _PROCESS.poll() is not None:
                break
        if godot_bridge.status().get("connected"):
            break
        time.sleep(0.25)

    monitor_state: dict[str, Any] = tiktok_monitor.status()
    clean_username = str(username or "").strip().lstrip("@")
    if clean_username:
        monitor_state = tiktok_monitor.start_monitor(
            clean_username,
            server_url=monitor_server,
            prepare_local_node=True,
        )

    with _LOCK:
        _refresh_locked()
        running = bool(
            _PROCESS is not None
            and _PROCESS.poll() is None
        )
        _STATE["state"] = "running" if running else "stopped"

    return {
        "ok": running,
        "execution": running,
        "state": "running" if running else "game_closed_during_start",
        "message": (
            "Jogo aberto. Capture a janela pelo TikTok LIVE Studio."
            if running
            else "O jogo fechou durante a inicializacao."
        ),
        "monitor": monitor_state,
        "monitor_configured": bool(clean_username),
        "windows_desktop": status(),
    }


def stop() -> dict[str, Any]:
    global _PROCESS

    monitor_state = tiktok_monitor.stop_monitor(
        stop_local_node_runtime=True,
    )

    with _LOCK:
        _refresh_locked()
        process = _PROCESS
        if process is None:
            _STATE.update(
                {
                    "state": "stopped",
                    "pid": 0,
                    "last_error": "",
                }
            )
            return {
                "ok": True,
                "stopped": False,
                "already_stopped": True,
                "monitor": monitor_state,
                "windows_desktop": status(),
            }

        try:
            process.terminate()
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3.0)
        except OSError:
            pass

        _PROCESS = None
        _STATE.update(
            {
                "state": "stopped",
                "pid": 0,
                "last_error": "",
            }
        )

    return {
        "ok": True,
        "stopped": True,
        "monitor": monitor_state,
        "windows_desktop": status(),
    }
