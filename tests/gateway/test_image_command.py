"""`.image` command — Keeper-only room image handout generation."""

import json

from agent.context import AgentCtx
from agent.services import build_services
from gateway.commands import CommandRouter
from gateway.imagegen import image_name, reset_imagegen_limiters
from infra.config import ImageGenSettings, Settings
from infra.embeddings import FakeEmbeddings
from infra.imagegen import FakeImageGen
from infra.llm import ChatResult, FakeLLM


class _Hub:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, session_key, event, *, exclude=None):
        self.events.append((session_key, event, exclude))


def _services(tmp_path, *, per_hour: int = 10, llm: FakeLLM | None = None):
    settings = Settings(
        locale="en",
        data_dir=str(tmp_path),
        imagegen=ImageGenSettings(provider="fake", api_key="fake", model="fake", per_room_per_hour=per_hour),
    )
    services = build_services(
        settings,
        llm=llm or FakeLLM(script=[]),
        embeddings=FakeEmbeddings(8),
    )
    services.imagegen = FakeImageGen()
    return services


def _player_ctx(chat_key: str) -> AgentCtx:
    return AgentCtx(chat_key=chat_key, user_id="p1", platform="tui", locale="en", extra={"role": "player"})


def _keeper_ctx(chat_key: str) -> AgentCtx:
    return AgentCtx(chat_key=chat_key, user_id="k1", platform="cli", locale="en")


async def test_image_command_denied_for_player(tmp_path):
    reset_imagegen_limiters()
    services = _services(tmp_path)
    router = CommandRouter(services)
    reply = await router.dispatch(_player_ctx("tui:group:img"), ".image a misty chapel")

    assert reply == services.i18n.with_locale("en").t("rooms.denied")


async def test_image_command_keeper_publishes_media_and_history(tmp_path):
    reset_imagegen_limiters()
    services = _services(tmp_path)
    hub = _Hub()
    router = CommandRouter(services, hub=hub)
    chat_key = "tui:group:img2"

    result = await router.dispatch(_keeper_ctx(chat_key), ".image scene misty chapel")

    assert "misty-chapel.png" in result
    assert result == services.i18n.with_locale("en").t(
        "commands.image.generated", kind="scene", file="scene-misty-chapel.png", hash=result.split("(")[-1][:12]
    )
    raw = await services.store.state_get(chat_key, "media_history")
    history = json.loads(raw or "[]")
    assert history[-1]["mime"] == "image/png"
    assert history[-1]["name"] == "scene-misty-chapel.png"
    assert hub.events[-1][1].kind == "media"
    # The image is room content, NOT attached to any character avatar.
    assert hub.events[-1][1].data.get("from") == "KP"


async def test_image_command_defaults_to_scene_kind(tmp_path):
    reset_imagegen_limiters()
    services = _services(tmp_path)
    router = CommandRouter(services)
    chat_key = "tui:group:img3"

    result = await router.dispatch(_keeper_ctx(chat_key), ".image misty chapel")

    assert "scene-misty-chapel.png" in result


async def test_image_command_empty_prompt_returns_usage(tmp_path):
    reset_imagegen_limiters()
    services = _services(tmp_path)
    router = CommandRouter(services)
    reply = await router.dispatch(_keeper_ctx("tui:group:img4"), ".image")

    assert reply == services.i18n.with_locale("en").t("commands.image.usage")


async def test_image_command_live_state_intent_uses_llm_to_expand_prompt(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a foggy chapel interior with a lone lantern", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img5"

    result = await router.dispatch(_keeper_ctx(chat_key), ".image 当前场景")

    # The LLM was called (an authoring-lane expansion), and the generated image used
    # the LLM's prompt, not the bare intent.
    assert len(llm.calls) == 1
    expected = image_name("scene", "a foggy chapel interior with a lone lantern")
    assert result == services.i18n.with_locale("en").t(
        "commands.image.generated",
        kind="scene",
        file=expected,
        hash=result.split("(")[-1][:12],
    )
    raw = await services.store.state_get(chat_key, "media_history")
    history = json.loads(raw or "[]")
    assert history[-1]["name"] == expected


async def test_image_command_plain_prompt_does_not_call_llm(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="unused", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)

    result = await router.dispatch(_keeper_ctx("tui:group:img6"), ".image misty chapel")

    # A plain description is passed through verbatim; the LLM is never consulted.
    assert len(llm.calls) == 0
    assert "scene-misty-chapel.png" in result


async def test_image_scene_bare_kind_expands_scene_intent(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a misty chapel interior", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img7"

    result = await router.dispatch(_keeper_ctx(chat_key), ".image scene")

    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    assert "当前场景" in sent
    assert "a-misty-chapel-interior.png" in result


async def test_image_portrait_bare_kind_expands_character_intent(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a robed detective portrait", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img8"

    result = await router.dispatch(_keeper_ctx(chat_key), ".image portrait")

    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    assert "当前角色" in sent
    assert "portrait-a-robed-detective-portrait.png" in result


async def test_image_combat_bare_kind_expands_combat_intent(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a tense hallway standoff", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img9"

    result = await router.dispatch(_keeper_ctx(chat_key), ".image combat")

    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    assert "当前战斗" in sent
    assert "combat-a-tense-hallway-standoff.png" in result


async def test_image_kind_with_extra_description_folds_into_intent(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a lighthouse in the fog", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img11"

    result = await router.dispatch(_keeper_ctx(chat_key), ".image scene 迷雾中的灯塔")

    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    assert "当前场景" in sent
    assert "迷雾中的灯塔" in sent
    assert "a-lighthouse-in-the-fog.png" in result
