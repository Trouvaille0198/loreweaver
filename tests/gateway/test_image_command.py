"""`.image` command — Keeper-only room image handout generation."""

import asyncio
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


async def _settle(router: CommandRouter) -> None:
    """Wait for the dispatch's background `.image` task to finish.

    `.image` generation now runs OUTSIDE the room's turn lock as a tracked background
    task (the command itself returns immediately). FakeImageGen/FakeLLM complete
    without real IO, so the tracked-task set drains within a few event-loop ticks;
    poll it instead of assuming timing.
    """
    tasks = getattr(router, "_image_background_tasks", None)
    for _ in range(200):
        if not tasks:
            return
        await asyncio.sleep(0)
    raise AssertionError("background image task did not settle")


def _assert_started(services, text: str) -> None:
    assert text == services.i18n.with_locale("en").t("commands.image.started"), text


async def _last_image_name(services, chat_key: str) -> str:
    raw = await services.store.state_get(chat_key, "media_history")
    history = json.loads(raw or "[]")
    assert history, "no media frame was published"
    return history[-1]["name"]


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

    # The command returns immediately; the minutes-long generation runs in the
    # background so the room's turn lock is not held for the whole stretch.
    _assert_started(services, result)
    await _settle(router)
    raw = await services.store.state_get(chat_key, "media_history")
    history = json.loads(raw or "[]")
    assert history[-1]["mime"] == "image/png"
    assert history[-1]["name"] == "scene-misty-chapel.png"
    # The generation prompt rides along for hover/audit. With no LLM script the
    # expand falls back to the raw prompt ("当前场景 misty chapel").
    assert history[-1].get("prompt") == "当前场景 misty chapel"
    media_events = [event for _, event, _ in hub.events if event.kind == "media"]
    assert media_events and media_events[-1].data.get("prompt") == "当前场景 misty chapel"
    # The media frame is room content, NOT attached to any character avatar.
    assert media_events[-1].data.get("from") == "KP"
    # A system event retires the "Generating…" spinner line.
    spinner_retired = [
        event for _, event, _ in hub.events
        if event.kind == "system" and event.data.get("spinner") is False
    ]
    assert spinner_retired


async def test_image_command_defaults_to_scene_kind(tmp_path):
    reset_imagegen_limiters()
    services = _services(tmp_path)
    router = CommandRouter(services)
    chat_key = "tui:group:img3"

    result = await router.dispatch(_keeper_ctx(chat_key), ".image misty chapel")

    _assert_started(services, result)
    await _settle(router)
    assert await _last_image_name(services, chat_key) == "scene-misty-chapel.png"


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

    _assert_started(services, result)
    await _settle(router)
    # The LLM was called (an authoring-lane expansion), and the generated image used
    # the LLM's prompt, not the bare intent.
    assert len(llm.calls) == 1
    expected = image_name("scene", "a foggy chapel interior with a lone lantern")
    assert await _last_image_name(services, chat_key) == expected


async def test_image_command_plain_prompt_does_not_call_llm(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="unused", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)

    result = await router.dispatch(_keeper_ctx("tui:group:img6"), ".image misty chapel")

    _assert_started(services, result)
    await _settle(router)
    # A plain description is passed through verbatim; the LLM is never consulted.
    assert len(llm.calls) == 0
    assert await _last_image_name(services, "tui:group:img6") == "scene-misty-chapel.png"


async def test_image_scene_bare_kind_expands_scene_intent(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a misty chapel interior", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img7"

    result = await router.dispatch(_keeper_ctx(chat_key), ".image scene")

    _assert_started(services, result)
    await _settle(router)
    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    assert "当前场景" in sent
    assert await _last_image_name(services, chat_key) == "scene-a-misty-chapel-interior.png"


async def test_image_portrait_bare_kind_expands_character_intent(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a robed detective portrait", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img8"

    result = await router.dispatch(_keeper_ctx(chat_key), ".image portrait")

    _assert_started(services, result)
    await _settle(router)
    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    assert "当前角色" in sent
    assert await _last_image_name(services, chat_key) == "portrait-a-robed-detective-portrait.png"


async def test_image_combat_bare_kind_expands_combat_intent(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a tense hallway standoff", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img9"

    result = await router.dispatch(_keeper_ctx(chat_key), ".image combat")

    _assert_started(services, result)
    await _settle(router)
    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    assert "当前战斗" in sent
    assert await _last_image_name(services, chat_key) == "combat-a-tense-hallway-standoff.png"


async def test_image_kind_with_extra_description_folds_into_intent(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a lighthouse in the fog", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img11"

    result = await router.dispatch(_keeper_ctx(chat_key), ".image scene 迷雾中的灯塔")

    _assert_started(services, result)
    await _settle(router)
    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    assert "当前场景" in sent
    assert "迷雾中的灯塔" in sent
    assert await _last_image_name(services, chat_key) == "scene-a-lighthouse-in-the-fog.png"


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

    result = await router.dispatch(_keeper_ctx(chat_key), ".image scene")

    _assert_started(services, result)
    await _settle(router)
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

    _assert_started(services, result)
    await _settle(router)
    assert len(llm.calls) == 1
    sent = llm.calls[0][0][-1]["content"]
    assert "昏黄的灯下" in sent
    assert await _last_image_name(services, chat_key) == "scene-a-rain-soaked-riverside-door.png"


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

    result = await router.dispatch(_keeper_ctx(chat_key), ".image clue")

    _assert_started(services, result)
    await _settle(router)
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

    result = await router.dispatch(_keeper_ctx(chat_key), ".image clue 玉蟾")

    _assert_started(services, result)
    await _settle(router)
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

    result = await router.dispatch(_keeper_ctx(chat_key), ".image portrait 老周")

    _assert_started(services, result)
    await _settle(router)
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

    result = await router.dispatch(_keeper_ctx(chat_key), ".image scene")

    _assert_started(services, result)
    await _settle(router)
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

    result = await router.dispatch(_keeper_ctx(chat_key), ".image scene")

    _assert_started(services, result)
    await _settle(router)
    assert len(services.imagegen.calls) == 1
    # A portrait-only provider must NOT send a scene illustration as a reference — the
    # caller gates by `reference_kinds`.
    assert services.imagegen.calls[0]["reference"] == "0"


async def test_image_scene_reference_scoped_to_active_module(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a misty harbor", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img21"
    from core.documents import DocumentStore

    player_pool = {
        "scenes": [{"name": "码头", "focus": "探索", "description": "雾中的码头。", "npcs_present": [], "clues": []}],
        "npcs": [],
        "clues": [],
        "background": "",
        "summary": "",
    }
    await DocumentStore(services.store).put_singleton(chat_key, "module_pool", {"keeper": {}, "player": player_pool}, source="test")

    # The index accumulates EVERY module this room ever ran (append-only, never purged
    # on module switch): a stale module's scene art AND the current module's both live
    # here. Only the current module's illustration may anchor the reference.
    store = MediaStore(services.store, str(tmp_path), allowed_mimes=ALLOWED_IMAGE_MIMES)
    stale_blob = b"\x89PNG\r\n\x1a\n" + b"stale-scene" * 3
    stale = await store.register_blob(room=chat_key, data=stale_blob, mime="image/png", name="module-stale-scenes-2.png", uploader="keeper")
    current_blob = b"\x89PNG\r\n\x1a\n" + b"current-scene" * 10
    current = await store.register_blob(room=chat_key, data=current_blob, mime="image/png", name="module-current-scenes-2.png", uploader="keeper")
    await services.store.state_set(
        chat_key,
        "module_media_index",
        json.dumps(
            [
                {"kind": "scenes", "subject": "旧场景", "hash": stale.hash, "name": stale.name},
                {"kind": "scenes", "subject": "新场景", "hash": current.hash, "name": current.name},
            ]
        ),
    )
    await services.store.state_set(chat_key, "active_module", json.dumps({"pack_id": "current", "name": "x"}))

    result = await router.dispatch(_keeper_ctx(chat_key), ".image scene")

    _assert_started(services, result)
    await _settle(router)
    assert len(services.imagegen.calls) == 1
    # The reference bytes are the CURRENT module's illustration (distinct lengths), not
    # the stale one that happens to come earlier in the append-only index.
    assert services.imagegen.calls[0]["reference"] == str(len(current_blob))


async def test_image_scene_reference_without_active_module_keeps_index_order(tmp_path):
    reset_imagegen_limiters()
    llm = FakeLLM(responder=lambda _m, _t: ChatResult(content="a misty harbor", tool_calls=[]))
    services = _services(tmp_path, llm=llm)
    router = CommandRouter(services)
    chat_key = "tui:group:img22"
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
    first_blob = b"\x89PNG\r\n\x1a\n" + b"first-scene" * 3
    first = await store.register_blob(room=chat_key, data=first_blob, mime="image/png", name="module-first-scenes-2.png", uploader="keeper")
    last_blob = b"\x89PNG\r\n\x1a\n" + b"last-scene" * 10
    last = await store.register_blob(room=chat_key, data=last_blob, mime="image/png", name="module-last-scenes-2.png", uploader="keeper")
    await services.store.state_set(
        chat_key,
        "module_media_index",
        json.dumps(
            [
                {"kind": "scenes", "subject": "首个", "hash": first.hash, "name": first.name},
                {"kind": "scenes", "subject": "末尾", "hash": last.hash, "name": last.name},
            ]
        ),
    )
    # No active_module: no pack id to scope by, so the pre-existing "most recent" pick
    # (the LAST entry in the store's stable hash order) must keep working unchanged.
    await services.store.state_set(chat_key, "active_module", "")

    result = await router.dispatch(_keeper_ctx(chat_key), ".image scene")

    _assert_started(services, result)
    await _settle(router)
    assert len(services.imagegen.calls) == 1
    records = await store.list_room_records(chat_key)
    expected = max(records, key=lambda r: r.hash)
    assert services.imagegen.calls[0]["reference"] == str(expected.size)
