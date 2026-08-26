"""The settlement proposal tool — KEEPER-ONLY, never auto-invoked.

`.settle` is the command surface; this is the same lane inside a Keeper turn.
The keeper (human or model) decides when the story is over and calls it; the
tool only PROPOSES (one declared settlement-lane call, `agent.settle`), stores
the proposal pending, and lands nothing. `.settle apply` stays the only path
that touches character data.

Deliberately NOT `prep_only`: a settlement is proposed exactly when play is
winding down — the play-phase toolset must keep it. Deliberately
`keeper_only=True`: the reply carries keeper judgments, and the engine never
offers it to a player-facing surface.
"""

from __future__ import annotations

from agent.context import AgentCtx
from agent.services import Services
from agent.settle import build_settlement, render_proposal, save_pending
from agent.tools import tool


class SettleTools:
    """Keeper tool provider for the post-campaign settlement ritual."""

    def __init__(self, services: Services) -> None:
        self.services = services

    @tool(
        keeper_only=True,
    )
    async def propose_settlement(self, ctx: AgentCtx, reason: str = "") -> str:
        """Propose the post-campaign settlement: analyze the room's process data (skill checks, campaign chronicle, character memories, sheets) and propose, per character, which skills earned improvement checks, small attribute changes, the folded life-summary, and an updated backstory. Call this when the scenario is clearly ending or has just ended — never mid-story, and never instead of the final scene. It changes NOTHING: the proposal is stored pending and landed by `.settle apply` (real dice, validated sheets). Keeper-only; the keeper decides when the story is over."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        sheets = await self.services.documents.list(ctx.chat_key, "sheet")
        if not sheets:
            return i18n.t("commands.settle.no_data")
        settlement = await build_settlement(self.services, ctx.chat_key)
        if settlement is None:
            return i18n.t("commands.settle.failed")
        await save_pending(self.services, ctx.chat_key, settlement)
        return f"{render_proposal(settlement, i18n)}\n{i18n.t('commands.settle.applied_hint')}"
