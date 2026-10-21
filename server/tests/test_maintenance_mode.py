import asyncio
import json

from starlette.requests import Request

from cobrinha_server.routes import session
from cobrinha_server.services import maintenance_mode


def test_maintenance_status_reads_distribution_flag(monkeypatch, tmp_path):
    config = tmp_path / "maintenance.json"
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "title": "Pausa técnica",
                "message": "Aguarde a atualização.",
                "owner": "Thiago Reyes",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(maintenance_mode, "CONFIG_FILE", config)

    current = maintenance_mode.status()

    assert current["enabled"] is True
    assert current["title"] == "Pausa técnica"
    assert current["message"] == "Aguarde a atualização."


def test_live_start_is_blocked_during_maintenance(monkeypatch):
    monkeypatch.setattr(
        maintenance_mode,
        "status",
        lambda: {
            "enabled": True,
            "title": "Sistema em manutenção",
            "message": "Aguarde a atualização.",
            "owner": "Thiago Reyes",
        },
    )

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request(
        {"type": "http", "method": "POST", "path": "/api/session/start", "headers": []},
        receive,
    )
    response = asyncio.run(session.api_session_start(request))

    assert response.status_code == 503
    assert b'"state":"maintenance"' in response.body
