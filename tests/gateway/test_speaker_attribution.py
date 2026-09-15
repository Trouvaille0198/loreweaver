"""Hub and CLI turns tag the Keeper's player line with who spoke (issue #29).

The room echo stays unprefixed — clients already render `player_action.name`. Direct
`run_kp_turn` callers stay verbatim so the cache-layout oracle is undisturbed.
"""

from __future__ import annotations

from agent.context import AgentCtx
from agent.history import DEFAULT_HISTORY_KEY, load_chain
from agent.kp_tools import build_kp_toolset
from agent.loop import run_kp_turn
from agent.player_line import attributed_player_line, player_line_body
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.worldbook import LoreEntry
from gateway.commands import CommandRouter
from gateway.hub import Event, RoomHub
from gateway.runner import GatewayRunner
from gateway.turn import run_turn
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.i18n import I18n
from infra.llm import FakeLLM, assistant_text

ROOM = "tui:group:speaker"


class _Member:
    transport = "tui"

    def __init__(self, member_id: str, name: str) -> None:
        self.id = member_id
        self.user_key = f"user:{member_id}"
        self.name = name
        self.events: list[Event] = []

    async def deliver(self, event: Event) -> None:
        self.events.append(event)


def _services(*replies: str):
    return build_services(
        Settings(locale="en"),
        llm=FakeLLM(script=[assistant_text(reply) for reply in replies]),
        embeddings=FakeEmbeddings(8),
    )


def test_attributed_player_line_tags_a_name_and_leaves_empty_name_verbatim():
    i18n = I18n("en")
    assert attributed_player_line(i18n, "morle (Hpeter)", "who am I?") == "[morle (Hpeter)]\nwho am I?"
    assert attributed_player_line(i18n, "  ", "bare") == "bare"
    assert attributed_player_line(I18n("zh"), "Ada", "开门") == "[Ada]\n开门"
    assert attributed_player_line(i18n, "Ada", "use {the key}") == "[Ada]\nuse {the key}"
    tagged = attributed_player_line(i18n, "morle (Hpeter)", "who am I?")
    assert player_line_body(tagged) == "who am I?"
    assert player_line_body("bare") == "bare"


async def test_two_players_kp_history_names_each_speaker_echo_stays_raw():
    services = _services("You are Tim Cook.", "You are morle.")
    hub = RoomHub()
    keeper = _Member("m-keeper", "keeper")
    hpeter = _Member("m-hpeter", "Hpeter")
    await hub.subscribe(ROOM, keeper)
    await hub.subscribe(ROOM, hpeter)
    router = CommandRouter(services)
    toolset = build_kp_toolset(services)

    ctx_keeper = AgentCtx(chat_key=ROOM, user_id=keeper.id, platform="tui", locale="en")
    ctx_hpeter = AgentCtx(chat_key=ROOM, user_id=hpeter.id, platform="tui", locale="en")
    await services.characters.save_character(
        ctx_keeper.uid(), ROOM, CharacterSheet(name="Tim Cook", system="coc7")
    )
    await services.characters.save_character(
        ctx_hpeter.uid(), ROOM, CharacterSheet(name="morle", system="coc7")
    )

    await run_turn(
        hub, services, ctx_keeper, "who am I playing?",
        command_router=router, toolset=toolset, origin=keeper,
    )
    await run_turn(
        hub, services, ctx_hpeter, "who am I?",
        command_router=router, toolset=toolset, origin=hpeter,
    )

    chain = await load_chain(services, ROOM, DEFAULT_HISTORY_KEY)
    users = [message["content"] for message in chain if message["role"] == "user"]
    assert users == [
        "[Tim Cook (keeper)]\nwho am I playing?",
        "[morle (Hpeter)]\nwho am I?",
    ]

    echoes = [event for event in keeper.events if event.kind == "player_action"]
    assert [(event.name, event.text) for event in echoes] == [
        ("Tim Cook (keeper)", "who am I playing?"),
        ("morle (Hpeter)", "who am I?"),
    ]

    # Scribe is off in the suite-wide conftest; each turn is one KP call.
    assert services.llm.calls[0][0][-1]["content"] == users[0]
    assert services.llm.calls[1][0][-1]["content"] == users[1]


async def test_direct_run_kp_turn_stays_verbatim():
    services = _services("The night is still.")
    ctx = AgentCtx(chat_key="direct-verbatim", user_id="u1", platform="tui", locale="en")
    await run_kp_turn(ctx, services, build_kp_toolset(services), "I wait by the window.")
    chain = await load_chain(services, ctx.chat_key, DEFAULT_HISTORY_KEY)
    assert [message["content"] for message in chain if message["role"] == "user"] == ["I wait by the window."]


async def test_standalone_cli_turn_tags_the_caller(tmp_path):
    services = build_services(
        Settings(_env_file=None, data_dir=str(tmp_path / "data"), locale="en"),
        llm=FakeLLM(script=[assistant_text("The bell tolls.")]),
        embeddings=FakeEmbeddings(8),
    )
    runner = GatewayRunner(services, [])
    ctx = AgentCtx(chat_key="cli:solo:speaker", user_id="Ada", platform="cli", locale="en")
    await services.characters.save_character(ctx.uid(), ctx.chat_key, CharacterSheet(name="Nora", system="coc7"))

    reply = await runner._answer_standalone(ctx, "I ring the bell")
    assert reply is not None
    chain = await load_chain(services, ctx.chat_key, DEFAULT_HISTORY_KEY)
    users = [message["content"] for message in chain if message["role"] == "user"]
    assert users == ["[Nora (Ada)]\nI ring the bell"]
    assert services.llm.calls[0][0][-1]["content"] == users[0]


async def test_hub_turn_speaker_tag_does_not_fire_name_keyed_lore():
    """The live wrap path: character name collides with a lore key, body does not."""
    captured: list[str] = []

    def responder(messages, tools):
        captured.append("\n\n".join(str(message.get("content") or "") for message in messages[:-1]))
        return assistant_text("The pier is quiet.")

    services = build_services(
        Settings(locale="en"),
        llm=FakeLLM(responder=responder),
        embeddings=FakeEmbeddings(8),
    )
    hub = RoomHub()
    member = _Member("m-keeper", "keeper")
    chat_key = ROOM + "-lore"
    await hub.subscribe(chat_key, member)
    ctx = AgentCtx(chat_key=chat_key, user_id=member.id, platform="tui", locale="en")
    await services.characters.save_character(
        ctx.uid(), ctx.chat_key, CharacterSheet(name="Lighthouse", system="coc7")
    )
    await services.worldbook.add(
        ctx.chat_key,
        LoreEntry(id="", title="The Light", content="The lighthouse keeper is the murderer.", keys=["Lighthouse"]),
    )

    await run_turn(
        hub,
        services,
        ctx,
        "I walk to the pier.",
        command_router=CommandRouter(services),
        toolset=build_kp_toolset(services),
        origin=member,
    )

    assert ctx.extra["user_message"] == "I walk to the pier."
    assert all("lighthouse keeper is the murderer" not in prompt.lower() for prompt in captured)
    assert services.llm.calls[0][0][-1]["content"] == "[Lighthouse (keeper)]\nI walk to the pier."
