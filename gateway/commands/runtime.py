"""Runtime mechanics commands: resources and strict combat turns."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from core.action_resolver import ActionResolution, ActionResolutionError, public_action_event, resolve_action
from core.character_manager import has_character
from core.combat import (
    ActionApplication,
    CombatError,
    CombatManager,
    CombatState,
    apply_action_result,
    claim_turn,
    create_combat,
    end_combat,
    end_turn,
    join_combat,
    release_claim,
    remove_combatant,
    start_combat,
)
from core.game_clock import advance_clock_state
from core.resources import ResourceError, ResourceLedger, resource_projection, resource_values
from core.rests import RestError, complete_rest
from core.runtime import ResourceCostSpec
from gateway.commands.rooms import _is_keeper
from gateway.commands.types import CommandCtx


def _runtime_unsupported(ctx: CommandCtx) -> str:
    return ctx.fail(ctx.i18n.t("commands.runtime.unsupported"))


def _budget_template(pack: Any) -> dict[str, int]:
    runtime = getattr(pack, "runtime_spec", None)
    if runtime is None:
        return {}
    result: dict[str, int] = {}
    defaults = dict(getattr(pack, "defaults", {}) or {})
    try:
        defaults.update(pack.compute_derived(defaults))
    except Exception:
        pass
    for key, value in runtime.budgets.items():
        resolved: Any = value
        if isinstance(value, Mapping) and "ref" in value:
            resolved = defaults.get(str(value["ref"]))
        elif isinstance(value, Mapping) and "value" in value:
            resolved = value["value"]
        if isinstance(resolved, bool):
            continue
        if isinstance(resolved, (int, float)):
            result[str(key)] = max(0, int(resolved))
    return result


def _state_text(ctx: CommandCtx, state: CombatState) -> str:
    return ctx.i18n.t(
        "commands.combat.status",
        phase=state.phase,
        round=state.round,
        current=state.current or "-",
        budget=json.dumps(dict(state.budget), ensure_ascii=False, sort_keys=True),
        order=" ".join(state.order) or "-",
    )

def _sheet_document_update(sheet: Any, row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        stored_data = json.loads(str(row.get("data") or "{}"))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(stored_data, Mapping):
        return None
    data = dict(sheet.to_dict())
    if stored_data.get("owner"):
        data["owner"] = stored_data["owner"]
    return {
        "type": "sheet",
        "id": str(row.get("id") or sheet.name),
        "schema_version": row.get("schema_version", 1),
        "data": data,
        "meta": row.get("meta", "{}"),
        "grants": row.get("grants", "[]"),
        "seq": row.get("seq", 0),
    }

def _actor_resource_values(combatant: Mapping[str, Any]) -> dict[str, Any]:
    raw = combatant.get("resources")
    if not isinstance(raw, Mapping):
        return {}
    values: dict[str, Any] = {}
    for pool_id, value in raw.items():
        if isinstance(value, Mapping):
            values[str(pool_id)] = value.get("current", 0)
        else:
            values[str(pool_id)] = value
    return values
def _damage_target_updates(
    state: CombatState,
    resolution: ActionResolution,
    *,
    dying_rules: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    updates: dict[str, dict[str, Any]] = {}
    for target_id, rolls in resolution.damage.items():
        target = state.combatants.get(target_id)
        if target is None:
            continue
        target_resources = {
            str(key): dict(value) if isinstance(value, Mapping) else value
            for key, value in (target.get("resources") or {}).items()
        } if isinstance(target.get("resources"), Mapping) else {}
        health_id = next(
            (
                str(key)
                for key, value in target_resources.items()
                if isinstance(value, Mapping) and value.get("role") == "health"
            ),
            "health",
        )
        temp_id = next(
            (
                str(key)
                for key, value in target_resources.items()
                if isinstance(value, Mapping) and value.get("role") == "temporary_health"
            ),
            "temporary_health",
        )
        health_current = target_resources.get(health_id, target.get("health", 0))
        if isinstance(health_current, Mapping):
            health_current = health_current.get("current", 0)
        temporary_current = target_resources.get(temp_id, target.get("temporary_health", 0))
        if isinstance(temporary_current, Mapping):
            temporary_current = temporary_current.get("current", 0)
        try:
            health_before = max(0, int(health_current or 0))
            temporary_before = max(0, int(temporary_current or 0))
        except (TypeError, ValueError):
            raise ActionResolutionError(f"target {target_id!r} has invalid health state") from None
        total_health_damage = sum(item.outcome.health_damage for item in rolls)
        total_temporary = sum(item.outcome.temporary_absorbed for item in rolls)
        health = max(0, health_before - total_health_damage)
        temporary = max(0, temporary_before - total_temporary)
        if target_resources:
            health_entry = target_resources.get(health_id)
            if isinstance(health_entry, Mapping):
                health_entry["current"] = health
            else:
                target_resources[health_id] = {"current": health}
            temp_entry = target_resources.get(temp_id)
            if isinstance(temp_entry, Mapping):
                temp_entry["current"] = temporary
            elif temporary_before or temp_id in target_resources:
                target_resources[temp_id] = {"current": temporary}
            updates[target_id] = {"resources": target_resources}
        else:
            updates[target_id] = {"health": health, "temporary_health": temporary}
        if health <= 0:
            rules = dict(dying_rules or {})
            death_save = rules.get("death_save")
            if isinstance(death_save, Mapping):
                rules.update(death_save)
            dying = target.get("dying")
            if isinstance(dying, Mapping) and str(dying.get("status") or "") not in {
                str(rules.get("death_status") or "dead")
            }:
                from core.dying import DyingState, apply_dying_damage

                dying_state = DyingState(
                    status=str(dying.get("status") or rules.get("zero_hp") or "unconscious"),
                    health=0,
                    maximum=max(0, int(dying.get("maximum", health_before) or health_before)),
                    successes=max(0, int(dying.get("successes", 0) or 0)),
                    failures=max(0, int(dying.get("failures", 0) or 0)),
                    stable=bool(dying.get("stable", False)),
                )
                damage_rules = rules.get("damage_while_dying")
                if isinstance(damage_rules, Mapping):
                    rules.update(damage_rules)
                dying_state = apply_dying_damage(
                    dying_state,
                    critical=resolution.critical,
                    rules=rules,
                    death_status=str(rules.get("death_status") or "dead"),
                )
            else:
                from core.dying import enter_zero_health

                dying_state = enter_zero_health(
                    -total_health_damage,
                    max(0, int((target_resources.get(health_id, {}) or {}).get("max", health_before) or health_before))
                    if isinstance(target_resources.get(health_id), Mapping)
                    else health_before,
                    rules=rules,
                    zero_status=str(rules.get("zero_hp") or "unconscious"),
                    death_status=str(rules.get("death_status") or "dead"),
                    incoming_damage=total_health_damage,
                )
            updates[target_id]["dying"] = dying_state.to_dict()
            updates[target_id]["state"] = dying_state.status
    return updates


def _condition_target_updates(state: CombatState, resolution: ActionResolution) -> dict[str, dict[str, Any]]:
    updates: dict[str, dict[str, Any]] = {}
    for target_id in resolution.targets:
        target = state.combatants.get(target_id)
        if target is None:
            continue
        conditions = [
            dict(condition)
            for condition in (target.get("conditions") or [])
            if isinstance(condition, Mapping)
        ]
        changed = False
        for effect in resolution.condition_effects:
            op = getattr(effect, "op", "")
            args = getattr(effect, "args", {})
            if op == "condition_remove":
                condition_id = str(args.get("condition") or "")
                conditions = [item for item in conditions if item.get("id") != condition_id]
                changed = True
            elif op == "condition_add":
                condition_id = str(args.get("condition") or "")
                if not condition_id:
                    continue
                conditions = [item for item in conditions if item.get("id") != condition_id]
                conditions.append(
                    {
                        "id": condition_id,
                        "source": resolution.action_id,
                        "target": target_id,
                        "start_round": state.round,
                        "start_turn": state.turn_index,
                        "duration": args.get("duration"),
                        "stacks": int(args.get("stacks", 1) or 1),
                        "visibility": "public",
                    }
                )
                changed = True
        if changed:
            updates[target_id] = {"conditions": conditions}
    return updates


def _action_slot(action_id: str, slot: str | None, action: Any) -> Any:
    if not slot:
        return action
    try:
        level = int(slot)
    except ValueError:
        raise ActionResolutionError("spell slot must be an integer") from None  # i18n-exempt: internal validation diagnostic
    if level < 1:
        raise ActionResolutionError("spell slot must be positive")
    pool = f"spell_slot_{level}"
    costs = tuple(action.resource_costs)
    if not any(item.pool == pool for item in costs):
        costs = (*costs, ResourceCostSpec(pool=pool, amount=1))
    return replace(action, resource_costs=costs)


class RuntimeCommands:
    """Command handlers that delegate all arithmetic to core runtime managers."""

    async def cmd_resource(self, ctx: CommandCtx) -> str:
        """Show or mutate the active character's generic resource pools."""
        pack = await ctx.services.room_rulepack(ctx.raw_ctx)
        runtime = getattr(pack, "runtime_spec", None)
        if runtime is None:
            return _runtime_unsupported(ctx)
        parts = ctx.args.split()
        action = (parts[0].casefold() if parts else "show")
        pool_id = parts[1] if len(parts) > 1 else ""
        amount_text = parts[2] if len(parts) > 2 else ""
        character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
        if not has_character(character):
            return ctx.fail(ctx.i18n.t("commands.resource.no_character"))
        ledger = ResourceLedger(character, pack)
        try:
            if action in {"show", "list", "status"}:
                if pool_id:
                    value = ledger.show(pool_id)
                    return ctx.i18n.t(
                        "commands.resource.one",
                        id=value.id,
                        value=value.current,
                        maximum="-" if value.maximum is None else value.maximum,
                    )
                projection = resource_projection(character, pack, ctx.locale)
                return ctx.i18n.t(
                    "commands.resource.show",
                    resources=json.dumps(projection, ensure_ascii=False, sort_keys=True),
                )
            if action not in {"spend", "set", "recover"} or not pool_id:
                return ctx.fail(ctx.i18n.t("commands.resource.usage"))
            if action == "recover" and not amount_text:
                mutation = ledger.recover(pool_id)
            else:
                try:
                    amount = int(amount_text)
                except ValueError:
                    return ctx.fail(ctx.i18n.t("commands.resource.usage"))
                mutation = (
                    ledger.spend(pool_id, amount)
                    if action == "spend"
                    else ledger.set(pool_id, amount)
                    if action == "set"
                    else ledger.recover(pool_id, amount)
                )
            await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
            return ctx.i18n.t(
                "commands.resource.changed",
                id=mutation.pool.id,
                before=mutation.before,
                after=mutation.after,
            )
        except (ResourceError, ValueError) as exc:
            return ctx.fail(ctx.i18n.t("commands.resource.failed", error=str(exc)))

    async def cmd_rest(self, ctx: CommandCtx) -> str:
        """Complete a declared short or long rest after explicit command input."""
        pack = await ctx.services.room_rulepack(ctx.raw_ctx)
        runtime = getattr(pack, "runtime_spec", None)
        if runtime is None:
            return _runtime_unsupported(ctx)
        parts = ctx.args.split()
        action = parts[0].casefold() if parts else "status"
        character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
        if not has_character(character):
            return ctx.fail(ctx.i18n.t("commands.resource.no_character"))
        if action == "status":
            return ctx.i18n.t(
                "commands.rest.status",
                resources=json.dumps(resource_projection(character, pack, ctx.locale), ensure_ascii=False, sort_keys=True),
            )
        if action not in {"short", "long"}:
            return ctx.fail(ctx.i18n.t("commands.rest.usage"))
        if not _is_keeper(ctx.raw_ctx):
            return ctx.fail(ctx.i18n.t("commands.rest.keeper_only"))
        recovery_dice = tuple(parts[1:]) if action == "short" else ()
        try:
            clock_raw = await ctx.services.store.state_get(ctx.chat_key, "game_clock")
            clock = json.loads(clock_raw) if clock_raw else {}
            elapsed = int(clock.get("elapsed_seconds", 0) or 0) if isinstance(clock, Mapping) else 0
            actor_document = await ctx.services.store.doc_get(ctx.chat_key, "sheet", character.name)
            result = complete_rest(
                character,
                pack,
                action,
                roller=ctx.services.dice,
                recovery_dice=recovery_dice,
                elapsed_seconds=max(0, elapsed),
            )
            expected_state: list[tuple[str, str | None]] = []
            state_updates: list[tuple[str, str | None]] = []
            if action == "long" and result.elapsed_seconds:
                seconds = result.elapsed_seconds
                if seconds % 86400 == 0:
                    delta = f"+{seconds // 86400} days"
                elif seconds % 3600 == 0:
                    delta = f"+{seconds // 3600} hours"
                else:
                    delta = f"+{seconds // 60} minutes"
                updated_clock, _ = advance_clock_state(clock, delta)
                expected_state.append(("game_clock", clock_raw))
                state_updates.append(("game_clock", json.dumps(updated_clock, ensure_ascii=False)))
            sheet_update = _sheet_document_update(character, actor_document)
            if sheet_update is None or actor_document is None:
                raise RestError("rest requires a readable character document")  # i18n-exempt: internal validation diagnostic
            committed = await ctx.services.store.compare_and_swap_room(
                ctx.chat_key,
                expected_state=expected_state,
                state_updates=state_updates,
                expected_documents=[("sheet", character.name, actor_document)],
                document_updates=[sheet_update],
            )
            if not committed:
                return ctx.fail(ctx.i18n.t("commands.rest.conflict"))
            await ctx.services.characters.sync_party_roster(ctx.chat_key, character)
            return ctx.i18n.t(
                "commands.rest.completed",
                kind=action,
                before=result.health_before,
                after=result.health_after,
            )
        except (RestError, ResourceError, ValueError) as exc:
            return ctx.fail(ctx.i18n.t("commands.rest.failed", error=str(exc)))

    async def _run_typed_action(self, ctx: CommandCtx, kind: str) -> str:
        pack = await ctx.services.room_rulepack(ctx.raw_ctx)
        runtime = getattr(pack, "runtime_spec", None)
        if runtime is None:
            return _runtime_unsupported(ctx)
        parts = ctx.args.split()
        if (kind == "action" and len(parts) < 1) or (kind != "action" and len(parts) < 2):
            return ctx.fail(ctx.i18n.t(f"commands.{kind}.usage"))
        action_id = parts[0]
        slot: str | None = None
        targets: list[str] = []
        for part in parts[1:]:
            if part.casefold().startswith("slot="):
                slot = part.split("=", 1)[1]
            else:
                targets.append(part)
        action = runtime.actions.get(action_id)
        if action is None:
            return ctx.fail(ctx.i18n.t(f"commands.{kind}.unknown_action", id=action_id))
        resolution_kind = action.resolution.get("kind") if isinstance(action.resolution, Mapping) else None
        if kind == "attack" and resolution_kind not in {None, "attack"}:
            return ctx.fail(ctx.i18n.t("commands.attack.unknown_action", id=action_id))
        if kind == "cast" and resolution_kind not in {None, "spell", "save", "attack"}:
            return ctx.fail(ctx.i18n.t("commands.cast.unknown_action", id=action_id))
        action = _action_slot(action_id, slot, action)
        manager = CombatManager(ctx.services.store, ctx.chat_key)
        state = await manager.get()
        if state is None or state.phase != "active" or state.current is None:
            return ctx.fail(ctx.i18n.t("commands.combat.empty"))
        actor_id = state.current
        keeper = _is_keeper(ctx.raw_ctx)
        controller = "keeper" if keeper else ctx.user_id
        raw = state.json()
        actor_sheet = None
        actor_document = None
        actor_entry = state.combatants.get(actor_id) or {}
        if str(actor_entry.get("controller") or "") == "human" or str(actor_entry.get("mechanics_ref") or "").startswith("sheet:"):
            candidate = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key, actor_id)
            if has_character(candidate):
                actor_sheet = candidate
                actor_document = await ctx.services.store.doc_get(ctx.chat_key, "sheet", actor_id)
        action_key = f"{state.id}:{state.round}:{state.turn_index}:{actor_id}:{action.id}" + (
            f":{','.join(targets)}" if targets else ""
        )
        existing = await ctx.services.store.state_get(ctx.chat_key, f"action_result:{action_key}")
        if existing is not None:
            return ctx.i18n.t("commands.action.replayed", result=existing)
        claimed = claim_turn(state, actor_id, controller, keeper_override=keeper)
        if not await manager.save(claimed, expected_raw=raw):
            return ctx.fail(ctx.i18n.t("commands.combat.conflict"))
        claim_token = str(claimed.claim["token"])
        try:
            actor = claimed.combatants[actor_id]
            resources = (
                {pool_id: value.current for pool_id, value in resource_values(actor_sheet, pack).items()}
                if actor_sheet is not None
                else _actor_resource_values(actor)
            )
            target_values: dict[str, int] = {}
            defenses: dict[str, Any] = {}
            temporary: dict[str, int] = {}
            for target_id in targets:
                target = claimed.combatants.get(target_id)
                if target is None:
                    raise ActionResolutionError(f"unknown target {target_id!r}")
                resolution = action.resolution if isinstance(action.resolution, Mapping) else {}
                defense_name = str(resolution.get("defense") or "")
                defense_map = target.get("defenses")
                if isinstance(defense_map, Mapping):
                    target_values[target_id] = int(defense_map.get(defense_name, defense_map.get("default", 10)) or 0)
                else:
                    target_values[target_id] = int(target.get(defense_name, 10) or 0) if defense_name else 0
                defenses[target_id] = target.get("defenses") if isinstance(target.get("defenses"), Mapping) else {}
                target_resources = target.get("resources")
                if isinstance(target_resources, Mapping):
                    temporary[target_id] = next(
                        (
                            int(value.get("current", 0) or 0)
                            for value in target_resources.values()
                            if isinstance(value, Mapping) and value.get("role") == "temporary_health"
                        ),
                        0,
                    )
            check_target = 10 if resolution_kind == "death_save" else (target_values.get(targets[0]) if targets else None)
            check_modifier = actor.get("modifier", 0)
            if isinstance(check_modifier, bool) or not isinstance(check_modifier, (int, float)):
                check_modifier = 0
            resolution = resolve_action(
                action,
                actor_id=actor_id,
                targets=targets,
                roller=ctx.services.dice,
                resolver=pack.resolver,
                check_target=check_target,
                check_modifier=int(check_modifier),
                resource_values=resources,
                target_defenses=defenses,
                target_temporary_health=temporary,
            )
            target_updates = _damage_target_updates(claimed, resolution, dying_rules=runtime.dying)
            target_updates.update(_condition_target_updates(claimed, resolution))
            actor_updates: dict[str, Any] = {}
            if resolution_kind == "death_save":
                dying = actor.get("dying")
                if not isinstance(dying, Mapping) or resolution.check is None:
                    raise ActionResolutionError("death save requires a dying actor")  # i18n-exempt: internal validation diagnostic
                rules = dict(runtime.dying)
                save_rules = rules.get("death_save")
                if isinstance(save_rules, Mapping):
                    rules.update(save_rules)
                from core.dying import DyingState, apply_dying_save

                dying_state = apply_dying_save(
                    DyingState(
                        status=str(dying.get("status") or rules.get("zero_hp") or "unconscious"),
                        health=0,
                        maximum=max(0, int(dying.get("maximum", 0) or 0)),
                        successes=max(0, int(dying.get("successes", 0) or 0)),
                        failures=max(0, int(dying.get("failures", 0) or 0)),
                        stable=bool(dying.get("stable", False)),
                    ),
                    resolution.check.rolled,
                    rules=rules,
                    stable_status=str(rules.get("stable_status") or "stabilized"),
                    death_status=str(rules.get("death_status") or "dead"),
                )
                actor_updates["dying"] = dying_state.to_dict()
                actor_updates["state"] = dying_state.status
            if resources and resolution.resource_costs:
                if actor_sheet is not None:
                    ledger = ResourceLedger(actor_sheet, pack)
                    for cost in resolution.resource_costs:
                        raw_amount = cost.amount
                        if isinstance(raw_amount, Mapping):
                            if "value" in raw_amount:
                                raw_amount = raw_amount["value"]
                            elif "ref" in raw_amount:
                                raw_amount = resources.get(str(raw_amount["ref"]), 0)
                        ledger.spend(cost.pool, int(raw_amount))
                    updated_resources = {
                        pool_id: {
                            "current": value.current,
                            "max": value.maximum,
                            "role": value.role,
                            "group": value.group,
                            "revision": value.revision,
                        }
                        for pool_id, value in resource_values(actor_sheet, pack).items()
                    }
                else:
                    updated_resources = {
                        str(pool_id): dict(value) if isinstance(value, Mapping) else value
                        for pool_id, value in (actor.get("resources") or {}).items()
                    }
                    for cost in resolution.resource_costs:
                        raw_amount = cost.amount
                        if isinstance(raw_amount, Mapping):
                            raw_amount = raw_amount.get("value", resources.get(str(raw_amount.get("ref")), 0))
                        entry = updated_resources.get(cost.pool)
                        if isinstance(entry, Mapping):
                            entry["current"] = max(0, int(entry.get("current", 0) or 0) - int(raw_amount))
                        else:
                            updated_resources[cost.pool] = {
                                "current": max(0, int(resources.get(cost.pool, 0)) - int(raw_amount))
                            }
                actor_updates["resources"] = updated_resources
            if resolution.succeeded and resolution.concentration:
                concentration = resolution.concentration
                if isinstance(concentration, Mapping):
                    condition_id = str(concentration.get("condition") or action.id)
                    duration = concentration.get("duration")
                else:
                    condition_id = str(concentration) if isinstance(concentration, str) else action.id
                    duration = None
                conditions = [
                    dict(condition)
                    for condition in (actor.get("conditions") or [])
                    if isinstance(condition, Mapping) and condition.get("id") != condition_id
                ]
                conditions.append(
                    {
                        "id": condition_id,
                        "source": action.id,
                        "target": actor_id,
                        "start_round": claimed.round,
                        "start_turn": claimed.turn_index,
                        "duration": duration,
                        "visibility": "public",
                    }
                )
                actor_updates["conditions"] = conditions
            updated_state = apply_action_result(
                claimed,
                ActionApplication(
                    action_id=action_key,
                    actor_id=actor_id,
                    budget_cost=resolution.budget_cost,
                    target_updates=target_updates,
                    actor_updates=actor_updates,
                    event=public_action_event(resolution),
                ),
                claim_token=claim_token,
            )
            sheet_update = _sheet_document_update(actor_sheet, actor_document) if actor_sheet is not None else None
            expected_documents = (
                [("sheet", actor_id, actor_document)]
                if sheet_update is not None and actor_document is not None
                else []
            )
            document_updates = [sheet_update] if sheet_update is not None else []
            committed, stored = await ctx.services.store.commit_idempotent_room_mutation(
                ctx.chat_key,
                action_key,
                resolution.to_dict(),
                expected_state=[("combat_state", claimed.json())],
                state_updates=[("combat_state", updated_state.json())],
                expected_documents=expected_documents,
                document_updates=document_updates,
            )
            if not committed:
                return ctx.i18n.t("commands.action.replayed", result=json.dumps(stored, ensure_ascii=False, sort_keys=True))
            if resolution.check is not None:
                ctx.dice(
                    "action",
                    expr=resolution.check.rolled.expression,
                    rolls=list(resolution.check.rolled.dice),
                    total=resolution.check.rolled.total,
                )
            display_result = resolution.to_dict() if keeper else public_action_event(resolution)
            return ctx.i18n.t(
                "commands.action.result",
                id=action.id,
                success=str(resolution.succeeded).lower(),
                result=json.dumps(display_result, ensure_ascii=False, sort_keys=True),
            )
        except (ActionResolutionError, CombatError, ValueError) as exc:
            try:
                released = release_claim(claimed, claim_token)
                await manager.save(released, expected_raw=claimed.json())
            except Exception:
                pass
            return ctx.fail(ctx.i18n.t(f"commands.{kind}.failed", error=str(exc)))

    async def cmd_attack(self, ctx: CommandCtx) -> str:
        return await self._run_typed_action(ctx, "attack")

    async def cmd_typed_cast(self, ctx: CommandCtx) -> str:
        return await self._run_typed_action(ctx, "cast")

    async def cmd_combat(self, ctx: CommandCtx) -> str:
        """Read or mutate the strict current-actor combat state."""
        pack = await ctx.services.room_rulepack(ctx.raw_ctx)
        if getattr(pack, "runtime_spec", None) is None:
            return _runtime_unsupported(ctx)
        parts = ctx.args.split()
        action = (parts[0].casefold() if parts else "status")
        manager = CombatManager(ctx.services.store, ctx.chat_key)
        try:
            state = await manager.get()
            if action in {"status", "show"}:
                return _state_text(ctx, state) if state is not None else ctx.i18n.t("commands.combat.empty")
            if action == "start":
                if not _is_keeper(ctx.raw_ctx):
                    return ctx.fail(ctx.i18n.t("commands.combat.keeper_only"))
                if state is not None and state.phase == "active":
                    return ctx.fail(ctx.i18n.t("commands.combat.already_active"))
                if state is not None and state.phase == "pending":
                    started = start_combat(state, budget=_budget_template(pack))
                    if not await manager.save(started, expected_raw=state.json()):
                        return ctx.fail(ctx.i18n.t("commands.combat.conflict"))
                    return _state_text(ctx, started)
                roster = await ctx.services.characters.get_party_roster(ctx.chat_key)
                entries = [
                    {
                        "id": str(item.get("name")),
                        "name": str(item.get("name")),
                        "initiative": int(item.get("initiative", 0) or 0),
                        "controller": "human",
                        "controller_id": ctx.user_id,
                    }
                    for item in roster
                    if isinstance(item, dict) and str(item.get("name") or "").strip()
                ]
                if not entries:
                    return ctx.fail(ctx.i18n.t("commands.combat.no_combatants"))
                encounter_id = parts[1] if len(parts) > 1 else f"{ctx.chat_key}:combat"
                created = create_combat(encounter_id, entries, budget=_budget_template(pack))
                started = start_combat(created, budget=_budget_template(pack))
                if not await manager.save(started, expected_raw=None):
                    return ctx.fail(ctx.i18n.t("commands.combat.conflict"))
                return _state_text(ctx, started)
            if state is None:
                return ctx.fail(ctx.i18n.t("commands.combat.empty"))
            if action == "action":
                ctx.args = " ".join(parts[1:])
                return await self._run_typed_action(ctx, "action")
            if action == "join":
                if not _is_keeper(ctx.raw_ctx):
                    return ctx.fail(ctx.i18n.t("commands.combat.keeper_only"))
                if len(parts) < 2:
                    return ctx.fail(ctx.i18n.t("commands.combat.usage"))
                initiative = int(parts[2]) if len(parts) > 2 else 0
                updated = join_combat(
                    state,
                    parts[1],
                    initiative=initiative,
                    controller="keeper",
                    controller_id="keeper",
                    budget=_budget_template(pack),
                )
            elif action == "remove":
                if not _is_keeper(ctx.raw_ctx) or len(parts) < 2:
                    return ctx.fail(ctx.i18n.t("commands.combat.usage"))
                updated = remove_combatant(state, parts[1])
            elif action == "end":
                if not _is_keeper(ctx.raw_ctx):
                    return ctx.fail(ctx.i18n.t("commands.combat.keeper_only"))
                updated = end_combat(state)
            elif action == "next":
                if state.current is None:
                    return ctx.fail(ctx.i18n.t("commands.combat.empty"))
                controller = "keeper" if _is_keeper(ctx.raw_ctx) else ctx.user_id
                claimed = claim_turn(state, state.current, controller, keeper_override=_is_keeper(ctx.raw_ctx))
                updated = end_turn(claimed, state.current, claim_token=str(claimed.claim["token"]))
            else:
                return ctx.fail(ctx.i18n.t("commands.combat.usage"))
            raw = state.json()
            if not await manager.save(updated, expected_raw=raw):
                return ctx.fail(ctx.i18n.t("commands.combat.conflict"))
            return _state_text(ctx, updated)
        except (CombatError, ValueError) as exc:
            return ctx.fail(ctx.i18n.t("commands.combat.failed", error=str(exc)))
