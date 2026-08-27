"""Pack-parameterized zero-health and save-ladder transitions."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from core.check_outcome import RollDetail
from core.dice_engine import DiceRoller


class DyingError(ValueError):
    """A dying transition has invalid inputs or pack data."""


@dataclass(frozen=True)
class DyingState:
    """Structured health/death state; no free-form save marks are required."""

    status: str
    health: int
    maximum: int
    successes: int = 0
    failures: int = 0
    stable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "health": self.health,
            "maximum": self.maximum,
            "successes": self.successes,
            "failures": self.failures,
            "stable": self.stable,
        }


def _rule_int(rules: dict[str, Any], key: str, default: int) -> int:
    value = rules.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DyingError(f"dying rule {key!r} must be an integer")
    return value


def enter_zero_health(
    health: int,
    maximum: int,
    *,
    rules: dict[str, Any],
    zero_status: str,
    death_status: str,
    incoming_damage: int = 0,
) -> DyingState:
    """Enter the pack's zero-health status, applying a massive-damage death rule."""
    if health > 0 or maximum < 0:
        raise DyingError("zero-health transition requires non-positive health and a valid maximum")  # i18n-exempt: internal validation diagnostic
    if isinstance(incoming_damage, bool) or not isinstance(incoming_damage, (int, float)) or incoming_damage < 0:
        raise DyingError("incoming damage must be non-negative")  # i18n-exempt: internal validation diagnostic
    threshold_mode = str(rules.get("massive_damage", "none"))
    threshold = _rule_int(rules, "massive_damage_threshold", maximum)
    if threshold_mode not in {"none", "maximum", "maximum_health", "fixed"}:
        raise DyingError("massive damage mode is invalid")  # i18n-exempt: internal validation diagnostic
    threshold_value = max(1, threshold)
    if threshold_mode in {"maximum", "maximum_health"}:
        threshold_value = max(1, int(maximum) + max(0, int(health)))
    if threshold_mode != "none" and int(incoming_damage) >= threshold_value:
        return DyingState(status=str(death_status), health=0, maximum=max(0, int(maximum)))
    return DyingState(status=str(zero_status), health=0, maximum=max(0, int(maximum)))


def roll_dying_save(roller: DiceRoller) -> RollDetail:
    """Roll exactly one declared d20-style save through the shared dice engine."""
    return roller.roll_detail("1d20")


def apply_dying_save(
    state: DyingState,
    roll: RollDetail,
    *,
    rules: dict[str, Any],
    stable_status: str,
    death_status: str,
) -> DyingState:
    """Apply natural-face overrides and success/failure thresholds."""
    if state.status in {stable_status, death_status}:
        return state
    target = _rule_int(rules, "target", 10)
    successes_to_stable = _rule_int(rules, "successes_to_stabilize", 3)
    failures_to_die = _rule_int(rules, "failures_to_die", 3)
    natural_success = _rule_int(rules, "natural_success", 20)
    natural_failure = _rule_int(rules, "natural_failure", 1)
    face = int(roll.dice[0]) if roll.dice else int(roll.total)
    successes = state.successes
    failures = state.failures
    if face == natural_success:
        successes += 2 if bool(rules.get("natural_success_double", True)) else 1
    elif face == natural_failure:
        failures += 2 if bool(rules.get("natural_failure_double", True)) else 1
    elif int(roll.total) >= target:
        successes += 1
    else:
        failures += 1
    if failures >= failures_to_die:
        return DyingState(death_status, 0, state.maximum, successes, failures, False)
    if successes >= successes_to_stable:
        return DyingState(stable_status, 0, state.maximum, successes, failures, True)
    return DyingState(state.status, 0, state.maximum, successes, failures, False)


def apply_dying_damage(
    state: DyingState,
    amount: int,
    *,
    critical: bool,
    rules: dict[str, Any],
    death_status: str,
) -> DyingState:
    """Damage while at zero health increments structured failures."""
    if state.status == death_status:
        return state
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
        raise DyingError("dying damage must be non-negative")  # i18n-exempt: internal validation diagnostic
    increment = _rule_int(rules, "critical_failure_increment" if critical else "failure_increment", 2 if critical else 1)
    failures_to_die = _rule_int(rules, "failures_to_die", 3)
    failures = state.failures + max(0, increment)
    if failures >= failures_to_die:
        return DyingState(death_status, 0, state.maximum, state.successes, failures, False)
    return DyingState(state.status, 0, state.maximum, state.successes, failures, False)


def heal_dying(
    state: DyingState,
    amount: int,
    *,
    wake_status: str,
    dead_status: str = "dead",
) -> DyingState:
    """Healing clears structured save marks and wakes a non-dead target."""
    if state.status == dead_status or amount <= 0:
        return state
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise DyingError("healing must be numeric")  # i18n-exempt: internal validation diagnostic
    health = min(state.maximum, max(0, int(amount)))
    return DyingState(wake_status, health, state.maximum, 0, 0, False)


def dying_rules_from_mapping(raw: Any) -> dict[str, Any]:
    """Copy a pack declaration while keeping the resolver's input deterministic."""
    if not isinstance(raw, dict):
        raise DyingError("dying rules must be a mapping")  # i18n-exempt: internal validation diagnostic
    return copy.deepcopy(raw)
