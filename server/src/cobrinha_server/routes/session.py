from __future__ import annotations

import os
from typing import Any

from fastapi import (
    APIRouter,
    Request,
)
from fastapi.responses import (
    JSONResponse,
)
from starlette.concurrency import (
    run_in_threadpool,
)

from cobrinha_server.services.session_control import (
    build_sequence,
    config_from_payload,
    normalize_runtime_encoder,
    normalize_runtime_sink,
    public_config,
    runtime_preview,
    save_config,
    status,
)
from cobrinha_server.services.session_real_bridge import (
    begin_real_session,
    public_status as real_bridge_status,
    stop_real_session,
)
from cobrinha_server.services.session_scheduler import (
    run_check_once as session_schedule_check,
    status as session_schedule_status,
)
from cobrinha_server.services.session_executor_plan import (
    preferred_live_executable,
)
from cobrinha_server.services import (
    maintenance_mode,
    windows_desktop_session,
)


router = APIRouter()


@router.get(
    "/api/session/status",
    response_class=JSONResponse,
)
def api_session_status() -> JSONResponse:
    session_state = status()
    if os.name == "nt":
        windows_state = (
            windows_desktop_session.status()
        )
        session_state[
            "windows_desktop"
        ] = windows_state
        session_state[
            "platform_mode"
        ] = "windows_screen_capture"
        if windows_state.get("running"):
            session_state["state"] = "running"
            session_state[
                "control_state"
            ] = "running"

    return JSONResponse(
        {
            "ok": True,
            "maintenance": maintenance_mode.status(),
            "session": session_state,
            "schedule":
                session_schedule_status(),
            "sequence": build_sequence(),
        }
    )


@router.get(
    "/api/session/config",
    response_class=JSONResponse,
)
def api_session_config() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "config": public_config(),
        }
    )


@router.post(
    "/api/session/config",
    response_class=JSONResponse,
)
async def api_session_config_save(
    request: Request,
) -> JSONResponse:
    try:
        payload: Any = (
            await request.json()
        )
    except Exception:
        payload = {}

    if not isinstance(
        payload,
        dict,
    ):
        payload = {}

    config = save_config(
        payload
    )

    return JSONResponse(
        {
            "ok": True,
            "config": config,
        }
    )


@router.get(
    "/api/session/schedule",
    response_class=JSONResponse,
)
def api_session_schedule() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "schedule":
                session_schedule_status(),
        }
    )


@router.post(
    "/api/session/schedule/check",
    response_class=JSONResponse,
)
def api_session_schedule_check() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "schedule":
                session_schedule_check(),
        }
    )


@router.get(
    "/api/session/runtime-preview",
    response_class=JSONResponse,
)
def api_session_runtime_preview() -> JSONResponse:
    config = public_config()

    preview = runtime_preview(
        session_id="panel-preview",
        executable=preferred_live_executable(
            config.get("godot_executable"),
            config.get("game_runtime"),
        ),
        server_url="http://127.0.0.1:9393",
        display_value=str(
            config.get("display")
            or ":0"
        ),
        encoder=str(
            config.get("video_encoder")
            or "x264enc"
        ),
        sink=str(
            config.get("rtmp_sink")
            or "rtmp2sink"
        ),
        bitrate_kbps=int(
            config.get("video_bitrate")
            or 2500
        ),
    )

    return JSONResponse(
        {
            "ok": True,
            "execution": False,
            "preview": preview,
        }
    )


@router.post(
    "/api/session/plan",
    response_class=JSONResponse,
)
async def api_session_plan(
    request: Request,
) -> JSONResponse:
    try:
        payload: Any = (
            await request.json()
        )
    except Exception:
        payload = {}

    if not isinstance(
        payload,
        dict,
    ):
        payload = {}

    config = config_from_payload(
        payload
    )

    return JSONResponse(
        {
            "ok": True,
            "execution": False,
            "requested_action": str(
                payload.get("action")
                or "start"
            ),
            "config": config,
            "sequence": build_sequence(),
            "message": (
                "Plano preparado. "
                "Nenhum componente foi acionado."
            ),
        }
    )


@router.post(
    "/api/session/start",
    response_class=JSONResponse,
)
async def api_session_start(
    request: Request,
) -> JSONResponse:
    if maintenance_mode.status()["enabled"]:
        return JSONResponse(
            maintenance_mode.blocked_result(),
            status_code=503,
        )

    try:
        payload: Any = (
            await request.json()
        )
    except Exception:
        payload = {}

    if not isinstance(
        payload,
        dict,
    ):
        payload = {}

    config = (
        save_config(payload)
        if any(
            key in payload
            for key in (
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
                "rtmp_sink",
                "display",
                "auto_start_enabled",
                "start_min_seconds",
                "start_max_seconds",
                "stop_min_seconds",
                "stop_max_seconds",
            )
        )
        else public_config()
    )

    if os.name == "nt":
        result = await run_in_threadpool(
            windows_desktop_session.start,
            executable=preferred_live_executable(
                payload.get("executable")
                or config.get("godot_executable"),
                payload.get("game_runtime")
                or config.get("game_runtime"),
            ),
            server_url=str(
                payload.get("server_url")
                or "http://127.0.0.1:9393"
            ),
            username=str(
                payload.get("username")
                or config.get("username")
                or ""
            ),
            monitor_server=str(
                payload.get("monitor_server")
                or config.get("monitor_server")
                or "http://127.0.0.1:2619"
            ),
        )
        return JSONResponse(
            result,
            status_code=(
                200 if result.get("ok") else 500
            ),
        )

    bridge = real_bridge_status()

    if not (
        bridge.get("real_execution_enabled")
        and bridge.get("streamlabs_network_enabled")
        and bridge.get("streamlabs_endpoint_present")
    ):
        return JSONResponse(
            {
                "ok": False,
                "execution": False,
                "state": "authorization_required",
                "message": (
                    "Os gates de execucao externa "
                    "continuam desabilitados."
                ),
                "sequence": build_sequence(),
            },
            status_code=409,
        )

    result = await run_in_threadpool(
        begin_real_session,
        session_id=str(
            payload.get("session_id")
            or "panel"
        ),
        executable=preferred_live_executable(
            payload.get("executable")
            or config.get(
                "godot_executable"
            ),
            payload.get("game_runtime")
            or config.get(
                "game_runtime"
            ),
        ),
        server_url=str(
            payload.get("server_url")
            or "http://127.0.0.1:9393"
        ),
        display_value=str(
            payload.get("display")
            or config.get("display")
            or ":0"
        ),
        username=str(
            payload.get("username")
            or config.get("username")
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

    return JSONResponse(
        result,
        status_code=(
            200
            if result.get("ok")
            else 500
        ),
    )


@router.post(
    "/api/session/stop",
    response_class=JSONResponse,
)
async def api_session_stop(
    request: Request,
) -> JSONResponse:
    try:
        payload: Any = (
            await request.json()
        )
    except Exception:
        payload = {}

    if not isinstance(
        payload,
        dict,
    ):
        payload = {}

    if os.name == "nt":
        result = await run_in_threadpool(
            windows_desktop_session.stop
        )
        return JSONResponse(
            result,
            status_code=(
                200 if result.get("ok") else 500
            ),
        )

    bridge = real_bridge_status()

    if not bridge.get(
        "real_execution_enabled"
    ):
        return JSONResponse(
            {
                "ok": False,
                "execution": False,
                "state":
                    "authorization_required",
                "message": (
                    "Os gates de execucao externa "
                    "continuam desabilitados."
                ),
            },
            status_code=409,
        )

    result = stop_real_session()

    return JSONResponse(
        result,
        status_code=(
            200
            if result.get("ok")
            else 500
        ),
    )
