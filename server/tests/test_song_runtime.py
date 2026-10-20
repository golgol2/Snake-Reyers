import json

from cobrinha_server.services import song_runtime


def _setup_song_runtime(monkeypatch, tmp_path):
    singing_dir = tmp_path / "assets" / "singing"
    input_dir = singing_dir / "originals"
    processed_dir = singing_dir / "processed"
    song_dir = processed_dir / "rose"
    private_dir = tmp_path / "private"
    runs_dir = tmp_path / "runs"
    logs_dir = runs_dir / "logs"

    for path in (
        input_dir,
        song_dir,
        private_dir,
        runs_dir,
        logs_dir,
    ):
        path.mkdir(
            parents=True,
            exist_ok=True,
        )

    vocal = song_dir / "vocals.wav"
    instrumental = song_dir / "instrumental.wav"
    timeline = song_dir / "vocal_timeline.json"

    vocal.write_bytes(b"vocal")
    instrumental.write_bytes(b"instrumental")
    timeline.write_text("[]", encoding="utf-8")

    (song_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "rose",
                "title": "Rose",
                "files": {
                    "vocal": str(vocal),
                    "instrumental": str(instrumental),
                    "timeline": str(timeline),
                },
            }
        ),
        encoding="utf-8",
    )

    gift_map = private_dir / "song_gift_map.json"
    gift_progress = runs_dir / "song_gift_progress.json"

    gift_map.write_text(
        json.dumps(
            {
                "version": 1,
                "mappings": {
                    "rose": {
                        "song_id": "rose",
                        "initial_count": 1,
                        "repeat_count": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        song_runtime,
        "SINGING_DIR",
        singing_dir,
    )
    monkeypatch.setattr(
        song_runtime,
        "SONG_PROCESSED_DIR",
        processed_dir,
    )
    monkeypatch.setattr(
        song_runtime,
        "SONG_INPUT_DIR",
        input_dir,
    )
    monkeypatch.setattr(
        song_runtime,
        "SONG_GIFT_MAP_FILE",
        gift_map,
    )
    monkeypatch.setattr(
        song_runtime,
        "SONG_GIFT_PROGRESS_FILE",
        gift_progress,
    )
    monkeypatch.setattr(
        song_runtime,
        "SONG_RUNTIME_FILE",
        runs_dir / "song_runtime.json",
    )
    monkeypatch.setattr(
        song_runtime,
        "LOGS_DIR",
        logs_dir,
    )

    return gift_progress


def _rose_event(count):
    return {
        "metadata": {
            "gift_name": "Rose",
            "count": count,
        },
    }


def _gift_event(name, count):
    return {
        "metadata": {
            "gift_name": name,
            "count": count,
        },
    }


def _add_processed_song(processed_dir, song_id, title):
    song_dir = processed_dir / song_id
    song_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    vocal = song_dir / "vocals.wav"
    instrumental = song_dir / "instrumental.wav"
    timeline = song_dir / "vocal_timeline.json"

    vocal.write_bytes(b"vocal")
    instrumental.write_bytes(b"instrumental")
    timeline.write_text("[]", encoding="utf-8")

    (song_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": song_id,
                "title": title,
                "files": {
                    "vocal": str(vocal),
                    "instrumental": str(instrumental),
                    "timeline": str(timeline),
                },
            }
        ),
        encoding="utf-8",
    )


def _progress(path):
    return json.loads(
        path.read_text(
            encoding="utf-8",
        )
    )


def test_processed_song_uses_local_files_when_manifest_paths_are_stale(
    tmp_path,
):
    song_dir = tmp_path / "processed" / "rose"
    song_dir.mkdir(parents=True)
    for name in (
        "vocals.wav",
        "instrumental.wav",
        "vocal_timeline.json",
    ):
        (song_dir / name).write_bytes(b"ready")

    manifest_path = song_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "id": "rose",
                "files": {
                    "vocal": "C:/maquina-antiga/vocals.wav",
                    "instrumental": "C:/maquina-antiga/instrumental.wav",
                    "timeline": "C:/maquina-antiga/vocal_timeline.json",
                },
            }
        ),
        encoding="utf-8",
    )

    song = song_runtime._processed_song_from_manifest(manifest_path)

    assert song is not None
    assert song["ready"] is True
    assert song["files"]["vocal"] == str((song_dir / "vocals.wav").resolve())


def test_gift_song_is_found_inside_windows_fanclub_folder(
    monkeypatch,
    tmp_path,
):
    _setup_song_runtime(monkeypatch, tmp_path)
    processed_dir = song_runtime.SONG_PROCESSED_DIR
    gift_map = song_runtime.SONG_GIFT_MAP_FILE
    _add_processed_song(
        processed_dir / "FANCLUB",
        "heart-me-08",
        "Heart Me 08",
    )
    gift_map.write_text(
        json.dumps(
            {
                "version": 2,
                "mappings": {
                    "heart-me": {
                        "song_id": "heart-me-08",
                        "song_ids": ["heart-me-08"],
                        "initial_count": 1,
                        "repeat_count": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = song_runtime.song_trigger_for_gift_event(
        _gift_event("Heart Me", 1)
    )

    assert result["should_play"] is True
    assert result["song_id"] == "heart-me-08"
    assert result["song"]["ready"] is True


def test_large_first_gift_triggers_once_and_drops_excess(monkeypatch, tmp_path):
    gift_progress = _setup_song_runtime(monkeypatch, tmp_path)

    result = song_runtime.song_trigger_for_gift_event(
        _rose_event(100)
    )

    assert result["should_play"] is True
    assert result["counted_count"] == 1
    assert result["ignored_count"] == 99
    assert result["accumulated_count"] == 0

    rose = _progress(gift_progress)["gifts"]["rose"]
    assert rose["first_triggered"] is True
    assert rose["accumulated_count"] == 0
    assert rose["total_triggers"] == 1


def test_large_repeat_gift_triggers_once_and_resets_counter(monkeypatch, tmp_path):
    gift_progress = _setup_song_runtime(monkeypatch, tmp_path)

    song_runtime.song_trigger_for_gift_event(
        _rose_event(1)
    )
    result = song_runtime.song_trigger_for_gift_event(
        _rose_event(100)
    )

    assert result["should_play"] is True
    assert result["threshold"] == 10
    assert result["counted_count"] == 10
    assert result["ignored_count"] == 90
    assert result["accumulated_count"] == 0

    rose = _progress(gift_progress)["gifts"]["rose"]
    assert rose["total_triggers"] == 2
    assert rose["accumulated_count"] == 0


def test_large_gift_only_fills_remaining_threshold(monkeypatch, tmp_path):
    gift_progress = _setup_song_runtime(monkeypatch, tmp_path)

    song_runtime.song_trigger_for_gift_event(
        _rose_event(1)
    )
    waiting = song_runtime.song_trigger_for_gift_event(
        _rose_event(4)
    )
    result = song_runtime.song_trigger_for_gift_event(
        _rose_event(100)
    )

    assert waiting["should_play"] is False
    assert waiting["accumulated_count"] == 4
    assert result["should_play"] is True
    assert result["counted_count"] == 6
    assert result["ignored_count"] == 94
    assert result["accumulated_count"] == 0

    rose = _progress(gift_progress)["gifts"]["rose"]
    assert rose["total_triggers"] == 2
    assert rose["accumulated_count"] == 0


def test_stale_overflow_counter_is_reset_before_counting(monkeypatch, tmp_path):
    gift_progress = _setup_song_runtime(monkeypatch, tmp_path)
    gift_progress.write_text(
        json.dumps(
            {
                "version": 1,
                "gifts": {
                    "rose": {
                        "first_triggered": True,
                        "accumulated_count": 109,
                        "total_received": 360,
                        "total_triggers": 26,
                        "initial_count": 1,
                        "repeat_count": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = song_runtime.song_trigger_for_gift_event(
        _rose_event(1)
    )

    assert result["should_play"] is False
    assert result["overflow_reset"] is True
    assert result["counted_count"] == 1
    assert result["accumulated_count"] == 1

    rose = _progress(gift_progress)["gifts"]["rose"]
    assert rose["accumulated_count"] == 1
    assert rose["overflow_reset_count"] == 1


def test_public_default_gift_map_is_used_without_private_config(
    monkeypatch,
    tmp_path,
):
    _setup_song_runtime(monkeypatch, tmp_path)
    song_runtime.SONG_GIFT_MAP_FILE.unlink()
    default_map = tmp_path / "config" / "song_gift_map.json"
    default_map.parent.mkdir(parents=True)
    default_map.write_text(
        json.dumps(
            {
                "version": 2,
                "mappings": {
                    "heart-me": {
                        "song_id": "rose",
                        "song_ids": ["rose"],
                        "initial_count": 1,
                        "repeat_count": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        song_runtime,
        "DEFAULT_SONG_GIFT_MAP_FILE",
        default_map,
    )

    result = song_runtime.song_trigger_for_gift_event(
        _gift_event("Heart Me", 1)
    )

    assert result["should_play"] is True
    assert result["song_id"] == "rose"
    assert song_runtime.gift_audio_diagnostics()["configuration_source"] == "default"


def test_mapped_gifts_keep_independent_song_counters(monkeypatch, tmp_path):
    gift_progress = _setup_song_runtime(monkeypatch, tmp_path)
    processed_dir = song_runtime.SONG_PROCESSED_DIR
    gift_map = song_runtime.SONG_GIFT_MAP_FILE

    _add_processed_song(processed_dir, "passinho-do-abismo", "TikTok")
    _add_processed_song(processed_dir, "ice-cream-cone", "Ice Cream Cone")
    gift_map.write_text(
        json.dumps(
            {
                "version": 1,
                "mappings": {
                    "rose": {
                        "song_id": "rose",
                        "initial_count": 1,
                        "repeat_count": 10,
                    },
                    "tiktok": {
                        "song_id": "passinho-do-abismo",
                        "initial_count": 1,
                        "repeat_count": 10,
                    },
                    "ice-cream-cone": {
                        "song_id": "ice-cream-cone",
                        "initial_count": 1,
                        "repeat_count": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    rose = song_runtime.song_trigger_for_gift_event(
        _gift_event("Rose", 1)
    )
    tiktok = song_runtime.song_trigger_for_gift_event(
        _gift_event("TikTok", 1)
    )
    ice_cream = song_runtime.song_trigger_for_gift_event(
        _gift_event("Ice Cream Cone", 1)
    )

    assert rose["should_play"] is True
    assert tiktok["should_play"] is True
    assert ice_cream["should_play"] is True

    waiting_rose = song_runtime.song_trigger_for_gift_event(
        _gift_event("Rose", 1)
    )
    waiting_tiktok = song_runtime.song_trigger_for_gift_event(
        _gift_event("TikTok", 1)
    )
    waiting_ice_cream = song_runtime.song_trigger_for_gift_event(
        _gift_event("Ice Cream Cone", 1)
    )

    assert waiting_rose["should_play"] is False
    assert waiting_rose["accumulated_count"] == 1
    assert waiting_tiktok["should_play"] is False
    assert waiting_tiktok["accumulated_count"] == 1
    assert waiting_ice_cream["should_play"] is False
    assert waiting_ice_cream["accumulated_count"] == 1

    progress = _progress(gift_progress)["gifts"]
    assert set(progress) == {
        "ice-cream-cone",
        "rose",
        "tiktok",
    }
    assert progress["rose"]["total_triggers"] == 1
    assert progress["tiktok"]["total_triggers"] == 1
    assert progress["ice-cream-cone"]["total_triggers"] == 1


def test_high_value_gift_plays_without_repeat_counter(monkeypatch, tmp_path):
    gift_progress = _setup_song_runtime(monkeypatch, tmp_path)
    processed_dir = song_runtime.SONG_PROCESSED_DIR
    gift_map = song_runtime.SONG_GIFT_MAP_FILE

    _add_processed_song(processed_dir, "doughnut", "Doughnut")
    gift_map.write_text(
        json.dumps(
            {
                "version": 1,
                "mappings": {
                    "doughnut": {
                        "song_id": "doughnut",
                        "initial_count": 1,
                        "repeat_count": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    first = song_runtime.song_trigger_for_gift_event(
        {
            "metadata": {
                "gift_name": "Doughnut",
                "count": 1,
                "gift_coin_value": 30,
                "gift_total_coins": 30,
            },
        }
    )
    second = song_runtime.song_trigger_for_gift_event(
        {
            "metadata": {
                "gift_name": "Doughnut",
                "count": 1,
                "gift_coin_value": 30,
                "gift_total_coins": 30,
            },
        }
    )

    assert first["should_play"] is True
    assert first["reason"] == "high_value_immediate"
    assert second["should_play"] is True
    assert second["reason"] == "high_value_immediate"
    assert second["accumulated_count"] == 0

    doughnut = _progress(gift_progress)["gifts"]["doughnut"]
    assert doughnut["total_triggers"] == 2
    assert doughnut["total_immediate_triggers"] == 2
    assert doughnut["accumulated_count"] == 0


def test_source_tracks_include_gift_subfolders(monkeypatch, tmp_path):
    _setup_song_runtime(monkeypatch, tmp_path)
    source = (
        song_runtime.SONG_INPUT_DIR
        / "rose"
        / "Rose Nova.wav"
    )
    source.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    source.write_bytes(b"audio")

    tracks = song_runtime.source_tracks()

    assert len(tracks) == 1
    assert tracks[0]["name"] == "Rose Nova.wav"
    assert tracks[0]["relative_path"] == "rose/Rose Nova.wav"
    assert tracks[0]["group"] == "rose"
    assert (
        song_runtime._song_id_from_source(source)
        == "rose-rose-nova"
    )


def test_gift_mapping_accepts_multiple_random_songs(monkeypatch, tmp_path):
    gift_progress = _setup_song_runtime(monkeypatch, tmp_path)
    processed_dir = song_runtime.SONG_PROCESSED_DIR
    _add_processed_song(
        processed_dir,
        "rose-remix",
        "Rose Remix",
    )
    monkeypatch.setattr(
        song_runtime.random,
        "choice",
        lambda items: items[-1],
    )

    mapping = song_runtime.update_gift_mapping(
        gift_name="Rose",
        song_id="rose-remix",
    )

    assert mapping["song_ids"] == [
        "rose",
        "rose-remix",
    ]

    visible = song_runtime.gift_mappings()[0]
    assert visible["song_ids"] == [
        "rose",
        "rose-remix",
    ]
    assert visible["song_count"] == 2
    assert visible["ready_song_count"] == 2
    assert visible["selection"] == "random"

    first = song_runtime.song_trigger_for_gift_event(
        {
            "metadata": {
                "gift_name": "Rose",
                "count": 1,
                "gift_coin_value": 30,
                "gift_total_coins": 30,
            },
        }
    )
    second = song_runtime.song_trigger_for_gift_event(
        {
            "metadata": {
                "gift_name": "Rose",
                "count": 1,
                "gift_coin_value": 30,
                "gift_total_coins": 30,
            },
        }
    )

    assert first["should_play"] is True
    assert first["song_id"] == "rose-remix"
    assert second["should_play"] is True
    assert second["song_id"] == "rose"

    rose = _progress(gift_progress)["gifts"]["rose"]
    assert rose["last_song_id"] == "rose"
    assert rose["song_ids"] == [
        "rose",
        "rose-remix",
    ]
