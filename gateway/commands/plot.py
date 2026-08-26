"""Story-pacing commands: `.hint` asks the AI Keeper for a spoiler-safe nudge.

The command does not run a second model lane. It turns the player's request into a
normal Keeper turn, so the Keeper can use the room's established facts, tools, and
state while the ordinary turn pipeline records and broadcasts the answer.
"""

from __future__ import annotations

from gateway.commands.types import CommandCtx


class PlotCommands:
    """Commands that help the table move a stalled story forward."""

    async def cmd_hint(self, ctx: CommandCtx) -> str | None:
        """`.hint [focus]` — ask the Keeper for a small lead or concrete next step."""
        focus = ctx.args.strip() or ctx.i18n.t("commands.hint.focus_unspecified")
        ctx.set_turn_message(ctx.i18n.t("commands.hint.turn_prompt", focus=focus))
        return None
