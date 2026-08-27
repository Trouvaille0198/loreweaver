"""Command-level tests for the unified character-claim model (M10 rework).

A companion is a CLAIMED character: the roster (module-imported or `.pc gen`-born)
holds every claimable character, players claim with `.pc claim`, the AI claims with
`.party add` / the companion tools, and only an AI claim makes a companion. Release
takes the companion whole — record, sheet and roster marker.
"""

from __future__ import annotations

import pytest

from agent.context import AgentCtx
from agent.npc import companion_uid, list_companions
from agent.services import build_services
from core.character_manager import CharacterSheet
from core.pregen_roster import pregen_add, pregen_entries, pregen_pristine_sheet, slug_for
from gateway.commands import CommandRouter
from infra.config import ImageGenSettings, Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM

pytestmark = pytest.mark.asyncio


def _services(script=None):
    return build_services(
        Settings(imagegen=ImageGenSettings()), llm=FakeLLM(script=script or []), embeddings=FakeEmbeddings(8)
    )


def _keeper(chat_key: str) -> AgentCtx:
    return AgentCtx(chat_key=chat_key, user_id="kp", locale="en")


def _player(chat_key: str) -> AgentCtx:
    return AgentCtx(chat_key=chat_key, user_id="p1", platform="tui", locale="en", extra={"role": "player"})


async def test_pc_gen_creates_a_claimable_room_character_that_players_claim():
    from infra.llm import assistant_text

    services = _services(
        script=[
            # 1) the casting call: AI writes name + description + appearance
            assistant_text(
                '{"name": "阿岚", "description": "瘴雾镇的调查员，沉默寡言", '
                '"appearance": "四十岁上下的瘦高男子，花白短发，常穿灰布长衫，右颊一道旧疤"}'
            ),
            # 2) the sheet concept call: empty -> deterministic standard-array fallback
            assistant_text(""),
        ]
    )
    router = CommandRouter(services)
    chat_key = "cli:gen:roster"
    keeper = _keeper(chat_key)
    await services.documents.put(
        chat_key, "module_brief", "midsummer", {"name": "仲夏节", "description": "集市展台的狮鹫蛋不见了。"}
    )

    # Keeper-only: a player cannot grow the cast.
    assert "Keeper" in await router.dispatch(_player(chat_key), ".pc gen")

    result = await router.dispatch(keeper, ".pc gen 调查员")  # the hint rides the prompt
    assert "✅" in result and "阿岚" in result
    entries = await pregen_entries(services.documents, chat_key)
    (entry,) = entries
    assert entry["name"] == "阿岚"
    assert entry["source"] == "room"  # room-born, not module-imported
    assert entry["blurb"] == "瘴雾镇的调查员，沉默寡言"
    # The appearance rides the roster entry — the portrait lane folds it into the prompt.
    assert "花白短发" in entry["appearance"]
    assert entry["claimed_by"] == ""
    # The pristine sheet is real and rule-legal.
    pristine = await pregen_pristine_sheet(services.documents, chat_key, entry["id"])
    assert pristine is not None and pristine.system != ""

    # A player claims it like any module pregen.
    claimed = await router.dispatch(_player(chat_key), ".pc claim 阿岚")
    assert "You now play" in claimed
    assert (await pregen_entries(services.documents, chat_key))[0]["claimed_by"] == "p1"


async def test_party_add_claims_a_roster_character_and_release_takes_it_whole():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:claim:ai"
    keeper = _keeper(chat_key)
    await pregen_add(services.documents, chat_key, CharacterSheet("沈墨", "coc7"), source="card", blurb="客栈的老板娘")

    added = await router.dispatch(keeper, ".party add 沈墨 谨慎辅助")
    assert "✅" in added and "沈墨" in added
    (companion,) = await list_companions(services.documents, chat_key)
    assert companion.name == "沈墨"
    assert companion.playstyle == "谨慎辅助"
    assert companion.pregen_id == slug_for("沈墨")
    entry = (await pregen_entries(services.documents, chat_key))[0]
    assert entry["claimed_by"] == companion.id
    assert entry["claimed_by_kind"] == "ai"
    # The sheet copy materialized under the companion's virtual uid.
    assert [c["name"] for c in await services.characters.list_characters(companion_uid(companion.id), chat_key)] == ["沈墨"]

    # Idempotent: re-claiming is "yours", not a duplicate.
    again = await router.dispatch(keeper, ".party add 沈墨")
    assert "✅" in again
    assert len(await list_companions(services.documents, chat_key)) == 1

    # A player cannot take the AI's claim.
    assert "already claimed" in await router.dispatch(_player(chat_key), ".pc claim 沈墨")

    # The keeper's `.pc release` on an AI claim releases it WHOLE: record gone, sheet
    # gone, marker cleared — the character is claimable again.
    released = await router.dispatch(keeper, ".pc release 沈墨")
    assert "Released" in released
    assert await list_companions(services.documents, chat_key) == []
    assert await services.characters.list_characters(companion_uid(companion.id), chat_key) == []
    entry = (await pregen_entries(services.documents, chat_key))[0]
    assert entry["claimed_by"] == ""
    assert entry["claimed_by_kind"] == ""
    assert "You now play" in await router.dispatch(_player(chat_key), ".pc claim 沈墨")


async def test_ai_cannot_claim_a_player_claimed_or_missing_character():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:claim:conflict"
    keeper = _keeper(chat_key)
    await pregen_add(services.documents, chat_key, CharacterSheet("白苏", "coc7"), source="card")

    # Player claims it first.
    assert "You now play" in await router.dispatch(_player(chat_key), ".pc claim 白苏")
    # The AI claim is refused — the seat is taken.
    refused = await router.dispatch(keeper, ".party add 白苏")
    assert refused.startswith("❌") and "白苏" in refused
    assert await list_companions(services.documents, chat_key) == []
    # And a name that is on no roster at all is refused too.
    assert "❌" in await router.dispatch(keeper, ".party add 不存在的角色")


async def test_pc_gen_reports_when_the_ai_cannot_name_the_character():
    from infra.llm import assistant_text

    services = _services(
        script=[
            assistant_text("not json at all"),
            assistant_text(""),
        ]
    )
    router = CommandRouter(services)
    chat_key = "cli:gen:noname"
    await services.documents.put(
        chat_key, "module_brief", "midsummer", {"name": "仲夏节", "description": "集市展台的狮鹫蛋不见了。"}
    )
    result = await router.dispatch(_keeper(chat_key), ".pc gen")
    assert "❌" in result and "name" in result
    assert await pregen_entries(services.documents, chat_key) == []


async def test_pc_gen_refuses_without_an_initialized_adventure():
    """No module summary -> the AI is never asked (it would only invent a
    placeholder); the command refuses loudly instead."""
    services = _services(script=[])
    router = CommandRouter(services)
    chat_key = "cli:gen:nomodule"
    result = await router.dispatch(_keeper(chat_key), ".pc gen")
    assert "❌" in result and "module" in result
    assert await pregen_entries(services.documents, chat_key) == []


async def test_pc_delete_removes_only_room_born_unclaimed_characters():
    services = _services()
    router = CommandRouter(services)
    chat_key = "cli:delete:room"
    keeper = _keeper(chat_key)
    # A room-born character and a module-imported one.
    await pregen_add(services.documents, chat_key, CharacterSheet("阿岚", "coc7"), source="room", blurb="瘴雾镇的调查员")
    await pregen_add(
        services.documents, chat_key, CharacterSheet("沈墨", "coc7"), source="pack:mod@1:cards/x.lorecard.json"
    )
    # A room-born character a player has claimed.
    await pregen_add(services.documents, chat_key, CharacterSheet("白苏", "coc7"), source="room")
    from core.pregen_roster import pregen_claim

    assert (await pregen_claim(services.documents, chat_key, "白苏", "p1", services.characters))[0] == "ok"

    # Players cannot delete at all.
    assert "Keeper" in await router.dispatch(_player(chat_key), ".pc delete 阿岚")

    # A module-imported cast member is the module's asset — refused.
    refused = await router.dispatch(keeper, ".pc delete 沈墨")
    assert "❌" in refused and "沈墨" in refused

    # A claimed character is someone's seat — refused.
    refused2 = await router.dispatch(keeper, ".pc delete 白苏")
    assert "❌" in refused2 and "白苏" in refused2

    # The room-born, unclaimed character deletes cleanly.
    done = await router.dispatch(keeper, ".pc delete 阿岚")
    assert "✅" in done and "阿岚" in done
    names = [e["name"] for e in await pregen_entries(services.documents, chat_key)]
    assert "阿岚" not in names and "沈墨" in names and "白苏" in names

    # Unknown names refuse.
    assert "❌" in await router.dispatch(keeper, ".pc delete 不存在")
