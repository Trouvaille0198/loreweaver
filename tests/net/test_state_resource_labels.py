"""M19 item 8: `sheet.resources` labels resolve to the VIEWER's locale.

A pack's resource labels used to be one frozen string, so a bar authored in Chinese
read 潮位 to an English player and vice versa. Labels are now declared per locale and
resolved at the wire boundary — the same room, the same character, two connections,
two readings. The party roster additionally persists its meters at sync time, so its
stored label must NOT be what ships either.
"""

from __future__ import annotations

from pathlib import Path

from agent.context import AgentCtx
from agent.services import build_services
from core import rulepacks as rulepacks_module
from core.character_manager import resource_label_map
from gateway.session import SessionSource
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM
from net.state import build_room_state

# A minimal self-contained system whose one meter is authored in two languages.
CHAOZHAN_YAML = """\
names: [chaozhan-fixture]
defaults:
  潮感: 50
sheet:
  label: Tide-reader
  attributes: {CHAO: 4, CHAOMAX: 9}
  resources:
    - {id: chao, label: {en: Tide, zh: 潮位}, value: CHAO, max: CHAOMAX}
    - {id: ledger, label: Ledger, value: CHAO, max: CHAOMAX}
"""


def _services():
    return build_services(Settings(locale="en"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(64))


def _ctx(room: str, *, user_id: str, locale: str) -> AgentCtx:
    chat_key = SessionSource(platform="tui", chat_type="group", chat_id=room).chat_key()
    return AgentCtx(chat_key=chat_key, user_id=user_id, platform="tui", locale=locale)


def _install_fixture_pack(tmp_path: Path) -> None:
    (tmp_path / "chaozhan-fixture.yaml").write_text(CHAOZHAN_YAML, encoding="utf-8")
    rulepacks_module._USER_RULEPACK_DIR = tmp_path
    rulepacks_module.reload_rulepacks()


def _restore_packs() -> None:
    rulepacks_module._USER_RULEPACK_DIR = None
    rulepacks_module.reload_rulepacks()


async def test_two_viewers_of_one_room_read_their_own_labels(tmp_path: Path) -> None:
    _install_fixture_pack(tmp_path)
    try:
        services = _services()
        zh_ctx = _ctx("labels", user_id="tui:zh", locale="zh")
        en_ctx = _ctx("labels", user_id="tui:en", locale="en")
        # One character EACH: sheets are room-scoped documents keyed by the character
        # name, and a name another player owns is refused (`CharacterNameTakenError`).
        # Two viewers of one room is the point here, not two owners of one sheet.
        zh_sheet = services.characters.generate_character("chaozhan-fixture", "顾晚棠")
        en_sheet = services.characters.generate_character("chaozhan-fixture", "Wan Tang")
        await services.characters.save_character(zh_ctx.user_id, zh_ctx.chat_key, zh_sheet)
        await services.characters.save_character(en_ctx.user_id, en_ctx.chat_key, en_sheet)

        zh_state = await build_room_state(services, zh_ctx)
        en_state = await build_room_state(services, en_ctx)

        zh_labels = {res["id"]: res["label"] for res in zh_state["character"]["resources"]}
        en_labels = {res["id"]: res["label"] for res in en_state["character"]["resources"]}
        assert zh_labels["chao"] == "潮位" and en_labels["chao"] == "Tide"
        # A single-language label is not a bug to route around: both viewers read it.
        assert zh_labels["ledger"] == en_labels["ledger"] == "Ledger"

        # The party roster stored ONE label at sync time; the wire re-labels anyway.
        zh_party = {res["id"]: res["label"] for res in zh_state["party"][0]["resources"]}
        en_party = {res["id"]: res["label"] for res in en_state["party"][0]["resources"]}
        assert zh_party["chao"] == "潮位" and en_party["chao"] == "Tide"
    finally:
        _restore_packs()


def test_runtime_pool_labels_resolve_per_viewer_locale() -> None:
    # dnd5e's meters are runtime-declared pools; the roster re-label lane reads
    # them too, grouped or not.
    zh = resource_label_map("dnd5e", "zh")
    en = resource_label_map("dnd5e", "en")
    assert zh["hp"] == "生命值" and zh["temp_hp"] == "临时生命值"
    assert en["hp"] == "HP" and en["temp_hp"] == "Temporary HP"
    assert zh["hit_die_d10"] == "生命骰" and en["spell_slot_1"] == "Level 1 Spell Slots"


async def test_runtime_pack_state_has_no_duplicate_top_level_meters() -> None:
    """A runtime pack's own sheet declares NO legacy `sheet.resources` on top of
    its pools: the state frame carries each top-level meter exactly once, the
    ungrouped pools ride `resources` (not a blank group), and grouped pools stay
    in `resource_groups`."""
    services = _services()
    ctx = _ctx("dnd-pools", user_id="tui:dnd", locale="zh")
    sheet = services.characters.generate_character("dnd5e", "Kael Thorn")
    await services.characters.save_character(ctx.user_id, ctx.chat_key, sheet)

    state = await build_room_state(services, ctx)
    char = state["character"]
    ids = [res["id"] for res in char["resources"]]
    assert ids == ["hp", "temp_hp"]  # each exactly once — no legacy duplicate
    assert {res["label"] for res in char["resources"]} == {"生命值", "临时生命值"}
    group_ids = [group["id"] for group in char.get("resource_groups", [])]
    assert set(group_ids) == {"hit_dice", "spell_slots"}
    grouped_ids = {item["id"] for group in char.get("resource_groups", []) for item in group["resources"]}
    assert grouped_ids.isdisjoint(ids)
