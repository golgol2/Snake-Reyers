from __future__ import annotations

import random
import threading
import time
from typing import Any

from cobrinha_server.core.json_store import (
    read_json,
    write_json_atomic,
)
from cobrinha_server.core.settings import (
    RUNS_DIR,
)
from cobrinha_server.services.session_control import (
    load_config,
    normalize_runtime_encoder,
    normalize_runtime_sink,
    status as session_status,
)
from cobrinha_server.services.session_executor_plan import (
    preferred_live_executable,
)
from cobrinha_server.services.session_real_bridge import (
    begin_real_session,
    public_status as real_bridge_status,
    stop_real_session,
)
from cobrinha_server.services import maintenance_mode


STATUS_FILE = RUNS_DIR / "session_scheduler_status.json"
LAST_RESULT_FILE = RUNS_DIR / "session_scheduler_last_result.json"
CHECK_INTERVAL_SECONDS = 1.0

_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.Lock()

_scheduled_action = ""
_scheduled_at = 0.0
_config_signature = ""
_running_generation = 0
_last_action = ""
_last_action_at = 0.0
_last_result: dict[str, Any] = {}


def start_session_scheduler() -> None:
    global _THREAD

    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return

        _STOP.clear()
        _THREAD = threading.Thread(
            target=_loop,
            name="cobrinha-session-scheduler",
            daemon=True,
        )
        _THREAD.start()


def stop_session_scheduler() -> None:
    _STOP.set()
    _write_status(
        {
            "state": "stopping",
            "enabled": False,
            "updated_at": time.time(),
        }
    )


def status() -> dict[str, Any]:
    payload = read_json(
        STATUS_FILE,
        {},
    )
    status_payload = (
        payload
        if isinstance(payload, dict)
        else {}
    )
    updated_at = float(
        status_payload.get("updated_at")
        or 0
    )

    return {
        **status_payload,
        "thread_alive": bool(
            _THREAD
            and _THREAD.is_alive()
        ),
        "status_age_seconds": (
            round(
                max(
                    0.0,
                    time.time() - updated_at,
                ),
                1,
            )
            if updated_at
            else 0
        ),
    }


def run_check_once() -> dict[str, Any]:
    global _scheduled_action
    global _scheduled_at
    global _config_signature
    global _running_generation

    maintenance = maintenance_mode.status()
    if maintenance["enabled"]:
        _scheduled_action = ""
        _scheduled_at = 0.0
        return _write_status(
            {
                "state": "maintenance",
                "enabled": False,
                "live_running": _session_running(session_status()),
                "message": maintenance["message"],
                "updated_at": time.time(),
            }
        )

    now = time.time()
    config = load_config()
    live = session_status()
    enabled = bool(
        config.get("auto_start_enabled")
    )
    running = _session_running(live)
    signature = _config_signature_for(
        config
    )

    if not enabled:
        _scheduled_action = ""
        _scheduled_at = 0.0
        _config_signature = signature
        return _write_status(
            {
                "state": "disabled",
                "enabled": False,
                "live_running": running,
                "updated_at": now,
            }
        )

    if signature != _config_signature:
        _scheduled_action = ""
        _scheduled_at = 0.0
        _config_signature = signature

    desired_action = (
        "stop"
        if running
        else "start"
    )

    if (
        desired_action == "stop"
        and _scheduled_action != "stop"
    ):
        _running_generation += 1

    if (
        _scheduled_action != desired_action
        or _scheduled_at <= 0
    ):
        _scheduled_action = desired_action
        _scheduled_at = (
            now
            + _delay_for(
                config,
                desired_action,
            )
        )
        return _write_status(
            _status_payload(
                "scheduled",
                config,
                live,
                now,
            )
        )

    if now < _scheduled_at:
        return _write_status(
            _status_payload(
                "waiting",
                config,
                live,
                now,
            )
        )

    action = _scheduled_action
    _scheduled_action = ""
    _scheduled_at = 0.0

    if action == "start":
        result = _start_session(
            config
        )
        action_at = time.time()
        _remember_last_result(
            "start",
            result,
            action_at,
        )
        next_live = session_status()
        return _write_status(
            {
                **_status_payload(
                    (
                        "started"
                        if result.get("ok")
                        else "start_error"
                    ),
                    config,
                    next_live,
                    action_at,
                ),
            }
        )

    planned_next_start_at = (
        time.time()
        + _delay_for(
            config,
            "start",
        )
    )
    result = _stop_session()
    action_at = time.time()
    _remember_last_result(
        "stop",
        result,
        action_at,
    )
    next_live = session_status()

    if bool(
        config.get("auto_start_enabled")
    ):
        _scheduled_action = "start"
        _scheduled_at = (
            planned_next_start_at
        )

    return _write_status(
        {
            **_status_payload(
                (
                    "stopped"
                    if result.get("ok")
                    else "stop_error"
                ),
                config,
                next_live,
                action_at,
            ),
        }
    )


def _loop() -> None:
    _write_status(
        {
            "state": "starting",
            "enabled": False,
            "updated_at": time.time(),
        }
    )

    while not _STOP.is_set():
        try:
            run_check_once()
        except Exception as exc:
            _write_status(
                {
                    "state": "error",
                    "enabled": False,
                    "error":
                        f"{type(exc).__name__}: {exc}",
                    "updated_at": time.time(),
                }
            )
            _STOP.wait(5.0)
            continue

        _STOP.wait(
            CHECK_INTERVAL_SECONDS
        )


def _start_session(
    config: dict[str, Any],
) -> dict[str, Any]:
    bridge = real_bridge_status()

    if not (
        bridge.get(
            "real_execution_enabled"
        )
        and bridge.get(
            "streamlabs_network_enabled"
        )
        and bridge.get(
            "streamlabs_endpoint_present"
        )
    ):
        return {
            "ok": False,
            "state":
                "authorization_required",
            "message": (
                "Os gates de execucao externa "
                "continuam desabilitados."
            ),
        }

    try:
        return begin_real_session(
            session_id=(
                "auto-schedule-"
                f"{int(time.time())}"
            ),
            executable=
                preferred_live_executable(
                    config.get(
                        "godot_executable"
                    ),
                    config.get(
                        "game_runtime"
                    ),
                ),
            server_url=
                "http://127.0.0.1:9393",
            display_value=str(
                config.get("display")
                or ":0"
            ),
            username=str(
                config.get("username")
                or ""
            ),
            encoder=
                normalize_runtime_encoder(
                    str(
                        config.get(
                            "video_encoder"
                        )
                        or ""
                    )
                ),
            sink=
                normalize_runtime_sink(
                    str(
                        config.get(
                            "rtmp_sink"
                        )
                        or ""
                    )
                ),
            bitrate_kbps=int(
                config.get(
                    "video_bitrate"
                )
                or 2500
            ),
            title=str(
                config.get("title")
                or ""
            ),
            game=str(
                config.get("game")
                or ""
            ),
            audience_type=str(
                config.get(
                    "audience_type"
                )
                or ""
            ),
        )
    except Exception as exc:
        return {
            "ok": False,
            "state": "start_exception",
            "error":
                f"{type(exc).__name__}: {exc}",
        }


def _stop_session() -> dict[str, Any]:
    bridge = real_bridge_status()

    if not bridge.get(
        "real_execution_enabled"
    ):
        return {
            "ok": False,
            "state":
                "authorization_required",
            "message": (
                "Os gates de execucao externa "
                "continuam desabilitados."
            ),
        }

    try:
        return stop_real_session()
    except Exception as exc:
        return {
            "ok": False,
            "state": "stop_exception",
            "error":
                f"{type(exc).__name__}: {exc}",
        }


def _session_running(
    payload: dict[str, Any],
) -> bool:
    runtime = (
        payload.get(
            "runtime_orchestrator"
        )
        or (
            payload.get("real_bridge")
            or {}
        ).get("runtime")
        or {}
    )

    session_state = str(
        payload.get("state")
        or ""
    )
    runtime_state = str(
        runtime.get("state")
        or ""
    )

    return (
        session_state == "running"
        or runtime_state
        in {
            "starting",
            "running",
            "rollback_partial",
        }
        or bool(runtime.get("game"))
        or bool(runtime.get("capture"))
        or bool(
            runtime.get(
                "monitor_started"
            )
        )
    )


def _status_payload(
    state: str,
    config: dict[str, Any],
    live: dict[str, Any],
    now: float,
) -> dict[str, Any]:
    remaining = (
        max(0, _scheduled_at - now)
        if _scheduled_at
        else 0
    )

    payload = {
        "state": state,
        "enabled": bool(
            config.get(
                "auto_start_enabled"
            )
        ),
        "live_running":
            _session_running(live),
        "scheduled_action":
            _scheduled_action,
        "scheduled_at":
            _scheduled_at,
        "remaining_seconds":
            round(remaining, 1),
        "start_min_seconds":
            int(
                config.get(
                    "start_min_seconds"
                )
                or 0
            ),
        "start_max_seconds":
            int(
                config.get(
                    "start_max_seconds"
                )
                or 0
            ),
        "stop_min_seconds":
            int(
                config.get(
                    "stop_min_seconds"
                )
                or 0
            ),
        "stop_max_seconds":
            int(
                config.get(
                    "stop_max_seconds"
                )
                or 0
            ),
        "running_generation":
            _running_generation,
        "updated_at": now,
    }

    last = _last_result_payload()
    if last:
        payload.update(last)

    return payload


def _remember_last_result(
    action: str,
    result: dict[str, Any],
    action_at: float,
) -> None:
    global _last_action
    global _last_action_at
    global _last_result

    _last_action = action
    _last_action_at = action_at
    _last_result = _compact_result(
        result
    )

    _write_last_result(
        {
            "last_action": _last_action,
            "last_result": _last_result,
            "last_action_at": _last_action_at,
        }
    )


def _last_result_payload() -> dict[str, Any]:
    if _last_action:
        return {
            "last_action": _last_action,
            "last_result": _last_result,
            "last_action_at": _last_action_at,
        }

    payload = read_json(
        LAST_RESULT_FILE,
        {},
    )
    return (
        payload
        if isinstance(payload, dict)
        else {}
    )


def _write_last_result(
    payload: dict[str, Any],
) -> None:
    try:
        write_json_atomic(
            LAST_RESULT_FILE,
            payload,
        )
    except OSError:
        pass


def _delay_for(
    config: dict[str, Any],
    action: str,
) -> float:
    if action == "stop":
        minimum = int(
            config.get(
                "stop_min_seconds"
            )
            or 1620
        )
        maximum = int(
            config.get(
                "stop_max_seconds"
            )
            or minimum
        )
    else:
        minimum = int(
            config.get(
                "start_min_seconds"
            )
            or 900
        )
        maximum = int(
            config.get(
                "start_max_seconds"
            )
            or minimum
        )

    low = max(
        1,
        min(minimum, maximum),
    )
    high = max(
        low,
        max(minimum, maximum),
    )

    return random.uniform(
        low,
        high,
    )


def _config_signature_for(
    config: dict[str, Any],
) -> str:
    keys = (
        "auto_start_enabled",
        "username",
        "monitor_server",
        "mode",
        "title",
        "game",
        "audience_type",
        "auto_streamlabs",
        "video_bitrate",
        "video_encoder",
        "game_runtime",
        "godot_executable",
        "rtmp_sink",
        "display",
        "start_min_seconds",
        "start_max_seconds",
        "stop_min_seconds",
        "stop_max_seconds",
    )

    return "|".join(
        f"{key}={config.get(key)!r}"
        for key in keys
    )


def _compact_result(
    result: dict[str, Any],
) -> dict[str, Any]:
    runtime = (
        result.get("runtime")
        if isinstance(
            result.get("runtime"),
            dict,
        )
        else {}
    )

    return {
        "ok": bool(
            result.get("ok")
        ),
        "state": str(
            result.get("state")
            or runtime.get("state")
            or ""
        ),
        "message": str(
            result.get("message")
            or result.get("error")
            or runtime.get("last_error")
            or ""
        ),
    }


def _write_status(
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        write_json_atomic(
            STATUS_FILE,
            payload,
        )
    except OSError:
        pass

    return payload
