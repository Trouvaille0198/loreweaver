"""The `.summary` lane: a player-safe, LLM-generated campaign recap ("概括").

Companion to `agent.chronicle.render_recap` (the deterministic "previously
on…"): recap renders what is already on file, this lane asks the authoring LLM
to CONDENSE the room's public play material — the rolling campaign summary,
the unfolded chronicle tail, and the unfolded conversation tail — into a
structured progress report (where the party is, what happened so far, open
threads).

Every input is a PLAYER projection, so keeper annotations structurally cannot
reach the prompt or the reply — the same contract `render_recap` keeps. The
module's keeper pool is never consulted, and no document the players have not
seen enters the prompt.

A command-triggered lane, not a per-turn call: its cost is not part of the
single-turn model budget in AGENTS.md.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.chronicle import (
    CAMPAIGN_SUMMARY_DOC_TYPE,
    CAMPAIGN_SUMMARY_ID,
    CHRONICLE_DOC_TYPE,
)
from agent.history import DEFAULT_HISTORY_KEY, load_chain
from core.documents import PLAYER_VIEWER
from infra.i18n import I18n
from infra.model_call_trace import lane_scope

logger = logging.getLogger(__name__)

__all__ = ["render_summary"]



def _entry_turn(doc: Any) -> int:
    """The chronicle record's turn number (0 when unset/unreadable)."""
    try:
        return int(doc.data.get("turn", 0))
    except (TypeError, ValueError):
        return 0

# How many unfolded chronicle records the prompt may carry (same tail as the recap).
_SUMMARY_RECORD_TAIL = 8
# How many trailing conversation messages may be sampled.
_MAX_HISTORY_MESSAGES = 24
# Hard cap on the whole assembled prompt body — a condensation, not an archive.
_MAX_INPUT_CHARS = 8000


async def render_summary(services: Any, chat_key: str, i18n: I18n) -> str | None:
    """The "where we are" recap for `.summary`: the campaign summary + the recent
    chronicle tail + the recent conversation, condensed by the authoring LLM.

    Returns the rendered recap text, or ``None`` when there is no play material
    yet (the command shows a localized empty notice). A failed or empty model
    reply degrades to the localized failed notice instead of an exception.
    """
    body = await _collect_material(services, chat_key, i18n)
    if not body.strip():
        return None

    llm = await services.main_llm(chat_key)
    try:
        with lane_scope("authoring", chat_key=chat_key):
            result = await llm.chat(
                [
                    {"role": "system", "content": i18n.t("agent.session_summary.system_prompt")},
                    {"role": "user", "content": body},
                ]
            )
    except Exception:  # noqa: BLE001
        logger.debug("session summary generation failed", exc_info=True)
        return i18n.t("commands.summary.failed")
    text = (result.content or "").strip()
    return text or i18n.t("commands.summary.failed")


async def _collect_material(services: Any, chat_key: str, i18n: I18n) -> str:
    """Assemble the player-visible play material, newest context last."""
    parts: list[str] = []

    summary = await services.documents.get_view(
        chat_key, CAMPAIGN_SUMMARY_DOC_TYPE, CAMPAIGN_SUMMARY_ID, PLAYER_VIEWER
    )
    if summary and str(summary.get("text", "")).strip():
        parts.append(
            i18n.t("agent.session_summary.summary_label") + "\n" + str(summary["text"]).strip()
        )

    pairs = await services.documents.list_views(chat_key, CHRONICLE_DOC_TYPE, PLAYER_VIEWER)
    tail = sorted(pairs, key=lambda pair: (_entry_turn(pair[0]), pair[0].id))
    tail = [(doc, view) for doc, view in tail if not doc.data.get("folded")][-_SUMMARY_RECORD_TAIL:]
    if tail:
        lines = [
            f"[turn {_entry_turn(doc)}] {str(view.get('text', '')).strip()}"
            for doc, view in tail
        ]
        parts.append(i18n.t("agent.session_summary.chronicle_label") + "\n" + "\n".join(lines))

    chain = await load_chain(services, chat_key, DEFAULT_HISTORY_KEY)
    conversation = [
        message
        for message in chain
        if message.get("role") in ("user", "assistant") and str(message.get("content", "")).strip()
    ][-_MAX_HISTORY_MESSAGES:]
    if conversation:
        lines = []
        for message in conversation:
            name = str(message.get("name", "")).strip()
            content = str(message["content"]).strip()
            lines.append(f"{name}: {content}" if name else content)
        parts.append(i18n.t("agent.session_summary.conversation_label") + "\n" + "\n".join(lines))

    body = "\n\n".join(parts)
    if len(body) > _MAX_INPUT_CHARS:
        body = body[:_MAX_INPUT_CHARS].rstrip() + "\n…"
    return body
