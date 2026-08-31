"""Pack-declared deterministic rest procedures."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.dice_engine import DiceRoller
from core.resources import (
    ResourceMutation,
    ResourceValue,
    recover_by_reset,
    recover_resource,
    resource_values,
    spend_resource,
)


class RestError(ValueError):
    """A rest is ineligible or its declared recovery data is invalid."""


@dataclass(frozen=True)
class RestResult:
    """Structured effects of one completed rest."""

    kind: str
    health_before: int
    health_after: int
    elapsed_seconds: int
    resource_mutations: tuple[ResourceMutation, ...] = ()
    recovery_rolls: tuple[dict[str, Any], ...] = ()
    reset_tags: tuple[str, ...] = ()
    conditions_cleared: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "health_before": self.health_before,
            "health_after": self.health_after,
            "elapsed_seconds": self.elapsed_seconds,
            "resource_mutations": [item.to_dict() for item in self.resource_mutations],
            "recovery_rolls": [dict(item) for item in self.recovery_rolls],
            "reset_tags": list(self.reset_tags),
            "conditions_cleared": self.conditions_cleared,
        }


def _runtime(pack: Any) -> Any:
    runtime = getattr(pack, "runtime_spec", None)
    if runtime is None:
        raise RestError("rule pack has no runtime procedures")  # i18n-exempt: internal validation diagnostic
    return runtime


def _seconds(value: Any, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) < 0:
        raise RestError(f"{path} must be a non-negative number")  # i18n-exempt: internal validation diagnostic
    return int(value)


def _health_pool(values: Mapping[str, ResourceValue]) -> str:
    for pool_id, value in values.items():
        if value.role == "health":
            return pool_id
    raise RestError("no health resource pool is declared")  # i18n-exempt: internal validation diagnostic


def _rest_state(sheet: Any) -> dict[str, Any]:
    value = getattr(sheet, "rest_state", None)
    if not isinstance(value, dict):
        value = {}
        sheet.rest_state = value
    return value


def complete_rest(
    sheet: Any,
    pack: Any,
    kind: str,
    *,
    roller: DiceRoller,
    recovery_dice: Sequence[str] = (),
    modifiers: Mapping[str, int] | None = None,
    elapsed_seconds: int = 0,
    fiction_completed: bool = True,
) -> RestResult:
    """Complete a declared rest; interrupted fiction produces no effects."""
    if not fiction_completed:
        raise RestError("rest fiction did not complete")  # i18n-exempt: internal validation diagnostic
    runtime = _runtime(pack)
    procedure = runtime.rests.get(str(kind))
    if procedure is None:
        raise RestError(f"rest procedure {kind!r} is not declared")  # i18n-exempt: internal validation diagnostic
    elapsed = _seconds(elapsed_seconds, path="elapsed_seconds")
    values = resource_values(sheet, pack)
    health_id = _health_pool(values)
    health_before = values[health_id].current
    mutations: list[ResourceMutation] = []
    recovery_rolls: list[dict[str, Any]] = []
    modifiers_map = {str(key): int(value) for key, value in (modifiers or {}).items()}

    if kind == "short":
        declared = procedure.get("recovery_dice", [])
        declared_ids = {str(declared)} if isinstance(declared, str) else {str(item) for item in (declared or [])}
        for pool_id in recovery_dice:
            pool_key = str(pool_id)
            if declared_ids and pool_key not in declared_ids:
                raise RestError(f"recovery pool {pool_key!r} is not allowed by this rest")  # i18n-exempt: internal validation diagnostic
            value = values.get(pool_key)
            if value is None or value.die is None or value.current <= 0:
                raise RestError(f"recovery pool {pool_key!r} is unavailable")  # i18n-exempt: internal validation diagnostic
            die_mutation = spend_resource(sheet, pack, pool_key, 1)
            mutations.append(die_mutation)
            roll = roller.roll_detail(value.die)
            bonus = modifiers_map.get(pool_key, 0)
            healing = max(0, int(roll.total) + bonus)
            heal_mutation = recover_resource(sheet, pack, health_id, healing)
            mutations.append(heal_mutation)
            recovery_rolls.append({"pool": pool_key, "expression": roll.expression, "dice": list(roll.dice), "total": roll.total, "modifier": bonus, "healing": heal_mutation.delta})
        reset_tags = tuple(str(item) for item in procedure.get("reset", []) or [])
        for tag in reset_tags:
            mutations.extend(recover_by_reset(sheet, pack, tag))
    else:
        state = _rest_state(sheet)
        cooldown = _seconds(procedure.get("cooldown", 0), path="rest.cooldown")
        last = state.get("last_long_elapsed")
        if last is not None and elapsed < int(last) + cooldown:
            raise RestError("long rest cooldown has not elapsed")  # i18n-exempt: internal validation diagnostic
        mutations.append(recover_resource(sheet, pack, health_id))
        reset_tags = tuple(str(item) for item in procedure.get("reset", []) or [])
        for tag in reset_tags:
            mutations.extend(recover_by_reset(sheet, pack, tag))
        recovery = procedure.get("recover") or {}
        if isinstance(recovery, Mapping) and recovery.get("hit_dice") == "half":
            # D&D 5e restores up to half of the character's total Hit Dice on
            # a long rest, rounded down; the player may choose which die types.
            # The command has no interactive allocation yet, so allocate the
            # legal pool deterministically without exceeding spent dice.
            recovery_pools = [value for value in resource_values(sheet, pack).values() if value.role == "recovery_die"]
            remaining = sum(value.maximum or 0 for value in recovery_pools) // 2
            for value in recovery_pools:
                if remaining <= 0:
                    break
                amount = min(remaining, max(0, (value.maximum or 0) - value.current))
                if amount:
                    mutations.append(recover_resource(sheet, pack, value.id, amount))
                    remaining -= amount
        state["last_long_elapsed"] = elapsed + _seconds(procedure.get("advance_time", 0), path="rest.advance_time")
        sheet.conditions = []
        sheet.rest_state = copy.deepcopy(state)
    health_after = resource_values(sheet, pack)[health_id].current
    return RestResult(
        kind=str(kind),
        health_before=health_before,
        health_after=health_after,
        elapsed_seconds=_seconds(procedure.get("advance_time", 0), path="rest.advance_time") if kind == "long" else 0,
        resource_mutations=tuple(mutations),
        recovery_rolls=tuple(recovery_rolls),
        reset_tags=reset_tags,
        conditions_cleared=kind == "long",
    )
