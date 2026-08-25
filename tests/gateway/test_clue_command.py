"""Tests for the structural clue-tracking lane: the `.clue` command, the Keeper's
`reveal_clue` tool, the `clue_log` document projection, and the `state` frame's
`clues` payload.

Deterministic and offline (FakeLLM/FakeEmbeddings, fresh in-memory store per
test). A networked player is `platform="tui", extra={"role": "player"}`; a
keeper is the trusted local `cli` platform — matching `_is_keeper`.
"""

from __future__ import annotations

from agent.context import AgentCtx
from agent.kp_tools_mechanics import CharacterTools
from agent.services import build_services
from core.documents import PLAYER_VIEWER
from core.worldbook import LoreEntry
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM

CHAT = "cli:dm:clues"


def _services():
    return build_services(Settings(), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


def _player_ctx(uid: str = "p1") -> AgentCtx:
    return AgentCtx(chat_key=CHAT, user_id=uid, platform="tui", locale="en", extra={"role": "player"})


def _keeper_ctx() -> AgentCtx:
    return AgentCtx(chat_key=CHAT, user_id="k1", platform="cli", locale="en")


async def _seed_clue(services, *, title: str = "The Sunken Bell", keys: list[str] | None = None,
                     content: str = "A green-crusted bell that hums near the lighthouse.",
                     secret: bool = False) -> None:
    await services.worldbook.add(
        CHAT,
        LoreEntry(
            id="",
            title=title,
            content=content,
            keys=keys or [title],
            category="clue",
            secret=secret,
        ),
    )


async def _log(services):
    from agent.clue_log import get_clue_log

    return await get_clue_log(services.documents, CHAT)


# ---------------------------------------------------------------------------
# .clue add — keeper registers a worldbook clue (by title or trigger key)
# ---------------------------------------------------------------------------


async def test_clue_add_keeper_snapshots_worldbook_entry_by_title():
    services = _services()
    router = CommandRouter(services)
    await _seed_clue(services)

    reply = await router.dispatch(_keeper_ctx(), ".clue add The Sunken Bell")

    assert "Clue recorded" in reply
    clues = await _log(services)
    assert [c["title"] for c in clues] == ["The Sunken Bell"]
    assert clues[0]["content"] == "A green-crusted bell that hums near the lighthouse."
    assert clues[0]["keys"] == ["The Sunken Bell"]


async def test_clue_add_matches_trigger_key():
    services = _services()
    router = CommandRouter(services)
    await _seed_clue(services, keys=["bell", "lighthouse", "hum"])

    await router.dispatch(_keeper_ctx(), ".clue add lighthouse")

    clues = await _log(services)
    assert [c["title"] for c in clues] == ["The Sunken Bell"]


async def test_clue_add_is_idempotent_by_title():
    services = _services()
    router = CommandRouter(services)
    await _seed_clue(services)

    await router.dispatch(_keeper_ctx(), ".clue add The Sunken Bell")
    reply = await router.dispatch(_keeper_ctx(), ".clue add The Sunken Bell")

    assert "already" in reply
    assert len(await _log(services)) == 1


async def test_clue_add_ignores_non_clue_worldbook_entries():
    services = _services()
    router = CommandRouter(services)
    await services.worldbook.add(
        CHAT,
        LoreEntry(id="", title="The Ferryman", content="Bound to the old pact.",
                  keys=["ferryman"], category="npc", secret=True),
    )

    reply = await router.dispatch(_keeper_ctx(), ".clue add ferryman")

    assert "No worldbook clue" in reply
    assert await _log(services) == []


async def test_clue_add_player_denied():
    services = _services()
    router = CommandRouter(services)
    await _seed_clue(services)

    reply = await router.dispatch(_player_ctx(), ".clue add The Sunken Bell")

    assert "keeper" in reply
    assert await _log(services) == []


# ---------------------------------------------------------------------------
# .clue list / remove
# ---------------------------------------------------------------------------


async def test_clue_list_shows_discovered_to_players():
    services = _services()
    router = CommandRouter(services)
    await _seed_clue(services, title="Letter", content="The draft was altered in 1914.")
    await router.dispatch(_keeper_ctx(), ".clue add Letter")

    reply = await router.dispatch(_player_ctx(), ".clue list")

    assert "Letter" in reply and "altered in 1914" in reply


async def test_clue_remove_retracts_keeper_only():
    services = _services()
    router = CommandRouter(services)
    await _seed_clue(services)
    await router.dispatch(_keeper_ctx(), ".clue add The Sunken Bell")

    reply = await router.dispatch(_keeper_ctx(), ".clue remove The Sunken Bell")

    assert "Clue removed" in reply
    assert await _log(services) == []
    denied = await router.dispatch(_player_ctx(), ".clue remove The Sunken Bell")
    assert "keeper" in denied


# ---------------------------------------------------------------------------
# Keeper tool — reveal_clue (the AI's structural half)
# ---------------------------------------------------------------------------


async def test_reveal_clue_tool_registers_discovery():
    services = _services()
    await _seed_clue(services, keys=["tape", "田中"])
    tools = CharacterTools(services)
    ctx = _keeper_ctx()

    reply = await tools.reveal_clue(ctx, "田中")

    assert "Clue recorded" in reply
    clues = await _log(services)
    assert clues[0]["title"] == "The Sunken Bell"


async def test_reveal_clue_tool_unknown_name_is_clean_failure():
    services = _services()
    reply = await CharacterTools(services).reveal_clue(_keeper_ctx(), "nonexistent")

    assert "No worldbook clue" in reply
    assert await _log(services) == []


# ---------------------------------------------------------------------------
# Projection + state frame — secret clues stay hidden until revealed
# ---------------------------------------------------------------------------


async def test_secret_clue_invisible_to_players_until_revealed():
    services = _services()
    router = CommandRouter(services)
    await _seed_clue(services, title="The Hidden Truth", content="The mayor is the cult's mouth.",
                     keys=["truth"], secret=True)

    # Player projection of the clue_log is empty — nothing has been found.
    view = await services.documents.get_view(CHAT, "clue_log", "clue_log", PLAYER_VIEWER)
    assert view is None or view == {"clues": []}

    # Keeper reveals it; only then does the player projection carry it.
    await router.dispatch(_keeper_ctx(), ".clue add The Hidden Truth")
    view = await services.documents.get_view(CHAT, "clue_log", "clue_log", PLAYER_VIEWER)
    assert [c["title"] for c in view["clues"]] == ["The Hidden Truth"]


async def test_state_frame_carries_discovered_clues():
    from net.state import build_room_state

    services = _services()
    router = CommandRouter(services)
    await _seed_clue(services, title="The Tape", content="Seventeen minutes of hums.")
    await router.dispatch(_keeper_ctx(), ".clue add The Tape")

    state = await build_room_state(services, _player_ctx())

    assert state["type"] == "state"
    assert [c["title"] for c in state.get("clues", [])] == ["The Tape"]
    assert state["clues"][0]["content"] == "Seventeen minutes of hums."


async def test_state_frame_omits_clues_when_none_discovered():
    from net.state import build_room_state

    services = _services()
    state = await build_room_state(services, _player_ctx())
    assert "clues" not in state
