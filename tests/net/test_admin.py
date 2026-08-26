"""Tests for the keeper-gated admin surface over the WS wire (`net.admin`).

Like `tests/net/test_tui_server.py`, a real `TuiServer` is bound to an ephemeral
localhost port and driven by a real `websockets` client, so the v1.1 `admin_*`
frames are exercised end to end. The LLM is a `MutableLLM` wrapping an offline
`FakeLLM`, so `admin_set_model` genuinely hot-reconfigures (and the follow-up
`admin_config` reflects it) without any network — mirroring the `.model` tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import agent.forge as forge_module
import core.rulepacks as rulepacks_module
import core.skills as skills_module
import net.keystore as keystore_module
import net.room_backup as room_backup_module
from agent.services import ROOM_LLM_SELECTION_KEY, build_services
from gateway.hub import RoomHub
from gateway.ops import get_enabled_skills, set_enabled_skills
from gateway.rooms import (
    get_keeper_binding,
    resolve_session_key,
    session_key_for_room,
    set_keeper_binding,
)
from gateway.session import SessionSource
from infra.config import ImageGenSettings, LLMSettings, Settings, TuiSettings
from infra.embeddings import FakeEmbeddings
from infra.i18n import get_i18n
from infra.imagegen import IMAGEGEN_PRESETS, FakeImageGen
from infra.llm import FakeLLM, assistant_text
from infra.media_store import ALLOWED_MEDIA_MIMES, MediaError, MediaStore
from infra.providers import PRESETS, MutableLLM
from net.admin import AdminService
from net.keystore import Keystore
from net.room_backup import (
    chat_key_for_room,
    delete_room_data,
    export_room,
    import_room,
    reset_room_state,
    room_rows,
    room_vector_points,
)
from net.tui_server import TuiServer
from tests.net.test_tui_server import _connect_and_join, _recv, _start

# A minimal valid SKILL.md the forge's skill generator can author (mirrors
# `tests/agent/test_forge.py`'s fixture); its name doesn't collide with any built-in skill id.
_VALID_SKILL_MD = """---
name: Grim Survival Horror
description: >
  Enable for a campaign about grinding, resource-scarce survival horror: supplies run out,
  wounds linger, and every choice costs something.
allowed-tools: []
metadata:
  scope: room
  content-rating: mature
---

# Grim survival horror

Track scarcity relentlessly: ammunition, food, and light sources are real, finite resources.
"""

# A minimal valid rulepack YAML (mirrors `tests/agent/test_forge_rulepack.py`'s fixture); its
# id/names don't collide with either built-in system (coc7/dnd5e).
_VALID_RULEPACK_YAML = """
names: [pulp-adventure, pulp]
set_keys: [pulp]
defaults:
  力量: 10
  意志: 10
alias:
  力量: [STR, strength]
derived:
  生命值上限:
    half_of: 意志
"""

_GENERATED_MODULE_MD = """# The Salt Marsh Vanishing

## Player-facing premise
Fisherfolk have gone missing near the marsh town of Greyreed.

## KEEPER-ONLY
The ferryman is the culprit, bound to an old pact.
"""


def _scripted_module_analysis_json() -> str:
    """A minimal well-formed module-analysis JSON — the shape `agent.forge`'s module generator's
    `upload_document(doc_type="module")` call triggers analysis for (mirrors
    `tests/agent/test_forge_module.py`'s fixture)."""
    return json.dumps(
        {
            "npcs": [
                {
                    "name": "The Ferryman",
                    "description": "A quiet old man.",
                    "secret": "He is the culprit.",
                    "role": "antagonist",
                }
            ],
            "summary": "Investigators uncover the truth behind the marsh disappearances.",
        }
    )


def _services(data_dir: str = "./data"):
    """Baseline services with a real `MutableLLM` (offline stub inner client) so
    the admin set-model path reconfigures live, exactly like `.model set`."""
    # Explicit llm AND imagegen blocks: the admin tests describe an unconfigured image
    # provider, so a developer's TRPG_IMAGEGEN__* in .env must not leak into them.
    settings = Settings(
        locale="en",
        data_dir=data_dir,
        llm=LLMSettings(provider="openai", chat_model="gpt-4o"),
        imagegen=ImageGenSettings(),
    )
    llm = MutableLLM(settings, builder=lambda s: FakeLLM(script=[]))
    return build_services(settings, llm=llm, embeddings=FakeEmbeddings(64))


async def _send(ws, frame: dict) -> dict:
    await ws.send(json.dumps(frame))
    return await _recv(ws)


async def _send_generate(ws, frame: dict) -> dict:
    """Send an `admin_generate` frame and read frames until the `admin_generated` result lands,
    skipping the `admin_generate_started` / `admin_generate_progress` frames an async module/pack
    generation streams first. Returns the `admin_generated` frame."""
    await ws.send(json.dumps(frame))
    while True:
        reply = await _recv(ws)
        if reply.get("type") in ("admin_generated", "admin_error"):
            return reply


def _update_services(command: str):
    settings = Settings(
        locale="en",
        llm=LLMSettings(provider="openai", chat_model="gpt-4o"),
        tui=TuiSettings(update_command=command),
    )
    return build_services(settings, llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def test_admin_skills_list_follows_the_caller_locale():
    """`admin_list_skills` returns the skill's `name-zh`/`description-zh` for a
    Chinese-locale caller and the English fields otherwise, so the KeeperSkills
    screen is readable in either language without client-side mappings."""
    services = _services()
    admin = AdminService(services, Keystore())

    zh = await admin.dispatch("keeper", "arkham", {"type": "admin_list_skills"}, get_i18n("zh"))
    assert zh["type"] == "admin_skills"
    romance = next(s for s in zh["skills"] if s["id"] == "romance-relationships")
    assert romance["name"] == "恋爱与关系"
    assert romance["description"].startswith("为以浪漫/亲密为核心的战役开启")
    assert romance["content_rating"] == "mature"
    assert romance["enabled"] is False

    en = await admin.dispatch("keeper", "arkham", {"type": "admin_list_skills"}, get_i18n("en"))
    assert en["type"] == "admin_skills"
    romance_en = next(s for s in en["skills"] if s["id"] == "romance-relationships")
    assert romance_en["name"] == "Romance & relationships"
    assert romance_en["description"].startswith("Enable for a campaign centered on romance")

    # A request-frame locale overrides the server locale: a Chinese-pinned client on an
    # English-locale server must still get the localized list.
    pinned = await admin.dispatch("keeper", "arkham", {"type": "admin_list_skills", "locale": "zh"}, get_i18n("en"))
    romance_pinned = next(s for s in pinned["skills"] if s["id"] == "romance-relationships")
    assert romance_pinned["name"] == "恋爱与关系"


def _st_preset_text(name: str, content: str) -> str:
    """A minimal valid SillyTavern completion preset (mirrors tests/core/test_preset.py)."""
    return json.dumps(
        {
            "name": name,
            "prompts": [
                {
                    "identifier": "main",
                    "name": "Main",
                    "role": "system",
                    "system_prompt": True,
                    "content": content,
                    "enabled": True,
                    "injection_position": 0,
                    "injection_depth": 4,
                }
            ],
        }
    )


async def test_admin_presets_save_enable_delete_flow(tmp_path):
    """`admin_save_preset` / `admin_enable_preset` / `admin_delete_preset` manage the
    per-room style layer end to end: an id is derived from the ST name when the request
    omits it, the list marks the caller's room's enabled preset, deleting a preset that
    is enabled clears the room's flag."""
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())

    empty = await admin.dispatch("keeper", "arkham", {"type": "admin_list_presets"}, get_i18n("en"))
    assert empty["type"] == "admin_presets"
    assert [p["id"] for p in empty["presets"]] == ["mature-mode"]  # the system tier alone
    assert all(p["system"] for p in empty["presets"])

    saved = await admin.dispatch(
        "keeper", "arkham", {"type": "admin_save_preset", "text": _st_preset_text("Duo", "Speak plainly. {{getvar::mood}}")}, get_i18n("en")
    )
    assert saved["type"] == "admin_presets"
    row = next(p for p in saved["presets"] if p["id"] == "duo")
    assert row["name"] == "duo"  # ST names live in the filename — the id is the display name
    assert row["parse_error"] is False
    assert row["prompt_count"] == 1
    assert "Speak plainly" in row["preview"]
    assert row["enabled"] is False

    enabled = await admin.dispatch("keeper", "arkham", {"type": "admin_enable_preset", "id": "duo", "on": True}, get_i18n("en"))
    assert next(p for p in enabled["presets"] if p["id"] == "duo")["enabled"] is True

    # Explicit id wins over the ST name; the file is overwritten, the list stays flat.
    overwritten = await admin.dispatch(
        "keeper", "arkham", {"type": "admin_save_preset", "id": "my-template", "text": _st_preset_text("Whatever", "New content.")}, get_i18n("en")
    )
    assert {p["id"] for p in overwritten["presets"] if not p["system"]} == {"duo", "my-template"}
    assert next(p for p in overwritten["presets"] if p["id"] == "my-template")["name"] == "my-template"

    disabled = await admin.dispatch("keeper", "arkham", {"type": "admin_enable_preset", "id": "duo", "on": False}, get_i18n("en"))
    assert next(p for p in disabled["presets"] if p["id"] == "duo")["enabled"] is False

    deleted = await admin.dispatch("keeper", "arkham", {"type": "admin_delete_preset", "id": "duo"}, get_i18n("en"))
    assert [p["id"] for p in deleted["presets"] if not p["system"]] == ["my-template"]


async def test_admin_delete_preset_clears_enabled_flag(tmp_path):
    """Deleting the preset the caller's room has enabled also clears its `preset_enabled`
    flag, so the room degrades to no style layer instead of pointing at a missing file."""
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())
    await admin.dispatch(
        "keeper", "arkham", {"type": "admin_save_preset", "text": _st_preset_text("Dune", "Sand.")}, get_i18n("en")
    )
    await admin.dispatch("keeper", "arkham", {"type": "admin_enable_preset", "id": "dune", "on": True}, get_i18n("en"))
    reply = await admin.dispatch("keeper", "arkham", {"type": "admin_delete_preset", "id": "dune"}, get_i18n("en"))
    assert [p["id"] for p in reply["presets"] if not p["system"]] == []
    assert await services.store.state_get("room:arkham", "preset_enabled") in (None, "")


async def test_admin_save_preset_rejects_invalid_text(tmp_path):
    services = _services(str(tmp_path))
    reply = await AdminService(services, Keystore()).dispatch(
        "keeper", "arkham", {"type": "admin_save_preset", "text": "not json at all"}, get_i18n("en")
    )
    assert reply["type"] == "admin_error" and reply["code"] == "invalid_preset"

    blank = await AdminService(services, Keystore()).dispatch(
        "keeper", "arkham", {"type": "admin_save_preset", "text": "   "}, get_i18n("en")
    )
    assert blank["type"] == "admin_error" and blank["code"] == "bad_request"


async def test_admin_enable_unknown_preset_is_rejected(tmp_path):
    services = _services(str(tmp_path))
    reply = await AdminService(services, Keystore()).dispatch(
        "keeper", "arkham", {"type": "admin_enable_preset", "id": "nope", "on": True}, get_i18n("en")
    )
    assert reply["type"] == "admin_error" and reply["code"] == "bad_request"


async def test_admin_delete_missing_preset_is_not_found(tmp_path):
    services = _services(str(tmp_path))
    reply = await AdminService(services, Keystore()).dispatch(
        "keeper", "arkham", {"type": "admin_delete_preset", "id": "nope"}, get_i18n("en")
    )
    assert reply["type"] == "admin_error" and reply["code"] == "not_found"


async def test_admin_presets_list_marks_system_tier_and_read_only(tmp_path):
    """The engine-shipped `mature-mode` preset appears in the listing as a SYSTEM
    preset: deletable nowhere, overwritable nowhere, but enable-able like any other."""
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())

    listed = await admin.dispatch("keeper", "arkham", {"type": "admin_list_presets"}, get_i18n("en"))
    assert listed["type"] == "admin_presets"
    mature = next(p for p in listed["presets"] if p["id"] == "mature-mode")
    assert mature["system"] is True
    assert mature["content_rating"] == "explicit"
    assert mature["prompt_count"] == 1
    assert mature["enabled"] is False

    # A user preset is marked system=False.
    await admin.dispatch("keeper", "arkham", {"type": "admin_save_preset", "text": _st_preset_text("Dune", "Sand.")}, get_i18n("en"))
    relisted = await admin.dispatch("keeper", "arkham", {"type": "admin_list_presets"}, get_i18n("en"))
    assert next(p for p in relisted["presets"] if p["id"] == "dune")["system"] is False

    # Deleting a system preset is refused.
    denied = await admin.dispatch("keeper", "arkham", {"type": "admin_delete_preset", "id": "mature-mode"}, get_i18n("en"))
    assert denied["type"] == "admin_error"

    # Saving over a system preset id is refused.
    overwritten = await admin.dispatch(
        "keeper", "arkham", {"type": "admin_save_preset", "id": "mature-mode", "text": _st_preset_text("Evil", "X.")}, get_i18n("en")
    )
    assert overwritten["type"] == "admin_error"

    # It still enables for the room (one preset at a time).
    enabled = await admin.dispatch("keeper", "arkham", {"type": "admin_enable_preset", "id": "mature-mode", "on": True}, get_i18n("en"))
    assert next(p for p in enabled["presets"] if p["id"] == "mature-mode")["enabled"] is True


async def test_admin_export_presets_bundles_only_user_tier(tmp_path):
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())
    text = _st_preset_text("Dune", "Sand.")
    await admin.dispatch("keeper", "arkham", {"type": "admin_save_preset", "text": text}, get_i18n("en"))

    exported = await admin.dispatch("keeper", "arkham", {"type": "admin_export_presets"}, get_i18n("en"))
    assert exported["type"] == "admin_preset_export_all"
    assert exported["presets"] == [{"id": "dune", "text": text}]
    # The engine-shipped system tier is never part of the bundle.
    assert all(p["id"] != "mature-mode" for p in exported["presets"])


async def test_admin_import_presets_round_trips_and_skips_bad_entries(tmp_path):
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())
    good = _st_preset_text("Dune", "Sand.")
    bad = "not json at all"

    reply = await admin.dispatch(
        "keeper", "arkham",
        {"type": "admin_import_presets", "presets": [{"id": "dune", "text": good}, {"id": "broken", "text": bad}]},
        get_i18n("en"),
    )
    assert reply["type"] == "admin_presets"
    assert reply["imported"] == ["dune"]
    assert reply["skipped"] == [{"id": "broken", "reason": "invalid"}]

    # A system id in the bundle is skipped, never overwritten.
    system_try = await admin.dispatch(
        "keeper", "arkham",
        {"type": "admin_import_presets", "presets": [{"id": "mature-mode", "text": _st_preset_text("Evil", "X.")}]},
        get_i18n("en"),
    )
    assert system_try["imported"] == []
    assert system_try["skipped"] == [{"id": "mature-mode", "reason": "system"}]
    # Nothing landed in the USER tier under the system id.
    assert not (tmp_path / "presets" / "mature-mode.json").exists()

    # An id-less entry derives its id from the ST name, like the paste box.
    derived = await admin.dispatch(
        "keeper", "arkham",
        {"type": "admin_import_presets", "presets": [{"text": _st_preset_text("Oasis", "Water.")}]},
        get_i18n("en"),
    )
    assert derived["imported"] == ["oasis"]

    malformed = await admin.dispatch("keeper", "arkham", {"type": "admin_import_presets", "presets": "nope"}, get_i18n("en"))
    assert malformed["type"] == "admin_error" and malformed["code"] == "bad_request"


async def test_admin_update_server_not_configured_is_rejected():
    services = _update_services("")  # feature off
    reply = await AdminService(services, Keystore()).dispatch(
        "keeper", "arkham", {"type": "admin_update_server"}, get_i18n("en")
    )
    assert reply["type"] == "admin_error" and reply["code"] == "not_configured"


async def test_admin_update_server_requires_keeper():
    services = _update_services("echo hi")
    reply = await AdminService(services, Keystore()).dispatch(
        "player", "arkham", {"type": "admin_update_server"}, get_i18n("en")
    )
    assert reply["type"] == "admin_error" and reply["code"] == "forbidden"


async def test_admin_update_server_failed_command_reports_output_and_does_not_restart():
    services = _update_services("echo boom; exit 1")
    with patch("net.updater.schedule_reexec") as reexec:
        reply = await AdminService(services, Keystore()).dispatch(
            "keeper", "arkham", {"type": "admin_update_server"}, get_i18n("en")
        )
    assert reply["type"] == "admin_update"
    assert reply["status"] == "failed"
    assert "boom" in reply["output"]
    reexec.assert_not_called()


async def test_admin_update_server_success_reports_restarting_and_schedules_reexec():
    services = _update_services("echo done")
    with patch("net.updater.schedule_reexec") as reexec:
        reply = await AdminService(services, Keystore()).dispatch(
            "keeper", "arkham", {"type": "admin_update_server"}, get_i18n("en")
        )
    assert reply["type"] == "admin_update"
    assert reply["status"] == "restarting"
    reexec.assert_called_once()


def test_welcome_frame_carries_version_and_keeper_gated_update_feature():
    from net.session import welcome_frame

    fields = {"room": "r", "id": "i", "name": "n", "role": "keeper", "locale": "en"}
    base = welcome_frame(fields)
    assert base["version"]  # resolve_version() returns a non-empty string
    assert "update" not in base["features"]  # no update command configured
    assert "update" in welcome_frame(fields, can_update=True)["features"]


async def test_run_update_command_captures_success_and_failure():
    from net.updater import run_update_command

    ok = await run_update_command("echo hello")
    assert ok.ok and "hello" in ok.output
    bad = await run_update_command("echo oops; exit 2")
    assert not bad.ok and "oops" in bad.output


async def test_admin_service_mints_room_scoped_single_use_chat_bind_token():
    services = _services()
    keystore = Keystore()
    admin = AdminService(services, keystore)

    reply = await admin.dispatch(
        "keeper",
        "arkham",
        {"type": "admin_mint_key", "purpose": "chat_bind", "expires_in": 60},
        get_i18n("en"),
    )

    minted = reply["minted"]
    assert minted["room"] == "arkham"
    assert minted["role"] == "keeper"
    assert minted["purpose"] == "chat_bind"
    assert minted["expires_at"] is not None
    assert keystore.get(minted["key"]) is None
    assert keystore.get(minted["key"], purpose="chat_bind") is not None

    await set_keeper_binding(services.store, "discord", "keeper-7", "arkham")
    listed = await admin.dispatch(
        "keeper",
        "arkham",
        {"type": "admin_list_keys"},
        get_i18n("en"),
    )
    binding = next(item for item in listed["keys"] if item["id"].startswith("chat:"))
    assert binding["key_masked"] == "discord:keeper-7"
    # The keystore's own chat_bind row ships masked and NEVER cleartext — its
    # token is a different credential from an invite (join) key.
    raw = next(item for item in listed["keys"] if item["purpose"] == "chat_bind" and not item["id"].startswith("chat:"))
    assert "key" not in raw
    assert raw["key_masked"] != minted["key"]

    await admin.dispatch(
        "keeper",
        "arkham",
        {"type": "admin_delete_key", "id": binding["id"]},
        get_i18n("en"),
    )
    assert await get_keeper_binding(services.store, "discord", "keeper-7") is None


async def test_keeper_can_get_and_set_config_list_and_mint_keys():
    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    foreign_key = keystore.add(room="dunwich", name="Foreign Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")

        # get_config: describe() + provider catalog + no override yet.
        config = await _send(ws, {"type": "admin_get_config"})
        assert config["type"] == "admin_config"
        assert config["provider"] == "openai"
        assert config["chat_model"] == "gpt-4o"
        assert config["override_active"] is False
        assert "deepseek" in config["providers"] and "anthropic" in config["providers"]
        assert "gpt-subscription" in config["providers"] and "chatgpt" in config["providers"]
        assert "minimax-cn" in config["providers"]
        provider_catalog = {provider["id"]: provider for provider in config["provider_catalog"]}
        assert provider_catalog["minimax-cn"] == {
            "id": "minimax-cn",
            "default_base_url": "https://api.minimaxi.com/v1",
            "auth_type": "api_key",
            "model_kinds": ["chat", "image"],
        }
        assert "api_key_masked" in config

        # set_model: validated, persisted, and hot-applied to the live MutableLLM.
        updated = await _send(ws, {"type": "admin_set_model", "provider": "deepseek", "chat_model": "deepseek-chat"})
        assert updated["type"] == "admin_config"
        assert updated["provider"] == "deepseek"
        assert updated["chat_model"] == "deepseek-chat"
        assert updated["override_active"] is True
        # live reconfigure mutated the shared settings, and it persisted.
        assert services.settings.llm.provider == "deepseek"
        assert await services.runtime_config.get() == {
            "provider": "deepseek",
            "chat_model": "deepseek-chat",
            "api_key": "",
            "base_url": "",
        }

        # an unknown provider is refused without mutating anything.
        bad = await _send(ws, {"type": "admin_set_model", "provider": "nope-9000"})
        assert bad["type"] == "admin_error"
        assert bad["code"] == "unknown_provider"
        assert bad["message"]
        assert services.settings.llm.provider == "deepseek"  # unchanged

        # list_keys exposes ONLY the caller's bound room: every row keeps its
        # masked value, and join-purpose rows ALSO carry the cleartext key on
        # this keeper-gated channel so the keeper can copy an invite to share.
        listed = await _send(ws, {"type": "admin_list_keys"})
        assert listed["type"] == "admin_keys"
        assert len(listed["keys"]) == 1
        only = listed["keys"][0]
        assert only["room"] == "arkham" and only["role"] == "keeper"
        assert only["key_masked"] != keeper_key
        assert "..." in only["key_masked"]
        assert only["key"] == keeper_key
        assert foreign_key not in json.dumps(listed)

        # Minting into a different room is forbidden; omission selects caller_room.
        cross_room = await _send(ws, {"type": "admin_mint_key", "room": "dunwich", "name": "Intruder"})
        assert cross_room["type"] == "admin_error"
        assert cross_room["code"] == "forbidden"

        # mint_key returns the fresh key ONCE in cleartext + a refreshed masked list.
        minted = await _send(ws, {"type": "admin_mint_key", "room": "arkham", "name": "Player One", "role": "player"})
        assert minted["type"] == "admin_keys"
        assert minted["minted"]["room"] == "arkham"
        assert minted["minted"]["role"] == "player"
        new_key = minted["minted"]["key"]
        assert new_key and new_key != keeper_key
        # the new key really landed in the keystore, and the list now has both.
        assert keystore.get(new_key) is not None
        assert len(minted["keys"]) == 2
        assert all("..." in entry["key_masked"] or entry["key_masked"] == "" for entry in minted["keys"])
        assert all(entry.get("id") for entry in minted["keys"])
        # Cleartext rides exactly on the join-purpose rows — and only there.
        assert all((entry.get("key") is not None) == (entry["purpose"] == "join") for entry in minted["keys"])
        assert {entry["key"] for entry in minted["keys"] if entry["purpose"] == "join"} == {
            keeper_key,
            new_key,
        }

        # update + delete a key in the keeper's OWN room (arkham) — allowed.
        new_id = next(entry["id"] for entry in minted["keys"] if entry["name"] == "Player One")
        updated_key = await _send(ws, {"type": "admin_update_key", "id": new_id, "name": "Co-Keeper", "role": "keeper"})
        assert updated_key["type"] == "admin_keys"
        changed = next(entry for entry in updated_key["keys"] if entry["id"] == new_id)
        assert changed["name"] == "Co-Keeper"
        assert changed["role"] == "keeper"
        assert keystore.get(new_key).role == "keeper"

        deleted_key = await _send(ws, {"type": "admin_delete_key", "id": new_id})
        assert deleted_key["type"] == "admin_keys"
        assert keystore.get(new_key) is None
        assert all(entry["id"] != new_id for entry in deleted_key["keys"])

        missing = await _send(ws, {"type": "admin_delete_key", "id": "missing"})
        assert missing["type"] == "admin_error"
        assert missing["code"] == "not_found"

        await ws.close()
    finally:
        await server.close()


async def test_last_keeper_key_cannot_be_demoted_or_deleted():
    """Anti-lockout: the room's only keeper key can never lose the keeper role.

    Regression for the TUI form bug that demoted the bootstrap keeper key to
    player (name/role form state is shared between mint and edit; a stale role
    was written back onto the selected key), after which the room permanently
    loses every keeper surface and no new keeper key can be minted.
    """
    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")

        listed = await _send(ws, {"type": "admin_list_keys"})
        keeper_id = next(entry["id"] for entry in listed["keys"] if entry["role"] == "keeper")

        # Demoting the last keeper is refused, and nothing is mutated.
        demote = await _send(ws, {"type": "admin_update_key", "id": keeper_id, "role": "player"})
        assert demote["type"] == "admin_error"
        assert demote["code"] == "last_keeper"
        assert keystore.get(keeper_key).role == "keeper"

        # Deleting the last keeper is refused too.
        delete = await _send(ws, {"type": "admin_delete_key", "id": keeper_id})
        assert delete["type"] == "admin_error"
        assert delete["code"] == "last_keeper"
        assert keystore.get(keeper_key) is not None

        # A pending keeper chat_bind token must NOT inflate the keeper count: it
        # cannot authenticate a connection, so the join key is still the room's only
        # keeper access and stays protected while the token is outstanding.
        minted_bind = await _send(
            ws, {"type": "admin_mint_key", "room": "arkham", "name": "bind", "purpose": "chat_bind"}
        )
        assert minted_bind["type"] == "admin_keys"
        demote = await _send(ws, {"type": "admin_update_key", "id": keeper_id, "role": "player"})
        assert demote["type"] == "admin_error"
        assert demote["code"] == "last_keeper"

        # With a second keeper key in the room, the FIRST (non-caller) may be demoted.
        minted = await _send(ws, {"type": "admin_mint_key", "room": "arkham", "name": "Co-Keeper", "role": "keeper"})
        second_id = next(entry["id"] for entry in minted["keys"] if entry["name"] == "Co-Keeper")
        demote = await _send(ws, {"type": "admin_update_key", "id": second_id, "role": "player"})
        assert demote["type"] == "admin_keys"
        changed = next(entry for entry in demote["keys"] if entry["id"] == second_id)
        assert changed["role"] == "player"

        # The caller's key is now the room's last keeper — protected again.
        delete = await _send(ws, {"type": "admin_delete_key", "id": keeper_id})
        assert delete["type"] == "admin_error"
        assert delete["code"] == "last_keeper"
        demote = await _send(ws, {"type": "admin_update_key", "id": keeper_id, "role": "player"})
        assert demote["type"] == "admin_error"
        assert demote["code"] == "last_keeper"

        await ws.close()
    finally:
        await server.close()


async def test_key_mutation_rechecks_room_after_external_move(tmp_path):
    services = _services(str(tmp_path))
    key_path = tmp_path / "keys.toml"
    keystore = Keystore.load(key_path)
    with keystore.persisted_mutation():
        keystore.add(room="arkham", name="Keeper", role="keeper")
        victim = keystore.add(room="arkham", name="Player", role="player")
    admin = AdminService(services, keystore)

    listed = await admin.dispatch(
        "keeper",
        "arkham",
        {"type": "admin_list_keys"},
        get_i18n("en"),
    )
    victim_id = next(item["id"] for item in listed["keys"] if item["name"] == "Player")

    external = Keystore.load(key_path)
    with external.persisted_mutation():
        assert external.update(victim, room="dunwich")

    updated = await admin.dispatch(
        "keeper",
        "arkham",
        {"type": "admin_update_key", "id": victim_id, "name": "Hijacked"},
        get_i18n("en"),
    )
    deleted = await admin.dispatch(
        "keeper",
        "arkham",
        {"type": "admin_delete_key", "id": victim_id},
        get_i18n("en"),
    )

    assert updated["type"] == deleted["type"] == "admin_error"
    assert updated["code"] == deleted["code"] == "forbidden"
    persisted = Keystore.load(key_path).get(victim)
    assert persisted is not None
    assert persisted.room == "dunwich"
    assert persisted.name == "Player"


async def test_player_role_connection_is_refused_every_admin_action():
    services = _services()
    keystore = Keystore()
    player_key = keystore.add(room="arkham", name="Player", role="player")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, player_key, "Player")

        for request in (
            {"type": "admin_get_config"},
            {"type": "admin_set_model", "provider": "deepseek"},
            {"type": "admin_list_keys"},
            {"type": "admin_mint_key", "room": "secret", "role": "keeper"},
            {"type": "admin_update_key", "id": "anything", "role": "keeper"},
            {"type": "admin_delete_key", "id": "anything"},
            {"type": "admin_delete_room", "room": "arkham"},
            {"type": "admin_export_room", "room": "arkham"},
            {"type": "admin_import_room", "path": "backup.json"},
            {"type": "admin_delete_room_data", "room": "arkham"},
            {"type": "admin_reset_room", "room": "arkham"},
            {"type": "admin_list_skills"},
            {"type": "admin_enable_skill", "id": "romance-relationships", "on": True},
            {"type": "admin_list_rules"},
            {"type": "admin_generate", "kind": "skill", "description": "anything"},
        ):
            reply = await _send(ws, request)
            assert reply["type"] == "admin_error"
            assert reply["code"] == "forbidden"
            assert reply["message"]

        # nothing leaked or mutated: no override persisted, no key minted.
        assert services.settings.llm.provider == "openai"
        assert await services.runtime_config.get() == {}
        assert len(keystore) == 1

        await ws.close()
    finally:
        await server.close()


async def test_admin_set_model_leaves_state_unchanged_when_provider_build_fails():
    """A failed candidate build happens before live or persisted state changes."""

    def _raising_builder(settings):
        if (settings.llm.provider or "").lower() == "anthropic":
            raise ValueError("anthropic SDK missing")
        return FakeLLM(script=[])

    settings = Settings(locale="en", llm=LLMSettings(provider="openai", chat_model="gpt-4o"))
    llm = MutableLLM(settings, builder=_raising_builder)
    services = build_services(settings, llm=llm, embeddings=FakeEmbeddings(64))

    reply = await AdminService(services, Keystore()).dispatch(
        "keeper",
        "",  # caller_room — irrelevant for the non-room-scoped set_model op
        {"type": "admin_set_model", "provider": "anthropic"},
        get_i18n("en"),
    )

    assert reply["type"] == "admin_error"
    assert reply["code"] == "set_failed"
    assert services.settings.llm.provider == "openai"  # unchanged
    assert await services.runtime_config.get() == {}  # not persisted
    assert isinstance(services.llm.inner, FakeLLM)


async def test_admin_set_model_rechecks_authorization_after_waiting_for_lock():
    services = _services()
    authorized = True
    await services.config_lock.acquire()
    task = asyncio.create_task(
        AdminService(services, Keystore()).dispatch(
            "keeper",
            "arkham",
            {"type": "admin_set_model", "provider": "deepseek"},
            get_i18n("en"),
            reauthorize=lambda: authorized,
        )
    )
    await asyncio.sleep(0)
    authorized = False
    services.config_lock.release()

    reply = await task

    assert reply["type"] == "admin_error"
    assert reply["code"] == "forbidden"
    assert services.settings.llm.provider == "openai"
    assert await services.runtime_config.get() == {}


async def test_revoking_chat_binding_evicts_live_direct_member() -> None:
    services = _services()
    hub = RoomHub()
    admin = AdminService(services, Keystore(), hub=hub)
    source = SessionSource(
        platform="discord",
        chat_type="dm",
        chat_id="dm-7",
        user_id="keeper-7",
    )

    class DirectMember:
        id = "discord:dm-7"
        user_key = "discord:keeper-7"
        transport = "discord"
        name = "Keeper"

        def __init__(self) -> None:
            self.source = source

        async def deliver(self, _event) -> None:
            return None

    await set_keeper_binding(services.store, "discord", "keeper-7", "arkham")
    member = DirectMember()
    session_key = session_key_for_room("arkham")
    await hub.subscribe(session_key, member)
    listed = await admin.dispatch("keeper", "arkham", {"type": "admin_list_keys"}, get_i18n("en"))
    binding_id = next(item["id"] for item in listed["keys"] if item["id"].startswith("chat:"))

    await admin.dispatch(
        "keeper",
        "arkham",
        {"type": "admin_delete_key", "id": binding_id},
        get_i18n("en"),
    )

    assert await get_keeper_binding(services.store, "discord", "keeper-7") is None
    assert member not in hub.members(session_key)
    assert await resolve_session_key(services.store, source) == source.chat_key()


async def test_admin_set_model_replaces_provider_scoped_credentials():
    import time

    from infra.oauth_flows import SubscriptionToken

    services = _services()
    admin = AdminService(services, Keystore())
    previous = {
        "provider": "deepseek",
        "chat_model": "deepseek-chat",
        "api_key": "sk-old-provider",
        "base_url": "https://old-provider.example/v1",
    }
    await services.runtime_config.replace(**previous)
    services.llm.apply(previous)
    await services.llm_profiles.save_subscription(
        "supergrok",
        SubscriptionToken("access-secret", "refresh-secret", time.time() + 3600),
    )

    switched = await admin.dispatch(
        "keeper",
        "",
        {"type": "admin_set_model", "provider": "supergrok"},
        get_i18n("en"),
    )

    assert switched["type"] == "admin_config"
    assert switched["provider"] == "supergrok"
    assert await services.runtime_config.get() == {
        "provider": "supergrok",
        "chat_model": "grok-4.6",
        "api_key": "",
        "base_url": "",
    }
    assert services.settings.llm.api_key == ""
    assert services.settings.llm.base_url == ""

    await services.llm_profiles.remember(
        "chatgpt",
        api_key="sk-chatgpt-proxy",
        base_url="https://chatgpt-proxy.example/v1",
    )
    proxy = await admin.dispatch(
        "keeper",
        "",
        {"type": "admin_set_model", "provider": "chatgpt", "chat_model": "proxy-model"},
        get_i18n("en"),
    )

    assert proxy["type"] == "admin_config"
    assert await services.runtime_config.get() == {
        "provider": "chatgpt",
        "chat_model": "proxy-model",
        "api_key": "sk-chatgpt-proxy",
        "base_url": "https://chatgpt-proxy.example/v1",
    }


async def test_admin_set_model_saves_supplied_key_under_selected_provider(tmp_path):
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())

    result = await admin.dispatch(
        "keeper",
        "",
        {
            "type": "admin_set_model",
            "provider": "deepseek",
            "chat_model": "deepseek-v4-flash",
            "api_key": "sk-deepseek-test",
            "base_url": "https://api.deepseek.com/v1",
        },
        get_i18n("en"),
    )

    assert result["type"] == "admin_config"
    assert result["provider"] == "deepseek"
    credentials = await services.llm_profiles.all()
    assert credentials["deepseek"] == {
        "api_key": "sk-deepseek-test",
        "base_url": "https://api.deepseek.com/v1",
        "chat_model": "deepseek-v4-flash",
    }
    assert "openai" not in credentials


async def _seed_reset_room(services, chat_key):
    await services.store.state_set(chat_key, "chat_history", '[{"role":"user"}]')
    await services.documents.put(chat_key, "sheet", "Ada", {"name": "Ada", "owner": "player-1"})
    await services.documents.put_singleton(chat_key, "module_pool", {"keeper": {}, "player": {"summary": "x"}})
    await services.store.state_set(chat_key, "rule_variant", "rule2")
    await services.store.set(user_key="", store_key="bound_room.discord:group:table", value=chat_key)
    await services.store.state_set("tui:group:dunwich", "chat_history", "keep")
    await services.vector_db.vector_store.upsert(
        [("doc-1:0", [0.1] * 64, {"chat_key": chat_key, "document_id": "doc-1", "chunk_index": 0})]
    )


async def test_admin_reset_room_all_wipes_everything_but_settings_and_keys(tmp_path):
    services = _services(str(tmp_path))
    keystore = Keystore.load(tmp_path / "keys.toml")
    with keystore.persisted_mutation():
        keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
        player_key = keystore.add(room="arkham", name="Ada", role="player")
        other_key = keystore.add(room="dunwich", name="Other", role="player")

    chat_key = chat_key_for_room("arkham")
    await _seed_reset_room(services, chat_key)

    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")

        # A different room's keeper reset is refused before anything is touched.
        forbidden = await _send(ws, {"type": "admin_reset_room", "room": "dunwich"})
        assert forbidden["type"] == "admin_error" and forbidden["code"] == "forbidden"
        # An unknown scope is a bad request (validated before any wipe).
        bad = await _send(ws, {"type": "admin_reset_room", "room": "arkham", "scope": "bogus"})
        assert bad["type"] == "admin_error" and bad["code"] == "bad_request"

        # A successful reset also broadcasts a fresh reset-flagged state frame to the
        # connected keeper (so its panel + chat log refresh without reconnecting), which
        # arrives alongside the admin_room_op reply — drain both regardless of order.
        await ws.send(json.dumps({"type": "admin_reset_room", "room": "arkham", "scope": "all"}))
        reset = None
        state = None
        for _ in range(4):
            frame = await _recv(ws)
            if frame["type"] == "admin_room_op":
                reset = frame
                break
            if frame["type"] == "state":
                state = frame
        assert reset is not None
        assert reset["action"] == "reset"
        assert reset["room"] == "arkham"
        assert reset["scope"] == "all"
        assert reset["store_rows"] == 1  # chat_history (runtime state)
        assert reset["documents"] == 2  # the Ada sheet + the module pool document
        assert reset["vector_points"] == 1
        assert reset["keys"] == 0  # reset never removes keys
        assert "path" not in reset  # no backup is written
        assert state is not None and state.get("reset") is True
    finally:
        await server.close()

    # Campaign state is gone...
    assert await services.store.state_get(chat_key, "chat_history") is None
    assert await services.documents.get(chat_key, "sheet", "Ada") is None
    assert await services.documents.get_singleton(chat_key, "module_pool") is None
    assert await services.vector_db.vector_store.count(filter={"chat_key": chat_key}) == 0
    # ...but room settings, keys, channel binding, and the unrelated room all survive.
    assert await services.store.state_get(chat_key, "rule_variant") == "rule2"
    assert keystore.get(keeper_key) is not None
    assert keystore.get(player_key) is not None
    assert keystore.get(other_key) is not None
    assert await services.store.get(user_key="", store_key="bound_room.discord:group:table") == chat_key


async def test_admin_reset_room_story_scope_keeps_characters_module_and_vectors(tmp_path):
    services = _services(str(tmp_path))
    keystore = Keystore.load(tmp_path / "keys.toml")
    with keystore.persisted_mutation():
        keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")

    chat_key = chat_key_for_room("arkham")
    await _seed_reset_room(services, chat_key)

    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        # Default scope ("story") when the field is omitted.
        await ws.send(json.dumps({"type": "admin_reset_room", "room": "arkham"}))
        reset = None
        for _ in range(4):
            frame = await _recv(ws)
            if frame["type"] == "admin_room_op":
                reset = frame
                break
        assert reset is not None
        assert reset["scope"] == "story"
        assert reset["store_rows"] == 1  # only chat_history
        assert reset["vector_points"] == 0  # module vectors untouched
    finally:
        await server.close()

    # Story gone, but the character, module and its vectors all survive.
    assert await services.store.state_get(chat_key, "chat_history") is None
    assert await services.documents.get(chat_key, "sheet", "Ada") is not None
    assert await services.documents.get_singleton(chat_key, "module_pool") is not None
    assert await services.vector_db.vector_store.count(filter={"chat_key": chat_key}) == 1
    assert await services.store.get(user_key="", store_key="bound_room.discord:group:table") == chat_key
    assert await services.store.state_get("tui:group:dunwich", "chat_history") == "keep"


async def test_keeper_can_export_delete_and_import_room_data(tmp_path):
    services = _services(str(tmp_path))
    keystore = Keystore.load(tmp_path / "keys.toml")
    with keystore.persisted_mutation():
        keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
        player_key = keystore.add(room="arkham", name="Ada", role="player")
        other_key = keystore.add(room="dunwich", name="Other", role="player")

    chat_key = chat_key_for_room("arkham")
    await services.store.state_set(chat_key, "chat_history", '[{"role":"user"}]')
    await services.store.state_set(chat_key, "active_character.player-1", "Ada")
    await services.documents.put(chat_key, "sheet", "Ada", {"name": "Ada", "owner": "player-1"})
    await services.documents.put(chat_key, "npc", "n1", {"name": "Dr. West"})
    await services.documents.put(chat_key, "lore", "l1", {"title": "Kingsport", "content": "a foggy port"})
    await services.store.set(user_key="", store_key="bound_room.discord:group:table", value=chat_key)
    await services.store.state_set("tui:group:dunwich", "chat_history", "keep")
    for base, value in {
        "skills_enabled": '["romance-relationships"]',
        "media_enabled": "1",
        "media_history": "[]",
        "audio_library": "[]",
        "audio_state": "{}",
        "relationships": '{"Ada":{"West":{"affection":5}}}',
        "usage_stats": '{"input_tokens":12}',
    }.items():
        await services.store.state_set(chat_key, base, value)
    media_store = MediaStore(
        services.store,
        services.settings.data_dir,
        allowed_mimes=ALLOWED_MEDIA_MIMES,
    )
    media_record = await media_store.register_blob(
        room=chat_key,
        data=b"private handout",
        mime="image/png",
        name="clue.png",
        uploader="keeper",
    )
    await services.vector_db.vector_store.upsert(
        [
            ("doc-1:0", [0.1] * 64, {"chat_key": chat_key, "document_id": "doc-1", "chunk_index": 0}),
            (
                f"{chat_key}:l1",
                [0.2] * 64,
                {"collection": "worldbook", "namespace": chat_key, "entry_id": "l1"},
            ),
        ]
    )

    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")

        # Export writes a snapshot; a client-supplied `path` is CONFINED to data_dir/room_backups
        # (only the filename is honored — never an arbitrary-location write). The traversal guard
        # itself is covered by test_admin_export_confines_the_path_to_the_backups_directory.
        backups = str((Path(services.settings.data_dir) / "room_backups").resolve())
        exported = await _send(ws, {"type": "admin_export_room", "room": "arkham", "path": "arkham-export.json"})
        assert exported["type"] == "admin_room_op"
        assert exported["action"] == "export"
        assert exported["room"] == "arkham"
        assert exported["path"].startswith(backups) and exported["path"].endswith("arkham-export.json")
        assert exported["keys"] == 2
        assert exported["store_rows"] == 1  # the channel binding
        assert exported["documents"] == 3  # sheet + npc + lore
        assert exported["room_state_rows"] == 9  # chat_history + active pointer + 7 runtime rows
        assert exported["vector_points"] == 2
        assert exported["media_files"] == 1
        snapshot = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
        assert {item["key"] for item in snapshot["keys"]} == {keeper_key, player_key}
        assert snapshot["media"][0]["hash"] == media_record.hash
        if os.name == "posix":
            assert stat.S_IMODE(Path(backups).stat().st_mode) == 0o700
            assert stat.S_IMODE(Path(exported["path"]).stat().st_mode) == 0o600

        deleted = await _send(
            ws,
            {"type": "admin_delete_room_data", "room": "arkham", "backup": True, "path": "arkham-delete-backup.json"},
        )
        assert deleted["type"] == "admin_room_op"
        assert deleted["action"] == "delete"
        assert deleted["path"].startswith(backups) and deleted["path"].endswith("arkham-delete-backup.json")
        assert deleted["keys"] == 2
        assert deleted["vector_points"] == 2
        assert deleted["media_files"] == 1
        assert Path(deleted["path"]).is_file()
        assert keystore.get(keeper_key) is None
        assert keystore.get(player_key) is None
        assert keystore.get(other_key) is not None  # a DIFFERENT room's key is untouched
        assert await services.store.state_get(chat_key, "chat_history") is None
        assert await services.store.state_get(chat_key, "active_character.player-1") is None
        assert await services.documents.get(chat_key, "sheet", "Ada") is None
        assert await services.documents.get(chat_key, "npc", "n1") is None
        assert await services.store.get(user_key="", store_key="bound_room.discord:group:table") is None
        assert await services.store.state_get("tui:group:dunwich", "chat_history") == "keep"
        assert await services.vector_db.vector_store.count(filter={"chat_key": chat_key}) == 0
        assert (
            await services.vector_db.vector_store.count(filter={"collection": "worldbook", "namespace": chat_key}) == 0
        )
        for base in (
            "skills_enabled",
            "media_enabled",
            "media_history",
            "audio_library",
            "audio_state",
            "relationships",
            "usage_stats",
        ):
            assert await services.store.state_get(chat_key, base) is None
        with pytest.raises(MediaError, match="media_not_found"):
            await media_store.read_bytes(chat_key, media_record.hash)

        # Revoking the room's keys invalidates the live Keeper connection immediately; it must
        # not retain stale admin authority merely because it authenticated before the delete.
        stale = await _send(ws, {"type": "admin_import_room", "path": Path(deleted["path"]).name})
        assert stale["type"] == "error"
        assert stale["code"] == "forbidden"
        await ws.close()

        # Restore through a newly minted, persisted out-of-band operations key. Importing INTO
        # another room is forbidden — see test_admin_room_ops_are_scoped_to_the_callers_room.
        with keystore.persisted_mutation():
            recovery_key = keystore.add(room="arkham", name="Recovery", role="keeper")
        ws, *_ = await _connect_and_join(url, recovery_key, "Recovery")
        if os.name == "posix":
            os.chmod(deleted["path"], 0o644)  # legacy backup permissions self-heal on import
        imported = await _send(ws, {"type": "admin_import_room", "path": Path(deleted["path"]).name})
        assert imported["type"] == "admin_room_op"
        assert imported["action"] == "import"
        assert imported["room"] == "arkham"
        assert imported["keys"] == 2
        assert imported["store_rows"] == 1
        assert imported["documents"] == 3
        assert imported["room_state_rows"] == 9
        assert imported["vector_points"] == 2
        assert imported["media_files"] == 1
        if os.name == "posix":
            assert stat.S_IMODE(Path(deleted["path"]).stat().st_mode) == 0o600
        assert keystore.get(keeper_key).room == "arkham"
        assert keystore.get(player_key).room == "arkham"
        assert await services.store.state_get(chat_key, "chat_history") == '[{"role":"user"}]'
        assert await services.store.state_get(chat_key, "active_character.player-1") == "Ada"
        restored_sheet = await services.documents.get(chat_key, "sheet", "Ada")
        assert restored_sheet is not None and restored_sheet.data["name"] == "Ada"
        restored_npc = await services.documents.get(chat_key, "npc", "n1")
        assert restored_npc is not None and restored_npc.data["name"] == "Dr. West"
        assert await services.store.get(user_key="", store_key="bound_room.discord:group:table") == chat_key
        assert await services.vector_db.vector_store.count(filter={"chat_key": chat_key}) == 1
        assert (
            await services.vector_db.vector_store.count(filter={"collection": "worldbook", "namespace": chat_key}) == 1
        )
        restored_record, restored_data = await media_store.read_bytes(chat_key, media_record.hash)
        assert restored_record.name == "clue.png"
        assert restored_data == b"private handout"
        assert await services.store.state_get(chat_key, "skills_enabled") == '["romance-relationships"]'
        assert await services.store.state_get(chat_key, "relationships") == '{"Ada":{"West":{"affection":5}}}'

        await ws.close()
    finally:
        await server.close()


async def test_room_backup_paths_are_room_owned_and_default_names_are_unique(tmp_path):
    services = _services(str(tmp_path))
    keystore = Keystore()
    keystore.add(room="arkham", name="Keeper", role="keeper")
    keystore.add(room="dunwich", name="Keeper", role="keeper")
    arkham_key = chat_key_for_room("arkham")
    dunwich_key = chat_key_for_room("dunwich")
    await services.store.state_set(arkham_key, "chat_history", "arkham-v1")
    await services.store.state_set(dunwich_key, "chat_history", "dunwich-v1")

    arkham = await export_room(services, keystore, "arkham", "shared.json")
    dunwich = await export_room(services, keystore, "dunwich", "shared.json")
    arkham_path = Path(arkham["path"])
    dunwich_path = Path(dunwich["path"])
    dunwich_bytes = dunwich_path.read_bytes()

    assert arkham_path.parent != dunwich_path.parent
    assert arkham_path.name == dunwich_path.name == "shared.json"

    # Replacing a named snapshot is confined to the exact room namespace.
    await services.store.state_set(arkham_key, "chat_history", "arkham-v2")
    replaced = await export_room(services, keystore, "arkham", "shared.json")
    assert Path(replaced["path"]) == arkham_path
    assert dunwich_path.read_bytes() == dunwich_bytes

    first_default = await export_room(services, keystore, "arkham")
    second_default = await export_room(services, keystore, "arkham")
    assert first_default["path"] != second_default["path"]

    # If Arkham's file is absent, an Arkham import must not discover Dunwich's same-name file.
    arkham_path.unlink()
    with pytest.raises(ValueError, match="not a room backup file"):
        await import_room(services, keystore, "shared.json", expected_room="arkham")
    assert dunwich_path.is_file()


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are platform-specific")
async def test_room_backup_rejects_import_symlinks_but_export_replaces_the_link(tmp_path):
    services = _services(str(tmp_path))
    room = "arkham"
    exported = await export_room(services, Keystore(), room, "original.json")
    room_dir = Path(exported["path"]).parent
    outside = tmp_path / "outside.json"
    outside.write_text("do not overwrite", encoding="utf-8")
    alias = room_dir / "alias.json"
    alias.symlink_to(outside)

    with pytest.raises(ValueError, match="not a room backup file"):
        await import_room(services, Keystore(), alias.name, expected_room=room)

    replaced = await export_room(services, Keystore(), room, alias.name)
    assert Path(replaced["path"]) == alias
    assert not alias.is_symlink()
    assert outside.read_text(encoding="utf-8") == "do not overwrite"


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are platform-specific")
async def test_room_backup_rejects_a_symlinked_room_directory(tmp_path):
    services = _services(str(tmp_path))
    room = "arkham"
    exported = await export_room(services, Keystore(), room, "snapshot.json")
    snapshot = Path(exported["path"])
    room_dir = snapshot.parent
    other_room = Path((await export_room(services, Keystore(), "dunwich", "snapshot.json"))["path"]).parent

    snapshot.unlink()
    room_dir.rmdir()
    room_dir.symlink_to(other_room, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        await import_room(services, Keystore(), "snapshot.json", expected_room=room)
    with pytest.raises(ValueError, match="must not be a symlink"):
        await export_room(services, Keystore(), room, "snapshot.json")


async def test_dotted_child_room_is_structurally_isolated_from_parent_room_ops(tmp_path):
    """Pre-M17, room content lived under prefix-shaped KV keys, so a dotted child room
    ("foo.bar") aliased its parent's key namespace and every room op had to FAIL CLOSED
    on ambiguity. M17 stores content in room-COLUMN-scoped tables, so the ambiguity class
    is gone structurally: parent ops must simply never touch the child's rows."""
    services = _services(str(tmp_path))
    keystore = Keystore()
    keystore.add(room="foo", name="Parent Keeper", role="keeper")
    keystore.add(room="foo.bar", name="Child Keeper", role="keeper")
    parent_key = chat_key_for_room("foo")
    child_key = chat_key_for_room("foo.bar")
    await services.store.state_set(parent_key, "chat_history", "parent")
    await services.store.state_set(child_key, "chat_history", "child")
    await services.documents.put(child_key, "sheet", "Bob", {"name": "Bob", "owner": "p1"})

    exported = await export_room(services, keystore, "foo", "parent.json")
    snapshot = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert all(row["key"] != "chat_history" or row["value"] == "parent" for row in snapshot["room_state"])
    assert snapshot["documents"] == []  # the child's sheet never leaks into the parent snapshot

    await delete_room_data(services, keystore, "foo")
    assert await services.store.state_get(child_key, "chat_history") == "child"
    assert await services.documents.get(child_key, "sheet", "Bob") is not None

    await services.store.state_set(parent_key, "chat_history", "parent-again")
    await reset_room_state(services, parent_key, scope="all", keystore=keystore)
    assert await services.store.state_get(parent_key, "chat_history") is None
    assert await services.store.state_get(child_key, "chat_history") == "child"
    assert await services.documents.get(child_key, "sheet", "Bob") is not None


async def test_reset_without_ambiguous_neighbor_still_wipes_and_a_prefix_named_sibling_survives(tmp_path):
    """The guard fails closed only on a TRUE dotted child; an ordinary sibling room whose id merely
    shares a leading substring (not a dotted prefix) is unaffected, and reset still works normally."""
    services = _services(str(tmp_path))
    keystore = Keystore()
    keystore.add(room="foo", name="Keeper", role="keeper")
    keystore.add(room="foobar", name="Sibling Keeper", role="keeper")  # NOT a dotted child of "foo"
    parent_key = chat_key_for_room("foo")
    sibling_key = chat_key_for_room("foobar")
    await services.store.state_set(parent_key, "chat_history", "parent-story")
    await services.documents.put(sibling_key, "lore", "e1", {"title": "sibling", "content": "x"})

    result = await reset_room_state(services, parent_key, scope="all", keystore=keystore)

    assert int(result.get("store_rows") or 0) >= 1
    assert await services.store.state_get(parent_key, "chat_history") is None  # parent wiped
    assert await services.documents.get(sibling_key, "lore", "e1") is not None  # sibling intact


async def test_vector_conflicting_ownership_fails_export_and_delete_without_erasing_point(tmp_path):
    services = _services(str(tmp_path))
    keystore = Keystore()
    keystore.add(room="arkham", name="Keeper", role="keeper")
    chat_key = chat_key_for_room("arkham")
    foreign_key = chat_key_for_room("dunwich")
    await services.vector_db.vector_store.upsert(
        [
            (
                "conflicted:0",
                [0.1] * 64,
                {
                    "chat_key": chat_key,
                    "namespace": foreign_key,
                    "document_id": "conflicted",
                },
            )
        ]
    )

    with pytest.raises(ValueError, match="conflicting room ownership"):
        await export_room(services, keystore, "arkham", "conflicted.json")
    with pytest.raises(ValueError, match="conflicting room ownership"):
        await delete_room_data(services, keystore, "arkham")

    points = await services.vector_db.vector_store.dump(filter={"chat_key": chat_key})
    assert [point["id"] for point in points] == ["conflicted:0"]


async def test_room_vector_restore_preserves_canonical_ids_and_future_upserts(tmp_path):
    services = _services(str(tmp_path))
    room = "arkham"
    chat_key = chat_key_for_room(room)
    await services.vector_db.store_document(
        "doc-1",
        "original.txt",
        "original clue",
        chat_key,
    )
    exported = await export_room(services, Keystore(), room, "vectors.json")

    # Importing over the live room is an upsert of the same deterministic point,
    # not a second namespaced alias.
    await import_room(
        services,
        Keystore(),
        Path(exported["path"]).name,
        expected_room=room,
    )
    points = await room_vector_points(services, chat_key)
    assert [point["id"] for point in points] == ["doc-1:0"]

    # Snapshots produced after the old buggy importer may themselves contain its
    # `:backup:` alias. Restore normalizes those from payload identity as well.
    source = Path(exported["path"])
    snapshot = json.loads(source.read_text(encoding="utf-8"))
    snapshot["vector_points"][0]["id"] = f"{chat_key}:backup:legacy"
    source.write_text(json.dumps(snapshot), encoding="utf-8")
    await delete_room_data(services, Keystore(), room)
    await import_room(services, Keystore(), source.name, expected_room=room)
    await services.vector_db.store_document(
        "doc-1",
        "updated.txt",
        "updated clue",
        chat_key,
    )

    points = await room_vector_points(services, chat_key)
    assert [point["id"] for point in points] == ["doc-1:0"]
    assert points[0]["payload"]["filename"] == "updated.txt"
    assert points[0]["payload"]["text"] == "updated clue"


def _quota_services(tmp_path, quota: int):
    """A room whose IMAGE quota is small enough for two files to cross it."""
    settings = Settings(
        _env_file=None,
        locale="en",
        data_dir=str(tmp_path),
        llm=LLMSettings(provider="openai", chat_model="gpt-4o"),
        imagegen=ImageGenSettings(),
        tui=TuiSettings(media_max_file_bytes=quota, media_room_quota_bytes=quota),
    )
    return build_services(settings, llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


def _quota_media_store(services, quota: int) -> MediaStore:
    return MediaStore(
        services.store,
        services.settings.data_dir,
        max_file_bytes=quota,
        room_quota_bytes=quota,
        allowed_mimes=ALLOWED_MEDIA_MIMES,
    )


async def test_loading_an_old_save_drops_the_extras_from_the_index_before_writing_its_own_media(tmp_path):
    """A load must not be refused over media it is on its way to delete.

    Staging only moved the extras' BLOBS aside — their `media_index` rows stayed, and the
    snapshot's media were written after. So a room that cleared its media and refilled the
    quota with new files could no longer load its own older save: the snapshot's hashes were
    no longer in the index to short-circuit the quota check, and the extras it was about to
    drop counted against them. (Save→upload→load without the clear passes only because the
    snapshot's hashes ARE still indexed.)
    """
    quota = 1024
    services = _quota_services(tmp_path, quota)
    keystore = Keystore()
    room = "arkham"
    chat_key = chat_key_for_room(room)
    media_store = _quota_media_store(services, quota)

    saved = await media_store.register_blob(
        room=chat_key, data=b"S" * 600, mime="image/png", name="handout.png", uploader="keeper"
    )
    await services.store.state_set(chat_key, "chat_history", "arkham-v1")
    exported = await export_room(services, keystore, room, "old.json")
    assert exported["media_files"] == 1

    # The room moves on: the saved handout is cleared and a new file refills the quota.
    assert await media_store.delete_room(chat_key) == 1
    replacement = await media_store.register_blob(
        room=chat_key, data=b"N" * 600, mime="image/png", name="new.png", uploader="keeper"
    )

    await import_room(services, keystore, Path(exported["path"]).name, expected_room=room)

    hashes = {record.hash for record in await media_store.list_room_records(chat_key)}
    assert hashes == {saved.hash}, "the load must end on exactly the snapshot's media"
    assert replacement.hash not in hashes
    _, restored = await media_store.read_bytes(chat_key, saved.hash)
    assert restored == b"S" * 600
    assert not (Path(services.settings.data_dir) / "media" / chat_key / replacement.hash).exists()


async def test_a_failed_leg_after_the_extras_left_the_index_puts_their_rows_and_files_back(tmp_path):
    """Dropping the extras earlier moves them under the SAME compensation, not out of it.

    The extras' index rows now go before the snapshot's media are written, so a later leg's
    failure has to restore both halves — rows and bytes — not just the staged files.
    """
    quota = 1024
    services = _quota_services(tmp_path, quota)
    keystore = Keystore()
    room = "arkham"
    chat_key = chat_key_for_room(room)
    media_store = _quota_media_store(services, quota)
    keystore.add(room=room, name="Keeper", role="keeper")

    saved = await media_store.register_blob(
        room=chat_key, data=b"S" * 600, mime="image/png", name="handout.png", uploader="keeper"
    )
    await services.store.state_set(chat_key, "chat_history", "arkham-v1")
    exported = await export_room(services, keystore, room, "rollback.json")
    assert await media_store.delete_room(chat_key) == 1
    replacement = await media_store.register_blob(
        room=chat_key, data=b"N" * 600, mime="image/png", name="new.png", uploader="keeper"
    )

    # The keys leg runs last, after every media mutation — a clean place to break.
    with patch.object(keystore_module.Keystore, "restore", side_effect=RuntimeError("keystore is down")):
        with pytest.raises(Exception):
            await import_room(services, keystore, Path(exported["path"]).name, expected_room=room)

    records = {record.hash: record for record in await media_store.list_room_records(chat_key)}
    assert replacement.hash in records, "the extra's index row must come back with its bytes"
    assert records[replacement.hash].name == "new.png"
    _, rolled_back = await media_store.read_bytes(chat_key, replacement.hash)
    assert rolled_back == b"N" * 600
    assert saved.hash not in records, "the snapshot's media must not survive a failed load"


async def test_room_import_rejects_a_vector_id_owned_by_another_room(tmp_path):
    services = _services(str(tmp_path))
    room = "arkham"
    chat_key = chat_key_for_room(room)
    foreign_key = chat_key_for_room("dunwich")
    await services.vector_db.store_document(
        "doc-1",
        "arkham.txt",
        "arkham clue",
        chat_key,
    )
    exported = await export_room(services, Keystore(), room, "collision.json")
    await services.vector_db.vector_store.delete(["doc-1:0"])
    await services.vector_db.vector_store.upsert(
        [
            (
                "doc-1:0",
                [0.9] * 64,
                {
                    "chat_key": foreign_key,
                    "document_id": "doc-1",
                    "chunk_index": 0,
                    "text": "foreign clue",
                },
            )
        ]
    )

    with pytest.raises(ValueError, match="vector id belongs to another room"):
        await import_room(
            services,
            Keystore(),
            Path(exported["path"]).name,
            expected_room=room,
        )

    [foreign] = await services.vector_db.vector_store.scroll()
    assert foreign.id == "doc-1:0"
    assert foreign.payload["chat_key"] == foreign_key
    assert await services.vector_db.vector_store.count(filter={"chat_key": chat_key}) == 0


@pytest.mark.parametrize(
    "case",
    [
        "document_foreign_namespace",
        "document_missing_chat_key",
        "worldbook_foreign_chat_key",
        "worldbook_missing_namespace",
        "nested_foreign_owner",
    ],
)
async def test_room_import_requires_every_vector_owner_field_to_match_target(tmp_path, case):
    services = _services(str(tmp_path))
    keystore = Keystore()
    keystore.add(room="arkham", name="Keeper", role="keeper")
    chat_key = chat_key_for_room("arkham")
    foreign_key = chat_key_for_room("dunwich")
    exported = await export_room(services, keystore, "arkham", f"vector-{case}.json")
    source = Path(exported["path"])
    snapshot = json.loads(source.read_text(encoding="utf-8"))
    payloads = {
        "document_foreign_namespace": {
            "chat_key": chat_key,
            "namespace": foreign_key,
            "document_id": "doc",
        },
        "document_missing_chat_key": {"namespace": chat_key, "document_id": "doc"},
        "worldbook_foreign_chat_key": {
            "collection": "worldbook",
            "namespace": chat_key,
            "chat_key": foreign_key,
            "entry_id": "entry",
        },
        "worldbook_missing_namespace": {
            "collection": "worldbook",
            "chat_key": chat_key,
            "entry_id": "entry",
        },
        "nested_foreign_owner": {
            "chat_key": chat_key,
            "document_id": "doc",
            "metadata": {"namespace": foreign_key},
        },
    }
    snapshot["vector_points"] = [{"id": "forged:0", "vector": [0.2] * 64, "payload": payloads[case]}]
    source.write_text(json.dumps(snapshot), encoding="utf-8")

    with pytest.raises(ValueError, match="vector owned by another room"):
        await import_room(services, keystore, source.name, expected_room="arkham")
    assert await services.vector_db.vector_store.count() == 0


async def test_import_rejects_foreign_bound_room_inside_the_same_store_transaction(tmp_path):
    services = _services(str(tmp_path))
    keystore = Keystore()
    keystore.add(room="arkham", name="Keeper", role="keeper")
    chat_key = chat_key_for_room("arkham")
    foreign_key = chat_key_for_room("dunwich")
    history_key = f"chat_history.{chat_key}"
    binding_key = "bound_room.discord:group:table"
    await services.store.set(store_key=history_key, value="snapshot")
    await services.store.set(store_key=binding_key, value=chat_key)
    exported = await export_room(services, keystore, "arkham", "binding.json")

    await services.store.set(store_key=history_key, value="live")
    await services.store.set(store_key=binding_key, value=foreign_key)
    with pytest.raises(ValueError, match="bound room already belongs"):
        await import_room(
            services,
            keystore,
            Path(exported["path"]).name,
            expected_room="arkham",
        )

    # The conflict check precedes every upsert in the BEGIN IMMEDIATE transaction.
    assert await services.store.get(store_key=history_key) == "live"
    assert await services.store.get(store_key=binding_key) == foreign_key


async def test_import_rollback_preserves_a_concurrent_foreign_room_binding(tmp_path):
    services = _services(str(tmp_path))
    keystore = Keystore()
    keystore.add(room="arkham", name="Keeper", role="keeper")
    chat_key = chat_key_for_room("arkham")
    foreign_key = chat_key_for_room("dunwich")
    history_key = f"chat_history.{chat_key}"
    binding_key = "bound_room.discord:group:table"
    await services.store.set(store_key=history_key, value="snapshot")
    await services.store.set(store_key=binding_key, value=chat_key)
    media_store = MediaStore(
        services.store,
        services.settings.data_dir,
        allowed_mimes=ALLOWED_MEDIA_MIMES,
    )
    await media_store.register_blob(
        room=chat_key,
        data=b"rollback trigger",
        mime="image/png",
        name="trigger.png",
        uploader="keeper",
    )
    exported = await export_room(services, keystore, "arkham", "binding-rollback.json")
    await services.store.set(store_key=history_key, value="live")

    async def _rebind_then_fail(_instance, **_kwargs):
        await services.store.set(store_key=binding_key, value=foreign_key)
        raise OSError("injected import failure after concurrent rebind")

    with patch.object(MediaStore, "validate_offer", new=_rebind_then_fail):
        with pytest.raises(OSError, match="concurrent rebind"):
            await import_room(
                services,
                keystore,
                Path(exported["path"]).name,
                expected_room="arkham",
            )

    assert await services.store.get(store_key=history_key) == "live"
    assert await services.store.get(store_key=binding_key) == foreign_key


async def test_room_export_refreshes_external_key_moves_before_snapshot(tmp_path):
    services = _services(str(tmp_path))
    key_path = tmp_path / "keys.toml"
    keystore = Keystore.load(key_path)
    with keystore.persisted_mutation():
        key = keystore.add(room="arkham", name="Keeper", role="keeper")

    external = Keystore.load(key_path)
    with external.persisted_mutation():
        assert external.update(key, room="dunwich")

    exported = await export_room(services, keystore, "arkham", "after-move.json")
    snapshot = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert snapshot["keys"] == []


async def test_room_export_rejects_media_over_the_backup_budget_before_writing(tmp_path, monkeypatch):
    services = _services(str(tmp_path))
    room = "arkham"
    chat_key = chat_key_for_room(room)
    media_store = MediaStore(
        services.store,
        services.settings.data_dir,
        allowed_mimes=ALLOWED_MEDIA_MIMES,
    )
    await media_store.register_blob(
        room=chat_key,
        data=b"large-for-this-test",
        mime="image/png",
        name="clue.png",
        uploader="keeper",
    )
    monkeypatch.setattr(room_backup_module, "MAX_BACKUP_MEDIA_BYTES", 4)

    with pytest.raises(ValueError, match="media backup byte limit exceeded"):
        await export_room(services, Keystore(), room, "oversized.json")

    backup_root = Path(services.settings.data_dir) / "room_backups"
    assert not list(backup_root.rglob("oversized.json"))


async def test_room_import_enforces_the_image_policy_not_the_aggregate_audio_limit(tmp_path):
    services = _services(str(tmp_path))
    room = "arkham"
    chat_key = chat_key_for_room(room)
    media_store = MediaStore(
        services.store,
        services.settings.data_dir,
        allowed_mimes=ALLOWED_MEDIA_MIMES,
    )
    record = await media_store.register_blob(
        room=chat_key,
        data=b"image",
        mime="image/png",
        name="clue.png",
        uploader="keeper",
    )
    exported = await export_room(services, Keystore(), room, "image-policy.json")
    await delete_room_data(services, Keystore(), room)

    # Audio's much larger configured limit must not make this image acceptable.
    services.settings.tui.media_max_file_bytes = 4
    assert services.settings.tui.audio_max_file_bytes > 4
    with pytest.raises(ValueError, match="invalid media entry"):
        await import_room(services, Keystore(), Path(exported["path"]).name, expected_room=room)
    with pytest.raises(MediaError, match="media_not_found"):
        await media_store.read_bytes(chat_key, record.hash)


async def test_delete_room_rolls_back_every_component_after_late_media_failure(tmp_path):
    services = _services(str(tmp_path))
    key_path = tmp_path / "keys.toml"
    keystore = Keystore.load(key_path)
    with keystore.persisted_mutation():
        keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    chat_key = chat_key_for_room("arkham")
    await services.store.state_set(chat_key, "chat_history", "original")
    await services.vector_db.vector_store.upsert(
        [("original:0", [0.25] * 64, {"chat_key": chat_key, "document_id": "original"})]
    )
    media_store = MediaStore(
        services.store,
        services.settings.data_dir,
        allowed_mimes=ALLOWED_MEDIA_MIMES,
    )
    media_record = await media_store.register_blob(
        room=chat_key,
        data=b"original handout",
        mime="image/png",
        name="original.png",
        uploader="keeper",
    )
    original_delete = MediaStore.delete_room
    recovery_key = ""

    async def _delete_then_fail(instance, target_room):
        nonlocal recovery_key
        await original_delete(instance, target_room)
        # Simulate a separate operations process minting a recovery credential after
        # the delete's keystore leg. The later rollback must merge, not erase it.
        external = Keystore.load(key_path)
        with external.persisted_mutation():
            recovery_key = external.add(room="arkham", name="Recovery", role="keeper")
        raise OSError("injected post-delete failure")

    with patch.object(MediaStore, "delete_room", new=_delete_then_fail):
        failed = await AdminService(services, keystore).dispatch(
            "keeper",
            "arkham",
            {"type": "admin_delete_room_data", "room": "arkham", "backup": False},
            get_i18n("en"),
        )
    assert failed["type"] == "admin_error"
    assert failed["code"] == "op_failed"

    assert await services.store.state_get(chat_key, "chat_history") == "original"
    restored_vectors = await room_vector_points(services, chat_key)
    assert [point["id"] for point in restored_vectors] == ["original:0"]
    persisted_keys = Keystore.load(key_path)
    assert persisted_keys.get(keeper_key) is not None
    assert persisted_keys.get(recovery_key) is not None
    restored_record, restored_data = await media_store.read_bytes(chat_key, media_record.hash)
    assert restored_record.name == "original.png"
    assert restored_data == b"original handout"


async def test_import_room_rolls_back_every_component_when_key_persistence_fails(tmp_path):
    services = _services(str(tmp_path))
    key_path = tmp_path / "keys.toml"
    keystore = Keystore.load(key_path)
    room = "arkham"
    chat_key = chat_key_for_room(room)
    with keystore.persisted_mutation():
        backup_key = keystore.add(room=room, name="Old Keeper", role="keeper")
    await services.store.state_set(chat_key, "chat_history", "backup")
    await services.vector_db.vector_store.upsert(
        [("backup:0", [0.1] * 64, {"chat_key": chat_key, "document_id": "backup"})]
    )
    media_store = MediaStore(
        services.store,
        services.settings.data_dir,
        allowed_mimes=ALLOWED_MEDIA_MIMES,
    )
    backup_media = await media_store.register_blob(
        room=chat_key,
        data=b"backup handout",
        mime="image/png",
        name="backup.png",
        uploader="keeper",
    )
    exported = await export_room(services, keystore, room, "rollback.json")
    await delete_room_data(services, keystore, room)

    with keystore.persisted_mutation():
        recovery_key = keystore.add(room=room, name="Recovery", role="keeper")
    await services.store.state_set(chat_key, "chat_history", "live")
    await services.vector_db.vector_store.upsert(
        [("live:0", [0.9] * 64, {"chat_key": chat_key, "document_id": "live"})]
    )
    live_media = await media_store.register_blob(
        room=chat_key,
        data=b"live handout",
        mime="image/png",
        name="live.png",
        uploader="recovery",
    )
    live_rows = await room_rows(services, chat_key)
    live_vectors = await room_vector_points(services, chat_key)

    original_writer = keystore_module.atomic_write_private
    failed = False

    def _fail_key_write_once(path, data, *, encoding="utf-8"):
        nonlocal failed
        if Path(path).resolve() == key_path.resolve() and not failed:
            failed = True
            raise OSError("injected keystore write failure")
        return original_writer(path, data, encoding=encoding)

    with patch.object(keystore_module, "atomic_write_private", new=_fail_key_write_once):
        failed_import = await AdminService(services, keystore).dispatch(
            "keeper",
            room,
            {"type": "admin_import_room", "path": Path(exported["path"]).name},
            get_i18n("en"),
        )
    assert failed_import["type"] == "admin_error"
    assert failed_import["code"] == "op_failed"

    def _normalized_rows(rows):
        return sorted((row["user_key"], row["store_key"], row["value"]) for row in rows)

    assert _normalized_rows(await room_rows(services, chat_key)) == _normalized_rows(live_rows)
    assert await room_vector_points(services, chat_key) == live_vectors
    assert keystore.get(recovery_key) is not None
    assert keystore.get(backup_key) is None
    records = await media_store.list_room_records(chat_key)
    assert [record.hash for record in records] == [live_media.hash]
    _, live_data = await media_store.read_bytes(chat_key, live_media.hash)
    assert live_data == b"live handout"
    with pytest.raises(MediaError, match="media_not_found"):
        await media_store.read_bytes(chat_key, backup_media.hash)


async def test_import_room_replaces_live_extras_the_snapshot_does_not_name(tmp_path):
    """A load is a checkpoint, not a merge: extras added after export must vanish.

    Campaign content, that is. Room wiring — `bound_room.*` bindings, like bearer keys —
    is restore-only, so a binding made after the save survives the load.
    """
    services = _services(str(tmp_path))
    keystore = Keystore()
    keystore.add(room="arkham", name="Keeper", role="keeper")
    chat_key = chat_key_for_room("arkham")
    await services.documents.put(chat_key, "lore", "kept", {"title": "Kingsport"})
    await services.store.state_set(chat_key, "game_clock", "snapshot-clock")
    await services.store.set(user_key="", store_key="bound_room.discord:group:table", value=chat_key)
    await services.vector_db.vector_store.upsert(
        [("kept:0", [0.1] * 64, {"chat_key": chat_key, "document_id": "kept", "chunk_index": 0})]
    )
    media_store = MediaStore(
        services.store,
        services.settings.data_dir,
        allowed_mimes=ALLOWED_MEDIA_MIMES,
    )
    kept_media = await media_store.register_blob(
        room=chat_key,
        data=b"kept handout",
        mime="image/png",
        name="kept.png",
        uploader="keeper",
    )
    exported = await export_room(services, keystore, "arkham", "checkpoint.json")

    await services.documents.put(chat_key, "lore", "extra", {"title": "after export"})
    await services.store.state_set(chat_key, "kp_notes", "after-export")
    await services.store.set(user_key="", store_key="bound_room.discord:group:extra", value=chat_key)
    await services.vector_db.vector_store.upsert(
        [("extra:0", [0.9] * 64, {"chat_key": chat_key, "document_id": "extra", "chunk_index": 0})]
    )
    extra_media = await media_store.register_blob(
        room=chat_key,
        data=b"extra handout",
        mime="image/png",
        name="extra.png",
        uploader="keeper",
    )

    await import_room(
        services,
        keystore,
        Path(exported["path"]).name,
        expected_room="arkham",
    )

    docs = await services.store.doc_list(chat_key)
    assert {(row["type"], row["id"]) for row in docs} == {("lore", "kept")}
    assert await services.store.state_get(chat_key, "game_clock") == "snapshot-clock"
    assert await services.store.state_get(chat_key, "kp_notes") is None
    assert await services.store.get(store_key="bound_room.discord:group:table") == chat_key
    # ... except the bindings, which are RESTORE-ONLY for the same reason bearer keys are:
    # `bound_room.*` says which platform conversation reaches this room — wiring, not
    # campaign content. A table connected after the save was taken keeps its way in; the
    # load replaces the story it walks into, not the door it walks through.
    assert await services.store.get(store_key="bound_room.discord:group:extra") == chat_key
    assert [point["id"] for point in await room_vector_points(services, chat_key)] == ["kept:0"]
    assert [record.hash for record in await media_store.list_room_records(chat_key)] == [kept_media.hash]
    with pytest.raises(MediaError, match="media_not_found"):
        await media_store.read_bytes(chat_key, extra_media.hash)


async def test_import_rollback_restores_history_after_a_later_leg_fails(tmp_path):
    """History is replaced before vectors/media/keys; a later failure must put it back."""
    services = _services(str(tmp_path))
    keystore = Keystore()
    keystore.add(room="arkham", name="Keeper", role="keeper")
    chat_key = chat_key_for_room("arkham")
    await services.store.history_append(
        chat_key,
        "chat_history",
        [{"id": "snap-1", "parent_id": None, "turn": 1, "role": "user", "content": "snapshot line"}],
    )
    media_store = MediaStore(
        services.store,
        services.settings.data_dir,
        allowed_mimes=ALLOWED_MEDIA_MIMES,
    )
    await media_store.register_blob(
        room=chat_key,
        data=b"snapshot handout",
        mime="image/png",
        name="snap.png",
        uploader="keeper",
    )
    exported = await export_room(services, keystore, "arkham", "history-rollback.json")

    await services.store.history_delete_room(chat_key)
    await services.store.history_append(
        chat_key,
        "chat_history",
        [{"id": "live-1", "parent_id": None, "turn": 2, "role": "user", "content": "live line"}],
    )
    original_history = await services.store.history_rows(chat_key)
    assert [row["id"] for row in original_history] == ["live-1"]

    async def _fail_after_history(_instance, **_kwargs):
        replaced = await services.store.history_rows(chat_key)
        assert [row["id"] for row in replaced] == ["snap-1"]
        raise OSError("injected import failure after history replace")

    with patch.object(MediaStore, "validate_offer", new=_fail_after_history):
        with pytest.raises(OSError, match="after history replace"):
            await import_room(
                services,
                keystore,
                Path(exported["path"]).name,
                expected_room="arkham",
            )

    assert await services.store.history_rows(chat_key) == original_history


async def test_import_completes_when_one_extra_media_blob_is_unreadable(tmp_path):
    """One bad EXTRA blob cannot block the repair a `.save load` IS.

    The load was going to drop that blob anyway, so a corrupt one is nothing the load
    can lose — it is logged and skipped, and everything else still restores.
    `delete_room_data` keeps the hard failure: there the blob is one the operation
    promises to be able to hand back, so refusing to start is the only safe answer.
    """
    services = _services(str(tmp_path))
    keystore = Keystore()
    keystore.add(room="arkham", name="Keeper", role="keeper")
    chat_key = chat_key_for_room("arkham")
    await services.documents.put(chat_key, "lore", "kept", {"title": "Kingsport"})
    await services.store.state_set(chat_key, "game_clock", "snapshot-clock")
    media_store = MediaStore(
        services.store,
        services.settings.data_dir,
        allowed_mimes=ALLOWED_MEDIA_MIMES,
    )
    kept_media = await media_store.register_blob(
        room=chat_key,
        data=b"kept handout",
        mime="image/png",
        name="kept.png",
        uploader="keeper",
    )
    exported = await export_room(services, keystore, "arkham", "corrupt-extra.json")

    await services.documents.put(chat_key, "lore", "extra", {"title": "after export"})
    await services.store.state_set(chat_key, "kp_notes", "after-export")
    extra_media = await media_store.register_blob(
        room=chat_key,
        data=b"extra handout",
        mime="image/png",
        name="extra.png",
        uploader="keeper",
    )
    # The index row still promises the blob; the bytes on disk no longer match it.
    media_store._path(chat_key, extra_media.hash).write_bytes(b"tampered bytes")

    await import_room(services, keystore, Path(exported["path"]).name, expected_room="arkham")

    docs = await services.store.doc_list(chat_key)
    assert {(row["type"], row["id"]) for row in docs} == {("lore", "kept")}
    assert await services.store.state_get(chat_key, "game_clock") == "snapshot-clock"
    assert await services.store.state_get(chat_key, "kp_notes") is None
    assert (await media_store.read_bytes(chat_key, kept_media.hash))[1] == b"kept handout"
    assert [record.hash for record in await media_store.list_room_records(chat_key)] == [kept_media.hash]


async def test_admin_room_ops_are_scoped_to_the_callers_room():
    """Security: a keeper key bound to room A cannot mutate/export/wipe/import room B — only its
    own room, including listing/minting/mutating access keys."""
    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")  # caller is bound to arkham
    victim_key = keystore.add(room="dunwich", name="Other Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        listed = await _send(ws, {"type": "admin_list_keys"})
        assert {entry["room"] for entry in listed["keys"]} == {"arkham"}
        victim_id = hashlib.sha256(victim_key.encode("utf-8")).hexdigest()[:16]
        for request in (
            {"type": "admin_mint_key", "room": "dunwich", "role": "keeper"},
            {"type": "admin_update_key", "id": victim_id, "role": "player"},
            {"type": "admin_delete_key", "id": victim_id},
            {"type": "admin_delete_room", "room": "dunwich"},
            {"type": "admin_export_room", "room": "dunwich"},
            {"type": "admin_delete_room_data", "room": "dunwich"},
            {"type": "admin_import_room", "path": "x.json", "room": "dunwich"},
        ):
            reply = await _send(ws, request)
            assert reply["type"] == "admin_error", request
            assert reply["code"] == "forbidden", request
        # The other room's key was never touched.
        assert keystore.get(victim_key) is not None
        assert keystore.get(victim_key).role == "keeper"
        assert keystore.get(victim_key).room == "dunwich"

        await ws.close()
    finally:
        await server.close()


async def test_admin_export_confines_the_path_to_the_backups_directory(tmp_path):
    """Security: a client-supplied export `path` cannot escape data_dir/room_backups — an
    absolute/traversal path is reduced to a bare filename inside the backups directory."""
    services = _services(str(tmp_path))
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    await services.store.state_set(chat_key_for_room("arkham"), "chat_history", "[]")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        exported = await _send(ws, {"type": "admin_export_room", "room": "arkham", "path": "/etc/loreweaver-evil.json"})
        assert exported["type"] == "admin_room_op"
        base = str((Path(services.settings.data_dir) / "room_backups").resolve())
        assert exported["path"].startswith(base)  # confined under the backups dir
        assert exported["path"].endswith("loreweaver-evil.json")  # only the filename survived
        assert not Path("/etc/loreweaver-evil.json").exists()  # nothing written outside

        await ws.close()
    finally:
        await server.close()


async def test_set_model_remembers_each_providers_key_and_reuses_it_on_switch_back():
    """The credential book: setting a key for a provider persists + remembers it; switching to
    another provider (with its own key) and then BACK reuses the first provider's saved key
    without re-supplying it — the multi-provider combo the model screen relies on."""
    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")

        # deepseek + its key: applied, persisted, remembered, and surfaced in saved_providers.
        first = await _send(
            ws,
            {"type": "admin_set_model", "provider": "deepseek", "chat_model": "deepseek-chat", "api_key": "sk-deep"},
        )
        assert first["type"] == "admin_config"
        assert first["provider"] == "deepseek"
        assert "deepseek" in first["saved_providers"]
        assert (await services.runtime_config.get())["api_key"] == "sk-deep"
        assert (await services.llm_profiles.get("deepseek"))["api_key"] == "sk-deep"

        # a different provider with its own key.
        await _send(ws, {"type": "admin_set_model", "provider": "openai", "api_key": "sk-open"})
        assert (await services.runtime_config.get())["api_key"] == "sk-open"
        assert (await services.llm_profiles.get("openai"))["api_key"] == "sk-open"

        # switch BACK to deepseek WITHOUT a key → the saved deepseek key is reused.
        back = await _send(ws, {"type": "admin_set_model", "provider": "deepseek"})
        assert back["provider"] == "deepseek"
        assert (await services.runtime_config.get())["api_key"] == "sk-deep"
        assert set(back["saved_providers"]) >= {"deepseek", "openai"}

        await ws.close()
    finally:
        await server.close()


async def test_set_model_preserves_the_legacy_profile_model_name():
    """Selecting a legacy credential profile as the default must not clear its model name."""
    services = _services()
    await services.llm_profiles.replace_static(
        "deepseek",
        api_key="sk-deep",
        chat_model="deepseek-chat",
    )

    reply = await AdminService(services, Keystore()).dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_set_model",
            "provider": "deepseek",
            "chat_model": "deepseek-chat",
        },
        get_i18n("en"),
    )

    assert reply["type"] == "admin_config"
    profile = next(profile for profile in reply["llms"] if profile["provider"] == "deepseek")
    assert profile["chat_model"] == "deepseek-chat"
    assert (await services.llm_profiles.get("deepseek"))["chat_model"] == "deepseek-chat"


async def test_preset_llm_endpoint_roundtrip_keeps_the_existing_key():
    """The config frame exposes a preset's effective URL even when the stored URL
    is empty. Sending that unchanged value back must not look like a trust-boundary
    move and silently clear the key."""
    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    captured: list[LLMSettings] = []

    async def _capture(settings: LLMSettings):
        captured.append(settings)
        return []

    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        first = await _send(
            ws,
            {
                "type": "admin_set_model",
                "provider": "deepseek",
                "chat_model": "deepseek-chat",
                "api_key": "sk-preset",
            },
        )
        effective_url = PRESETS["deepseek"]
        assert first["base_url"] == effective_url
        assert services.settings.llm.base_url == ""

        with patch("net.admin.list_models", new=_capture):
            await _send(
                ws,
                {
                    "type": "admin_list_models",
                    "provider": "deepseek",
                    "base_url": effective_url,
                },
            )
        assert captured[-1].api_key == "sk-preset"

        saved = await _send(
            ws,
            {
                "type": "admin_set_model",
                "provider": "deepseek",
                "base_url": effective_url,
            },
        )
        assert saved["type"] == "admin_config"
        assert services.settings.llm.api_key == "sk-preset"
        assert await services.llm_profiles.get("deepseek") == {
            "api_key": "sk-preset",
            "base_url": effective_url,
            "chat_model": "deepseek-chat",
        }

        await ws.close()
    finally:
        await server.close()


async def test_new_model_endpoint_never_receives_or_remembers_the_old_key():
    """A caller-selected URL is a new trust boundary: preview/save must not attach
    the key associated with the previous URL, including on a later keyless save."""
    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    captured: list[LLMSettings] = []

    async def _capture(settings: LLMSettings):
        captured.append(settings)
        return []

    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        await _send(
            ws,
            {
                "type": "admin_set_model",
                "provider": "openai",
                "base_url": "https://old.example/v1",
                "api_key": "sk-old-endpoint",
            },
        )

        with patch("net.admin.list_models", new=_capture):
            await _send(
                ws,
                {
                    "type": "admin_list_models",
                    "provider": "openai",
                    "base_url": "https://new.example/v1",
                },
            )
        assert captured[-1].base_url == "https://new.example/v1"
        assert captured[-1].api_key == ""

        changed = await _send(
            ws,
            {
                "type": "admin_set_model",
                "provider": "openai",
                "base_url": "https://new.example/v1",
            },
        )
        assert changed["type"] == "admin_config"
        assert services.settings.llm.base_url == "https://new.example/v1"
        assert services.settings.llm.api_key == ""
        assert await services.llm_profiles.get("openai") == {"base_url": "https://new.example/v1"}

        # A later save with omitted fields must not resurrect the old saved key.
        await _send(ws, {"type": "admin_set_model", "provider": "openai"})
        assert services.settings.llm.api_key == ""

        # Supplying the replacement key on the same request is accepted and paired
        # only with that new endpoint.
        await _send(
            ws,
            {
                "type": "admin_set_model",
                "provider": "openai",
                "base_url": "https://third.example/v1",
                "api_key": "sk-third-endpoint",
            },
        )
        assert await services.llm_profiles.get("openai") == {
            "api_key": "sk-third-endpoint",
            "base_url": "https://third.example/v1",
        }

        # Presence matters: an explicitly empty key clears it while retaining the
        # unchanged endpoint (the TUI omits blank fields when it means "reuse").
        await _send(
            ws,
            {"type": "admin_set_model", "provider": "openai", "api_key": ""},
        )
        assert await services.llm_profiles.get("openai") == {"base_url": "https://third.example/v1"}

        await ws.close()
    finally:
        await server.close()


async def test_model_runtime_persistence_failure_reports_error_without_compensation():
    services = _services()
    original_inner = services.llm.inner

    with patch.object(
        services.runtime_config,
        "replace",
        new=AsyncMock(side_effect=OSError("database is read-only")),
    ):
        result = await AdminService(services, Keystore()).dispatch(
            "keeper",
            "arkham",
            {
                "type": "admin_set_model",
                "provider": "deepseek",
                "chat_model": "deepseek-chat",
                "api_key": "sk-new",
            },
            get_i18n("en"),
        )

    assert result["type"] == "admin_error"
    assert result["code"] == "set_failed"
    assert services.settings.llm.provider == "deepseek"
    assert services.settings.llm.chat_model == "deepseek-chat"
    assert services.llm.inner is not original_inner
    assert await services.runtime_config.get() == {}


async def test_model_credential_persistence_failure_keeps_applied_runtime():
    services = _services()
    with patch.object(
        services.llm_profiles,
        "replace_static",
        new=AsyncMock(side_effect=OSError("database is read-only")),
    ):
        result = await AdminService(services, Keystore()).dispatch(
            "keeper",
            "arkham",
            {
                "type": "admin_set_model",
                "provider": "deepseek",
                "chat_model": "deepseek-chat",
                "api_key": "sk-new",
            },
            get_i18n("en"),
        )

    assert result["type"] == "admin_error"
    assert result["code"] == "set_failed"
    assert services.settings.llm.provider == "deepseek"
    assert await services.runtime_config.get() == {
        "provider": "deepseek",
        "chat_model": "deepseek-chat",
        "api_key": "sk-new",
        "base_url": "",
    }
    assert await services.llm_profiles.get("deepseek") == {}


async def test_admin_set_llm_lane_configures_and_clears_web_override():
    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        initial = await _send(ws, {"type": "admin_get_config"})
        assert initial["scribe"]["provider"] == ""
        assert initial["scribe"]["override_active"] is False

        updated = await _send(
            ws,
            {
                "type": "admin_set_llm_lane",
                "lane": "scribe",
                "provider": "deepseek",
                "chat_model": "deepseek-chat",
                "api_key": "sk-scribe-secret",
                "enabled": True,
            },
        )
        assert updated["scribe"]["provider"] == "deepseek"
        assert updated["scribe"]["chat_model"] == "deepseek-chat"
        assert updated["scribe"]["api_key_masked"].endswith("cret")
        assert updated["scribe"]["override_active"] is True
        assert "sk-scribe-secret" not in json.dumps(updated)
        assert services.settings.scribe.provider == "deepseek"
        assert (await services.scribe_runtime_config.get())["api_key"] == "sk-scribe-secret"

        cleared = await _send(ws, {"type": "admin_set_llm_lane", "lane": "scribe", "clear": True})
        assert cleared["scribe"]["override_active"] is False
        assert cleared["scribe"]["provider"] == ""
        assert services.settings.scribe.provider == ""
        await ws.close()
    finally:
        await server.close()


async def test_admin_set_imagegen_configures_runtime_and_masks_key():
    import time

    from infra.oauth_flows import SubscriptionToken

    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")

        config = await _send(ws, {"type": "admin_get_config"})
        assert config["imagegen"]["configured"] is False
        assert config["imagegen"]["api_key_masked"] == ""

        updated = await _send(
            ws,
            {
                "type": "admin_set_imagegen",
                "provider": "openai",
                "base_url": "https://images.example/v1",
                "model": "image-model",
                "api_key": "sk-image-secret",
                "size": "512x512",
            },
        )

        assert updated["type"] == "admin_config"
        assert updated["imagegen"]["provider"] == "openai"
        assert updated["imagegen"]["model"] == "image-model"
        assert updated["imagegen"]["size"] == "512x512"
        assert updated["imagegen"]["configured"] is True
        assert updated["imagegen"]["api_key_masked"].endswith("cret")
        assert "sk-image-secret" not in json.dumps(updated)
        assert services.imagegen is not None
        assert (await services.imagegen_runtime_config.get())["api_key"] == "sk-image-secret"
        assert (await services.imagegen_credentials.get("openai"))["api_key"] == "sk-image-secret"

        with patch("net.admin.list_models", return_value=[]):
            listed = await _send(ws, {"type": "admin_list_models", "provider": "openai"})
        assert listed["type"] == "admin_models"
        assert listed["imagegen"]["configured"] is True

        # Switching to the OAuth-backed image provider replaces the whole
        # provider-scoped snapshot. Neither the previous OpenAI key/base_url nor
        # malicious values supplied on this frame may reach SuperGrok.
        await services.llm_profiles.save_subscription(
            "supergrok",
            SubscriptionToken("access-secret", "refresh-secret", time.time() + 3600),
        )
        supergrok = await _send(
            ws,
            {
                "type": "admin_set_imagegen",
                "provider": "supergrok",
                "model": "grok-imagine-image",
                "api_key": "sk-must-be-ignored",
                "base_url": "https://must-not-receive-token.example/v1",
            },
        )
        assert supergrok["type"] == "admin_config"
        assert supergrok["imagegen"]["provider"] == "supergrok"
        assert services.settings.imagegen.api_key == ""
        assert services.settings.imagegen.base_url == ""
        assert await services.imagegen_runtime_config.get() == {
            "provider": "supergrok",
            "model": "grok-imagine-image",
            "size": "512x512",
            "api_key": "",
            "base_url": "",
        }

        await ws.close()
    finally:
        await server.close()


async def test_new_image_endpoint_never_reuses_or_remembers_the_old_key():
    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        first = await _send(
            ws,
            {
                "type": "admin_set_imagegen",
                "provider": "openai",
                "model": "image-old",
                "base_url": "https://images-old.example/v1",
                "api_key": "sk-old-image-endpoint",
            },
        )
        assert first["type"] == "admin_config"

        changed = await _send(
            ws,
            {
                "type": "admin_set_imagegen",
                "provider": "openai",
                "model": "image-new",
                "base_url": "https://images-new.example/v1",
            },
        )
        assert changed["type"] == "admin_config"
        assert services.settings.imagegen.base_url == "https://images-new.example/v1"
        assert services.settings.imagegen.api_key == ""
        assert await services.imagegen_credentials.get("openai") == {"base_url": "https://images-new.example/v1"}

        # Omitted fields on the unchanged endpoint keep the safe, cleared state.
        await _send(
            ws,
            {"type": "admin_set_imagegen", "provider": "openai", "model": "image-newer"},
        )
        assert services.settings.imagegen.api_key == ""

        await ws.close()
    finally:
        await server.close()


async def test_admin_set_imagegen_reuses_the_chat_credential_key():
    """A provider's imagegen often shares its CHAT key (qwen chat + qwen image, one
    api_key). The imagegen config must back-fill that key from the generic chat
    credential book, or it lands with api_key='' and imagegen is silently skipped."""
    services = _services()
    await services.llm_profiles.replace_static(
        "qwen", api_key="sk-qwen-chat", base_url="https://qwen.example/v1"
    )
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        updated = await _send(
            ws,
            {"type": "admin_set_imagegen", "provider": "qwen", "model": "qwen-image-3.0-pro"},
        )
        assert updated["type"] == "admin_config"
        assert updated["imagegen"]["configured"] is True
        assert updated["imagegen"]["api_key_masked"].endswith("chat")
        # The chat key was back-filled, so build_imagegen can build a real client.
        assert (await services.imagegen_runtime_config.get())["api_key"] == "sk-qwen-chat"
        assert services.imagegen is not None
        await ws.close()
    finally:
        await server.close()


async def test_preset_image_endpoint_roundtrip_keeps_the_existing_key():
    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        first = await _send(
            ws,
            {
                "type": "admin_set_imagegen",
                "provider": "openai",
                "model": "gpt-image-1",
                "api_key": "sk-image-preset",
            },
        )
        effective_url = IMAGEGEN_PRESETS["openai"]["base_url"]
        assert first["imagegen"]["base_url"] == effective_url
        assert services.settings.imagegen.base_url == ""

        saved = await _send(
            ws,
            {
                "type": "admin_set_imagegen",
                "provider": "openai",
                "model": "gpt-image-1",
                "base_url": effective_url,
            },
        )
        assert saved["type"] == "admin_config"
        assert services.settings.imagegen.api_key == "sk-image-preset"
        assert await services.imagegen_credentials.get("openai") == {
            "api_key": "sk-image-preset",
            "base_url": effective_url,
        }

        await ws.close()
    finally:
        await server.close()


async def test_imagegen_build_failure_preserves_the_previous_live_state():
    services = _services()
    original_settings = services.settings.imagegen.model_copy(
        update={
            "provider": "openai",
            "model": "old-image-model",
            "api_key": "sk-old-image",
            "base_url": "https://old-images.example/v1",
        }
    )
    original_client = object()
    services.settings.imagegen = original_settings
    services.imagegen = original_client

    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        with patch("net.admin.build_imagegen", side_effect=RuntimeError("builder failed")):
            failed = await _send(
                ws,
                {
                    "type": "admin_set_imagegen",
                    "provider": "openai",
                    "model": "new-image-model",
                    "api_key": "sk-new-image",
                },
            )

        assert failed["type"] == "admin_error"
        assert failed["code"] == "set_failed"
        assert services.settings.imagegen is original_settings
        assert services.imagegen is original_client
        assert await services.imagegen_runtime_config.get() == {}

        await ws.close()
    finally:
        await server.close()


async def test_imagegen_runtime_persistence_failure_reports_error_without_compensation():
    services = _services()
    original_settings = services.settings.imagegen
    original_client = services.imagegen

    with patch.object(
        services.imagegen_runtime_config,
        "replace",
        new=AsyncMock(side_effect=OSError("database is read-only")),
    ):
        result = await AdminService(services, Keystore()).dispatch(
            "keeper",
            "arkham",
            {
                "type": "admin_set_imagegen",
                "provider": "openai",
                "model": "gpt-image-1",
                "api_key": "sk-image",
            },
            get_i18n("en"),
        )

    assert result["type"] == "admin_error"
    assert result["code"] == "set_failed"
    assert services.settings.imagegen is not original_settings
    assert services.settings.imagegen.provider == "openai"
    assert services.settings.imagegen.model == "gpt-image-1"
    assert services.imagegen is not original_client
    assert await services.imagegen_runtime_config.get() == {}


async def test_admin_list_models_returns_the_providers_live_catalog():
    """`admin_list_models` answers with the provider's model IDs (the live /models fetch is
    stubbed here so the test stays offline)."""
    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)

    async def _fake_list_models(_llm):
        return ["alpha-1", "beta-2"]

    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        with patch("net.admin.list_models", new=_fake_list_models):
            reply = await _send(ws, {"type": "admin_list_models", "provider": "deepseek", "api_key": "sk-x"})
        assert reply["type"] == "admin_models"
        assert reply["provider"] == "deepseek"
        assert reply["models"] == ["alpha-1", "beta-2"]

        # an unknown provider is refused, not queried.
        bad = await _send(ws, {"type": "admin_list_models", "provider": "nope-9000"})
        assert bad["type"] == "admin_error"
        assert bad["code"] == "unknown_provider"

        await ws.close()
    finally:
        await server.close()


async def test_admin_list_models_filters_siliconflow_and_reuses_same_endpoint_profile_key():
    services = _services()
    await services.llm_profiles.replace_static(
        "siliconflow::embedding::BAAI/bge-m3",
        api_key="sk-siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        chat_model="BAAI/bge-m3",
        kind="embedding",
        embedding_dim="1024",
    )
    captured = {}

    async def _fake_list_models(settings, kind):
        captured["settings"] = settings
        captured["kind"] = kind
        return ["Kwai-Kolors/Kolors"]

    with patch("net.admin.list_models", new=_fake_list_models):
        reply = await AdminService(services, Keystore()).dispatch(
            "keeper",
            "arkham",
            {
                "type": "admin_list_models",
                "provider": "siliconflow",
                "kind": "image",
                "base_url": "https://api.siliconflow.cn/v1",
            },
            get_i18n("en"),
        )

    assert reply["type"] == "admin_models"
    assert reply["kind"] == "image"
    assert reply["models"] == ["Kwai-Kolors/Kolors"]
    assert captured["kind"] == "image"
    assert captured["settings"].api_key == "sk-siliconflow"


async def test_admin_list_rules_returns_the_built_in_systems():
    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")

        reply = await _send(ws, {"type": "admin_list_rules"})
        assert reply["type"] == "admin_rules"
        by_id = {system["id"]: system["built_in"] for system in reply["systems"]}
        assert by_id.get("coc7") is True
        assert by_id.get("dnd5e") is True

        await ws.close()
    finally:
        await server.close()


async def test_admin_list_skills_reflects_the_callers_room_and_enable_toggles_it():
    services = _services()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    chat_key = chat_key_for_room("arkham")
    await set_enabled_skills(services.store, chat_key, ["romance-relationships"])
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")

        listed = await _send(ws, {"type": "admin_list_skills"})
        assert listed["type"] == "admin_skills"
        by_id = {skill["id"]: skill for skill in listed["skills"]}
        assert "romance-relationships" in by_id and "image-gen" in by_id
        # enabled reflects THIS room's store flag, set above for romance-relationships only.
        assert by_id["romance-relationships"]["enabled"] is True
        assert by_id["image-gen"]["enabled"] is False
        assert by_id["romance-relationships"]["content_rating"] == "mature"
        assert by_id["romance-relationships"]["name"]
        assert by_id["romance-relationships"]["description"]

        # toggling ON another skill leaves the first one enabled and a follow-up admin_skills
        # reflects both.
        enabled = await _send(ws, {"type": "admin_enable_skill", "id": "image-gen", "on": True})
        assert enabled["type"] == "admin_skills"
        by_id = {skill["id"]: skill for skill in enabled["skills"]}
        assert by_id["romance-relationships"]["enabled"] is True
        assert by_id["image-gen"]["enabled"] is True
        assert set(await get_enabled_skills(services.store, chat_key)) == {"romance-relationships", "image-gen"}

        # toggling it back off removes it and nothing else.
        disabled = await _send(ws, {"type": "admin_enable_skill", "id": "image-gen", "on": False})
        by_id = {skill["id"]: skill for skill in disabled["skills"]}
        assert by_id["image-gen"]["enabled"] is False
        assert by_id["romance-relationships"]["enabled"] is True
        assert await get_enabled_skills(services.store, chat_key) == ["romance-relationships"]

        # an unknown skill id is refused, not silently ignored.
        bad = await _send(ws, {"type": "admin_enable_skill", "id": "no-such-skill", "on": True})
        assert bad["type"] == "admin_error"
        assert bad["code"] == "bad_request"

        await ws.close()
    finally:
        await server.close()


async def test_admin_generate_authors_and_installs_skill_rule_and_module(tmp_path):
    """`admin_generate` for each `kind` runs the matching `agent.forge` engine end to end (a real
    `TuiServer` + FakeLLM-scripted responses, mirroring `tests/agent/test_forge*.py`'s fixtures),
    and a bogus `kind`/a blank `description` are refused as `admin_error{bad_request}`."""
    skill_dir = tmp_path / "skills"
    rulepack_dir = tmp_path / "rulepacks"
    module_dir = tmp_path / "modules"
    for directory in (skill_dir, rulepack_dir, module_dir):
        directory.mkdir()

    original_skill_dir = skills_module._USER_SKILL_DIR
    original_rulepack_dir = rulepacks_module._USER_RULEPACK_DIR
    original_module_dir = forge_module._USER_MODULE_DIR
    skills_module._USER_SKILL_DIR = skill_dir
    rulepacks_module._USER_RULEPACK_DIR = rulepack_dir
    forge_module._USER_MODULE_DIR = module_dir
    skills_module.reload_skills()
    rulepacks_module.reload_rulepacks()
    try:
        script = [
            assistant_text(_VALID_SKILL_MD),
            assistant_text(_VALID_RULEPACK_YAML),
            assistant_text(_GENERATED_MODULE_MD),
            assistant_text(_scripted_module_analysis_json()),
        ]
        settings = Settings(
            locale="en", data_dir=str(tmp_path), llm=LLMSettings(provider="openai", chat_model="gpt-4o")
        )
        services = build_services(settings, llm=FakeLLM(script=script), embeddings=FakeEmbeddings(64))
        keystore = Keystore()
        keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
        server = TuiServer(services, keystore, port=0)
        url = await _start(server)
        try:
            ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")

            skill_reply = await _send(
                ws, {"type": "admin_generate", "kind": "skill", "description": "a grim survival horror campaign"}
            )
            assert skill_reply["type"] == "admin_generated"
            assert skill_reply["kind"] == "skill"
            assert skill_reply["ok"] is True
            assert skill_reply["id"] == "grim-survival-horror"
            assert skill_reply["name"] == "Grim Survival Horror"
            assert skill_reply["error"] == ""

            rule_reply = await _send(
                ws, {"type": "admin_generate", "kind": "rule", "description": "a pulp adventure system"}
            )
            assert rule_reply["type"] == "admin_generated"
            assert rule_reply["ok"] is True
            assert rule_reply["id"] == "pulp-adventure"

            module_reply = await _send_generate(
                ws, {"type": "admin_generate", "kind": "module", "description": "a marsh mystery", "locale": "zh"}
            )
            assert "你正在根据守秘人" in services.llm.calls[2][0][0]["content"]
            assert module_reply["type"] == "admin_generated"
            assert module_reply["ok"] is True
            assert module_reply["name"] == "The Salt Marsh Vanishing"
            # `detail` carries the per-room install outcome — the only signal (beyond `ok`) that the
            # module actually reached the room's knowledge pool. Present + non-empty for a module.
            assert module_reply["detail"]
            # skill/rule generation has no per-room install step, so their `detail` is empty.
            assert skill_reply["detail"] == ""
            assert rule_reply["detail"] == ""

            bogus = await _send(ws, {"type": "admin_generate", "kind": "bogus", "description": "x"})
            assert bogus["type"] == "admin_error"
            assert bogus["code"] == "bad_request"

            blank = await _send(ws, {"type": "admin_generate", "kind": "skill", "description": "   "})
            assert blank["type"] == "admin_error"
            assert blank["code"] == "bad_request"

            await ws.close()
        finally:
            await server.close()
    finally:
        skills_module._USER_SKILL_DIR = original_skill_dir
        rulepacks_module._USER_RULEPACK_DIR = original_rulepack_dir
        forge_module._USER_MODULE_DIR = original_module_dir
        skills_module.reload_skills()
        rulepacks_module.reload_rulepacks()


async def test_admin_generate_module_prompt_returns_only_prompt_text_and_request_id():
    """The prompt helper uses the authoring LLM without entering the module install pipeline."""
    settings = Settings(
        locale="en", llm=LLMSettings(provider="openai", chat_model="gpt-4o")
    )
    services = build_services(
        settings,
        llm=FakeLLM(
            script=[
                assistant_text("A clockmaker's vanished apprentice leaves clues in stopped clocks."),
                assistant_text("A haunted railway station hides a dispute over a missing timetable."),
            ]
        ),
        embeddings=FakeEmbeddings(64),
    )
    admin = AdminService(services, Keystore())

    suggestion = await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_generate",
            "kind": "module_prompt",
            "description": json.dumps({"idea": "", "mode": "suggest"}),
            "locale": "en",
            "request_id": "prompt-1",
        },
        get_i18n("en"),
    )
    assert suggestion == {
        "type": "admin_generated",
        "kind": "module_prompt",
        "ok": True,
        "id": "",
        "name": "",
        "error": "",
        "detail": "A clockmaker's vanished apprentice leaves clues in stopped clocks.",
        "request_id": "prompt-1",
    }
    assert "module_system_prompt" not in services.llm.calls[0][0][0]["content"]

    rewrite = await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_generate",
            "kind": "module_prompt",
            "description": json.dumps({
                "idea": "A haunted railway station",
                "mode": "rewrite",
                "rule_strategy": "use:coc7",
                "room_system": "coc7",
            }),
            "locale": "en",
            "request_id": "prompt-2",
        },
        get_i18n("en"),
    )
    assert rewrite["detail"] == "A haunted railway station hides a dispute over a missing timetable."
    assert rewrite["request_id"] == "prompt-2"
    assert "A haunted railway station" in services.llm.calls[1][0][1]["content"]
    # The rule requirement is now terse: the system is named (CoC) in one breath, NOT a full
    # roll/target/outcome/parameter spec dump (no d100/STR enumeration).
    assert "CoC" in services.llm.calls[1][0][1]["content"]
    assert "d100" not in services.llm.calls[1][0][1]["content"]
    assert "STR" not in services.llm.calls[1][0][1]["content"]


async def test_admin_generate_module_honors_media_and_companion_options(tmp_path):
    """Protocol 2.5: `options` on `admin_generate` (kind="module") reaches the forge's media and
    companion passes; unknown ids are ignored, and a malformed `options` shape is dropped rather
    than erroring the request."""
    skill_dir = tmp_path / "skills"
    module_dir = tmp_path / "modules"
    for directory in (skill_dir, module_dir):
        directory.mkdir()

    original_skill_dir = skills_module._USER_SKILL_DIR
    original_module_dir = forge_module._USER_MODULE_DIR
    skills_module._USER_SKILL_DIR = skill_dir
    forge_module._USER_MODULE_DIR = module_dir
    skills_module.reload_skills()
    try:
        shot_list = json.dumps(
            [{"kind": "cover", "subject": "Greyreed at dusk", "prompt": "the marsh town at dusk, moody painting", "caption": "Greyreed."}]
        )
        script = [
            # First generation: media + companion -> module, shot list, companion skill.
            # (The page path no longer imports the module, so no module-analysis call runs.)
            assistant_text(_GENERATED_MODULE_MD),
            assistant_text(shot_list),
            assistant_text(_VALID_SKILL_MD),
            # Second generation (malformed options): module only, no option passes.
            assistant_text(_GENERATED_MODULE_MD),
        ]
        settings = Settings(
            locale="en",
            data_dir=str(tmp_path),
            llm=LLMSettings(provider="openai", chat_model="gpt-4o"),
            imagegen=ImageGenSettings(provider="fake", api_key="fake", model="fake"),
        )
        services = build_services(settings, llm=FakeLLM(script=script), embeddings=FakeEmbeddings(64))
        services.imagegen = FakeImageGen()
        keystore = Keystore()
        keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
        server = TuiServer(services, keystore, port=0)
        url = await _start(server)
        try:
            ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")

            reply = await _send_generate(
                ws,
                {
                    "type": "admin_generate",
                    "kind": "module",
                    "description": "a marsh mystery",
                    "options": {"media": ["cover", "bogus-kind"], "companion": ["skills"]},
                },
            )
            assert reply["type"] == "admin_generated"
            assert reply["ok"] is True
            # The unknown "bogus-kind" id is ignored: exactly one render, named by the shot list.
            assert len(services.imagegen.calls) == 1
            assert "module-the-salt-marsh-vanishing-cover-1.png" in reply["detail"]
            assert "Companion KP skill installed" in reply["detail"]

            malformed = await _send_generate(
                ws,
                {
                    "type": "admin_generate",
                    "kind": "module",
                    "description": "another marsh mystery",
                    "options": {"media": "cover"},
                },
            )
            assert malformed["type"] == "admin_generated"
            assert malformed["ok"] is True
            # `media` was not a list -> dropped, so no media pass ran for the second module.
            assert len(services.imagegen.calls) == 1
            assert "media deck" not in malformed["detail"]

            await ws.close()
        finally:
            await server.close()
    finally:
        skills_module._USER_SKILL_DIR = original_skill_dir
        forge_module._USER_MODULE_DIR = original_module_dir
        skills_module.reload_skills()


async def test_deleting_a_room_over_the_wire_drops_its_turn_lock_after_the_frame(tmp_path):
    """M23 review regression: the destructive admin frames run INSIDE the room's turn
    lock, so `delete_room_data`'s in-op disposal necessarily declines — the session layer
    must drop the bookkeeping right after the lock releases, or every room ever deleted
    over the wire leaks its lock (the exact leak the hub parameter was added to close)."""
    services = _services(str(tmp_path))
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    chat_key = chat_key_for_room("arkham")
    await services.store.state_set(chat_key, "chat_history", "[]")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        reply = await _send(ws, {"type": "admin_delete_room_data", "room": "arkham", "backup": False})
        assert reply["type"] not in {"error", "admin_error"}, reply
        # The reply frame races the server-side disposal by one statement; give it a beat.
        for _ in range(100):
            if chat_key not in server.hub._turn_locks:
                break
            await asyncio.sleep(0.01)
        assert chat_key not in server.hub._turn_locks
        assert chat_key not in server.hub._active_turns
        await ws.close()
    finally:
        await server.close()


async def test_admin_set_llm_profile_writes_only_the_unified_book(tmp_path):
    """`admin_set_llm` saves a chat profile once in the unified book — under the
    typed `provider::model` key — and must NOT also create a bare `provider`
    duplicate (the pre-unification mirror is gone)."""
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())

    saved = await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_set_llm",
            "provider": "deepseek",
            "chat_model": "deepseek-chat",
            "api_key": "sk-profile-secret",
        },
        get_i18n("en"),
    )
    assert saved["type"] == "admin_config"

    assert (await services.llm_profiles.get("deepseek::deepseek-chat"))["api_key"] == "sk-profile-secret"
    # No bare-provider duplicate in the unified book.
    assert await services.llm_profiles.get("deepseek") == {}


async def test_admin_set_llm_profile_does_not_leak_key_into_legacy_book_for_non_chat_kinds(tmp_path):
    """Image/embedding profiles stay in the typed book only — the legacy per-provider
    book is chat-scoped, so an image key must never surface there."""
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())

    saved = await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_set_llm",
            "provider": "minimax-cn",
            "chat_model": "image-01",
            "kind": "image",
            "api_key": "sk-image-secret",
        },
        get_i18n("en"),
    )
    assert saved["type"] == "admin_config"

    assert (await services.llm_profiles.get("minimax-cn::image::image-01"))["api_key"] == "sk-image-secret"
    assert await services.llm_profiles.get("minimax-cn") == {}


async def test_admin_delete_llm_profile_clears_rooms_referencing_it(tmp_path):
    """Deleting a profile must clear every room whose model selection still
    references it, so no room is left holding a dangling reference that would
    silently fall back to the global default."""
    from agent.services import ROOM_LLM_SELECTION_KEY

    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())
    i18n = get_i18n("en")

    await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_set_llm",
            "provider": "deepseek",
            "chat_model": "deepseek-chat",
            "api_key": "sk-profile-secret",
        },
        i18n,
    )
    # A room assigns the deepseek chat profile to its main lane.
    await services.store.state_set(
        "arkham",
        ROOM_LLM_SELECTION_KEY,
        json.dumps({"main": "deepseek::deepseek-chat"}),
    )
    selection = await services.room_model_selection("arkham")
    assert selection["main"] == "deepseek::deepseek-chat"

    deleted = await admin.dispatch(
        "keeper",
        "arkham",
        {"type": "admin_delete_llm", "id": "deepseek::deepseek-chat"},
        i18n,
    )
    assert deleted["type"] == "admin_config"
    assert await services.llm_profiles.get("deepseek::deepseek-chat") == {}
    # The referencing room's lane was cleared, not left dangling.
    selection = await services.room_model_selection("arkham")
    assert selection["main"] == ""


async def test_admin_export_llm_config_round_trips_every_book(tmp_path):
    """`admin_export_llm` returns every saved credential book plus the runtime
    selection; `admin_import_llm` restores them and hot-swaps."""
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())
    i18n = get_i18n("en")

    await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_set_llm",
            "provider": "deepseek",
            "chat_model": "deepseek-chat",
            "api_key": "sk-profile-secret",
        },
        i18n,
    )
    await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_set_llm",
            "provider": "minimax-cn",
            "chat_model": "image-01",
            "kind": "image",
            "api_key": "sk-image-secret",
        },
        i18n,
    )

    exported = await admin.dispatch("keeper", "arkham", {"type": "admin_export_llm"}, i18n)
    assert exported["type"] == "admin_llm_export"
    assert exported["ok"] is True
    config = exported["config"]
    assert config["format"] == "loreweaver-llm-config"
    assert config["version"] == 2
    assert config["llm_profiles"]["deepseek::deepseek-chat"]["api_key"] == "sk-profile-secret"
    assert config["llm_profiles"]["minimax-cn::image::image-01"]["api_key"] == "sk-image-secret"
    assert "llm_credentials" not in config
    # The live effective selection is merged into `runtime` even without a
    # persisted override, so the export round-trips the running model.
    assert config["runtime"]["provider"] == "openai"
    assert config["runtime"]["chat_model"] == "gpt-4o"

    # Wipe every book, then import the export back.
    await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_import_llm",
            "config": {**config, "llm_profiles": {}, "runtime": {}},
        },
        i18n,
    )
    assert await services.llm_profiles.all() == {}

    imported = await admin.dispatch("keeper", "arkham", {"type": "admin_import_llm", "config": config}, i18n)
    assert imported["type"] == "admin_config"
    assert (await services.llm_profiles.get("deepseek::deepseek-chat"))["api_key"] == "sk-profile-secret"


async def test_admin_export_import_round_trips_imagegen_selection(tmp_path):
    """The LLM config export/import must carry the LIVE image-generation runtime
    selection (which provider/model produces images) — not just the credential
    boxes — so an import restores imagegen without the operator re-picking it."""
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())
    i18n = get_i18n("en")

    await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_set_imagegen",
            "provider": "qwen",
            "model": "qwen-image-3.0-pro",
            "api_key": "sk-image-runtime",
        },
        i18n,
    )

    exported = await admin.dispatch("keeper", "arkham", {"type": "admin_export_llm"}, i18n)
    config = exported["config"]
    assert config["imagegen_runtime"]["provider"] == "qwen"
    assert config["imagegen_runtime"]["model"] == "qwen-image-3.0-pro"
    assert config["imagegen_runtime"]["api_key"] == "sk-image-runtime"

    # Wipe the runtime selection, then import back — imagegen must be restored.
    await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_import_llm",
            "config": {**config, "llm_profiles": {}, "runtime": {}, "imagegen_runtime": {}},
        },
        i18n,
    )
    assert await services.imagegen_runtime_config.get() == {}

    imported = await admin.dispatch("keeper", "arkham", {"type": "admin_import_llm", "config": config}, i18n)
    assert imported["type"] == "admin_config"
    restored = await services.imagegen_runtime_config.get()
    assert restored["provider"] == "qwen"
    assert restored["model"] == "qwen-image-3.0-pro"
    assert restored["api_key"] == "sk-image-runtime"
    # The restored selection builds a live imagegen client.
    assert services.imagegen is not None


async def test_admin_import_llm_config_rejects_malformed_documents(tmp_path):
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())
    i18n = get_i18n("en")

    bad_format = await admin.dispatch(
        "keeper",
        "arkham",
        {"type": "admin_import_llm", "config": {"format": "nope", "version": 1}},
        i18n,
    )
    assert bad_format["type"] == "admin_error"
    assert bad_format["code"] == "bad_request"

    bad_version = await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_import_llm",
            "config": {
                "format": "loreweaver-llm-config",
                "version": 99,
                "llm_profiles": {},
                "llm_credentials": {},
                "runtime": {},
                "imagegen_credentials": {},
            },
        },
        i18n,
    )
    assert bad_version["type"] == "admin_error"
    assert bad_version["code"] == "bad_request"

    bad_provider = await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_import_llm",
            "config": {
                "format": "loreweaver-llm-config",
                "version": 1,
                "llm_profiles": {"nope-9000::model": {"api_key": "sk-x"}},
                "llm_credentials": {},
                "runtime": {},
                "imagegen_credentials": {},
            },
        },
        i18n,
    )
    assert bad_provider["type"] == "admin_error"
    assert bad_provider["code"] == "bad_request"

    # Nothing was written by the failed imports.
    assert await services.llm_profiles.all() == {}
    assert await services.llm_profiles.all() == {}


async def test_admin_import_llm_config_accepts_v1_and_merges_legacy(tmp_path):
    """Importing a v1 export (two books) folds its legacy provider-keyed
    `llm_credentials` entries into the unified `llm_profiles` book."""
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())
    i18n = get_i18n("en")

    v1 = {
        "format": "loreweaver-llm-config",
        "version": 1,
        "llm_profiles": {
            "openai::gpt-4o": {"api_key": "sk-open", "chat_model": "gpt-4o", "kind": "chat"},
        },
        "llm_credentials": {
            "deepseek": {
                "api_key": "sk-deep",
                "chat_model": "deepseek-chat",
                "base_url": "https://api.deepseek.com/v1",
            },
        },
        "runtime": {"provider": "openai", "chat_model": "gpt-4o"},
        "imagegen_credentials": {},
        "imagegen_runtime": {},
    }
    imported = await admin.dispatch(
        "keeper",
        "arkham",
        {"type": "admin_import_llm", "config": v1},
        i18n,
    )
    assert imported["type"] == "admin_config"
    assert (await services.llm_profiles.get("openai::gpt-4o"))["api_key"] == "sk-open"
    # The v1 legacy entry was folded into the unified book under its typed id.
    assert (await services.llm_profiles.get("deepseek::deepseek-chat"))["api_key"] == "sk-deep"


async def test_admin_llm_profiles_and_room_assignments_are_separate(tmp_path):
    services = _services(str(tmp_path))
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        saved = await _send(
            ws,
            {
                "type": "admin_set_llm",
                "provider": "deepseek",
                "chat_model": "deepseek-chat",
                "api_key": "sk-profile-secret",
            },
        )
        assert saved["type"] == "admin_config"
        first_id = "deepseek::deepseek-chat"
        assert any(profile["id"] == first_id for profile in saved["llms"])
        assert next(profile for profile in saved["llms"] if profile["id"] == first_id)["has_key"] is True
        assert "sk-profile-secret" not in json.dumps(saved)

        second = await _send(
            ws,
            {
                "type": "admin_set_llm",
                "provider": "deepseek",
                "chat_model": "deepseek-reasoner",
                "api_key": "sk-profile-secret",
            },
        )
        assert {profile["id"] for profile in second["llms"]} == {
            first_id,
            "deepseek::deepseek-reasoner",
        }

        room = await _send(
            ws,
            {"type": "admin_set_room_model", "main": "deepseek::deepseek-reasoner", "scribe": ""},
        )
        assert room["type"] == "admin_room_config"
        assert room["stored"]["main"] == "deepseek::deepseek-reasoner"
        assert room["stored"]["scribe"] == ""
        selected_llm = FakeLLM(script=[])
        services._room_llm_cache["deepseek::deepseek-reasoner"] = selected_llm
        assert await services.main_llm("arkham") is selected_llm
        assert await services.main_llm("tui:group:arkham") is selected_llm

        deleted = await _send(ws, {"type": "admin_delete_llm", "id": first_id})
        assert deleted["type"] == "admin_config"
        assert {profile["id"] for profile in deleted["llms"]} == {"deepseek::deepseek-reasoner"}

        deleted = await _send(ws, {"type": "admin_delete_llm", "id": "deepseek::deepseek-reasoner"})
        assert deleted["type"] == "admin_config"
        assert deleted["llms"] == []
        await ws.close()
    finally:
        await server.close()


async def test_admin_room_model_save_is_not_shadowed_by_legacy_bare_name_row(tmp_path):
    """A stale `llm_selection` row written under the bare room name by an older
    engine must not shadow the canonical session-key row after a save.

    Older versions persisted under the bare room name (``arkham``); the current
    engine writes under the canonical session key (``tui:group:arkham``). The
    dual-spelling read used to prefer the caller's own spelling, so the web
    model screen (which reads by the bare name) kept seeing the stale row and
    snapped the imagegen assignment back to the old value after a successful
    save. The canonical row must win, and saving must retire the legacy row.
    """
    services = _services(str(tmp_path))
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    # Legacy state: an older engine stored the selection under the bare room name.
    await services.store.state_set(
        "arkham",
        ROOM_LLM_SELECTION_KEY,
        json.dumps(
            {
                "main": "",
                "scribe": "",
                "director": "",
                "imagegen": "openai::image::legacy-image",
                "scribe_enabled": True,
                "director_enabled": True,
            }
        ),
    )
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        image = await _send(
            ws,
            {
                "type": "admin_set_llm",
                "provider": "openai",
                "chat_model": "gpt-image-1",
                "kind": "image",
                "api_key": "sk-profile-secret",
            },
        )
        assert image["type"] == "admin_config"
        image_id = next(p["id"] for p in image["llms"] if p["kind"] == "image")
        assert image_id == "openai::image::gpt-image-1"

        # Before any new save the legacy row is still visible (read fallback).
        room = await _send(ws, {"type": "admin_get_room_config"})
        assert room["stored"]["imagegen"] == "openai::image::legacy-image"

        # Saving a new imagegen must stick: the reply and later reads return the
        # NEW value, not the legacy one (regression: it used to snap back).
        saved = await _send(ws, {"type": "admin_set_room_model", "imagegen": image_id})
        assert saved["type"] == "admin_room_config"
        assert saved["stored"]["imagegen"] == image_id

        room = await _send(ws, {"type": "admin_get_room_config"})
        assert room["stored"]["imagegen"] == image_id

        # Self-heal: the legacy bare-name row is gone, the canonical row holds the save.
        assert await services.store.state_get("arkham", ROOM_LLM_SELECTION_KEY) is None
        canonical = await services.store.state_get(
            session_key_for_room("arkham"), ROOM_LLM_SELECTION_KEY
        )
        assert json.loads(canonical)["imagegen"] == image_id
        await ws.close()
    finally:
        await server.close()


async def test_admin_profile_key_never_crosses_to_a_changed_endpoint(tmp_path):
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())
    frame = {
        "type": "admin_set_llm",
        "provider": "openai",
        "chat_model": "gpt-4o",
        "kind": "chat",
        "api_key": "sk-private-a",
        "base_url": "https://gateway-a.example/v1",
    }
    saved = await admin.dispatch("keeper", "arkham", frame, get_i18n("en"))
    assert saved["type"] == "admin_config"

    rejected = await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_set_llm",
            "provider": "openai",
            "chat_model": "gpt-4o",
            "kind": "chat",
            "base_url": "https://gateway-b.example/v1",
        },
        get_i18n("en"),
    )

    assert rejected["type"] == "admin_error"
    assert rejected["code"] == "set_failed"
    persisted = await services.llm_profiles.get("openai::gpt-4o")
    assert persisted["base_url"] == "https://gateway-a.example/v1"
    assert persisted["api_key"] == "sk-private-a"


async def test_admin_profile_honors_authless_and_provider_capabilities(tmp_path):
    services = _services(str(tmp_path))
    admin = AdminService(services, Keystore())

    local = await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_set_llm",
            "provider": "ollama",
            "chat_model": "llama3.2",
            "kind": "chat",
        },
        get_i18n("en"),
    )
    unsupported = await admin.dispatch(
        "keeper",
        "arkham",
        {
            "type": "admin_set_llm",
            "provider": "deepseek",
            "chat_model": "not-an-embedding-model",
            "kind": "embedding",
            "embedding_dim": 1024,
            "api_key": "sk-test",
        },
        get_i18n("en"),
    )

    assert local["type"] == "admin_config"
    assert next(profile for profile in local["llms"] if profile["provider"] == "ollama")["has_key"] is False
    assert unsupported["type"] == "admin_error"
    assert unsupported["code"] == "bad_request"


async def test_admin_model_kinds_gate_embedding_and_room_assignments(tmp_path):
    services = _services(str(tmp_path))
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        chat = await _send(
            ws,
            {
                "type": "admin_set_llm",
                "provider": "openai",
                "chat_model": "gpt-4o",
                "api_key": "sk-shared",
            },
        )
        chat_profile = next(profile for profile in chat["llms"] if profile["chat_model"] == "gpt-4o")
        assert chat_profile["kind"] == "chat"

        embedding = await _send(
            ws,
            {
                "type": "admin_set_llm",
                "provider": "openai",
                "chat_model": "text-embedding-3-small",
                "kind": "embedding",
                "embedding_dim": 3,
            },
        )
        embedding_profile = next(
            profile for profile in embedding["llms"] if profile["chat_model"] == "text-embedding-3-small"
        )
        assert embedding_profile["id"] == "openai::embedding::text-embedding-3-small"
        assert embedding_profile["kind"] == "embedding"
        assert embedding_profile["embedding_dim"] == 3

        image = await _send(
            ws,
            {
                "type": "admin_set_llm",
                "provider": "openai",
                "chat_model": "gpt-image-1",
                "kind": "image",
            },
        )
        image_profile = next(profile for profile in image["llms"] if profile["chat_model"] == "gpt-image-1")
        assert image_profile["kind"] == "image"

        rejected = await _send(
            ws,
            {
                "type": "admin_set_embedding",
                "profile_id": chat_profile["id"],
                "embedding_dim": 3,
            },
        )
        assert rejected["type"] == "admin_error"
        assert rejected["code"] == "bad_request"

        await services.vector_db.vector_store.upsert(
            [
                (
                    "module:0",
                    [0.25] * 64,
                    {
                        "text": "The drowned archive lies beneath the chapel.",
                        "chat_key": "arkham",
                        "document_id": "module",
                        "filename": "module.md",
                        "document_type": "module",
                        "chunk_index": 0,
                    },
                )
            ]
        )
        with patch("net.admin.OpenAIEmbeddings", side_effect=lambda settings: FakeEmbeddings(settings.embedding_dim)):
            selected = await _send(
                ws,
                {
                    "type": "admin_set_embedding",
                    "profile_id": embedding_profile["id"],
                    "embedding_dim": 3,
                },
            )
        assert selected["embedding_profile"] == embedding_profile["id"]
        assert selected["embedding_model"] == "text-embedding-3-small"
        assert selected["embedding_dim"] == 3
        assert selected["embedding_rebuilt"] == 1
        assert services.embeddings.dim == 3
        assert services.vector_db.embeddings is services.embeddings
        assert services.worldbook.embeddings is services.embeddings
        [rebuilt] = await services.vector_db.vector_store.dump()
        assert len(rebuilt["vector"]) == 3

        rejected = await _send(ws, {"type": "admin_set_room_model", "main": embedding_profile["id"]})
        assert rejected["type"] == "admin_error"
        assert rejected["code"] == "bad_request"

        room = await _send(ws, {"type": "admin_set_room_model", "imagegen": image_profile["id"]})
        assert room["type"] == "admin_room_config"
        assert room["stored"]["imagegen"] == image_profile["id"]
        selected_imagegen = FakeImageGen()
        services._room_imagegen_cache[image_profile["id"]] = selected_imagegen
        assert await services.imagegen_for_room("arkham") is selected_imagegen

        deleted = await _send(ws, {"type": "admin_delete_llm", "id": embedding_profile["id"]})
        assert deleted["embedding_profile"] == ""
        assert deleted["embedding_rebuilt"] == 1
        assert services.embeddings is services.base_embeddings
        assert services.embeddings.dim == 64
        [restored] = await services.vector_db.vector_store.dump()
        assert len(restored["vector"]) == 64
        await ws.close()
    finally:
        await server.close()


async def test_admin_untyped_model_profiles_remain_chat_compatible(tmp_path):
    services = _services(str(tmp_path))
    await services.llm_profiles.replace_static(
        "deepseek::deepseek-chat",
        api_key="sk-legacy",
        chat_model="deepseek-chat",
    )
    await services.store.state_set(
        "arkham",
        ROOM_LLM_SELECTION_KEY,
        json.dumps({"main": "deepseek::deepseek-chat"}),
    )
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        config = await _send(ws, {"type": "admin_get_config"})
        legacy = next(profile for profile in config["llms"] if profile["id"] == "deepseek::deepseek-chat")
        assert legacy["kind"] == "chat"
        assert legacy["chat_model"] == "deepseek-chat"

        room = await _send(ws, {"type": "admin_get_room_config"})
        assert room["stored"]["main"] == "deepseek::deepseek-chat"
        await ws.close()
    finally:
        await server.close()


async def test_admin_embedding_hot_swap_failure_keeps_live_index_and_config(tmp_path):
    class FailingEmbeddings:
        @property
        def dim(self) -> int:
            return 3

        async def embed(self, texts):
            raise RuntimeError("provider unavailable")

    services = _services(str(tmp_path))
    profile_id = "openai::embedding::broken-embedding"
    await services.llm_profiles.replace_static(
        profile_id,
        api_key="sk-broken",
        chat_model="broken-embedding",
        kind="embedding",
        embedding_dim="3",
    )
    await services.vector_db.vector_store.upsert(
        [("doc:0", [0.5] * 64, {"text": "Keep this vector", "chat_key": "arkham"})]
    )
    original_embeddings = services.embeddings
    original_points = await services.vector_db.vector_store.dump()
    keystore = Keystore()
    keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
    server = TuiServer(services, keystore, port=0)
    url = await _start(server)
    try:
        ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")
        with patch("net.admin.OpenAIEmbeddings", return_value=FailingEmbeddings()):
            reply = await _send(
                ws,
                {
                    "type": "admin_set_embedding",
                    "profile_id": profile_id,
                    "embedding_dim": 3,
                },
            )

        assert reply["type"] == "admin_error"
        assert reply["code"] == "set_failed"
        assert await services.runtime_config.get() == {}
        assert services.embeddings is original_embeddings
        assert services.embeddings.dim == 64
        assert await services.vector_db.vector_store.dump() == original_points
        await ws.close()
    finally:
        await server.close()


async def test_admin_generate_module_streams_started_progress_then_result(tmp_path):
    """Async module generation: `admin_generate kind=module` immediately replies
    `admin_generate_started`, streams at least one `admin_generate_progress` stage, then pushes
    the final `admin_generated` — all on the requesting connection, without the client blocking
    on a single request/reply."""
    module_dir = tmp_path / "modules"
    module_dir.mkdir()
    original_module_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = module_dir
    try:
        script = [
            assistant_text(_GENERATED_MODULE_MD),
            assistant_text(_scripted_module_analysis_json()),
        ]
        settings = Settings(
            locale="en", data_dir=str(tmp_path), llm=LLMSettings(provider="openai", chat_model="gpt-4o")
        )
        services = build_services(settings, llm=FakeLLM(script=script), embeddings=FakeEmbeddings(64))
        keystore = Keystore()
        keeper_key = keystore.add(room="arkham", name="Keeper", role="keeper")
        server = TuiServer(services, keystore, port=0)
        url = await _start(server)
        try:
            ws, *_ = await _connect_and_join(url, keeper_key, "Keeper")

            await ws.send(json.dumps({"type": "admin_generate", "kind": "module", "description": "a marsh mystery", "locale": "zh"}))
            seen: list[str] = []
            result = None
            for _ in range(20):
                frame = await _recv(ws)
                seen.append(str(frame.get("type")))
                if frame.get("type") == "admin_generate_started":
                    assert frame.get("kind") == "module"
                if frame.get("type") == "admin_generate_progress":
                    assert frame.get("stage")
                if frame.get("type") == "admin_generated":
                    result = frame
                    break
            # The started frame came first, at least one progress frame streamed, and the
            # final result landed.
            assert seen[0] == "admin_generate_started"
            assert any(t == "admin_generate_progress" for t in seen)
            assert result is not None and result.get("ok") is True
            assert result.get("kind") == "module"
        finally:
            await server.close()
    finally:
        forge_module._USER_MODULE_DIR = original_module_dir
