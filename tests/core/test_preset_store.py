"""`core.preset_store` — disk persistence for imported ST presets (never breaks a turn)."""

from __future__ import annotations

import json

from core.preset import MAX_PRESET_BYTES
from core.preset_store import (
    delete_preset,
    list_preset_ids,
    load_preset,
    load_preset_text,
    preset_source,
    presets_dir,
    sanitize_preset_id,
    save_preset_text,
)

_PRESET = {
    "temperature": 0.9,
    "prompts": [
        {"identifier": "main", "name": "Main", "content": "Write plainly.", "role": "system", "enabled": True},
        {"identifier": "chatHistory", "name": "History", "content": "", "marker": True},
    ],
    "prompt_order": [
        {
            "character_id": 100001,
            "order": [{"identifier": "main", "enabled": True}, {"identifier": "chatHistory", "enabled": True}],
        }
    ],
}


def test_sanitize_preset_id_slugs_and_falls_back():
    assert sanitize_preset_id("双人成行v10.0—青云上.json") == "v10-0"
    assert sanitize_preset_id("My Great Preset (final).json") == "my-great-preset-final"
    assert sanitize_preset_id("青云上.json") == "preset"  # nothing latin survives
    assert sanitize_preset_id("") == ""


def test_save_load_roundtrip_and_listing(tmp_path):
    text = json.dumps(_PRESET, ensure_ascii=False)
    path = save_preset_text(tmp_path, "qingyun", text)
    assert path == presets_dir(tmp_path) / "qingyun.json"
    assert list_preset_ids(tmp_path) == ["mature-mode", "qingyun"]  # system tier first, then the user file

    preset = load_preset(tmp_path, "qingyun")
    assert preset is not None
    assert preset.sampling["temperature"] == 0.9
    assert [prompt.identifier for prompt in preset.prompts] == ["main", "chatHistory"]


def test_load_preset_degrades_to_none_on_any_failure(tmp_path):
    assert load_preset(tmp_path, "missing") is None
    assert load_preset(tmp_path, "../etc/passwd") is None  # not a preset id at all
    save_preset_text(tmp_path, "broken", "not json {{{")
    assert load_preset(tmp_path, "broken") is None
    oversized = presets_dir(tmp_path) / "huge.json"
    oversized.write_bytes(b"x" * (MAX_PRESET_BYTES + 1))
    assert load_preset(tmp_path, "huge") is None


def test_save_preset_text_rejects_bad_ids(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        save_preset_text(tmp_path, "Bad Id!", "{}")


def test_system_presets_list_and_load_beside_user_tier(tmp_path):
    """The engine-shipped `mature-mode` preset is discoverable in the SYSTEM tier
    without anything under the user data dir, and a same-named user file never
    shadows it (system wins)."""
    ids = list_preset_ids(tmp_path)
    assert "mature-mode" in ids  # system tier ships with the engine

    preset = load_preset(tmp_path, "mature-mode")
    assert preset is not None
    assert preset.content_rating == "explicit"
    assert preset_source(tmp_path, "mature-mode") == "system"

    # A user file with the same id is shadowed — and cannot be written in the first place.
    import pytest

    with pytest.raises(ValueError):
        save_preset_text(tmp_path, "mature-mode", '{"prompts": [{"identifier": "a", "content": "x", "enabled": true}]}')
    assert preset_source(tmp_path, "mature-mode") == "system"
    # The shadowed file would be invisible anyway: list has the id exactly once.
    assert list_preset_ids(tmp_path).count("mature-mode") == 1

    # Deleting a system preset is refused (returns False), user presets still delete.
    assert delete_preset(tmp_path, "mature-mode") is False
    assert load_preset(tmp_path, "mature-mode") is not None

    save_preset_text(tmp_path, "mine", '{"prompts": [{"identifier": "a", "content": "x", "enabled": true}]}')
    assert preset_source(tmp_path, "mine") == "user"
    assert delete_preset(tmp_path, "mine") is True
    assert load_preset(tmp_path, "mine") is None


def test_load_preset_text_returns_verbatim_both_tiers(tmp_path):
    assert load_preset_text(tmp_path, "mature-mode") is not None  # system tier
    assert "x_loreweaver_content_rating" in load_preset_text(tmp_path, "mature-mode")
    save_preset_text(tmp_path, "mine", '{"prompts": [{"identifier": "a", "content": "x", "enabled": true}]}')
    assert load_preset_text(tmp_path, "mine") == '{"prompts": [{"identifier": "a", "content": "x", "enabled": true}]}'
    assert load_preset_text(tmp_path, "missing") is None
