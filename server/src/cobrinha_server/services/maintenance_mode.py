from __future__ import annotations

from typing import Any

from cobrinha_server.core.json_store import read_json
from cobrinha_server.core.settings import PROJECT_DIR


CONFIG_FILE = PROJECT_DIR.parent / "maintenance.json"
DEFAULT_MESSAGE = (
    "As lives estão temporariamente bloqueadas enquanto preparamos "
    "a próxima versão oficial do Snake Reyers."
)


def status() -> dict[str, Any]:
    payload = read_json(CONFIG_FILE, {})
    if not isinstance(payload, dict):
        payload = {}
    return {
        "enabled": bool(payload.get("enabled")),
        "title": str(payload.get("title") or "Sistema em manutenção"),
        "message": str(payload.get("message") or DEFAULT_MESSAGE),
        "owner": str(payload.get("owner") or "Thiago Reyes"),
    }


def blocked_result() -> dict[str, Any]:
    current = status()
    return {
        "ok": False,
        "execution": False,
        "state": "maintenance",
        "message": current["message"],
        "maintenance": current,
    }
