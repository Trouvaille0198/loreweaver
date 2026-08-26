"""Player-owned character roster state is complete and owner-filtered."""

from __future__ import annotations

from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM
from net.state import build_room_state


def _services():
    return build_services(Settings(locale="en"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def test_state_lists_every_owned_character_with_full_sheet_details():
    services = _services()
    chat_key = "owned-character-roster"
    owner = "player-a"

    alice = services.characters.generate_character("coc7", "Alice")
    alice.background = "A patient archivist."
    alice.notes = "Private memory for Alice."
    alice.equipment = ["Oil lamp"]
    alice.items = [{"name": "Brass key", "description": "Cold to the touch."}]
    alice.secondary_attributes["IDEA"] = 65
    alice.occupation = "Archivist"
    await services.characters.save_character(owner, chat_key, alice)

    bob = services.characters.generate_character("coc7", "Bob")
    bob.background = "A cautious surveyor."
    await services.characters.save_character(owner, chat_key, bob)

    other = services.characters.generate_character("coc7", "Other")
    other.notes = "Another player's private note."
    await services.characters.save_character("player-b", chat_key, other)

    await services.characters.sync_party_roster(chat_key, alice, status_effects=["shaken"])
    await services.characters.set_active_character(owner, chat_key, "Alice")

    state = await build_room_state(services, AgentCtx(chat_key=chat_key, user_id=owner, locale="en"))
    characters = state["characters"]

    assert [character["name"] for character in characters] == ["Alice", "Bob"]
    alice_wire = characters[0]
    assert alice_wire["name"] == state["character"]["name"] == "Alice"
    assert alice_wire["background"] == alice.background
    assert alice_wire["notes"] == alice.notes
    assert alice_wire["equipment"] == alice.equipment
    assert alice_wire["items"] == alice.items
    assert alice_wire["secondary_attributes"]["IDEA"] == 65
    assert alice_wire["fields"]["occupation"] == "Archivist"
    assert alice_wire["status_effects"] == ["shaken"]
    assert "skills" in alice_wire
    assert "resources" in alice_wire
    assert "attributes" in alice_wire

    other_state = await build_room_state(services, AgentCtx(chat_key=chat_key, user_id="player-b", locale="en"))
    assert [character["name"] for character in other_state["characters"]] == ["Other"]
    assert "Alice" not in {character["name"] for character in other_state["characters"]}
    assert other_state["characters"][0]["notes"] == other.notes


async def test_state_omits_owned_character_list_when_player_has_no_sheets():
    services = _services()
    state = await build_room_state(
        services,
        AgentCtx(chat_key="empty-owned-roster", user_id="player-a", locale="en"),
    )

    assert "characters" not in state
