#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVER_DIR="${ROOT_DIR}/server"
MONITOR_DIR="${SERVER_DIR}/external/tiktok-live-monitoring-server"
DOWNLOAD_DIR="${TMPDIR:-/tmp}/snake-reyers-installer"

for command in python3 node npm; do
	if ! command -v "${command}" >/dev/null 2>&1; then
		echo "Dependencia ausente: ${command}" >&2
		exit 2
	fi
done

NODE_MAJOR="$(node -p 'Number(process.versions.node.split(".")[0])')"
if (( NODE_MAJOR < 22 )); then
	echo "Node.js 22 ou superior e necessario. Versao atual: $(node --version)" >&2
	exit 2
fi

python3 -m venv "${SERVER_DIR}/.venv"
"${SERVER_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${SERVER_DIR}/.venv/bin/python" -m pip install -r "${SERVER_DIR}/requirements.txt"

PYTHON="${SERVER_DIR}/.venv/bin/python"
if command -v nvidia-smi >/dev/null 2>&1; then
	if ! "${PYTHON}" -m pip install \
		--index-url https://download.pytorch.org/whl/cu126 \
		torch==2.7.1 torchaudio==2.7.1; then
		"${PYTHON}" -m pip install \
			--index-url https://download.pytorch.org/whl/cpu \
			torch==2.7.1 torchaudio==2.7.1
	fi
else
	"${PYTHON}" -m pip install \
		--index-url https://download.pytorch.org/whl/cpu \
		torch==2.7.1 torchaudio==2.7.1
fi
"${PYTHON}" -m pip install -r "${SERVER_DIR}/requirements-voice.txt"

export TTS_HOME="${SERVER_DIR}/runtime/tts"
TTS_MODEL_FILE="${TTS_HOME}/tts/tts_models--multilingual--multi-dataset--xtts_v2/model.pth"
if [[ "${SNAKE_REYERS_SKIP_TTS_MODEL:-0}" != "1" ]] && [[ ! -f "${TTS_MODEL_FILE}" ]]; then
	if [[ "${SNAKE_REYERS_ACCEPT_COQUI_LICENSE:-0}" != "1" ]]; then
		printf '%s\n' "O XTTS usa a Coqui Public Model License (CPML)."
		read -r -p "Voce aceita a CPML para baixar o modelo XTTS agora? [S/N] " answer
		if [[ ! "${answer}" =~ ^[SsYy]$ ]]; then
			echo "A instalacao completa exige aceitar a licenca do modelo XTTS." >&2
			exit 3
		fi
	fi
	COQUI_TOS_AGREED=1 "${PYTHON}" "${SERVER_DIR}/scripts/prepare_tts_model.py"
elif [[ -f "${TTS_MODEL_FILE}" ]]; then
	echo "Modelo XTTS ja esta preparado."
fi

install_audio_packages() {
	local manifest="${SERVER_DIR}/assets/audio-manifest.json"
	local marker="${SERVER_DIR}/assets/.audio-package-version"
	local manifest_hash

	[[ -f "${manifest}" ]] || {
		echo "Manifesto dos pacotes de audio ausente: ${manifest}" >&2
		exit 4
	}

	manifest_hash="$(sha256sum "${manifest}" | awk '{print $1}')"
	if [[ -f "${marker}" ]] && [[ "$(cat "${marker}")" == "${manifest_hash}" ]]; then
		echo "Vozes e musicas ja estao instaladas."
		return
	fi

	mkdir -p "${DOWNLOAD_DIR}"
	"${PYTHON}" - "${manifest}" "${ROOT_DIR}" "${DOWNLOAD_DIR}" <<'PY'
import hashlib
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

manifest_path = Path(sys.argv[1])
root = Path(sys.argv[2])
download_dir = Path(sys.argv[3])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

for pack in manifest["packs"]:
    name = pack["name"]
    expected = pack["sha256"].lower()
    candidates = (root.parent / name, root / name, root / "packages" / name)
    archive = next((path for path in candidates if path.is_file()), download_dir / name)
    downloaded_archive = archive.parent == download_dir
    if not archive.is_file():
        required = int(pack.get("bytes") or 0) + 512 * 1024 * 1024
        free = shutil.disk_usage(download_dir).free
        if required > 0 and free < required:
            raise SystemExit(
                f"Espaco insuficiente para baixar {name}: "
                f"{free / 1024**3:.1f} GB livres; "
                f"{required / 1024**3:.1f} GB necessarios agora."
            )
        print(f"Baixando {name}...")
        urllib.request.urlretrieve(pack["url"], archive)
    hasher = hashlib.sha256()
    with archive.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != expected:
        raise SystemExit(f"Pacote de audio invalido: {archive}")
    print(f"Instalando {name}...")
    try:
        with zipfile.ZipFile(archive) as package:
            expanded = sum(item.file_size for item in package.infolist())
            free = shutil.disk_usage(root).free
            required = expanded + 512 * 1024 * 1024
            if free < required:
                raise SystemExit(
                    f"Espaco insuficiente para extrair {name}: "
                    f"{free / 1024**3:.1f} GB livres; "
                    f"{required / 1024**3:.1f} GB necessarios agora."
                )
            package.extractall(root)
    finally:
        if downloaded_archive:
            archive.unlink(missing_ok=True)
PY
	printf '%s' "${manifest_hash}" >"${marker}"
}

install_audio_packages

if [[ -f "${MONITOR_DIR}/package-lock.json" ]]; then
	(cd "${MONITOR_DIR}" && npm ci --omit=dev)
else
	(cd "${MONITOR_DIR}" && npm install --omit=dev)
fi

chmod +x "${ROOT_DIR}/game/linux/SnakeReyers.x86_64" 2>/dev/null || true
chmod +x "${ROOT_DIR}/launcher/create_shortcut_linux.sh" 2>/dev/null || true
mkdir -p "${SERVER_DIR}/private" "${SERVER_DIR}/runs/logs"

if ! "${ROOT_DIR}/launcher/create_shortcut_linux.sh"; then
	echo "Aviso: nao foi possivel criar o atalho na Area de Trabalho." >&2
fi

echo "Snake Reyers instalado com TTS, vozes e musicas. Use o atalho da Area de Trabalho para iniciar."
