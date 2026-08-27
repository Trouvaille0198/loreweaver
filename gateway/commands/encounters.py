"""Commands for projected stat-block and encounter catalogs."""

from __future__ import annotations

import json

from core.combat import CombatManager, create_combat, start_combat
from core.documents import KEEPER_VIEWER, PLAYER_VIEWER
from core.encounters import EncounterError, calculate_budget, encounter_instances, parse_encounter
from core.statblocks import StatBlockError, parse_statblock
from gateway.commands.rooms import _is_keeper
from gateway.commands.types import CommandCtx


class EncounterCommands:
    """Document-backed catalog reads and keeper-owned encounter mutations."""

    async def cmd_statblock(self, ctx: CommandCtx) -> str:
        parts = ctx.args.split()
        action = (parts[0].casefold() if parts else "list")
        keeper = _is_keeper(ctx.raw_ctx)
        viewer = KEEPER_VIEWER if keeper else PLAYER_VIEWER
        try:
            if action in {"list", "ls"}:
                pairs = await ctx.services.documents.list_views(ctx.chat_key, "statblock", viewer)
                if not pairs:
                    return ctx.i18n.t("commands.statblock.empty")
                payload = [view for _doc, view in pairs]
                return ctx.i18n.t(
                    "commands.statblock.list",
                    statblocks=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                )
            if action == "show" and len(parts) >= 2:
                view = await ctx.services.documents.get_view(ctx.chat_key, "statblock", parts[1], viewer)
                if view is None:
                    return ctx.fail(ctx.i18n.t("commands.statblock.not_found", id=parts[1]))
                return ctx.i18n.t(
                    "commands.statblock.show",
                    statblock=json.dumps(view, ensure_ascii=False, sort_keys=True),
                )
            if action == "bind" and len(parts) >= 3:
                if not keeper:
                    return ctx.fail(ctx.i18n.t("commands.statblock.keeper_only"))
                statblock = await ctx.services.documents.get_view(ctx.chat_key, "statblock", parts[2], KEEPER_VIEWER)
                npc_doc = await ctx.services.documents.get(ctx.chat_key, "npc", parts[1])
                if statblock is None or npc_doc is None:
                    return ctx.fail(ctx.i18n.t("commands.statblock.not_found", id=parts[-1]))
                data = dict(npc_doc.data)
                data["mechanics_ref"] = f"statblock:{parts[2]}"
                await ctx.services.documents.put(ctx.chat_key, "npc", npc_doc.id, data, services=ctx.services)
                return ctx.i18n.t("commands.statblock.bound", npc=parts[1], id=parts[2])
            return ctx.fail(ctx.i18n.t("commands.statblock.usage"))
        except (StatBlockError, ValueError) as exc:
            return ctx.fail(ctx.i18n.t("commands.statblock.failed", error=str(exc)))

    async def cmd_encounter(self, ctx: CommandCtx) -> str:
        parts = ctx.args.split()
        action = (parts[0].casefold() if parts else "list")
        keeper = _is_keeper(ctx.raw_ctx)
        viewer = KEEPER_VIEWER if keeper else PLAYER_VIEWER
        try:
            if action in {"list", "ls"}:
                pairs = await ctx.services.documents.list_views(ctx.chat_key, "encounter", viewer)
                if not pairs:
                    return ctx.i18n.t("commands.encounter.empty")
                return ctx.i18n.t(
                    "commands.encounter.list",
                    encounters=json.dumps([view for _doc, view in pairs], ensure_ascii=False, sort_keys=True),
                )
            if len(parts) < 2:
                return ctx.fail(ctx.i18n.t("commands.encounter.usage"))
            document = await ctx.services.documents.get(ctx.chat_key, "encounter", parts[1])
            if document is None:
                return ctx.fail(ctx.i18n.t("commands.encounter.not_found", id=parts[1]))
            encounter = parse_encounter(document.data, encounter_id=document.id)
            if action == "show":
                view = encounter.to_dict(keeper=keeper)
                return ctx.i18n.t("commands.encounter.show", encounter=json.dumps(view, ensure_ascii=False, sort_keys=True))
            if action == "budget":
                if not keeper:
                    return ctx.fail(ctx.i18n.t("commands.encounter.keeper_only"))
                statblocks = {}
                for entry in encounter.entries:
                    stat_doc = await ctx.services.documents.get(ctx.chat_key, "statblock", entry.reference)
                    if stat_doc is None:
                        raise EncounterError(f"unknown stat block {entry.reference!r}")
                    statblocks[entry.reference] = parse_statblock("document", stat_doc.data, statblock_id=stat_doc.id)
                party_size = int(parts[2]) if len(parts) > 2 else len(await ctx.services.characters.get_party_roster(ctx.chat_key))
                runtime = (await ctx.services.room_rulepack(ctx.raw_ctx)).runtime_spec
                declaration = dict(runtime.encounters.get("budget", {})) if runtime is not None else {}
                result = calculate_budget(encounter, statblocks, party_size=party_size, declaration=declaration)
                return ctx.i18n.t("commands.encounter.budget", budget=json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
            if action == "start":
                if not keeper:
                    return ctx.fail(ctx.i18n.t("commands.encounter.keeper_only"))
                statblocks = {}
                for entry in encounter.entries:
                    stat_doc = await ctx.services.documents.get(ctx.chat_key, "statblock", entry.reference)
                    if stat_doc is None:
                        raise EncounterError(f"unknown stat block {entry.reference!r}")
                    statblocks[entry.reference] = parse_statblock("document", stat_doc.data, statblock_id=stat_doc.id)
                instances = encounter_instances(encounter, statblocks)
                combatants = {item["id"]: item for item in instances}
                runtime = (await ctx.services.room_rulepack(ctx.raw_ctx)).runtime_spec
                budget = {}
                if runtime is not None:
                    budget = {
                        str(key): int(value)
                        for key, value in runtime.budgets.items()
                        if isinstance(value, (int, float)) and not isinstance(value, bool)
                    }
                state = start_combat(create_combat(encounter.id, combatants, budget=budget), budget=budget)
                manager = CombatManager(ctx.services.store, ctx.chat_key)
                if not await manager.save(state, expected_raw=None):
                    return ctx.fail(ctx.i18n.t("commands.combat.conflict"))
                return ctx.i18n.t("commands.encounter.started", id=encounter.id)
            return ctx.fail(ctx.i18n.t("commands.encounter.usage"))
        except (EncounterError, StatBlockError, ValueError) as exc:
            return ctx.fail(ctx.i18n.t("commands.encounter.failed", error=str(exc)))
