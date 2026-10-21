from __future__ import annotations

from fastapi import (
    APIRouter,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
)
from fastapi.templating import (
    Jinja2Templates,
)
from starlette.concurrency import (
    run_in_threadpool,
)

from cobrinha_server.bridge.godot_bridge import (
    godot_bridge,
    preview_bridge,
)
from cobrinha_server.core.settings import (
    PROJECT_DIR,
)
from cobrinha_server.services import (
    content_updates,
    maintenance_mode,
    music_runtime,
    runtime_cleanup,
    song_runtime,
    system_updates,
    user_ai_settings,
)
from cobrinha_server.services.session_control import (
    status as session_status,
)
from cobrinha_server.services.session_real_bridge import (
    public_status as real_bridge_status,
)


router = APIRouter()

templates = Jinja2Templates(
    directory=str(
        PROJECT_DIR
        / "src"
        / "cobrinha_server"
        / "templates"
    )
)


def _live_runtime_active() -> bool:
    session = session_status()
    bridge = real_bridge_status()
    runtime = bridge.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    active_states = {"starting", "running", "stopping"}
    return (
        str(session.get("state") or "").lower() in active_states
        or str(runtime.get("state") or "").lower() in active_states
    )


def _cleanup_block_reason() -> str:
    if _live_runtime_active():
        return "A limpeza só pode ser executada com a live parada."

    processing = song_runtime.status().get("processing")
    if isinstance(processing, dict) and bool(processing.get("active")):
        return "Aguarde o processamento da música terminar."

    return ""


def _system_update_status() -> dict[str, object]:
    status = system_updates.public_status()
    live_active = _live_runtime_active()
    return {
        **status,
        "live_active": live_active,
        "can_install": (
            bool(status.get("update_available"))
            and not bool(status.get("busy"))
            and not live_active
        ),
    }


@router.get(
    "/api/system-updates",
    response_class=JSONResponse,
)
def api_system_updates_status() -> JSONResponse:
    return JSONResponse(_system_update_status())


@router.post(
    "/api/system-updates/check",
    response_class=JSONResponse,
)
async def api_system_updates_check() -> JSONResponse:
    try:
        await run_in_threadpool(
            system_updates.synchronize,
            install=False,
        )
    except system_updates.SystemUpdateBusy as exc:
        return JSONResponse(
            {**_system_update_status(), "ok": False, "error": str(exc)},
            status_code=409,
        )
    except Exception as exc:
        return JSONResponse(
            {**_system_update_status(), "ok": False, "error": str(exc)},
            status_code=502,
        )
    return JSONResponse(_system_update_status())


@router.post(
    "/api/system-updates/install",
    response_class=JSONResponse,
)
async def api_system_updates_install() -> JSONResponse:
    if _live_runtime_active():
        return JSONResponse(
            {
                **_system_update_status(),
                "ok": False,
                "error": "Finalize a live antes de atualizar o sistema.",
            },
            status_code=409,
        )

    try:
        result = await run_in_threadpool(
            system_updates.synchronize,
            install=True,
        )
        if result.get("installed"):
            system_updates.schedule_restart()
            result = _system_update_status()
    except system_updates.SystemUpdateBusy as exc:
        return JSONResponse(
            {**_system_update_status(), "ok": False, "error": str(exc)},
            status_code=409,
        )
    except Exception as exc:
        return JSONResponse(
            {**_system_update_status(), "ok": False, "error": str(exc)},
            status_code=502,
        )
    return JSONResponse(result)


@router.get(
    "/api/content-updates",
    response_class=JSONResponse,
)
def api_content_updates_status() -> JSONResponse:
    return JSONResponse(content_updates.public_status())


@router.post(
    "/api/content-updates/check",
    response_class=JSONResponse,
)
async def api_content_updates_check() -> JSONResponse:
    try:
        result = await run_in_threadpool(
            content_updates.synchronize,
            install=False,
        )
    except content_updates.ContentUpdateBusy as exc:
        return JSONResponse(
            {**content_updates.public_status(), "ok": False, "error": str(exc)},
            status_code=409,
        )
    except Exception as exc:
        return JSONResponse(
            {**content_updates.public_status(), "ok": False, "error": str(exc)},
            status_code=502,
        )
    return JSONResponse(result)


@router.post(
    "/api/content-updates/install",
    response_class=JSONResponse,
)
async def api_content_updates_install() -> JSONResponse:
    try:
        result = await run_in_threadpool(
            content_updates.synchronize,
            install=True,
        )
    except (
        content_updates.ContentUpdateBusy,
        content_updates.ContentUpdateBlocked,
    ) as exc:
        return JSONResponse(
            {**content_updates.public_status(), "ok": False, "error": str(exc)},
            status_code=409,
        )
    except Exception as exc:
        return JSONResponse(
            {**content_updates.public_status(), "ok": False, "error": str(exc)},
            status_code=502,
        )
    return JSONResponse(result)


@router.post(
    "/api/content-updates/settings",
    response_class=JSONResponse,
)
async def api_content_updates_settings(
    request: Request,
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    configuration = content_updates.update_configuration(
        auto_install=payload.get("auto_install", True),
    )
    return JSONResponse(
        {
            **content_updates.public_status(),
            "ok": True,
            "configuration": configuration,
        }
    )


@router.get(
    "/",
    response_class=HTMLResponse,
)
def panel(
    request: Request,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="panel.html",
        context={"maintenance": maintenance_mode.status()},
    )


@router.get(
    "/api/settings/openai",
    response_class=JSONResponse,
)
def api_openai_settings() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "settings": user_ai_settings.public_status(),
        }
    )


@router.post(
    "/api/settings/openai",
    response_class=JSONResponse,
)
async def api_openai_settings_update(
    request: Request,
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        settings = user_ai_settings.update(payload)
    except ValueError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=400,
        )
    return JSONResponse(
        {"ok": True, "settings": settings}
    )


@router.get(
    "/api/maintenance/cleanup",
    response_class=JSONResponse,
)
async def api_cleanup_analysis() -> JSONResponse:
    analysis = await run_in_threadpool(runtime_cleanup.analyze)
    blocked_reason = _cleanup_block_reason()
    return JSONResponse(
        {
            **analysis,
            "live_active": _live_runtime_active(),
            "blocked": bool(blocked_reason),
            "blocked_reason": blocked_reason,
        }
    )


@router.post(
    "/api/maintenance/cleanup",
    response_class=JSONResponse,
)
async def api_cleanup_execute() -> JSONResponse:
    blocked_reason = _cleanup_block_reason()
    if blocked_reason:
        return JSONResponse(
            {
                "ok": False,
                "error": blocked_reason,
                "live_active": _live_runtime_active(),
            },
            status_code=409,
        )

    result = await run_in_threadpool(runtime_cleanup.cleanup)
    return JSONResponse(
        {
            **result,
            "live_active": False,
        },
        status_code=200 if result.get("ok") else 500,
    )

@router.get(
    "/api/music/runtime",
    response_class=JSONResponse,
)
def api_music_runtime_status() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "music":
                music_runtime.status(),
        }
    )


@router.get(
    "/api/songs/runtime",
    response_class=JSONResponse,
)
def api_song_runtime_status() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "song_runtime":
                song_runtime.status(),
        }
    )


@router.post(
    "/api/songs/process",
    response_class=JSONResponse,
)
async def api_song_process(
    request: Request,
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    if not isinstance(
        payload,
        dict,
    ):
        payload = {}

    try:
        state = song_runtime.start_processing(
            payload.get(
                "source_path",
                payload.get(
                    "path",
                    "",
                ),
            )
        )
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error":
                    f"{type(exc).__name__}: {exc}",
                "song_runtime":
                    song_runtime.status(),
            },
            status_code=400,
        )

    return JSONResponse(
        {
            "ok": True,
            "song_runtime": state,
        }
    )


@router.post(
    "/api/songs/gift-map",
    response_class=JSONResponse,
)
async def api_song_gift_map_update(
    request: Request,
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    if not isinstance(
        payload,
        dict,
    ):
        payload = {}

    try:
        append_raw = payload.get(
            "append",
            True,
        )
        append = str(
            append_raw
        ).strip().lower() not in {
            "0",
            "false",
            "nao",
            "no",
            "replace",
        }
        mapping = song_runtime.update_gift_mapping(
            gift_name=payload.get(
                "gift_name",
                "",
            ),
            song_id=payload.get(
                "song_id",
                "",
            ),
            append=append,
        )
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error":
                    f"{type(exc).__name__}: {exc}",
                "song_runtime":
                    song_runtime.status(),
            },
            status_code=400,
        )

    return JSONResponse(
        {
            "ok": True,
            "mapping": mapping,
            "song_runtime":
                song_runtime.status(),
        }
    )


@router.post(
    "/api/songs/gift-map/remove",
    response_class=JSONResponse,
)
async def api_song_gift_map_remove(
    request: Request,
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    if not isinstance(
        payload,
        dict,
    ):
        payload = {}

    removed = song_runtime.remove_gift_mapping(
        gift_name=payload.get(
            "gift_name",
            "",
        ),
        song_id=payload.get(
            "song_id",
            "",
        ),
    )

    return JSONResponse(
        {
            "ok": True,
            "removed": removed,
            "song_runtime":
                song_runtime.status(),
        }
    )


@router.post(
    "/api/songs/play",
    response_class=JSONResponse,
)
async def api_song_play(
    request: Request,
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    if not isinstance(
        payload,
        dict,
    ):
        payload = {}

    try:
        message = song_runtime.song_ready_message(
            payload.get(
                "song_id",
                "",
            )
        )
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error":
                    f"{type(exc).__name__}: {exc}",
                "song_runtime":
                    song_runtime.status(),
            },
            status_code=400,
        )

    official_sent = False
    preview_sent = False

    try:
        official_sent = bool(
            await godot_bridge.send(
                message
            )
        )
    except Exception:
        official_sent = False

    try:
        preview_sent = bool(
            await preview_bridge.send(
                message
            )
        )
    except Exception:
        preview_sent = False

    song_payload = message.get(
        "payload",
        {},
    )

    if isinstance(
        song_payload,
        dict,
    ):
        song_runtime.mark_playing(
            song_payload.get(
                "song_id",
                "",
            ),
            song_payload.get(
                "title",
                "",
            ),
            duration=song_payload.get(
                "timeline_end",
                0.0,
            ),
        )

    return JSONResponse(
        {
            "ok": True,
            "message": message,
            "official_sent": official_sent,
            "preview_sent": preview_sent,
            "song_runtime":
                song_runtime.status(),
        }
    )


@router.post(
    "/api/songs/stop",
    response_class=JSONResponse,
)
async def api_song_stop() -> JSONResponse:
    message = song_runtime.song_stop_message()

    official_sent = False
    preview_sent = False

    try:
        official_sent = bool(
            await godot_bridge.send(
                message
            )
        )
    except Exception:
        official_sent = False

    try:
        preview_sent = bool(
            await preview_bridge.send(
                message
            )
        )
    except Exception:
        preview_sent = False

    song_runtime.mark_idle()

    return JSONResponse(
        {
            "ok": True,
            "message": message,
            "official_sent": official_sent,
            "preview_sent": preview_sent,
            "song_runtime":
                song_runtime.status(),
        }
    )


@router.post(
    "/api/music/runtime",
    response_class=JSONResponse,
)
async def api_music_runtime_update(
    request: Request,
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    if not isinstance(
        payload,
        dict,
    ):
        payload = {}

    state = music_runtime.update(
        volume=(
            payload.get("volume")
            if "volume" in payload
            else None
        ),
        enabled=(
            payload.get("enabled")
            if "enabled" in payload
            else None
        ),
        current_track=(
            payload.get("current_track")
            if "current_track" in payload
            else None
        ),
    )

    message = (
        music_runtime.websocket_message()
    )

    official_sent = False
    preview_sent = False

    try:
        official_sent = bool(
            await godot_bridge.send(
                message
            )
        )
    except Exception:
        official_sent = False

    try:
        preview_sent = bool(
            await preview_bridge.send(
                message
            )
        )
    except Exception:
        preview_sent = False

    return JSONResponse(
        {
            "ok": True,
            "music": state,
            "official_sent":
                official_sent,
            "preview_sent":
                preview_sent,
        }
    )
