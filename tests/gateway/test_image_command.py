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
from infra.media_store import ALLOWED_IMAGE_MIMES, MediaStore


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
    # The generation prompt rides along for hover/audit. With no LLM script the
    # expand falls back to the raw prompt ("当前场景 misty chapel").
    assert history[-1].get("prompt") == "当前场景 misty chapel"
    assert hub.events[-2][1].kind == "media"
    # The trailing event retires the "Generating…" spinner line.
    assert hub.events[-1][1].kind == "system"
    assert hub.events[-1][1].data.get("spinner") is False
    # The image is room content, NOT attached to any character avatar.
    assert hub.events[-2][1].data.get("from") == "KP"
    assert hub.events[-2][1].data.get("prompt") == "当前场景 misty chapel"


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


async def test_image_scene_includes_knowledge_pool_material_in_prompt(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a rain-soaked warehouse lobby", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img12"
    # Seed a player-visible scene description into the module knowledge pool.
    from core.documents import MODULE_POOL_ID
    from infra.store import Store

    player_pool = {
        "scenes": [
            {
                "name": "观澜阁正门",
                "focus": "探索",
                "description": "被夜色浸透的老仓库，铁门虚掩，前院堆着防雨布盖住的木箱。",
                "npcs_present": ["阿蔚"],
                "clues": [],
            }
        ],
        "npcs": [
            {"name": "阿蔚", "description": "绑低马尾，戴细框眼镜，穿水蓝色冲锋衣。", "role": "client"}
        ],
        "background": "梅雨中的上海老仓库。",
        "summary": "调查废弃仓库。",
    }
    store = services.store
    docs = __import__("core.documents", fromlist=["DocumentStore"]).DocumentStore(store)
    await docs.put_singleton(
        chat_key, "module_pool", {"keeper": {}, "player": player_pool}, source="test"
    )

    await router.dispatch(_keeper_ctx(chat_key), ".image scene")

    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    # The LLM was handed the scene's visual description, not just a name.
    assert "被夜色浸透" in sent
    assert "水蓝色冲锋衣" in sent


async def test_image_last_uses_most_recent_keeper_text(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a rain-soaked riverside door", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img13"
    from agent.history import DEFAULT_HISTORY_KEY, append_message

    await append_message(
        services, chat_key, DEFAULT_HISTORY_KEY,
        role="assistant", content="昏黄的灯下，苏州河在雨里缓缓喘息，一扇铁门虚掩。", turn=1,
    )

    result = await router.dispatch(_keeper_ctx(chat_key), ".image last")

    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    assert "昏黄的灯下" in sent
    assert "a-rain-soaked-riverside-door.png" in result


async def test_image_last_without_history_returns_usage(tmp_path):
    reset_imagegen_limiters()
    services = _services(tmp_path)
    router = CommandRouter(services)

    reply = await router.dispatch(_keeper_ctx("tui:group:img14"), ".image last")

    assert reply == services.i18n.with_locale("en").t("commands.image.no_recent_text")


async def test_image_clue_includes_clues_material_in_prompt(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a weathered jade toad amulet", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img15"
    from core.documents import MODULE_POOL_ID, DocumentStore

    player_pool = {
        "scenes": [{"name": "展厅", "focus": "探索", "description": "大跨度展厅，玻璃柜台内放瓷碗银器。", "npcs_present": [], "clues": []}],
        "npcs": [],
        "clues": [
            {"name": "青白玉蟾", "description": "巴掌大的青白玉蟾，腹底有暗褐色沁色，颈系褪色红绳。", "location": "案桌中央", "leads_to": "镇水之物"}
        ],
        "background": "",
        "summary": "",
    }
    docs = DocumentStore(services.store)
    await docs.put_singleton(chat_key, "module_pool", {"keeper": {}, "player": player_pool}, source="test")

    await router.dispatch(_keeper_ctx(chat_key), ".image clue")

    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    # The clue's own description (from the clues pool) reached the LLM.
    assert "青白玉蟾" in sent
    assert "暗褐色沁色" in sent


async def test_image_clue_focus_filters_material(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a jade toad amulet closeup", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img16"
    from core.documents import DocumentStore

    player_pool = {
        "scenes": [{"name": "展厅", "focus": "探索", "description": "玻璃柜台内放瓷碗银器，案桌中央一只樟木匣内卧着青白玉蟾。", "npcs_present": [], "clues": []}],
        "npcs": [],
        "clues": [
            {"name": "青白玉蟾", "description": "巴掌大青白玉蟾，腹底暗褐色沁色。", "location": "案桌", "leads_to": "镇水之物"},
            {"name": "访客登记簿", "description": "写着徐建国的名字。", "location": "桌上", "leads_to": "行踪"},
        ],
        "background": "",
        "summary": "",
    }
    docs = DocumentStore(services.store)
    await docs.put_singleton(chat_key, "module_pool", {"keeper": {}, "player": player_pool}, source="test")

    await router.dispatch(_keeper_ctx(chat_key), ".image clue 玉蟾")

    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    # The clue FOCUS reaches the LLM as part of the intent.
    assert "当前线索" in sent
    assert "玉蟾" in sent
    # The focused clue's own description is present in the material.
    assert "青白玉蟾" in sent


async def test_image_names_from_knowledge_pool(tmp_path):
    reset_imagegen_limiters()
    services = _services(tmp_path)
    chat_key = "tui:group:img17"
    from core.documents import DocumentStore

    player_pool = {
        "scenes": [],
        "npcs": [{"name": "阿蔚", "description": "低马尾", "role": "client"}, {"name": "徐建国", "description": "矮壮", "role": "antagonist"}],
        "clues": [{"name": "青白玉蟾", "description": "镇水之物", "location": "案桌", "leads_to": "x"}, {"name": "访客登记簿", "description": "行踪", "location": "桌", "leads_to": "y"}],
        "background": "",
        "summary": "",
    }
    await DocumentStore(services.store).put_singleton(chat_key, "module_pool", {"keeper": {}, "player": player_pool}, source="test")

    from net.state import _image_names

    names = await _image_names(services, chat_key)
    assert names == {"npcs": ["阿蔚", "徐建国"], "clues": ["青白玉蟾", "访客登记簿"]}


async def test_image_portrait_reuses_module_illustration_as_reference(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a portrait of Lao Zhou", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img18"
    from core.documents import DocumentStore

    player_pool = {
        "scenes": [],
        "npcs": [{"name": "老周", "description": "驼背，旧式长衫", "role": "gatekeeper"}],
        "clues": [],
        "background": "",
        "summary": "",
    }
    await DocumentStore(services.store).put_singleton(chat_key, "module_pool", {"keeper": {}, "player": player_pool}, source="test")

    # A module illustration of 老周 lives in the media store and is indexed by subject.
    store = MediaStore(services.store, str(tmp_path), allowed_mimes=ALLOWED_IMAGE_MIMES)
    blob = b"\x89PNG\r\n\x1a\n" + b"lao-zhou-portrait" * 10
    record = await store.register_blob(room=chat_key, data=blob, mime="image/png", name="module-mystery-npcs-1.png", uploader="keeper")
    await services.store.state_set(
        chat_key,
        "module_media_index",
        json.dumps([{"kind": "npcs", "subject": "老周", "hash": record.hash, "name": record.name}]),
    )

    await router.dispatch(_keeper_ctx(chat_key), ".image portrait 老周")

    assert len(services.imagegen.calls) == 1
    # The 老周 illustration rode along as the generation reference.
    assert services.imagegen.calls[0]["reference"] != "0"
    # Portrait references want character consistency — NO face-extraction hint.
    assert "do NOT extract or reproduce any person or face" not in services.imagegen.calls[0]["prompt"]


async def test_image_scene_reuses_recent_illustration_as_reference(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a misty harbor", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img19"
    from core.documents import DocumentStore

    player_pool = {
        "scenes": [{"name": "码头", "focus": "探索", "description": "雾中的码头。", "npcs_present": [], "clues": []}],
        "npcs": [],
        "clues": [],
        "background": "",
        "summary": "",
    }
    await DocumentStore(services.store).put_singleton(chat_key, "module_pool", {"keeper": {}, "player": player_pool}, source="test")

    store = MediaStore(services.store, str(tmp_path), allowed_mimes=ALLOWED_IMAGE_MIMES)
    blob = b"\x89PNG\r\n\x1a\n" + b"harbor-scene" * 10
    record = await store.register_blob(room=chat_key, data=blob, mime="image/png", name="module-mystery-scenes-2.png", uploader="keeper")
    await services.store.state_set(
        chat_key,
        "module_media_index",
        json.dumps([{"kind": "scenes", "subject": "码头", "hash": record.hash, "name": record.name}]),
    )

    await router.dispatch(_keeper_ctx(chat_key), ".image scene")

    assert len(services.imagegen.calls) == 1
    # FakeImageGen anchors every kind, so a bare scene request reuses the most recent
    # scene illustration as reference.
    assert services.imagegen.calls[0]["reference"] != "0"
    # A scene reference carries the no-face-extraction hint so the provider keeps only
    # style/atmosphere (a person in the reference must not become the subject).
    assert "do NOT extract or reproduce any person or face" in services.imagegen.calls[0]["prompt"]


async def test_image_scene_does_not_reference_when_provider_only_anchors_portraits(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a misty harbor", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    # A provider that only accepts portrait references: scene must NOT carry one.
    services.imagegen.reference_kinds = frozenset({"portrait"})
    router = CommandRouter(services)
    chat_key = "tui:group:img20"
    from core.documents import DocumentStore

    player_pool = {
        "scenes": [{"name": "码头", "focus": "探索", "description": "雾中的码头。", "npcs_present": [], "clues": []}],
        "npcs": [],
        "clues": [],
        "background": "",
        "summary": "",
    }
    await DocumentStore(services.store).put_singleton(chat_key, "module_pool", {"keeper": {}, "player": player_pool}, source="test")

    store = MediaStore(services.store, str(tmp_path), allowed_mimes=ALLOWED_IMAGE_MIMES)
    blob = b"\x89PNG\r\n\x1a\n" + b"harbor-scene" * 10
    record = await store.register_blob(room=chat_key, data=blob, mime="image/png", name="module-mystery-scenes-2.png", uploader="keeper")
    await services.store.state_set(
        chat_key,
        "module_media_index",
        json.dumps([{"kind": "scenes", "subject": "码头", "hash": record.hash, "name": record.name}]),
    )

    await router.dispatch(_keeper_ctx(chat_key), ".image scene")

    assert len(services.imagegen.calls) == 1
    # A portrait-only provider must NOT send a scene illustration as a reference — the
    # caller gates by `reference_kinds`.
    assert services.imagegen.calls[0]["reference"] == "0"
