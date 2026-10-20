from __future__ import annotations

import importlib.util
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from cobrinha_server.core.json_store import (
    read_json,
    write_json_atomic,
)
from cobrinha_server.core.settings import (
    ASSETS_DIR,
    CONFIG_DIR,
    LOGS_DIR,
    PRIVATE_DIR,
    RUNS_DIR,
)
from cobrinha_server.services.audio_timeline import (
    write_audio_timeline_json,
)


SINGING_DIR = ASSETS_DIR / "singing"
SONG_INPUT_DIR = SINGING_DIR / "originals"
SONG_PROCESSED_DIR = SINGING_DIR / "processed"
SONG_RUNTIME_FILE = RUNS_DIR / "song_runtime.json"
SONG_GIFT_MAP_FILE = PRIVATE_DIR / "song_gift_map.json"
DEFAULT_SONG_GIFT_MAP_FILE = CONFIG_DIR / "song_gift_map.json"
SONG_GIFT_PROGRESS_FILE = RUNS_DIR / "song_gift_progress.json"
SONG_PROCESS_LOG_FILE = LOGS_DIR / "song_processing.log"
DEMUX_RUNS_DIR = RUNS_DIR / "demucs"

DEFAULT_GIFT_INITIAL_SING_COUNT = 1
DEFAULT_GIFT_REPEAT_SING_COUNT = 10
HIGH_VALUE_GIFT_IMMEDIATE_COINS = 30
SONG_ACTIVE_STALE_GRACE_SECONDS = 1.5
HEART_ME_BAND_GIFT_KEYS = {
    "heart-me",
    "heartme",
    "hartme",
    "hatme",
}
HEART_ME_BAND_CANONICAL_GIFT_KEY = "heart-me"
HEART_ME_BAND_SPEECH_HOLD_SECONDS = 10.0

SUPPORTED_AUDIO_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
}

_LOCK = threading.Lock()
_PROCESS_THREAD: threading.Thread | None = None
_GIFT_AUDIO_DIAGNOSTICS_CACHE: dict[str, Any] = {}


def _ensure_dirs() -> None:
    for path in (
        SINGING_DIR,
        SONG_INPUT_DIR,
        SONG_PROCESSED_DIR,
        DEMUX_RUNS_DIR,
        LOGS_DIR,
    ):
        path.mkdir(
            parents=True,
            exist_ok=True,
        )


def demucs_available() -> bool:
    return (
        importlib.util.find_spec(
            "demucs"
        )
        is not None
    )


def ffmpeg_available() -> bool:
    return shutil.which(
        "ffmpeg"
    ) is not None


def _safe_slug(value: str) -> str:
    normalized = (
        unicodedata.normalize(
            "NFKD",
            value,
        )
        .encode(
            "ascii",
            "ignore",
        )
        .decode(
            "ascii",
        )
        .lower()
    )

    slug = re.sub(
        r"[^a-z0-9]+",
        "-",
        normalized,
    ).strip("-")

    return slug or "musica"


def _gift_key(value: object) -> str:
    return _safe_slug(
        str(
            value
            or ""
        )
    )


def _canonical_gift_key(value: object) -> str:
    gift_key = _gift_key(
        value
    )
    if gift_key in HEART_ME_BAND_GIFT_KEYS:
        return HEART_ME_BAND_CANONICAL_GIFT_KEY
    return gift_key


def _audio_file_info(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    stat = resolved.stat()

    return {
        "name": resolved.name,
        "stem": resolved.stem,
        "path": str(resolved),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def source_tracks() -> list[dict[str, Any]]:
    _ensure_dirs()

    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    for base in (
        SONG_INPUT_DIR,
    ):
        if not base.is_dir():
            continue

        for path in sorted(
            base.rglob("*"),
            key=lambda item:
                item.relative_to(
                    base
                ).as_posix().casefold(),
        ):
            if (
                not path.is_file()
                or path.name.startswith(".")
                or path.suffix.casefold()
                not in SUPPORTED_AUDIO_EXTENSIONS
            ):
                continue

            resolved = str(
                path.resolve()
            )

            if resolved in seen:
                continue

            seen.add(resolved)
            info = _audio_file_info(path)
            relative_path = path.relative_to(
                base
            )
            info["relative_path"] = (
                relative_path.as_posix()
            )
            info["group"] = (
                ""
                if relative_path.parent == Path(".")
                else relative_path.parent.as_posix()
            )
            result.append(info)

    return result


def _manifest_path(song_id: str) -> Path:
    return (
        SONG_PROCESSED_DIR
        / song_id
        / "manifest.json"
    )


def _processed_song_from_manifest(
    manifest_path: Path,
) -> dict[str, Any] | None:
    raw = read_json(
        manifest_path,
        {},
    )

    if not isinstance(
        raw,
        dict,
    ):
        return None

    files = raw.get(
        "files",
        {},
    )

    if not isinstance(
        files,
        dict,
    ):
        files = {}

    resolved_files: dict[str, str] = {}
    for key, fallback in (
        ("vocal", "vocals.wav"),
        ("instrumental", "instrumental.wav"),
        ("timeline", "vocal_timeline.json"),
    ):
        configured = Path(
            str(
                files.get(key)
                or ""
            )
        ).expanduser()
        local = manifest_path.parent / fallback
        selected = (
            local
            if local.is_file()
            else configured
        )
        if selected.is_file():
            resolved_files[key] = str(
                selected.resolve()
            )

    ready = len(resolved_files) == 3
    raw["files"] = {
        **files,
        **resolved_files,
    }

    raw["ready"] = ready
    return raw


def processed_songs() -> list[dict[str, Any]]:
    _ensure_dirs()

    result: list[dict[str, Any]] = []
    seen_song_ids: set[str] = set()

    for manifest_path in sorted(
        SONG_PROCESSED_DIR.rglob("manifest.json"),
        key=lambda item: (
            len(item.relative_to(SONG_PROCESSED_DIR).parts),
            item.relative_to(SONG_PROCESSED_DIR).as_posix().casefold(),
        ),
    ):
        song = _processed_song_from_manifest(
            manifest_path
        )

        if song is not None:
            song_id = str(
                song.get("id")
                or manifest_path.parent.name
            ).strip()
            if not song_id or song_id in seen_song_ids:
                continue
            seen_song_ids.add(song_id)
            result.append(song)

    return result


def _read_state() -> dict[str, Any]:
    raw = read_json(
        SONG_RUNTIME_FILE,
        {},
    )

    if not isinstance(
        raw,
        dict,
    ):
        raw = {}

    processing = raw.get(
        "processing",
        {},
    )

    if not isinstance(
        processing,
        dict,
    ):
        processing = {}

    active = raw.get(
        "active",
        {},
    )

    if not isinstance(
        active,
        dict,
    ):
        active = {}

    active_started_at = float(
        active.get(
            "started_at",
            0.0,
        )
        or 0.0
    )
    active_duration = float(
        active.get(
            "duration",
            0.0,
        )
        or 0.0
    )
    active_playing = bool(
        active.get(
            "playing",
            False,
        )
    )

    if (
        active_playing
        and active_started_at > 0.0
        and active_duration > 0.0
        and time.time()
        - active_started_at
        > active_duration + SONG_ACTIVE_STALE_GRACE_SECONDS
    ):
        active_playing = False

    return {
        "processing": {
            "active": bool(
                processing.get(
                    "active",
                    False,
                )
            ),
            "song_id": str(
                processing.get(
                    "song_id",
                    "",
                )
                or ""
            ),
            "source_path": str(
                processing.get(
                    "source_path",
                    "",
                )
                or ""
            ),
            "started_at": float(
                processing.get(
                    "started_at",
                    0.0,
                )
                or 0.0
            ),
            "finished_at": float(
                processing.get(
                    "finished_at",
                    0.0,
                )
                or 0.0
            ),
            "error": str(
                processing.get(
                    "error",
                    "",
                )
                or ""
            ),
            "message": str(
                processing.get(
                    "message",
                    "",
                )
                or ""
            ),
        },
        "active": {
            "playing": active_playing,
            "song_id": str(
                active.get(
                    "song_id",
                    "",
                )
                or ""
            ),
            "title": str(
                active.get(
                    "title",
                    "",
                )
                or ""
            ),
            "started_at": float(
                active.get(
                    "started_at",
                    0.0,
                )
                or 0.0
            ),
            "duration": active_duration,
            "expected_finished_at": (
                active_started_at
                + active_duration
                if (
                    active_started_at > 0.0
                    and active_duration > 0.0
                )
                else 0.0
            ),
        },
    }


def _clean_trigger_count(
    value: object,
    fallback: int,
) -> int:
    try:
        number = int(
            value
            or fallback
        )
    except (
        TypeError,
        ValueError,
    ):
        number = fallback

    return max(
        1,
        number,
    )


def _clean_song_id(
    value: object,
) -> str:
    text = str(
        value
        or ""
    ).strip()

    if not text:
        return ""

    return _safe_slug(text)


def _clean_song_ids(
    value: object,
) -> list[str]:
    candidates: list[object] = []

    if isinstance(
        value,
        dict,
    ):
        for key in (
            "song_id",
            "song",
            "id",
        ):
            if value.get(key):
                candidates.append(
                    value.get(key)
                )

        for key in (
            "song_ids",
            "songs",
            "song_list",
        ):
            songs = value.get(key)
            if isinstance(
                songs,
                (list, tuple, set),
            ):
                candidates.extend(songs)
            elif songs:
                candidates.append(songs)
    elif isinstance(
        value,
        (list, tuple, set),
    ):
        candidates.extend(value)
    elif value:
        candidates.append(value)

    result: list[str] = []
    seen: set[str] = set()

    for candidate in candidates:
        if isinstance(
            candidate,
            dict,
        ):
            song_id = _clean_song_id(
                candidate.get("song_id")
                or candidate.get("song")
                or candidate.get("id")
            )
        else:
            song_id = _clean_song_id(candidate)

        if (
            not song_id
            or song_id in seen
        ):
            continue

        seen.add(song_id)
        result.append(song_id)

    return result


def _rule_song_ids(
    rule: dict[str, Any],
) -> list[str]:
    return _clean_song_ids(rule)


def _rule_selection(
    rule: dict[str, Any],
    song_ids: list[str],
) -> str:
    if len(song_ids) <= 1:
        return "first"

    selection = str(
        rule.get("selection")
        or ""
    ).strip().lower()

    if selection not in {
        "first",
        "random",
    }:
        selection = (
            "random"
            if len(song_ids) > 1
            else "first"
        )

    return selection


def _ready_song_options(
    rule: dict[str, Any],
) -> tuple[
    list[tuple[str, dict[str, Any]]],
    list[str],
]:
    ready: list[tuple[str, dict[str, Any]]] = []
    missing: list[str] = []

    for song_id in _rule_song_ids(rule):
        try:
            ready.append(
                (
                    song_id,
                    _song_by_id(song_id),
                )
            )
        except FileNotFoundError:
            missing.append(song_id)

    return ready, missing


def _select_song_option(
    options: list[tuple[str, dict[str, Any]]],
    *,
    selection: str,
    last_song_id: object = "",
) -> tuple[str, dict[str, Any]]:
    if not options:
        raise FileNotFoundError(
            "Musica processada nao encontrada."
        )

    if (
        selection != "random"
        or len(options) <= 1
    ):
        return options[0]

    last = str(
        last_song_id
        or ""
    )
    candidates = [
        option
        for option in options
        if option[0] != last
    ]

    if not candidates:
        candidates = options

    return random.choice(candidates)


def _song_id_from_source(
    source: Path,
) -> str:
    try:
        relative = source.resolve().relative_to(
            SONG_INPUT_DIR.resolve()
        )
    except ValueError:
        return _safe_slug(source.stem)

    if relative.parent == Path("."):
        return _safe_slug(source.stem)

    return _safe_slug(
        relative.with_suffix("").as_posix()
    )


def _read_gift_rules() -> dict[str, dict[str, Any]]:
    raw = read_json(SONG_GIFT_MAP_FILE, {})

    private_mappings = (
        raw.get("mappings", raw)
        if isinstance(raw, dict)
        else {}
    )
    if not isinstance(private_mappings, dict) or not private_mappings:
        raw = read_json(DEFAULT_SONG_GIFT_MAP_FILE, {})

    if not isinstance(
        raw,
        dict,
    ):
        raw = {}

    mappings = raw.get(
        "mappings",
        raw,
    )

    if not isinstance(
        mappings,
        dict,
    ):
        mappings = {}

    thresholds = raw.get(
        "thresholds",
        {},
    )

    if not isinstance(
        thresholds,
        dict,
    ):
        thresholds = {}

    result: dict[str, dict[str, Any]] = {}

    for key, value in mappings.items():
        gift_key = _canonical_gift_key(key)
        if not gift_key:
            continue

        song_ids = _clean_song_ids(value)

        if isinstance(
            value,
            dict,
        ):
            initial_count = _clean_trigger_count(
                value.get("initial_count")
                or value.get("first_count"),
                DEFAULT_GIFT_INITIAL_SING_COUNT,
            )
            repeat_count = _clean_trigger_count(
                value.get("repeat_count")
                or value.get("next_count"),
                DEFAULT_GIFT_REPEAT_SING_COUNT,
            )
            selection = _rule_selection(
                value,
                song_ids,
            )
        else:
            threshold = thresholds.get(
                gift_key,
            )
            if not isinstance(
                threshold,
                dict,
            ):
                threshold = {}
            initial_count = _clean_trigger_count(
                threshold.get(
                    "initial_count"
                ),
                DEFAULT_GIFT_INITIAL_SING_COUNT,
            )
            repeat_count = _clean_trigger_count(
                threshold.get(
                    "repeat_count"
                ),
                DEFAULT_GIFT_REPEAT_SING_COUNT,
            )
            selection = _rule_selection(
                {},
                song_ids,
            )

        if song_ids:
            result[gift_key] = {
                "song_id": song_ids[0],
                "song_ids": song_ids,
                "selection": selection,
                "initial_count": initial_count,
                "repeat_count": repeat_count,
            }

    return result


def gift_audio_diagnostics() -> dict[str, Any]:
    cache_key = "|".join(
        (
            str(SONG_GIFT_MAP_FILE),
            str(DEFAULT_SONG_GIFT_MAP_FILE),
            str(SONG_PROCESSED_DIR),
        )
    )
    cached_at = float(
        _GIFT_AUDIO_DIAGNOSTICS_CACHE.get("at") or 0.0
    )
    if (
        _GIFT_AUDIO_DIAGNOSTICS_CACHE.get("key") == cache_key
        and time.time() - cached_at < 15.0
    ):
        return dict(
            _GIFT_AUDIO_DIAGNOSTICS_CACHE.get("value") or {}
        )

    mappings = gift_mappings()
    ready = [item for item in mappings if item.get("ready")]
    missing = [
        {
            "gift_key": str(item.get("gift_key") or ""),
            "missing_song_ids": list(item.get("missing_song_ids") or []),
        }
        for item in mappings
        if not item.get("ready")
    ]
    private_payload = read_json(SONG_GIFT_MAP_FILE, {})
    private_mappings = (
        private_payload.get("mappings", private_payload)
        if isinstance(private_payload, dict)
        else {}
    )
    result = {
        "configured_gifts": len(mappings),
        "ready_gifts": len(ready),
        "all_ready": bool(mappings) and len(ready) == len(mappings),
        "configuration_source": (
            "private"
            if isinstance(private_mappings, dict) and private_mappings
            else "default"
        ),
        "missing": missing,
    }
    _GIFT_AUDIO_DIAGNOSTICS_CACHE.update(
        {
            "key": cache_key,
            "at": time.time(),
            "value": result,
        }
    )
    return dict(result)


def _read_gift_map() -> dict[str, str]:
    result: dict[str, str] = {}

    for gift_key, rule in _read_gift_rules().items():
        song_ids = _rule_song_ids(rule)
        song_id = song_ids[0] if song_ids else ""

        if song_id:
            result[gift_key] = song_id

    return result


def _write_gift_rules(
    rules: dict[str, dict[str, Any]],
) -> None:
    mappings: dict[str, dict[str, Any]] = {}

    for gift_key, rule in sorted(
        rules.items()
    ):
        song_ids = _rule_song_ids(rule)

        if not song_ids:
            continue

        mappings[gift_key] = {
            "song_id": song_ids[0],
            "song_ids": song_ids,
            "selection": _rule_selection(
                rule,
                song_ids,
            ),
            "initial_count": _clean_trigger_count(
                rule.get(
                    "initial_count"
                ),
                DEFAULT_GIFT_INITIAL_SING_COUNT,
            ),
            "repeat_count": _clean_trigger_count(
                rule.get(
                    "repeat_count"
                ),
                DEFAULT_GIFT_REPEAT_SING_COUNT,
            ),
        }

    write_json_atomic(
        SONG_GIFT_MAP_FILE,
        {
            "version": 2,
            "updated_at": time.time(),
            "mappings": mappings,
        },
    )


def gift_mappings() -> list[dict[str, Any]]:
    rules = _read_gift_rules()
    songs_by_id = {
        str(
            song.get("id")
            or ""
        ): song
        for song in processed_songs()
    }

    result: list[dict[str, Any]] = []

    for gift_key, rule in sorted(
        rules.items()
    ):
        song_ids = _rule_song_ids(rule)
        ready_songs: list[dict[str, Any]] = []
        missing_song_ids: list[str] = []

        for song_id in song_ids:
            song = songs_by_id.get(
                song_id,
                {},
            )

            if song.get("ready"):
                ready_songs.append(song)
            else:
                missing_song_ids.append(song_id)

        song_id = (
            song_ids[0]
            if song_ids
            else ""
        )
        song_titles = [
            str(
                song.get("title")
                or song.get("id")
                or ""
            )
            for song in ready_songs
            if (
                song.get("title")
                or song.get("id")
            )
        ]
        song_title = (
            song_titles[0]
            if song_titles
            else ""
        )
        result.append(
            {
                "gift_key": gift_key,
                "song_id": song_id,
                "song_ids": song_ids,
                "song_title": song_title,
                "song_titles": song_titles,
                "song_count": len(song_ids),
                "ready_song_count": len(ready_songs),
                "missing_song_ids": missing_song_ids,
                "selection": _rule_selection(
                    rule,
                    song_ids,
                ),
                "ready": bool(
                    ready_songs
                ),
                "all_ready": (
                    len(ready_songs)
                    == len(song_ids)
                    and bool(song_ids)
                ),
                "initial_count": _clean_trigger_count(
                    rule.get(
                        "initial_count"
                    ),
                    DEFAULT_GIFT_INITIAL_SING_COUNT,
                ),
                "repeat_count": _clean_trigger_count(
                    rule.get(
                        "repeat_count"
                    ),
                    DEFAULT_GIFT_REPEAT_SING_COUNT,
                ),
            }
        )

    return result


def update_gift_mapping(
    *,
    gift_name: object,
    song_id: object,
    append: bool = True,
    selection: object = "",
    initial_count: object = None,
    repeat_count: object = None,
) -> dict[str, Any]:
    gift_key = _canonical_gift_key(
        gift_name
    )
    clean_song_id = _clean_song_id(
        song_id
    )

    if not gift_key:
        raise ValueError(
            "Informe o nome do presente."
        )

    if not clean_song_id:
        raise ValueError(
            "Informe a musica processada."
        )

    _song_by_id(
        clean_song_id
    )

    with _LOCK:
        rules = _read_gift_rules()
        previous = rules.get(
            gift_key,
            {},
        )
        previous_song_ids = _rule_song_ids(
            previous
        )
        song_ids = (
            list(previous_song_ids)
            if append
            else []
        )

        if clean_song_id not in song_ids:
            song_ids.append(clean_song_id)

        selection_rule = {
            **previous,
            "song_ids": song_ids,
        }

        if (
            append
            and len(previous_song_ids) <= 1
            and len(song_ids) > 1
        ):
            selection_rule["selection"] = "random"

        requested_selection = str(
            selection or ""
        ).strip().lower()
        if requested_selection in {
            "random",
            "first",
        }:
            selection_rule["selection"] = requested_selection

        rules[gift_key] = {
            "song_id": song_ids[0],
            "song_ids": song_ids,
            "selection": _rule_selection(
                selection_rule,
                song_ids,
            ),
            "initial_count": _clean_trigger_count(
                (
                    initial_count
                    if initial_count is not None
                    else previous.get("initial_count")
                ),
                DEFAULT_GIFT_INITIAL_SING_COUNT,
            ),
            "repeat_count": _clean_trigger_count(
                (
                    repeat_count
                    if repeat_count is not None
                    else previous.get("repeat_count")
                ),
                DEFAULT_GIFT_REPEAT_SING_COUNT,
            ),
        }
        _write_gift_rules(rules)

    return {
        "gift_key": gift_key,
        "song_id": clean_song_id,
        "song_ids": song_ids,
    }


def remove_gift_mapping(
    gift_name: object,
    song_id: object = "",
) -> bool:
    gift_key = _canonical_gift_key(
        gift_name
    )

    if not gift_key:
        return False

    clean_song_id = _clean_song_id(
        song_id
    )

    with _LOCK:
        rules = _read_gift_rules()
        existed = gift_key in rules

        if (
            existed
            and clean_song_id
        ):
            previous = rules.get(
                gift_key,
                {},
            )
            song_ids = [
                item
                for item in _rule_song_ids(previous)
                if item != clean_song_id
            ]

            if song_ids:
                rules[gift_key] = {
                    **previous,
                    "song_id": song_ids[0],
                    "song_ids": song_ids,
                }
            else:
                rules.pop(
                    gift_key,
                    None,
                )
        else:
            rules.pop(
                gift_key,
                None,
            )

        _write_gift_rules(rules)

    return existed


def song_for_gift_name(
    gift_name: object,
) -> dict[str, Any] | None:
    gift_key = _canonical_gift_key(
        gift_name
    )

    if not gift_key:
        return None

    song_id = _read_gift_map().get(
        gift_key,
        "",
    )

    if not song_id:
        return None

    try:
        rule = _read_gift_rules().get(
            gift_key,
            {},
        )
        options, _missing = _ready_song_options(
            rule
        )
        _song_id, song = _select_song_option(
            options,
            selection=_rule_selection(
                rule,
                _rule_song_ids(rule),
            ),
        )
        return song
    except FileNotFoundError:
        return None


def _gift_count_from_event(
    event: dict[str, Any],
) -> int:
    metadata = (
        event.get("metadata")
        if isinstance(
            event.get("metadata"),
            dict,
        )
        else {}
    )

    for value in (
        metadata.get("count"),
        metadata.get("gift_count"),
        event.get("count"),
    ):
        try:
            count = int(value or 0)
        except (
            TypeError,
            ValueError,
        ):
            count = 0

        if count > 0:
            return count

    return 1


def _gift_coin_value_from_event(
    event: dict[str, Any],
) -> int:
    metadata = (
        event.get("metadata")
        if isinstance(
            event.get("metadata"),
            dict,
        )
        else {}
    )

    for value in (
        metadata.get("gift_coin_value"),
        metadata.get("coin_value"),
        metadata.get("diamond_count"),
        metadata.get("diamondCount"),
        event.get("gift_coin_value"),
        event.get("coin_value"),
        event.get("diamond_count"),
        event.get("diamondCount"),
    ):
        try:
            coin_value = int(float(value or 0))
        except (
            TypeError,
            ValueError,
        ):
            coin_value = 0

        if coin_value > 0:
            return coin_value

    received_count = _gift_count_from_event(event)
    try:
        total_coins = int(
            float(
                metadata.get("gift_total_coins")
                or event.get("gift_total_coins")
                or 0
            )
        )
    except (
        TypeError,
        ValueError,
    ):
        total_coins = 0

    if total_coins > 0 and received_count > 0:
        return max(
            0,
            total_coins // received_count,
        )

    return 0


def _read_gift_progress() -> dict[str, Any]:
    raw = read_json(
        SONG_GIFT_PROGRESS_FILE,
        {},
    )

    if not isinstance(
        raw,
        dict,
    ):
        raw = {}

    gifts = raw.get(
        "gifts",
        {},
    )

    if not isinstance(
        gifts,
        dict,
    ):
        gifts = {}

    return {
        "version": 1,
        "gifts": {
            str(key): dict(value)
            for key, value in gifts.items()
            if isinstance(
                value,
                dict,
            )
        },
        "updated_at": float(
            raw.get("updated_at")
            or 0.0
        ),
    }


def _write_gift_progress(
    state: dict[str, Any],
) -> None:
    state["version"] = 1
    state["updated_at"] = time.time()
    write_json_atomic(
        SONG_GIFT_PROGRESS_FILE,
        state,
    )


def reset_gift_progress() -> dict[str, Any]:
    state = {
        "version": 1,
        "gifts": {},
        "updated_at": time.time(),
    }
    write_json_atomic(
        SONG_GIFT_PROGRESS_FILE,
        state,
    )
    return dict(state)


def gift_trigger_status() -> dict[str, Any]:
    with _LOCK:
        state = _read_gift_progress()

    return state


def song_trigger_for_gift_event(
    event: dict[str, Any],
    *,
    consume: bool = True,
) -> dict[str, Any]:
    metadata = (
        event.get("metadata")
        if isinstance(
            event.get("metadata"),
            dict,
        )
        else {}
    )

    gift_name = (
        metadata.get("gift_name")
        or event.get("text")
        or ""
    )

    gift_key = _canonical_gift_key(
        gift_name
    )

    if not gift_key:
        return {
            "mapped": False,
            "should_play": False,
            "reason": "gift_name_empty",
        }

    rule = _read_gift_rules().get(
        gift_key,
    )

    if not rule:
        return {
            "mapped": False,
            "should_play": False,
            "gift_key": gift_key,
            "reason": "gift_not_mapped",
        }

    song_ids = _rule_song_ids(rule)
    selection = _rule_selection(
        rule,
        song_ids,
    )
    song_options, missing_song_ids = (
        _ready_song_options(rule)
    )

    if not song_options:
        return {
            "mapped": True,
            "should_play": False,
            "gift_key": gift_key,
            "song_id": (
                song_ids[0]
                if song_ids
                else ""
            ),
            "song_ids": song_ids,
            "missing_song_ids": missing_song_ids,
            "selection": selection,
            "reason": "song_not_ready",
        }

    received_count = _gift_count_from_event(
        event
    )
    gift_coin_value = _gift_coin_value_from_event(
        event
    )
    high_value_immediate = (
        gift_coin_value >= HIGH_VALUE_GIFT_IMMEDIATE_COINS
    )
    initial_count = _clean_trigger_count(
        rule.get("initial_count"),
        DEFAULT_GIFT_INITIAL_SING_COUNT,
    )
    repeat_count = _clean_trigger_count(
        rule.get("repeat_count"),
        DEFAULT_GIFT_REPEAT_SING_COUNT,
    )
    if gift_key == HEART_ME_BAND_CANONICAL_GIFT_KEY:
        initial_count = 1
        repeat_count = 1

    if not consume:
        song_id, song = _select_song_option(
            song_options,
            selection=selection,
        )
        preview_threshold = initial_count
        preview_counted = min(
            received_count,
            preview_threshold,
        )
        return {
            "mapped": True,
            "should_play": True,
            "gift_key": gift_key,
            "song_id": song_id,
            "song_ids": song_ids,
            "missing_song_ids": missing_song_ids,
            "selection": selection,
            "song": song,
            "received_count": received_count,
            "counted_count": preview_counted,
            "ignored_count": max(
                0,
                received_count - preview_counted,
            ),
            "gift_coin_value": gift_coin_value,
            "high_value_immediate": high_value_immediate,
            "initial_count": initial_count,
            "repeat_count": repeat_count,
            "threshold": preview_threshold,
            "reason": "preview",
        }

    if high_value_immediate:
        with _LOCK:
            state = _read_gift_progress()
            gifts = state.setdefault(
                "gifts",
                {},
            )
            progress = dict(
                gifts.get(
                    gift_key,
                    {},
                )
                if isinstance(
                    gifts.get(gift_key),
                    dict,
                )
                else {}
            )
            song_id, song = _select_song_option(
                song_options,
                selection=selection,
                last_song_id=progress.get(
                    "last_song_id",
                    "",
                ),
            )
            total_received = int(
                progress.get(
                    "total_received",
                    0,
                )
                or 0
            ) + received_count
            total_triggers = int(
                progress.get(
                    "total_triggers",
                    0,
                )
                or 0
            ) + 1
            total_immediate_triggers = int(
                progress.get(
                    "total_immediate_triggers",
                    0,
                )
                or 0
            ) + 1
            accumulated = int(
                progress.get(
                    "accumulated_count",
                    0,
                )
                or 0
            )

            progress.update(
                {
                    "gift_key": gift_key,
                    "song_id": song_id,
                    "song_ids": song_ids,
                    "selection": selection,
                    "first_triggered": True,
                    "accumulated_count": accumulated,
                    "total_received": total_received,
                    "total_triggers": total_triggers,
                    "total_immediate_triggers": total_immediate_triggers,
                    "last_triggered_at": time.time(),
                    "last_immediate_triggered_at": time.time(),
                    "last_song_id": song_id,
                    "last_song_title": str(
                        song.get("title")
                        or ""
                    ),
                    "gift_coin_value": gift_coin_value,
                    "high_value_immediate": True,
                    "initial_count": initial_count,
                    "repeat_count": repeat_count,
                    "next_threshold": repeat_count,
                    "updated_at": time.time(),
                }
            )
            gifts[gift_key] = progress
            _write_gift_progress(state)

        return {
            "mapped": True,
            "should_play": True,
            "gift_key": gift_key,
            "song_id": song_id,
            "song_ids": song_ids,
            "missing_song_ids": missing_song_ids,
            "selection": selection,
            "song": song,
            "received_count": received_count,
            "counted_count": 0,
            "ignored_count": 0,
            "accumulated_count": accumulated,
            "gift_coin_value": gift_coin_value,
            "high_value_immediate": True,
            "initial_count": initial_count,
            "repeat_count": repeat_count,
            "threshold": 1,
            "total_received": total_received,
            "total_triggers": total_triggers,
            "total_immediate_triggers": total_immediate_triggers,
            "reason": "high_value_immediate",
        }

    with _LOCK:
        state = _read_gift_progress()
        gifts = state.setdefault(
            "gifts",
            {},
        )
        progress = dict(
            gifts.get(
                gift_key,
                {},
            )
            if isinstance(
                gifts.get(gift_key),
                dict,
            )
            else {}
        )

        first_triggered = bool(
            progress.get(
                "first_triggered",
                False,
            )
        )
        accumulated = int(
            progress.get(
                "accumulated_count",
                0,
            )
            or 0
        )
        total_received = int(
            progress.get(
                "total_received",
                0,
            )
            or 0
        )
        total_counted = int(
            progress.get(
                "total_counted",
                0,
            )
            or 0
        )
        total_ignored = int(
            progress.get(
                "total_ignored_count",
                0,
            )
            or 0
        )
        total_triggers = int(
            progress.get(
                "total_triggers",
                0,
            )
            or 0
        )
        total_received += received_count
        threshold = (
            repeat_count
            if first_triggered
            else initial_count
        )

        overflow_reset = False
        if accumulated >= threshold:
            accumulated = 0
            overflow_reset = True
            progress["last_overflow_reset_at"] = time.time()
            progress["overflow_reset_count"] = (
                int(
                    progress.get(
                        "overflow_reset_count",
                        0,
                    )
                    or 0
                )
                + 1
            )

        remaining = max(
            0,
            threshold - accumulated,
        )
        counted_count = min(
            received_count,
            remaining,
        )
        ignored_count = max(
            0,
            received_count - counted_count,
        )

        accumulated += counted_count
        total_counted += counted_count
        total_ignored += ignored_count
        should_play = accumulated >= threshold

        if should_play:
            song_id, song = _select_song_option(
                song_options,
                selection=selection,
                last_song_id=progress.get(
                    "last_song_id",
                    "",
                ),
            )
            accumulated = 0
            first_triggered = True
            total_triggers += 1
            progress["last_triggered_at"] = time.time()
            progress["last_song_id"] = song_id
            progress["last_song_title"] = str(
                song.get("title")
                or ""
            )
        else:
            song_id = (
                str(
                    progress.get("last_song_id")
                    or ""
                )
                or song_ids[0]
            )
            song = None

        progress.update(
            {
                "gift_key": gift_key,
                "song_id": song_id,
                "song_ids": song_ids,
                "selection": selection,
                "first_triggered": first_triggered,
                "accumulated_count": accumulated,
                "total_received": total_received,
                "total_counted": total_counted,
                "total_ignored_count": total_ignored,
                "total_triggers": total_triggers,
                "initial_count": initial_count,
                "repeat_count": repeat_count,
                "next_threshold": (
                    repeat_count
                    if first_triggered
                    else initial_count
                ),
                "updated_at": time.time(),
            }
        )
        gifts[gift_key] = progress
        _write_gift_progress(state)

    return {
        "mapped": True,
        "should_play": should_play,
        "gift_key": gift_key,
        "song_id": song_id,
        "song_ids": song_ids,
        "missing_song_ids": missing_song_ids,
        "selection": selection,
        "song": song if should_play else None,
        "received_count": received_count,
        "counted_count": counted_count,
        "ignored_count": ignored_count,
        "accumulated_count": accumulated,
        "initial_count": initial_count,
        "repeat_count": repeat_count,
        "threshold": threshold,
        "total_received": total_received,
        "total_counted": total_counted,
        "total_ignored_count": total_ignored,
        "total_triggers": total_triggers,
        "overflow_reset": overflow_reset,
        "reason": (
            "threshold_reached"
            if should_play
            else "threshold_waiting"
        ),
    }


def song_for_gift_event(
    event: dict[str, Any],
) -> dict[str, Any] | None:
    trigger = song_trigger_for_gift_event(
        event
    )

    song = trigger.get(
        "song"
    )

    return (
        song
        if isinstance(
            song,
            dict,
        )
        else None
    )


def _write_state(
    state: dict[str, Any],
) -> None:
    write_json_atomic(
        SONG_RUNTIME_FILE,
        state,
    )


def status() -> dict[str, Any]:
    with _LOCK:
        state = _read_state()

    return {
        **state,
        "demucs_available":
            demucs_available(),
        "ffmpeg_available":
            ffmpeg_available(),
            "input_dir": str(
                SONG_INPUT_DIR.resolve()
            ),
            "originals_dir": str(
                SONG_INPUT_DIR.resolve()
            ),
        "processed_dir": str(
            SONG_PROCESSED_DIR.resolve()
        ),
        "sources": source_tracks(),
        "songs": processed_songs(),
        "gift_mappings":
            gift_mappings(),
        "gift_progress":
            gift_trigger_status(),
        "log_file": str(
            SONG_PROCESS_LOG_FILE.resolve()
        ),
    }


def singing_active() -> bool:
    with _LOCK:
        state = _read_state()

    active = (
        state.get("active")
        if isinstance(
            state.get("active"),
            dict,
        )
        else {}
    )

    return bool(
        active.get("playing")
    )


def singing_blocks_speech() -> bool:
    with _LOCK:
        state = _read_state()

    active = (
        state.get("active")
        if isinstance(
            state.get("active"),
            dict,
        )
        else {}
    )

    if not bool(
        active.get("playing")
    ):
        return False

    song_id = _safe_slug(
        str(
            active.get("song_id")
            or ""
        )
    )

    if song_id.startswith(
        HEART_ME_BAND_CANONICAL_GIFT_KEY
    ):
        started_at = float(
            active.get("started_at")
            or 0.0
        )
        if started_at <= 0.0:
            return True

        return (
            time.time() - started_at
            <= HEART_ME_BAND_SPEECH_HOLD_SECONDS
        )

    return True


def _resolve_source(
    source_path: object,
) -> Path:
    requested = str(
        source_path
        or ""
    ).strip()

    if not requested:
        raise ValueError(
            "Informe uma musica em assets/singing/originals."
        )

    candidate = Path(
        requested
    ).expanduser()

    if not candidate.is_absolute():
        candidate = SONG_INPUT_DIR / requested

    candidate = candidate.resolve()
    allowed = {
        str(
            Path(
                item["path"]
            ).resolve()
        )
        for item in source_tracks()
    }

    if str(candidate) not in allowed:
        raise ValueError(
            "A musica precisa estar em assets/singing/originals."
        )

    return candidate


def start_processing(
    source_path: object,
) -> dict[str, Any]:
    global _PROCESS_THREAD

    _ensure_dirs()

    if not demucs_available():
        raise RuntimeError(
            "Demucs nao esta instalado no .venv."
        )

    if not ffmpeg_available():
        raise RuntimeError(
            "ffmpeg nao esta disponivel no sistema."
        )

    source = _resolve_source(
        source_path
    )
    song_id = _song_id_from_source(
        source
    )

    with _LOCK:
        if (
            _PROCESS_THREAD is not None
            and _PROCESS_THREAD.is_alive()
        ):
            raise RuntimeError(
                "Ja existe uma musica sendo processada."
            )

        state = _read_state()
        state["processing"] = {
            "active": True,
            "song_id": song_id,
            "source_path": str(source),
            "started_at": time.time(),
            "finished_at": 0.0,
            "error": "",
            "message": "Processando com Demucs.",
        }
        _write_state(state)

        _PROCESS_THREAD = threading.Thread(
            target=_process_song_worker,
            args=(source, song_id),
            name="cobrinha-song-demucs",
            daemon=True,
        )
        _PROCESS_THREAD.start()

    return status()


def _set_processing_result(
    *,
    song_id: str,
    source_path: Path,
    error: str = "",
    message: str = "",
) -> None:
    with _LOCK:
        state = _read_state()
        state["processing"] = {
            "active": False,
            "song_id": song_id,
            "source_path": str(source_path),
            "started_at": float(
                state.get(
                    "processing",
                    {},
                ).get(
                    "started_at",
                    0.0,
                )
                or 0.0
            ),
            "finished_at": time.time(),
            "error": error,
            "message": message,
        }
        _write_state(state)


def _run_logged(
    command: list[str],
    *,
    cwd: Path | None = None,
) -> None:
    with SONG_PROCESS_LOG_FILE.open(
        "a",
        encoding="utf-8",
    ) as log:
        log.write(
            "\n\n$ "
            + " ".join(command)
            + "\n"
        )
        log.flush()

        subprocess.run(
            command,
            cwd=(
                str(cwd)
                if cwd is not None
                else None
            ),
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )


def _find_demucs_file(
    root: Path,
    name: str,
) -> Path:
    for path in root.rglob(name):
        if path.is_file():
            return path

    raise FileNotFoundError(
        f"Demucs nao gerou {name}."
    )


def _convert_to_pcm_wav(
    source: Path,
    target: Path,
) -> None:
    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    _run_logged(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-ar",
            "48000",
            "-ac",
            "2",
            "-sample_fmt",
            "s16",
            str(target),
        ]
    )


def _process_song_worker(
    source: Path,
    song_id: str,
) -> None:
    output_dir = (
        SONG_PROCESSED_DIR
        / song_id
    )
    demucs_dir = (
        DEMUX_RUNS_DIR
        / song_id
    )

    try:
        shutil.rmtree(
            demucs_dir,
            ignore_errors=True,
        )
        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        _run_logged(
            [
                sys.executable,
                "-m",
                "demucs",
                "--two-stems",
                "vocals",
                "-n",
                "htdemucs",
                "-o",
                str(demucs_dir),
                str(source),
            ]
        )

        raw_vocal = _find_demucs_file(
            demucs_dir,
            "vocals.wav",
        )
        raw_instrumental = _find_demucs_file(
            demucs_dir,
            "no_vocals.wav",
        )

        vocal_path = (
            output_dir / "vocals.wav"
        )
        instrumental_path = (
            output_dir
            / "instrumental.wav"
        )
        timeline_path = (
            output_dir
            / "vocal_timeline.json"
        )

        _convert_to_pcm_wav(
            raw_vocal,
            vocal_path,
        )
        _convert_to_pcm_wav(
            raw_instrumental,
            instrumental_path,
        )

        write_audio_timeline_json(
            vocal_path,
            timeline_path,
            window_ms=45,
            silence_threshold=0.018,
            micro_pause_max=0.22,
            min_speech_ms=80,
            min_pause_ms=45,
        )

        timeline = read_json(
            timeline_path,
            {},
        )

        manifest = {
            "id": song_id,
            "title": source.stem,
            "source_path": str(
                source.resolve()
            ),
            "created_at": time.time(),
            "duration": float(
                timeline.get(
                    "timeline_end",
                    0.0,
                )
                or 0.0
            )
            if isinstance(
                timeline,
                dict,
            )
            else 0.0,
            "files": {
                "vocal": str(
                    vocal_path.resolve()
                ),
                "instrumental": str(
                    instrumental_path.resolve()
                ),
                "timeline": str(
                    timeline_path.resolve()
                ),
            },
        }

        write_json_atomic(
            output_dir / "manifest.json",
            manifest,
        )

        _set_processing_result(
            song_id=song_id,
            source_path=source,
            message="Musica processada.",
        )

    except Exception as exc:
        _set_processing_result(
            song_id=song_id,
            source_path=source,
            error=(
                f"{type(exc).__name__}: {exc}"
            ),
            message="Falha ao processar musica.",
        )


def _song_by_id(
    song_id: object,
) -> dict[str, Any]:
    clean_song_id = _safe_slug(
        str(
            song_id
            or ""
        )
    )

    song = _processed_song_from_manifest(
        _manifest_path(
            clean_song_id
        )
    )

    if (
        song is None
        or not song.get("ready")
    ):
        song = next(
            (
                candidate
                for candidate in processed_songs()
                if _safe_slug(
                    str(candidate.get("id") or "")
                ) == clean_song_id
                and candidate.get("ready")
            ),
            None,
        )

    if (
        song is None
        or not song.get("ready")
    ):
        raise FileNotFoundError(
            "Musica processada nao encontrada."
        )

    return song


def song_ready_message(
    song_id: object,
) -> dict[str, Any]:
    song = _song_by_id(
        song_id
    )
    files = song.get(
        "files",
        {},
    )

    if not isinstance(
        files,
        dict,
    ):
        files = {}

    timeline_path = Path(
        str(
            files.get(
                "timeline",
                "",
            )
        )
    )
    timeline = read_json(
        timeline_path,
        {},
    )

    if not isinstance(
        timeline,
        dict,
    ):
        timeline = {}

    segments = timeline.get(
        "segments",
        [],
    )

    if not isinstance(
        segments,
        list,
    ):
        segments = []

    job_id = (
        "song:"
        + str(
            song.get("id")
            or ""
        )
        + ":"
        + str(
            int(
                time.time() * 1000
            )
        )
    )

    return {
        "type": "song_ready",
        "source": "song_runtime",
        "time": time.time(),
        "payload": {
            "job_id": job_id,
            "song_id": song.get(
                "id",
                "",
            ),
            "title": song.get(
                "title",
                "",
            ),
            "vocal_path": files.get(
                "vocal",
                "",
            ),
            "instrumental_path": files.get(
                "instrumental",
                "",
            ),
            "mix_path": song.get(
                "source_path",
                "",
            ),
            "source_path": song.get(
                "source_path",
                "",
            ),
            "timeline_path": files.get(
                "timeline",
                "",
            ),
            "segments": segments,
            "timeline_end": float(
                timeline.get(
                    "timeline_end",
                    song.get(
                        "duration",
                        0.0,
                    ),
                )
                or 0.0
            ),
            "vocal_volume": 0.0,
            "instrumental_volume": 0.85,
            "mix_volume": 0.9,
        },
    }


def song_stop_message() -> dict[str, Any]:
    return {
        "type": "song_stop",
        "source": "song_runtime",
        "time": time.time(),
        "payload": {},
    }


def mark_playing(
    song_id: object,
    title: object,
    *,
    duration: object = 0.0,
) -> None:
    try:
        clean_duration = max(
            0.0,
            float(
                duration
                or 0.0
            ),
        )
    except (
        TypeError,
        ValueError,
    ):
        clean_duration = 0.0

    with _LOCK:
        state = _read_state()
        state["active"] = {
            "playing": True,
            "song_id": str(
                song_id
                or ""
            ),
            "title": str(
                title
                or ""
            ),
            "started_at": time.time(),
            "duration": clean_duration,
        }
        _write_state(state)


def mark_idle() -> None:
    with _LOCK:
        state = _read_state()
        state["active"] = {
            "playing": False,
            "song_id": "",
            "title": "",
            "started_at": 0.0,
        }
        _write_state(state)


def websocket_message() -> dict[str, Any]:
    return {
        "type": "song_runtime",
        "payload": status(),
    }
