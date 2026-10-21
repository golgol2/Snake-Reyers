from __future__ import annotations

from dataclasses import (
    dataclass,
)
import os
import signal
import threading
import time
from pathlib import Path
from typing import (
    Any,
    Callable,
)
from urllib.parse import (
    urlsplit,
)
from urllib.request import (
    Request,
    urlopen,
)

from cobrinha_server.bridge.godot_bridge import (
    godot_bridge,
)
from cobrinha_server.core.json_store import (
    read_json,
    write_json_atomic,
)
from cobrinha_server.core.settings import (
    OUTPUT_METRICS_FILE,
    RUNS_DIR,
)
from cobrinha_server.services import (
    maintenance_mode,
    music_runtime,
    runtime_cleanup,
    song_runtime,
    tiktok_monitor,
)
from cobrinha_server.services.session_executor_plan import (
    EXPORTED_GAME_DIR,
    GAME_DIR,
)
from cobrinha_server.services.live_session_state import (
    reset_live_transient_state,
)
from cobrinha_server.services.session_runtime_adapters import (
    RuntimeExecutionBlocked,
    SubprocessCommandRunner,
    monitor_start_call,
    monitor_stop_call,
    real_execution_enabled,
)
from cobrinha_server.services.session_runtime_orchestrator import (
    GAME_BRIDGE_TIMEOUT_SECONDS,
    rollback_with_adapters,
    start_with_rollback,
    status as runtime_status,
)
from cobrinha_server.services.output_redaction import (
    fingerprint,
)
from cobrinha_server.services.streamlabs_provider import (
    STREAMLABS_API_BASE,
    StreamlabsProviderError,
    acquire_with_transport,
    end_last_live_with_transport,
    network_enabled,
    public_config_status as streamlabs_config_status,
)


STREAMLABS_START_ENDPOINT_ENV = (
    "COBRINHA_STREAMLABS_START_ENDPOINT"
)
FAILED_RTMP_HOSTS_FILE = (
    RUNS_DIR / "rtmp_failed_hosts.json"
)
_SESSION_OPERATION_LOCK = threading.Lock()


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


STREAMLABS_RTMP_START_MAX_ATTEMPTS = _env_int(
    "COBRINHA_STREAMLABS_RTMP_START_ATTEMPTS",
    3,
    minimum=1,
    maximum=10,
)
STREAMLABS_RTMP_CREDENTIAL_RENEW_ATTEMPTS = _env_int(
    "COBRINHA_STREAMLABS_RTMP_CREDENTIAL_RENEW_ATTEMPTS",
    8,
    minimum=1,
    maximum=10,
)
RTMP_BAD_HOST_COOLDOWN_SECONDS = _env_int(
    "COBRINHA_RTMP_BAD_HOST_COOLDOWN_SECONDS",
    21600,
    minimum=60,
    maximum=86400,
)
STREAMLABS_RTMP_READY_DELAY_SECONDS = _env_float(
    "COBRINHA_STREAMLABS_RTMP_READY_DELAY_SECONDS",
    6.0,
    minimum=0.0,
    maximum=30.0,
)


@dataclass
class ProcessSnapshot:
    pid: int
    cwd: str
    command: str


def _runtime_session_active(
    runtime: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    current = (
        runtime
        if isinstance(runtime, dict)
        else runtime_status()
    )
    state = str(
        current.get("state")
        or ""
    )

    if state in {
        "starting",
        "running",
    }:
        return True, state, current

    if (
        current.get("game")
        or current.get("capture")
        or current.get("monitor_started")
    ):
        return True, "active", current

    return False, state, current


def _safe_int_value(
    value: object,
) -> int:
    try:
        return int(
            float(
                value
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        return 0


def _safe_float_value(
    value: object,
) -> float:
    try:
        return float(
            value
        )
    except (
        TypeError,
        ValueError,
    ):
        return 0.0


def _active_external_capture_from_metrics(
    *,
    max_age_seconds: float = 30.0,
) -> dict[str, Any]:
    metrics = read_json(
        OUTPUT_METRICS_FILE,
        {},
    )

    if not isinstance(
        metrics,
        dict,
    ):
        return {}

    updated_at = _safe_float_value(
        metrics.get(
            "updated_at"
        )
    )
    age_seconds = (
        time.time()
        - updated_at
        if updated_at > 0
        else 999999.0
    )

    if (
        updated_at <= 0
        or age_seconds
        > max_age_seconds
    ):
        return {}

    pid = _safe_int_value(
        metrics.get("pid")
    )

    if (
        pid <= 1
        or not _owned_process(
            "capture",
            pid,
        )
    ):
        return {}

    rtmp = metrics.get("rtmp")
    if not isinstance(
        rtmp,
        dict,
    ):
        return {}

    if not (
        metrics.get(
            "rtmp_url_present"
        )
        and rtmp.get(
            "has_stats"
        )
        and _safe_int_value(
            rtmp.get(
                "out_acked"
            )
        )
        > 0
    ):
        return {}

    return {
        "pid": pid,
        "age_seconds":
            round(
                max(
                    0.0,
                    age_seconds,
                ),
                1,
            ),
        "rtmp_fingerprint": str(
            metrics.get(
                "rtmp_fingerprint"
            )
            or ""
        ),
        "out_total": _safe_int_value(
            rtmp.get(
                "out_total"
            )
        ),
        "out_acked": _safe_int_value(
            rtmp.get(
                "out_acked"
            )
        ),
    }


def _require_execution_gate() -> None:
    if not real_execution_enabled():
        raise RuntimeExecutionBlocked(
            "real_session_execution_disabled"
        )


def _require_all_gates() -> None:
    _require_execution_gate()

    if not network_enabled():
        raise StreamlabsProviderError(
            "streamlabs_network_disabled"
        )


def http_json_transport(
    method: str,
    endpoint: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    _require_all_gates()

    import json

    multipart_payload = None

    if isinstance(
        body,
        dict,
    ):
        candidate = body.get(
            "_multipart"
        )

        if isinstance(
            candidate,
            (
                bytes,
                bytearray,
            ),
        ):
            multipart_payload = bytes(
                candidate
            )

    payload = (
        multipart_payload
        if multipart_payload
        is not None
        else (
            None
            if body is None
            else json.dumps(
                body
            ).encode("utf-8")
        )
    )

    request = Request(
        str(endpoint),
        data=payload,
        headers=dict(headers),
        method=str(method),
    )

    with urlopen(
        request,
        timeout=25,
    ) as response:
        raw = response.read()

    decoded = json.loads(
        raw.decode("utf-8")
    )

    if not isinstance(
        decoded,
        dict,
    ):
        raise StreamlabsProviderError(
            "streamlabs_response_not_object"
        )

    return decoded


def streamlabs_destination_provider(
    *,
    title: str,
    game: str,
    audience_type: str,
    endpoint: str | None = None,
    transport: Callable[
        [
            str,
            str,
            dict[str, str],
            dict[str, Any] | None,
        ],
        dict[str, Any],
    ]
    | None = None,
) -> dict[str, Any]:
    selected_endpoint = str(
        endpoint
        or os.environ.get(
            STREAMLABS_START_ENDPOINT_ENV,
            ""
        )
        or (
            STREAMLABS_API_BASE
            + "/stream/start"
        )
    ).strip()

    selected_transport = (
        transport
        or http_json_transport
    )

    credential = acquire_with_transport(
        endpoint=selected_endpoint,
        title=title,
        game=game,
        audience_type=audience_type,
        transport=selected_transport,
    )

    return {
        "destination_url":
            credential.destination_url,
        "provider":
            credential.provider,
        "session_id":
            credential.session_id,
    }


def _rtmp_destination_info(
    destination_url: object,
) -> dict[str, str]:
    text = str(
        destination_url
        or ""
    ).strip()

    if not text:
        return {
            "rtmp_host": "",
            "rtmp_app": "",
            "rtmp_fingerprint": "",
        }

    try:
        parsed = urlsplit(
            text
        )
    except ValueError:
        return {
            "rtmp_host": "",
            "rtmp_app": "",
            "rtmp_fingerprint": (
                fingerprint(
                    text
                )
            ),
        }

    parts = [
        part
        for part in parsed.path.split("/")
        if part
    ]

    return {
        "rtmp_host":
            str(
                parsed.netloc
                or parsed.hostname
                or ""
            ),
        "rtmp_app":
            str(
                parts[0]
                if parts
                else ""
            ),
        "rtmp_fingerprint":
            fingerprint(
                text
            ),
    }


def _streamlabs_destination_avoiding_failed_hosts(
    *,
    title: str,
    game: str,
    audience_type: str,
    blocked_hosts: set[str],
    streamlabs_attempt: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    destination = streamlabs_destination_provider(
        title=title,
        game=game,
        audience_type=audience_type,
    )
    return (
        {
            **destination,
            **_rtmp_destination_info(
                destination.get(
                    "destination_url"
                )
            ),
            "credential_renewal": 1,
        },
        [],
    )


def _streamlabs_attempt_info(
    destination: dict[str, Any] | None,
    *,
    attempt: int,
    previous_attempts: list[dict[str, Any]] | None = None,
    skipped_destinations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current = (
        destination
        if isinstance(
            destination,
            dict,
        )
        else {}
    )

    info: dict[str, Any] = {
        "auto": True,
        "attempt": int(
            attempt
            or 1
        ),
        "stream_id": str(
            current.get(
                "session_id"
            )
            or ""
        ),
        "provider": str(
            current.get(
                "provider"
            )
            or "streamlabs"
        ),
        "rtmp_fingerprint": str(
            current.get(
                "rtmp_fingerprint"
            )
            or ""
        ),
        "rtmp_host": str(
            current.get(
                "rtmp_host"
            )
            or ""
        ),
        "rtmp_app": str(
            current.get(
                "rtmp_app"
            )
            or ""
        ),
        "credential_renewal": int(
            current.get(
                "credential_renewal"
            )
            or 1
        ),
    }

    if previous_attempts:
        info["previous_attempts"] = (
            previous_attempts
        )

    if skipped_destinations:
        info["skipped_destinations"] = (
            skipped_destinations
        )

    return info


def _normalize_rtmp_host(
    value: object,
) -> str:
    return str(
        value
        or ""
    ).strip().lower()


def _load_recent_failed_rtmp_hosts() -> set[str]:
    return set()


def _remember_failed_rtmp_host(
    host: object,
    *,
    reason: str,
) -> None:
    return


def _load_failed_rtmp_host_map() -> dict[str, dict[str, Any]]:
    raw = read_json(
        FAILED_RTMP_HOSTS_FILE,
        {},
    )
    source = (
        raw.get(
            "hosts"
        )
        if isinstance(
            raw,
            dict,
        )
        else {}
    )

    if not isinstance(
        source,
        dict,
    ):
        return {}

    now = time.time()
    hosts: dict[str, dict[str, Any]] = {}

    for raw_host, raw_item in source.items():
        host = _normalize_rtmp_host(
            raw_host
        )

        if not host:
            continue

        item = (
            raw_item
            if isinstance(
                raw_item,
                dict,
            )
            else {}
        )

        try:
            failed_at = float(
                item.get(
                    "last_failed_at"
                )
                or 0
            )
        except (
            TypeError,
            ValueError,
        ):
            failed_at = 0.0

        if (
            failed_at <= 0
            or now
            - failed_at
            > RTMP_BAD_HOST_COOLDOWN_SECONDS
        ):
            continue

        hosts[host] = {
            **item,
            "last_failed_at":
                failed_at,
        }

    return hosts


def _write_failed_rtmp_host_map(
    hosts: dict[str, dict[str, Any]],
) -> None:
    write_json_atomic(
        FAILED_RTMP_HOSTS_FILE,
        {
            "updated_at":
                time.time(),
            "cooldown_seconds":
                RTMP_BAD_HOST_COOLDOWN_SECONDS,
            "hosts":
                hosts,
        },
    )


def _result_error_text(
    result: dict[str, Any],
) -> str:
    candidates: list[object] = [
        result.get(
            "error"
        ),
        result.get(
            "message"
        ),
    ]

    runtime = result.get(
        "runtime"
    )

    if isinstance(
        runtime,
        dict,
    ):
        candidates.append(
            runtime.get(
                "last_error"
            )
        )

    for value in candidates:
        text = str(
            value
            or ""
        ).strip()

        if text:
            return text

    return ""


def _should_retry_streamlabs_rtmp_start(
    result: dict[str, Any],
) -> bool:
    message = _result_error_text(
        result
    ).lower()

    if not message:
        return False

    if (
        "saida inicial muito baixa" in message
        or "low_initial_output" in message
        or "sem metricas" in message
        or "renderer" in message
        or "monitor_not_ready" in message
        or "game_bridge_not_ready" in message
        or "game_window_not_ready" in message
    ):
        return False

    return (
        "no_initial_ack" in message
        or "sem ack inicial" in message
        or "rtmp_ack_missing" in message
    )


def _proc_snapshot(
    pid: int,
) -> ProcessSnapshot | None:
    proc = Path(
        "/proc"
    ) / str(
        int(pid)
    )

    if not proc.exists():
        return None

    try:
        status_text = (
            proc
            / "status"
        ).read_text(
            encoding="utf-8",
            errors="replace",
        )

        for status_line in (
            status_text.splitlines()
        ):
            if not status_line.startswith(
                "State:"
            ):
                continue

            process_state = (
                status_line.split(
                    ":",
                    1,
                )[1].strip()
            )

            if process_state.startswith(
                "Z"
            ):
                return None

            break

    except Exception:
        pass

    try:
        cwd = str(
            (proc / "cwd").resolve()
        )
    except Exception:
        cwd = ""

    try:
        raw = (
            proc
            / "cmdline"
        ).read_bytes()

        command = (
            raw.replace(
                b"\x00",
                b" ",
            )
            .decode(
                "utf-8",
                errors="replace",
            )
            .strip()
        )
    except Exception:
        command = ""

    return ProcessSnapshot(
        pid=int(pid),
        cwd=cwd,
        command=command,
    )


def _owned_process(
    role: str,
    pid: int,
    *,
    session_id: str = "",
) -> bool:
    snapshot = _proc_snapshot(
        pid
    )

    if snapshot is None:
        return True

    role = str(
        role
        or ""
    ).strip()

    command = snapshot.command

    if role == "game":
        if "--cobrinha-live" not in command:
            return False

        if (
            snapshot.cwd
            == str(GAME_DIR)
            and str(GAME_DIR)
            in command
        ):
            return True

        return (
            snapshot.cwd
            == str(EXPORTED_GAME_DIR)
            or str(EXPORTED_GAME_DIR)
            in command
        )

    if role == "display":
        return (
            "Xephyr"
            in command
            and (
                not session_id
                or (
                    ":"
                    in command
                )
            )
        )

    if role == "capture":
        if (
            "gst_html_capture_pipeline.py"
            in command
            and (
                not session_id
                or (
                    "cobrinha_session_"
                    + session_id
                    + ".monitor"
                )
                in command
            )
        ):
            return True

        return (
            "gst-launch-1.0"
            in command
            and "ximagesrc"
            in command
            and "pulsesrc"
            in command
            and "flvmux"
            in command
            and (
                not session_id
                or (
                    "cobrinha_session_"
                    + session_id
                    + ".monitor"
                )
                in command
            )
        )

    return False


def stop_owned_process(
    role: str,
    pid: int,
) -> bool:
    _require_execution_gate()

    pid = int(pid)

    if pid <= 1:
        return False

    runtime = runtime_status()

    session_id = str(
        runtime.get("session_id")
        or ""
    ).strip()

    if not _owned_process(
        role,
        pid,
        session_id=session_id,
    ):
        return False

    try:
        os.kill(
            pid,
            signal.SIGTERM,
        )
    except ProcessLookupError:
        return True

    deadline = (
        time.monotonic()
        + 4.0
    )

    while (
        time.monotonic()
        < deadline
    ):
        if (
            _proc_snapshot(
                pid
            )
            is None
        ):
            return True

        time.sleep(
            0.10
        )

    try:
        os.kill(
            pid,
            signal.SIGKILL,
        )
    except ProcessLookupError:
        return True

    deadline = (
        time.monotonic()
        + 2.0
    )

    while (
        time.monotonic()
        < deadline
    ):
        if (
            _proc_snapshot(
                pid
            )
            is None
        ):
            return True

        time.sleep(
            0.10
        )

    return False


def _parent_pid(
    pid: int,
) -> int:
    try:
        fields = (
            Path("/proc")
            / str(int(pid))
            / "stat"
        ).read_text(
            encoding="utf-8",
            errors="replace",
        ).split()

        return int(fields[3])
    except Exception:
        return 0


def _pid_descends_from(
    pid: int,
    ancestor_pid: int,
    *,
    max_depth: int = 12,
) -> bool:
    current = int(pid)
    ancestor = int(ancestor_pid)

    if current <= 1 or ancestor <= 1:
        return False

    if current == ancestor:
        return True

    for _ in range(
        max(
            1,
            int(max_depth),
        )
    ):
        current = _parent_pid(
            current
        )

        if current <= 1:
            return False

        if current == ancestor:
            return True

    return False


def _resolve_godot_window_xid(
    game_pid: int,
    display_value: str,
    *,
    timeout_seconds: float = 20.0,
) -> int:
    _require_execution_gate()

    import subprocess

    game_pid = int(game_pid)

    if game_pid <= 1:
        return 0

    display_value = str(
        display_value
        or ":0"
    ).strip()

    env = os.environ.copy()
    env["DISPLAY"] = (
        display_value
        or ":0"
    )

    deadline = (
        time.monotonic()
        + max(
            1.0,
            float(timeout_seconds),
        )
    )

    while time.monotonic() < deadline:
        try:
            found = subprocess.run(
                [
                    "xdotool",
                    "search",
                    "--onlyvisible",
                    "--name",
                    "COBRINHA 3D",
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
        except Exception:
            return 0

        for raw_xid in (
            found.stdout
            or ""
        ).splitlines():
            raw_xid = raw_xid.strip()

            if not raw_xid.isdigit():
                continue

            owner = subprocess.run(
                [
                    "xdotool",
                    "getwindowpid",
                    raw_xid,
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )

            raw_owner = (
                owner.stdout
                or ""
            ).strip()

            if not raw_owner.isdigit():
                continue

            owner_pid = int(
                raw_owner
            )

            if (
                owner_pid == game_pid
                or _pid_descends_from(
                    owner_pid,
                    game_pid,
                )
            ):
                return int(raw_xid)

        time.sleep(0.20)

    return 0


def _wait_monitor_ready(
    *,
    timeout_seconds: float = 35.0,
    interval_seconds: float = 0.5,
) -> dict[str, Any]:
    deadline = (
        time.monotonic()
        + max(
            1.0,
            float(
                timeout_seconds
            ),
        )
    )

    last: dict[str, Any] = {}

    while (
        time.monotonic()
        < deadline
    ):
        current = (
            tiktok_monitor.status()
        )

        if isinstance(
            current,
            dict,
        ):
            last = current

        if bool(
            current.get(
                "listening"
            )
            or current.get(
                "connected"
            )
        ):
            return {
                **current,
                "ready": True,
            }

        time.sleep(
            max(
                0.10,
                float(
                    interval_seconds
                ),
            )
        )

    return {
        **last,
        "ready": False,
    }


def _wait_godot_bridge_ready(
    *,
    timeout_seconds: float = GAME_BRIDGE_TIMEOUT_SECONDS,
    interval_seconds: float = 0.25,
) -> dict[str, Any]:
    deadline = (
        time.monotonic()
        + max(
            1.0,
            float(
                timeout_seconds
            ),
        )
    )

    last: dict[str, Any] = {}

    def ready_status(
        current: dict[str, Any],
    ) -> dict[str, Any] | None:
        connected = bool(
            current.get(
                "connected"
            )
        )

        if not connected:
            return None

        last_by_type = current.get(
            "last_by_type"
        )

        if isinstance(
            last_by_type,
            dict,
        ) and (
            "hello" in last_by_type
            or "heartbeat" in last_by_type
        ):
            return {
                **current,
                "ready": True,
                "reason": "godot_bridge_ready",
            }

        connected_at = float(
            current.get(
                "connected_at"
            )
            or 0.0
        )

        if (
            connected_at > 0.0
            and time.time()
            - connected_at
            >= 1.0
        ):
            return {
                **current,
                "ready": True,
                "reason": "godot_bridge_connected",
            }

        return None

    while (
        time.monotonic()
        < deadline
    ):
        current = (
            godot_bridge.status()
        )

        if isinstance(
            current,
                dict,
        ):
            last = current

        ready = ready_status(
            current
        )

        if ready is not None:
            return ready

        time.sleep(
            max(
                0.10,
                float(
                    interval_seconds
                ),
            )
        )

    current = godot_bridge.status()
    if isinstance(
        current,
        dict,
    ):
        ready = ready_status(
            current
        )

        if ready is not None:
            return ready

        last = current

    return {
        **last,
        "ready": False,
        "reason": "timeout",
    }


def begin_real_session(
    *,
    session_id: str,
    executable: str,
    server_url: str,
    display_value: str,
    username: str,
    encoder: str,
    sink: str,
    bitrate_kbps: int,
    title: str,
    game: str,
    audience_type: str,
) -> dict[str, Any]:
    if maintenance_mode.status()["enabled"]:
        return maintenance_mode.blocked_result()

    if not _SESSION_OPERATION_LOCK.acquire(
        blocking=False
    ):
        return {
            "ok": False,
            "state": "busy",
            "message": (
                "Ja existe uma tentativa de iniciar "
                "ou parar a live em andamento."
            ),
            "runtime": runtime_status(),
        }

    try:
        active, active_state, runtime = (
            _runtime_session_active()
        )

        if active:
            return {
                "ok": True,
                "state": (
                    "already_running"
                    if active_state == "running"
                    else "already_starting"
                ),
                "message": (
                    "A live ja esta rodando "
                    "ou iniciando."
                ),
                "runtime": runtime,
            }

        external_capture = (
            _active_external_capture_from_metrics()
        )

        if external_capture:
            return {
                "ok": True,
                "state":
                    "already_running_external",
                "message": (
                    "Ja existe uma captura RTMP ativa. "
                    "Novo stream/start foi bloqueado para "
                    "evitar live duplicada no TikTok."
                ),
                "runtime": runtime,
                "external_capture":
                    external_capture,
            }

        _require_all_gates()
        session_reset = reset_live_transient_state(
            reason="live_session_start",
        )
        music_start = (
            music_runtime.choose_session_start_track()
        )

        streamlabs_attempt = 1
        max_attempts = (
            STREAMLABS_RTMP_START_MAX_ATTEMPTS
        )
        streamlabs_attempts: list[
            dict[str, Any]
        ] = []
        locked_destination: dict[
            str,
            Any,
        ] = {}

        while True:
            runner = (
                SubprocessCommandRunner()
            )
            last_destination: dict[
                str,
                Any,
            ] = {}
            skipped_destinations: list[
                dict[str, Any]
            ] = []

            def provide_destination() -> dict[str, Any]:
                nonlocal last_destination
                nonlocal locked_destination

                if locked_destination:
                    destination = dict(
                        locked_destination
                    )
                else:
                    destination = (
                        streamlabs_destination_provider(
                        title=title,
                        game=game,
                        audience_type=
                            audience_type,
                        )
                    )
                    destination = {
                        **destination,
                        **_rtmp_destination_info(
                            destination.get(
                                "destination_url"
                            )
                        ),
                        "credential_renewal": 1,
                    }
                    locked_destination = dict(
                        destination
                    )

                last_destination = destination

                if (
                    STREAMLABS_RTMP_READY_DELAY_SECONDS
                    > 0
                ):
                    time.sleep(
                        STREAMLABS_RTMP_READY_DELAY_SECONDS
                    )

                return destination

            result = start_with_rollback(
                session_id=session_id,
                executable=executable,
                server_url=server_url,
                display_value=display_value,
                username=username,
                encoder=encoder,
                sink=sink,
                bitrate_kbps=bitrate_kbps,
                runner=runner,
                monitor_start=(
                    lambda monitor_username:
                        monitor_start_call(
                            username=
                                monitor_username,
                            start_callable=
                                tiktok_monitor.start_monitor,
                        )
                ),
                monitor_wait=(
                    lambda:
                        _wait_monitor_ready()
                ),
                game_bridge_wait=(
                    lambda:
                        _wait_godot_bridge_ready()
                ),
                window_resolver=(
                    lambda game_pid, active_display:
                        _resolve_godot_window_xid(
                            game_pid,
                            active_display,
                        )
                ),
                monitor_stop=(
                    lambda:
                        monitor_stop_call(
                            stop_callable=
                                tiktok_monitor.stop_monitor,
                        )
                ),
                destination_provider=
                    provide_destination,
                process_stop=
                    stop_owned_process,
            )

            result["streamlabs"] = (
                _streamlabs_attempt_info(
                    last_destination,
                    attempt=
                        streamlabs_attempt,
                    previous_attempts=
                        streamlabs_attempts,
                    skipped_destinations=
                        skipped_destinations,
                )
            )
            result["music_start"] = music_start

            if result.get("ok"):
                result["runtime_reset"] = session_reset
                return result

            if result.get(
                "external_destination_started"
            ):
                result["streamlabs_end"] = (
                    {
                        "attempted": False,
                        "ended": False,
                        "reason":
                            "kept_open_for_local_retry",
                    }
                )
            else:
                result["streamlabs_end"] = {
                    "attempted": False,
                    "ended": False,
                    "reason":
                        "destination_not_started",
                }

            if (
                streamlabs_attempt
                >= max_attempts
                or not _should_retry_streamlabs_rtmp_start(
                    result
                )
            ):
                return result

            streamlabs_attempts.append(
                {
                    **result.get(
                        "streamlabs",
                        {},
                    ),
                    "start_error":
                        _result_error_text(
                            result
                        ),
                    "streamlabs_end":
                        result.get(
                            "streamlabs_end",
                            {},
                        ),
                }
            )
            streamlabs_attempt += 1
    finally:
        _SESSION_OPERATION_LOCK.release()


def stop_real_session() -> dict[str, Any]:
    if not _SESSION_OPERATION_LOCK.acquire(
        blocking=False
    ):
        return {
            "ok": False,
            "state": "busy",
            "message": (
                "Ja existe uma tentativa de iniciar "
                "ou parar a live em andamento."
            ),
            "runtime": runtime_status(),
        }

    try:
        _require_execution_gate()

        runtime_before_stop = runtime_status()
        completed_live = (
            str(runtime_before_stop.get("state") or "").lower()
            == "running"
        )

        runner = (
            SubprocessCommandRunner()
        )

        result = rollback_with_adapters(
            runner=runner,
            monitor_stop=(
                lambda:
                    monitor_stop_call(
                        stop_callable=
                            tiktok_monitor.stop_monitor,
                    )
            ),
            process_stop=
                stop_owned_process,
        )

        result["streamlabs_end"] = (
            _end_streamlabs_live_if_available()
        )
        result["runtime_reset"] = reset_live_transient_state(
            reason="live_session_stop",
        )

        if result.get("ok") and completed_live:
            processing = song_runtime.status().get("processing")
            processing_active = (
                isinstance(processing, dict)
                and bool(processing.get("active"))
            )
            result["automatic_cleanup"] = (
                runtime_cleanup.register_completed_live(
                    processing_active=processing_active,
                )
            )
        else:
            result["automatic_cleanup"] = {
                "counted": False,
                "reason": (
                    "stop_failed"
                    if not result.get("ok")
                    else "session_was_not_running"
                ),
                **runtime_cleanup.automatic_cleanup_status(),
            }

        return result
    finally:
        _SESSION_OPERATION_LOCK.release()


def _end_streamlabs_live_if_available() -> dict[str, Any]:
    if not network_enabled():
        return {
            "attempted": False,
            "ended": False,
            "reason":
                "streamlabs_network_disabled",
        }

    try:
        return end_last_live_with_transport(
            transport=http_json_transport,
        )
    except Exception as exc:
        return {
            "attempted": False,
            "ended": False,
            "error":
                f"{type(exc).__name__}:{exc}",
        }


def public_status() -> dict[str, Any]:
    return {
        "real_execution_enabled":
            real_execution_enabled(),
        "streamlabs_network_enabled":
            network_enabled(),
        "streamlabs_endpoint_present":
            True,
        "streamlabs_endpoint_source":
            (
                "environment"
                if str(
                    os.environ.get(
                        STREAMLABS_START_ENDPOINT_ENV,
                        ""
                    )
                    or ""
                ).strip()
                else "default_reference"
            ),
        "runtime":
            runtime_status(),
        "external_capture":
            _active_external_capture_from_metrics(),
    }
