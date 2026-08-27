"""Deterministic typed action resolution over pack-declared action specs."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.check_outcome import CheckOutcome, RollDetail
from core.damage import DamageOutcome, DefenseProfile, apply_damage
from core.dice_engine import DiceRoller
from core.resolution import CheckResolver
from core.runtime import ActionSpec, DamageComponentSpec, ResourceCostSpec


class ActionResolutionError(ValueError):
    """A typed action cannot be resolved from the supplied state."""


@dataclass(frozen=True)
class DamageRoll:
    """One rolled component plus its defense arithmetic."""

    damage_type: str
    rolled: RollDetail
    outcome: DamageOutcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.damage_type,
            "roll": {
                "expression": self.rolled.expression,
                "dice": list(self.rolled.dice),
                "total": self.rolled.total,
                "modifiers": dict(self.rolled.modifiers),
            },
            "damage": self.outcome.to_dict(),
        }


def _to_data(value: Any) -> Any:
    method = getattr(value, "to_dict", None)
    return method() if callable(method) else copy.deepcopy(value)

@dataclass(frozen=True)
class ActionResolution:
    """Pure action facts ready for one atomic state/document commit."""


    action_id: str
    actor_id: str
    targets: tuple[str, ...]
    check: CheckOutcome | None = None
    damage: Mapping[str, tuple[DamageRoll, ...]] = field(default_factory=dict)
    budget_cost: Mapping[str, int] = field(default_factory=dict)
    resource_costs: tuple[ResourceCostSpec, ...] = ()
    success_effects: tuple[Any, ...] = ()
    failure_effects: tuple[Any, ...] = ()
    condition_effects: tuple[Any, ...] = ()
    concentration: Any = False
    critical: bool = False

    @property
    def succeeded(self) -> bool:
        return self.check is None or self.check.rank.success

    def to_dict(self) -> dict[str, Any]:
        check: dict[str, Any] | None = None
        if self.check is not None:
            check = {
                "target": self.check.target,
                "margin": self.check.margin,
                "rank": {
                    "id": self.check.rank.id,
                    "tier": self.check.rank.tier,
                    "success": self.check.rank.success,
                    "critical": self.check.rank.critical,
                    "fumble": self.check.rank.fumble,
                },
                "roll": {
                    "expression": self.check.rolled.expression,
                    "dice": list(self.check.rolled.dice),
                    "total": self.check.rolled.total,
                    "modifiers": dict(self.check.rolled.modifiers),
                },
            }
        return {
            "action_id": self.action_id,
            "actor_id": self.actor_id,
            "targets": list(self.targets),
            "check": check,
            "damage": {key: [item.to_dict() for item in values] for key, values in self.damage.items()},
            "budget_cost": dict(self.budget_cost),
            "resource_costs": [item.to_dict() for item in self.resource_costs],
            "success_effects": [_to_data(item) for item in self.success_effects],
            "failure_effects": [_to_data(item) for item in self.failure_effects],
            "condition_effects": [_to_data(item) for item in self.condition_effects],
            "concentration": self.concentration,
            "critical": self.critical,
        }


def public_action_event(resolution: ActionResolution) -> dict[str, Any]:
    """Strip keeper-grade defense arithmetic from a player-facing action fact."""
    payload = resolution.to_dict()
    check = payload.get("check")
    if isinstance(check, dict):
        check.pop("target", None)
    for rolls in payload.get("damage", {}).values():
        for item in rolls:
            damage = item.get("damage") if isinstance(item, dict) else None
            if isinstance(damage, dict):
                for key in ("factor", "immune", "resistant", "vulnerable"):
                    damage.pop(key, None)
    return payload

def _target_count(action: ActionSpec, targets: Sequence[str]) -> None:
    targeting = dict(action.targeting)
    actual = len(targets)
    exact = targeting.get("count")
    minimum = targeting.get("min")
    maximum = targeting.get("max")
    if exact is not None and actual != int(exact):
        raise ActionResolutionError(f"action requires exactly {exact} target(s)")
    if minimum is not None and actual < int(minimum):
        raise ActionResolutionError(f"action requires at least {minimum} target(s)")
    if maximum is not None and actual > int(maximum):
        raise ActionResolutionError(f"action allows at most {maximum} target(s)")


def _cost_values(costs: Mapping[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for key, value in costs.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) < 0:
            raise ActionResolutionError(f"action cost {key!r} must be a non-negative number")  # i18n-exempt: internal validation diagnostic
        result[str(key)] = int(value)
    return result


def _resource_amount(cost: ResourceCostSpec, resources: Mapping[str, Any]) -> int:
    raw = cost.amount
    if isinstance(raw, Mapping):
        if "value" in raw:
            raw = raw["value"]
        elif "ref" in raw:
            raw = resources.get(str(raw["ref"]), 0)
        else:
            raise ActionResolutionError(f"resource cost {cost.pool!r} has no value")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) < 0:
        raise ActionResolutionError(f"resource cost {cost.pool!r} must be non-negative")
    return int(raw)


def validate_resource_costs(costs: Sequence[ResourceCostSpec], resources: Mapping[str, Any]) -> dict[str, int]:
    """Validate all costs before any state mutation."""
    result: dict[str, int] = {}
    for cost in costs:
        amount = _resource_amount(cost, resources)
        current = resources.get(cost.pool)
        if isinstance(current, Mapping):
            current = current.get("current", 0)
        if isinstance(current, bool) or not isinstance(current, (int, float)) or int(current) < amount:
            raise ActionResolutionError(f"resource pool {cost.pool!r} is insufficient")
        result[cost.pool] = result.get(cost.pool, 0) + amount
        if int(current) < result[cost.pool]:
            raise ActionResolutionError(f"resource pool {cost.pool!r} is insufficient")
    return result


def _damage_roll(
    roller: DiceRoller,
    component: DamageComponentSpec,
    *,
    defense: DefenseProfile | Mapping[str, Any] | None,
    temporary_health: int,
    critical: bool,
) -> DamageRoll:
    rolled = roller.roll_detail(component.roll)
    raw_total = rolled.total
    policy = component.critical
    if critical and (policy is None or policy is True or policy == "double"):
        raw_total *= 2
    elif critical and isinstance(policy, Mapping) and policy.get("mode") == "double":
        raw_total *= 2
    outcome = apply_damage(
        raw_total,
        component.type,
        defenses=defense,
        temporary_health=temporary_health,
        tags=component.tags,
    )
    return DamageRoll(damage_type=component.type, rolled=rolled, outcome=outcome)


def resolve_action(
    action: ActionSpec,
    *,
    actor_id: str,
    targets: Sequence[str] = (),
    roller: DiceRoller,
    resolver: CheckResolver | None = None,
    check_target: int | None = None,
    check_modifier: int = 0,
    check_variant: str | None = None,
    check_difficulty: str | None = None,
    resource_values: Mapping[str, Any] | None = None,
    target_defenses: Mapping[str, DefenseProfile | Mapping[str, Any]] | None = None,
    target_temporary_health: Mapping[str, int] | None = None,
) -> ActionResolution:
    """Roll and interpret one action without writing state.

    Callers must commit the returned facts together with combat and sheet state.
    ``CheckResolver.interpret`` supplies semantic critical/success flags; no rank
    identifier is used for branching.
    """
    target_ids = tuple(str(item) for item in targets)
    _target_count(action, target_ids)
    budget_cost = _cost_values(action.cost)
    resource_costs = validate_resource_costs(action.resource_costs, resource_values or {})
    del resource_costs  # the typed specs are retained in the result; values are validated above.

    check: CheckOutcome | None = None
    critical = False
    if action.resolution is not None:
        if resolver is None:
            raise ActionResolutionError("action requires a check resolver")
        rolled = roller.roll_for_check(resolver, modifiers={})
        check = resolver.interpret(
            rolled,
            check_target,
            variant=check_variant,
            difficulty=check_difficulty,
            modifier=int(check_modifier),
        )
        critical = bool(check.rank.critical)
    succeeded = check is None or check.rank.success
    damage: dict[str, tuple[DamageRoll, ...]] = {}
    if succeeded and action.damage:
        for target_id in target_ids:
            target_defense = (target_defenses or {}).get(target_id)
            temporary = int((target_temporary_health or {}).get(target_id, 0) or 0)
            damage[target_id] = tuple(
                _damage_roll(
                    roller,
                    component,
                    defense=target_defense,
                    temporary_health=temporary,
                    critical=critical,
                )
                for component in action.damage
            )
    return ActionResolution(
        action_id=action.id,
        actor_id=str(actor_id),
        targets=target_ids,
        check=check,
        damage=damage,
        budget_cost=budget_cost,
        resource_costs=action.resource_costs,
        success_effects=action.on_success if succeeded else (),
        failure_effects=action.on_failure if not succeeded else (),
        condition_effects=action.conditions if succeeded else (),
        concentration=action.concentration,
        critical=critical,
    )
