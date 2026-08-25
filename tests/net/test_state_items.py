"""Phase 2: the wire party roster carries structured `items` for each member, so a
client can render item detail (not just the equipment name list). `net.state._party`
must pass the roster's `items` through to the wire frame.

Deterministic and offline (FakeLLM/FakeEmbeddings, fresh in-memory store).
"""

from __future__ import annotations

from agent.context import AgentCtx
from agent.items import ensure_catalog
from agent.kp_tools_mechanics import CharacterTools
from agent.services import build_services
from core.character_manager import CharacterSheet
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM
from net.state import build_room_state


def _services():
    return build_services(Settings(locale="en"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def test_party_member_items_reach_the_wire_state():
    services = _services()
    chat_key = "tui:group:wire"
    uid = "tui:player"
    await services.characters.save_character(uid, chat_key, CharacterSheet("Alice", "coc7"))
    await ensure_catalog(
        services.documents,
        chat_key,
        [
            {
                "name": "Fencing Sword",
                "kind": "weapon",
                "slot": "main_hand",
                "effect": "+2 attack",
                "lore": "A captain's blade.",
                "origin": "the sunken galleon",
                "bonus": {"attack": 2},
            },
        ],
    )
    ctx = AgentCtx(chat_key=chat_key, user_id=uid, locale="en")
    tools = CharacterTools(services)
    await tools.grant_item(ctx, "Alice", "Fencing Sword")
    await tools.equip_item(ctx, "Alice", "Fencing Sword")

    state = await build_room_state(services, ctx)
    alice = next((m for m in state["party"] if m.get("name") == "Alice"), None)
    assert alice is not None
    # The equipment name list (legacy surface) and the structured detail both ride the wire.
    assert alice["equipment"] == ["Fencing Sword (main_hand)"]
    assert alice["items"][0]["name"] == "Fencing Sword"
    assert alice["items"][0]["kind"] == "weapon"
    assert alice["items"][0]["effect"] == "+2 attack"
    assert alice["items"][0]["lore"] == "A captain's blade."
    assert alice["items"][0]["origin"] == "the sunken galleon"
    assert alice["items"][0]["equipped_slot"] == "main_hand"


async def test_secret_items_never_reach_the_wire_party():
    """A `secret` item is omitted from both the equipment list and the structured
    item views a client receives — it stays keeper-side (iron rule 3)."""
    services = _services()
    chat_key = "tui:group:secret"
    uid = "tui:player"
    await services.characters.save_character(uid, chat_key, CharacterSheet("Alice", "coc7"))
    await ensure_catalog(
        services.documents,
        chat_key,
        [
            {"name": "Map", "kind": "misc", "effect": "", "bonus": {}},
            {"name": "Hidden Ledger", "kind": "misc", "effect": "", "secret": True, "bonus": {}},
        ],
    )
    ctx = AgentCtx(chat_key=chat_key, user_id=uid, locale="en")
    tools = CharacterTools(services)
    await tools.grant_item(ctx, "Alice", "Map")
    await tools.grant_item(ctx, "Alice", "Hidden Ledger")

    state = await build_room_state(services, ctx)
    alice = next((m for m in state["party"] if m.get("name") == "Alice"), None)
    assert alice["equipment"] == ["Map"]
    assert [i["name"] for i in alice["items"]] == ["Map"]
