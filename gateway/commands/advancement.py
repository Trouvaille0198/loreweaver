"""Commands for keeper-granted, owner-chosen advancement plans."""

from __future__ import annotations

import json

from core.advancement import (
    AdvancementError,
    apply_advancement,
    cancel_advancement,
    choose_advancement,
    eligible_level,
    grant_advancement,
)
from core.character_manager import has_character
from gateway.commands.rooms import _is_keeper
from gateway.commands.types import CommandCtx


class AdvancementCommands:
    """Thin command facade; deterministic progression lives in ``core.advancement``."""

    async def cmd_advance(self, ctx: CommandCtx) -> str:
        pack = await ctx.services.room_rulepack(ctx.raw_ctx)
        if getattr(pack, "runtime_spec", None) is None:
            return ctx.fail(ctx.i18n.t("commands.runtime.unsupported"))
        character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
        if not has_character(character):
            return ctx.fail(ctx.i18n.t("commands.advance.no_character"))
        parts = ctx.args.split()
        action = parts[0].casefold() if parts else "status"
        try:
            pending = (getattr(character, "advancement", {}) or {}).get("pending")
            if action == "status":
                current = int(getattr(character, "level", 1) or 1)
                modes = list(pack.runtime_spec.advancement.get("modes", []))
                eligible = {mode: eligible_level(character, pack, mode=mode) for mode in modes}
                return ctx.i18n.t(
                    "commands.advance.status",
                    level=current,
                    xp=int(getattr(character, "xp", 0) or 0),
                    pending=json.dumps(pending, ensure_ascii=False, sort_keys=True) if pending else "-",
                    eligible=json.dumps(eligible, ensure_ascii=False, sort_keys=True),
                )
            if action == "grant":
                if not _is_keeper(ctx.raw_ctx):
                    return ctx.fail(ctx.i18n.t("commands.advance.keeper_only"))
                mode = parts[1].casefold() if len(parts) > 1 else "milestone"
                plan = grant_advancement(character, pack, mode=mode)
                await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
                return ctx.i18n.t("commands.advance.granted", plan=json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True))
            if action == "choose":
                choices = {}
                hp_mode = "fixed"
                for item in parts[1:]:
                    if "=" not in item:
                        return ctx.fail(ctx.i18n.t("commands.advance.usage"))
                    key, value = item.split("=", 1)
                    if key == "hp":
                        hp_mode = value
                    else:
                        choices[key] = value
                plan = choose_advancement(character, pack, choices, hp_mode=hp_mode)
                await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
                return ctx.i18n.t("commands.advance.chosen", plan=json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True))
            if action == "apply":
                if not _is_keeper(ctx.raw_ctx):
                    return ctx.fail(ctx.i18n.t("commands.advance.keeper_only"))
                result = apply_advancement(character, pack, roller=ctx.services.dice)
                await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
                return ctx.i18n.t("commands.advance.applied", result=json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
            if action == "cancel":
                if not cancel_advancement(character):
                    return ctx.fail(ctx.i18n.t("commands.advance.no_pending"))
                await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
                return ctx.i18n.t("commands.advance.cancelled")
            if action == "xp" and len(parts) > 1:
                if not _is_keeper(ctx.raw_ctx):
                    return ctx.fail(ctx.i18n.t("commands.advance.keeper_only"))
                character.xp = max(0, int(parts[1]))
                await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
                return ctx.i18n.t("commands.advance.xp_set", xp=character.xp)
            return ctx.fail(ctx.i18n.t("commands.advance.usage"))
        except (AdvancementError, ValueError) as exc:
            return ctx.fail(ctx.i18n.t("commands.advance.failed", error=str(exc)))

    async def cmd_level(self, ctx: CommandCtx) -> str:
        if not ctx.args.strip():
            ctx.args = "status"
        return await self.cmd_advance(ctx)

    async def cmd_xp(self, ctx: CommandCtx) -> str:
        if ctx.args.strip() and ctx.args.split()[0].lstrip("+-").isdigit():
            ctx.args = f"xp {ctx.args.strip()}"
        else:
            ctx.args = "status" if not ctx.args.strip() else ctx.args
        return await self.cmd_advance(ctx)
