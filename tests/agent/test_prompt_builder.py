"""Tests for agent.prompt_builder.build_system_prompt: assembling the
core.prompt_sections builders (per docs/specs/M1.md §6.4) into the full
AI-KP system prompt for one turn, through the real `build_services` wiring
(FakeLLM/FakeEmbeddings keep everything offline and deterministic).
"""

from __future__ import annotations

import json

from agent.context import AgentCtx
from agent.prompt_builder import build_system_prompt, build_system_prompt_parts
from agent.services import build_services
from core.modvars import build_spec, define_modvar, set_modvar
from core.prompt_sections import (
    inject_document_context_prompt,
    inject_game_state_prompt,
    inject_interaction_style_prompt,
    inject_system_expertise_prompt,
    inject_trpg_system_prompt,
)
from core.relationships import RelationshipManager
from core.rulepacks import load_rulepack
from core.worldbook import inject_world_lore_prompt
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM

SENTINEL_SECRET = "SENTINEL_ONLY_THE_HARBORMASTER_KNOWS"


def _services(locale: str = "en"):
    settings = Settings(locale=locale)
    return build_services(settings, llm=FakeLLM(), embeddings=FakeEmbeddings(64))


async def _seed_ready_keeper_pool(services, chat_key: str) -> None:
    await services.store.state_set(chat_key, "module_init_status", "ready")
    await services.documents.put_singleton(
        chat_key,
        "module_pool",
        {
            "keeper": {
                "summary": "A quiet fishing town hides a cult beneath the lighthouse.",
                "truths": [{"name": "The Truth", "description": SENTINEL_SECRET}],
            },
            "player": {"summary": "A quiet fishing town."},
        },
    )


async def test_build_system_prompt_includes_keeper_discipline_and_joins_all_sections_in_order():
    services = _services("en")
    chat_key = "chat-prompt-builder"
    ctx = AgentCtx(chat_key=chat_key, user_id="u1", locale="en")

    await _seed_ready_keeper_pool(services, chat_key)

    prompt = await build_system_prompt(ctx, services)
    i18n = services.i18n.with_locale("en")

    # Red-line: the localized keeper-secrecy discipline block must be present
    # (it rides in via inject_document_context_prompt whenever a ready
    # keeper pool exists), and so — for reasoning purposes — must the secret
    # itself (the discipline text is what prevents it leaking into OUTPUT,
    # not its absence from the prompt).
    assert i18n.t("prompt.keeper_discipline") in prompt
    assert SENTINEL_SECRET in prompt

    # Every section contributed non-empty content, in the STABLE HEAD -> VOLATILE
    # TAIL order (P1, M1 §6.4 revision): identity, expertise, style and the module
    # pool are the room's configuration and lead; live game state follows, because
    # it is what changes every turn.
    markers = [
        i18n.t("prompt.system.intro"),  # trpg_system        \
        load_rulepack("coc7").expertise_text("en"),  # system_expertise  > stable head
        i18n.t("prompt.style.narrative"),  # interaction_style       |
        i18n.t("prompt.document.pool_title"),  # document_context    /
        i18n.t("prompt.game_state.title"),  # game_state         > volatile tail
    ]
    positions = [prompt.index(marker) for marker in markers]
    assert positions == sorted(positions), "sections must appear in the fixed §6.4 order"

    # Consecutive non-empty sections are joined by a blank line.
    assert "\n\n" in prompt


async def test_build_system_prompt_pins_silent_player_boundary_in_attribution():
    """A speaking player's declared action never lands on a present-but-silent
    character: the attribution block must carry the boundary in BOTH locales, so
    the model neither decides a silent player's action nor throws the decision
    at them (observed failure: the KP kept asking the unspoken 白苏 to act)."""
    for locale, needle in (
        ("en", "whose player has NOT spoken this scene"),
        ("zh", "这一场景还没有发言的玩家角色"),
    ):
        services = _services(locale)
        ctx = AgentCtx(chat_key=f"chat-attribution-{locale}", user_id="u1", locale=locale)
        prompt = await build_system_prompt(ctx, services)
        assert needle in prompt, f"{locale}: silent-player clause missing from attribution"
        # The freshness block no longer invites a spoken line for silent characters.
        assert "descriptive beat" in prompt if locale == "en" else "描写性的片段" in prompt


async def test_build_system_prompt_is_localized_per_ctx_locale():
    services = _services("en")  # process-wide default is en; ctx below asks for zh
    chat_key = "chat-prompt-builder-zh"
    ctx = AgentCtx(chat_key=chat_key, user_id="u1", locale="zh")

    await _seed_ready_keeper_pool(services, chat_key)

    prompt = await build_system_prompt(ctx, services)
    zh = services.i18n.with_locale("zh")

    assert zh.t("prompt.keeper_discipline") in prompt
    assert zh.t("prompt.game_state.title") in prompt
    assert SENTINEL_SECRET in prompt


async def test_room_system_drives_expertise_before_any_character_exists():
    services = _services("en")
    chat_key = "world-card-system-prompt"
    ctx = AgentCtx(chat_key=chat_key, user_id="first-player", locale="en")
    await services.store.state_set(chat_key, "room_system", "dnd5e")

    prompt = await build_system_prompt(ctx, services)

    assert load_rulepack("dnd5e").expertise_text("en") in prompt
    assert load_rulepack("coc7").expertise_text("en") not in prompt


async def test_build_system_prompt_filters_party_to_active_character_system():
    services = _services("en")
    chat_key = "chat-mixed-system-prompt"
    ctx = AgentCtx(chat_key=chat_key, user_id="dnd-player", locale="en")
    coc = services.characters.generate_character("coc7", "Nora Vance")
    dnd = services.characters.generate_character("dnd5e", "Kael Thorn")
    await services.characters.save_character("coc-player", chat_key, coc)
    await services.characters.save_character(ctx.user_id, chat_key, dnd)

    prompt = await build_system_prompt(ctx, services)

    assert "Kael Thorn" in prompt
    assert "Nora Vance" not in prompt
    roster = await services.characters.get_party_roster(chat_key)
    assert {member["name"] for member in roster} == {"Nora Vance", "Kael Thorn"}


async def test_build_system_prompt_survives_a_brand_new_chat_with_no_seeded_state():
    services = _services("en")
    ctx = AgentCtx(chat_key="chat-prompt-builder-empty", user_id="u1", locale="en")

    prompt = await build_system_prompt(ctx, services)

    # No prior session, no module pool: the always-on framing sections still
    # render, and there is no keeper-discipline block to leak in.
    i18n = services.i18n.with_locale("en")
    assert prompt
    assert i18n.t("prompt.game_state.title") in prompt
    assert i18n.t("prompt.style.narrative") in prompt
    assert i18n.t("prompt.keeper_discipline") not in prompt


# ---------------------------------------------------------------------------
# Deterministic relationship tracks (好感/情欲, core.relationships) fold-in --
# the last section, read straight off the store like the skills block above.
# ---------------------------------------------------------------------------


async def test_build_system_prompt_with_no_relationship_state_is_byte_identical_to_before():
    """CRITICAL INVARIANT: a chat with no relationship tracks ever set must assemble EXACTLY the
    prompt the plain section list produces -- the fold-in contributes nothing at all, not even an
    empty header, when the room's relationship state is empty. (Re-expressed against the P1
    stable-head/volatile-tail order; the invariant itself is unchanged.)"""
    services = _services("en")
    chat_key = "chat-prompt-builder-no-relationships"
    ctx = AgentCtx(chat_key=chat_key, user_id="u1", locale="en")

    await _seed_ready_keeper_pool(services, chat_key)

    i18n = services.i18n.with_locale("en")
    document_context = await inject_document_context_prompt(
        ctx, services.vector_db, services.store, i18n, services.settings.enable_vector_db
    )
    # This room has no conversation yet, so the retrieval context is empty too.
    world_lore = await inject_world_lore_prompt(ctx, services.worldbook, i18n, role="keeper", recent_context="")
    # The P1 layout, hand-assembled: stable head (identity, expertise, style, the
    # module pool) then volatile tail (lore, live state).
    plain_sections = [
        await inject_trpg_system_prompt(ctx, i18n),
        await inject_system_expertise_prompt(
            ctx, services.characters, i18n, default_system=services.settings.default_rulepack
        ),
        await inject_interaction_style_prompt(ctx, i18n),
        # With a ready module pool the document context rides the stable head, and the
        # settlement reminder follows it (the head's order: ... style, pool, settlement).
        document_context,
        i18n.t("prompt.settlement_notice"),
        world_lore,
        await inject_game_state_prompt(ctx, services.characters, services.store, i18n),
    ]
    expected = "\n\n".join(section for section in plain_sections if section)  # no skills enabled here

    actual = await build_system_prompt(ctx, services)

    assert actual == expected
    assert i18n.t("prompt.relationships_header") not in actual


async def test_build_system_prompt_folds_in_a_set_relationship_track_as_the_last_section():
    services = _services("en")
    chat_key = "chat-prompt-builder-with-relationships"
    ctx = AgentCtx(chat_key=chat_key, user_id="u1", locale="en")

    manager = RelationshipManager(services.store)
    await manager.adjust(chat_key, "Alice", "Bob", "affection", 30)

    prompt = await build_system_prompt(ctx, services)
    i18n = services.i18n.with_locale("en")

    assert i18n.t("prompt.relationships_header") in prompt
    assert "Alice" in prompt and "Bob" in prompt
    # It's the LAST section: nothing else appears after it.
    header_pos = prompt.index(i18n.t("prompt.relationships_header"))
    assert header_pos == max(
        prompt.index(marker) for marker in (i18n.t("prompt.relationships_header"), i18n.t("prompt.game_state.title"))
    )
    assert prompt.rstrip().endswith(prompt[header_pos:].rstrip())


async def test_build_system_prompt_relationship_fold_in_is_localized_per_ctx_locale():
    services = _services("en")
    chat_key = "chat-prompt-builder-relationships-zh"
    ctx = AgentCtx(chat_key=chat_key, user_id="u1", locale="zh")

    manager = RelationshipManager(services.store)
    await manager.adjust(chat_key, "Alice", "Bob", "affection", 30)

    prompt = await build_system_prompt(ctx, services)
    zh = services.i18n.with_locale("zh")

    assert zh.t("prompt.relationships_header") in prompt
    assert zh.t("relationships.track.affection") in prompt


# ---------------------------------------------------------------------------
# Deterministic module variables (core.modvars) fold-in --
# ---------------------------------------------------------------------------


async def test_build_system_prompt_folds_in_module_variables_with_keeper_tag():
    services = _services("en")
    chat_key = "chat-prompt-builder-modvars"
    ctx = AgentCtx(chat_key=chat_key, user_id="u1", locale="en")

    await define_modvar(
        services.documents,
        chat_key,
        build_spec("town_fear", "number", labels={"en": "Town Fear"}, minimum=0, maximum=10),
    )
    await define_modvar(services.documents, chat_key, build_spec("culprit_alerted", "bool", visibility="keeper"))
    await set_modvar(services.documents, chat_key, "town_fear", 7)

    prompt = await build_system_prompt(ctx, services)
    i18n = services.i18n.with_locale("en")

    assert i18n.t("prompt.modvars_header") in prompt
    assert "Town Fear" in prompt and "7" in prompt
    # The keeper-only variable IS in the keeper prompt, tagged never-reveal (iron rule #3's
    # behavioral side; net.state's player filter is the structural side).
    assert "culprit_alerted" in prompt and "KEEPER-ONLY" in prompt


async def test_build_system_prompt_without_module_variables_has_no_modvars_header():
    services = _services("en")
    ctx = AgentCtx(chat_key="chat-prompt-builder-no-modvars", user_id="u1", locale="en")

    prompt = await build_system_prompt(ctx, services)

    assert services.i18n.with_locale("en").t("prompt.modvars_header") not in prompt


# ---------------------------------------------------------------------------
# The room's AI reply-length mode (the `ai_length` store flag) --
# ---------------------------------------------------------------------------


async def test_build_system_prompt_folds_brief_directive_only_when_room_asks_for_it():
    """`ai_length=concise`/`brief` fold their brevity directive into the stable head;
    the default (unset, or an explicit "normal") leaves the prompt byte-identical —
    the setting is off unless a keeper turned it on. Each mode injects exactly its own
    directive, never the other's."""
    services = _services("en")
    chat_key = "chat-prompt-builder-ai-length"
    ctx = AgentCtx(chat_key=chat_key, user_id="u1", locale="en")
    i18n = services.i18n.with_locale("en")

    baseline = await build_system_prompt(ctx, services)
    assert i18n.t("prompt.style.brief") not in baseline
    assert i18n.t("prompt.style.concise") not in baseline

    await services.store.state_set(chat_key, "ai_length", "concise")
    concise = await build_system_prompt(ctx, services)
    assert i18n.t("prompt.style.concise") in concise
    assert i18n.t("prompt.style.brief") not in concise  # never the other mode's directive
    assert i18n.t("prompt.style.narrative") in concise  # the style layer still leads
    parts = await build_system_prompt_parts(ctx, services)
    assert i18n.t("prompt.style.concise") in parts.stable
    assert i18n.t("prompt.style.concise") not in parts.volatile

    await services.store.state_set(chat_key, "ai_length", "brief")
    brief = await build_system_prompt(ctx, services)
    assert i18n.t("prompt.style.brief") in brief
    assert i18n.t("prompt.style.concise") not in brief

    await services.store.state_set(chat_key, "ai_length", "normal")
    back = await build_system_prompt(ctx, services)
    assert back == baseline


async def test_system_prompt_carries_the_settlement_reminder():
    """The AI-KP knows the settlement ritual exists and may REMIND the keeper when
    the story clearly ends — but must never trigger settlement itself."""
    services = _services("en")
    ctx = AgentCtx(chat_key="chat-settle-reminder", user_id="u1", locale="en")
    await _seed_ready_keeper_pool(services, ctx.chat_key)

    prompt = await build_system_prompt(ctx, services)
    i18n = services.i18n.with_locale("en")

    assert i18n.t("prompt.settlement_notice") in prompt
    assert "NEVER trigger settlement yourself" in prompt


async def test_system_prompt_carries_each_characters_latest_memory():
    """The keeper sees each PC's most recent experience line — narrative continuity
    from the character's own story — and a room without memories stays unchanged."""
    services = _services("en")
    chat_key = "chat-memory-prompt"
    ctx = AgentCtx(chat_key=chat_key, user_id="u1", locale="en")
    from core.character_memory import CHARACTER_MEMORY_DOC_TYPE

    await services.documents.put(
        chat_key, CHARACTER_MEMORY_DOC_TYPE, "Vera",
        {"entries": [{"text": "Vera traced the watermark to the chapel.", "turn": 3}], "summary": "", "keeper": ""},
    )

    prompt = await build_system_prompt(ctx, services)

    assert "Vera traced the watermark to the chapel." in prompt

    # A room with no memory documents contributes nothing.
    plain = await build_system_prompt(AgentCtx(chat_key="chat-no-mem", user_id="u1", locale="en"), services)
    assert "Character memories" not in plain
