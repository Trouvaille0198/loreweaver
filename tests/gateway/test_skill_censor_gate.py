"""Tests for the mature-mode censor gate (preset tier).

A room with the system preset `mature-mode` enabled — its
`x_loreweaver_content_rating` marker is `explicit` — bypasses the output
word-filter ENTIRELY for that room, regardless of the configured `Censor`;
see `gateway.ops.room_content_unfiltered` and `gateway.turn.run_turn`. The
marker reads the same way the old `mature-mode` skill's `content_rating` did.
Every other room keeps the configured `Censor`'s behavior exactly as before.
"""

from __future__ import annotations

from agent.context import AgentCtx
from agent.kp_tools import build_kp_toolset
from agent.services import build_services
from core.preset_store import load_preset
from gateway.commands import CommandRouter
from gateway.hub import RoomHub
from gateway.ops import Censor, get_enabled_preset, room_content_unfiltered, set_enabled_preset
from gateway.turn import run_turn
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM, assistant_text

REPLY_TEXT = "The naughtyword lingers in the air."


def _responder(messages, tools):
    return assistant_text(REPLY_TEXT)


def _services():
    return build_services(Settings(locale="en"), llm=FakeLLM(responder=_responder), embeddings=FakeEmbeddings(8))


async def _run(services, chat_key: str, censor: Censor):
    hub = RoomHub()
    router = CommandRouter(services)
    toolset = build_kp_toolset(services)
    ctx = AgentCtx(chat_key=chat_key, user_id="u1", locale="en")
    return await run_turn(
        hub, services, ctx, "I greet the shopkeeper", command_router=router, toolset=toolset, censor=censor
    )


def test_system_preset_mature_mode_carries_the_unfiltered_marker() -> None:
    """The engine-shipped `mature-mode` preset parses with content_rating=explicit —
    the gate signal the preset tier replaces the deleted skill with."""
    preset = load_preset("./data", "mature-mode")
    assert preset is not None
    assert preset.content_rating == "explicit"


async def test_room_content_unfiltered_false_by_default() -> None:
    services = _services()
    assert not await room_content_unfiltered(services.store, "room-plain", services.settings.data_dir)


async def test_room_content_unfiltered_true_once_mature_preset_enabled() -> None:
    services = _services()
    chat_key = "room-mature-flag"
    await set_enabled_preset(services.store, chat_key, "mature-mode")

    assert await get_enabled_preset(services.store, chat_key) == "mature-mode"
    assert await room_content_unfiltered(services.store, chat_key, services.settings.data_dir)


async def test_censor_still_applies_without_a_mature_preset_enabled() -> None:
    services = _services()
    censor = Censor({"naughtyword": 5})

    result = await _run(services, "room-no-mature-preset", censor)

    assert result is not None
    assert "naughtyword" not in result.reply  # masked by the configured Censor, as before


async def test_censor_is_bypassed_once_the_mature_preset_is_enabled_for_the_room() -> None:
    services = _services()
    censor = Censor({"naughtyword": 5})
    chat_key = "room-with-mature-preset"
    await set_enabled_preset(services.store, chat_key, "mature-mode")

    result = await _run(services, chat_key, censor)

    assert result is not None
    assert "naughtyword" in result.reply  # the mature-mode gate bypassed the word-filter entirely


async def test_censor_bypass_is_room_scoped_not_global() -> None:
    """Enabling the mature preset in ONE room must not affect a DIFFERENT room's censor."""
    services = _services()
    censor = Censor({"naughtyword": 5})
    await set_enabled_preset(services.store, "room-a-mature", "mature-mode")

    unaffected = await _run(services, "room-b-plain", censor)
    affected = await _run(services, "room-a-mature", censor)

    assert unaffected is not None and "naughtyword" not in unaffected.reply
    assert affected is not None and "naughtyword" in affected.reply
