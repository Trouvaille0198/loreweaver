"""Deterministic typed damage, defenses, cover, and temporary health."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


class DamageError(ValueError):
    """A damage component or defense declaration is invalid."""


@dataclass(frozen=True)
class DefenseProfile:
    """Generic defensive tags and bounded factors for one target."""

    immunity: frozenset[str] = frozenset()
    resistance: frozenset[str] = frozenset()
    vulnerability: frozenset[str] = frozenset()
    factors: Mapping[str, float] = field(default_factory=dict)
    cover: Any = None
    nonmagical: bool | None = None
    silvered: bool | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> DefenseProfile:
        mapping = dict(raw or {})
        def tags(key: str) -> frozenset[str]:
            value = mapping.get(key) or []
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, (list, tuple, set, frozenset)):
                raise DamageError(f"defense {key} must be a list")
            return frozenset(str(item) for item in value)
        factors_raw = mapping.get("factors") or {}
        if not isinstance(factors_raw, Mapping):
            raise DamageError("defense factors must be a mapping")  # i18n-exempt: internal validation diagnostic
        factors: dict[str, float] = {}
        for key, value in factors_raw.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise DamageError(f"defense factor {key!r} must be a non-negative number")  # i18n-exempt: internal validation diagnostic
            factors[str(key)] = float(value)
        return cls(
            immunity=tags("immunity"),
            resistance=tags("resistance"),
            vulnerability=tags("vulnerability"),
            factors=factors,
            cover=mapping.get("cover"),
            nonmagical=mapping.get("nonmagical"),
            silvered=mapping.get("silvered"),
        )


@dataclass(frozen=True)
class DamageOutcome:
    """The complete arithmetic evidence for one damage component."""

    raw: int
    adjusted: int
    temporary_absorbed: int
    health_damage: int
    factor: float
    tags: tuple[str, ...] = ()
    immune: bool = False
    resistant: bool = False
    vulnerable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "adjusted": self.adjusted,
            "temporary_absorbed": self.temporary_absorbed,
            "health_damage": self.health_damage,
            "factor": self.factor,
            "tags": list(self.tags),
            "immune": self.immune,
            "resistant": self.resistant,
            "vulnerable": self.vulnerable,
        }


def _factor_for(damage_type: str, defenses: DefenseProfile, tags: Iterable[str]) -> tuple[float, bool, bool, bool]:
    damage_id = str(damage_type)
    immune = damage_id in defenses.immunity
    resistant = damage_id in defenses.resistance
    vulnerable = damage_id in defenses.vulnerability
    if immune:
        return 0.0, immune, resistant, vulnerable
    # Resistance and vulnerability cancel before any custom bounded factors.
    factor = 1.0
    if resistant and not vulnerable:
        factor *= 0.5
    elif vulnerable and not resistant:
        factor *= 2.0
    for tag in tags:
        factor *= float(defenses.factors.get(str(tag), 1.0))
    factor *= float(defenses.factors.get(damage_id, 1.0))
    return factor, immune, resistant, vulnerable


def apply_damage(
    amount: int,
    damage_type: str,
    *,
    defenses: DefenseProfile | Mapping[str, Any] | None = None,
    temporary_health: int = 0,
    tags: Iterable[str] = (),
) -> DamageOutcome:
    """Apply factors in fixed order, then absorb adjusted damage with temporary health."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
        raise DamageError("damage amount must be a non-negative number")  # i18n-exempt: internal validation diagnostic
    if isinstance(temporary_health, bool) or not isinstance(temporary_health, (int, float)) or temporary_health < 0:
        raise DamageError("temporary health must be a non-negative number")  # i18n-exempt: internal validation diagnostic
    profile = defenses if isinstance(defenses, DefenseProfile) else DefenseProfile.from_mapping(defenses)
    tag_tuple = tuple(str(tag) for tag in tags)
    raw = max(0, int(amount))
    factor, immune, resistant, vulnerable = _factor_for(str(damage_type), profile, tag_tuple)
    adjusted = 0 if immune else max(0, int(math.floor(raw * factor)))
    absorbed = min(max(0, int(temporary_health)), adjusted)
    return DamageOutcome(
        raw=raw,
        adjusted=adjusted,
        temporary_absorbed=absorbed,
        health_damage=adjusted - absorbed,
        factor=factor,
        tags=tag_tuple,
        immune=immune,
        resistant=resistant,
        vulnerable=vulnerable,
    )


def cover_modifier(cover: Any, declaration: Mapping[str, Any] | None) -> dict[str, Any]:
    """Resolve a pack-declared cover level to generic attack/defense modifiers."""
    if cover is None:
        return {"roll_modifier": 0, "defense_factor": 1.0, "action_block": False}
    raw = (declaration or {}).get(str(cover)) if isinstance(declaration, Mapping) else None
    if raw is None:
        raise DamageError(f"unknown cover level {cover!r}")
    if not isinstance(raw, Mapping):
        raise DamageError("cover declaration must be a mapping")  # i18n-exempt: internal validation diagnostic
    modifier = raw.get("roll_modifier", 0)
    factor = raw.get("defense_factor", 1.0)
    if isinstance(modifier, bool) or not isinstance(modifier, (int, float)):
        raise DamageError("cover roll modifier must be numeric")  # i18n-exempt: internal validation diagnostic
    if isinstance(factor, bool) or not isinstance(factor, (int, float)) or factor < 0:
        raise DamageError("cover defense factor must be non-negative")  # i18n-exempt: internal validation diagnostic
    return {
        "roll_modifier": int(modifier),
        "defense_factor": float(factor),
        "action_block": bool(raw.get("action_block", False)),
    }


def apply_defense_factor(value: int, factor: float) -> int:
    """Clamp a defense-adjusted integer at zero."""
    if isinstance(factor, bool) or not isinstance(factor, (int, float)) or factor < 0:
        raise DamageError("defense factor must be non-negative")  # i18n-exempt: internal validation diagnostic
    return max(0, int(math.floor(max(0, int(value)) * float(factor))))
