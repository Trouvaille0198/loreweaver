"""Generic concentration linkage and damage-triggered saves."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.check_outcome import CheckOutcome
from core.conditions import ConditionState


class ConcentrationError(ValueError):
    """A concentration declaration or check is invalid."""


@dataclass(frozen=True)
class ConcentrationResult:
    """Whether a linked effect survives a damage-triggered check."""

    dc: int
    outcome: CheckOutcome | None
    maintained: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "dc": self.dc,
            "maintained": self.maintained,
            "outcome": None if self.outcome is None else {
                "target": self.outcome.target,
                "margin": self.outcome.margin,
                "success": self.outcome.rank.success,
                "critical": self.outcome.rank.critical,
            },
        }


def start_concentration(
    conditions: Iterable[ConditionState],
    condition: ConditionState,
    *,
    concentration_id: str,
) -> tuple[ConditionState, ...]:
    """Replace the actor's previous concentration effect with the new one."""
    return tuple(
        item
        for item in conditions
        if not (item.target == condition.target and item.id == str(concentration_id))
    ) + (condition,)


def concentration_dc(damage: int, declaration: Mapping[str, Any] | None = None) -> int:
    """Compute a bounded damage-trigger DC from pack data."""
    if isinstance(damage, bool) or not isinstance(damage, (int, float)) or damage < 0:
        raise ConcentrationError("concentration damage must be non-negative")  # i18n-exempt: internal validation diagnostic
    config = dict(declaration or {})
    minimum = config.get("minimum", 10)
    divisor = config.get("divisor", 2)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (minimum, divisor)) or divisor <= 0:
        raise ConcentrationError("concentration DC declaration is invalid")  # i18n-exempt: internal validation diagnostic
    return max(int(minimum), int(damage) // int(divisor))


def resolve_concentration(
    damage: int,
    *,
    declaration: Mapping[str, Any] | None = None,
    outcome: CheckOutcome | None = None,
) -> ConcentrationResult:
    """Return a pure concentration check result; rolling remains caller-owned."""
    dc = concentration_dc(damage, declaration)
    return ConcentrationResult(dc=dc, outcome=outcome, maintained=outcome is not None and outcome.rank.success)
