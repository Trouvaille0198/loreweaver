"""The player-facing character roster command lists and switches owned sheets."""

from __future__ import annotations

from agent.context import AgentCtx
from agent.services import build_services
from core.character_manager import CharacterSheet
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM


def _services():
    return build_services(Settings(locale="en"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


async def test_characters_command_lists_only_the_callers_sheets():
    services = _services()
    room = "characters-command"
    await services.characters.save_character("player-a", room, CharacterSheet("Alice", "coc7"))
    await services.characters.save_character("player-a", room, CharacterSheet("Bob", "dnd5e"))
    await services.characters.save_character("player-b", room, CharacterSheet("Other", "coc7"))
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key=room, user_id="player-a", locale="en")

    reply = await router.dispatch(ctx, ".characters")

    assert reply is not None
    assert "Your characters (2)" in reply
    assert "Alice [coc7]" in reply
    assert "Bob [dnd5e]" in reply
    assert "Other" not in reply


async def test_characters_command_switches_only_to_an_owned_sheet():
    services = _services()
    room = "characters-switch-command"
    await services.characters.save_character("player-a", room, CharacterSheet("Alice", "coc7"))
    await services.characters.save_character("player-a", room, CharacterSheet("Bob", "coc7"))
    await services.characters.save_character("player-b", room, CharacterSheet("Other", "coc7"))
    router = CommandRouter(services)
    ctx = AgentCtx(chat_key=room, user_id="player-a", locale="en")

    switched = await router.dispatch(ctx, ".characters switch Alice")
    assert switched == "Now playing Alice [coc7]."
    assert (await services.characters.get_character("player-a", room)).name == "Alice"

    refused = await router.dispatch(ctx, ".characters switch Other")
    assert refused is not None and "do not own" in refused
    assert (await services.characters.get_character("player-a", room)).name == "Alice"


def test_characters_command_marks_its_personal_listing_private():
    router = CommandRouter(_services())

    resolved = router.resolve(".characters", "en")

    assert resolved is not None
    assert resolved[0].private_reply is True
