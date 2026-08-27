"""Pack-defined, two-party advancement transactions."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.dice_engine import DiceRoller


class AdvancementError(ValueError):
    """An advancement grant, choice, or apply operation is invalid."""


@dataclass(frozen=True)
class AdvancementPlan:
    """Validated pending level plan; no mutation happens until apply."""

    id: str
    mode: str
    from_level: int
    to_level: int
    choices: Mapping[str, Any] = field(default_factory=dict)
    hp_mode: str = "fixed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "from_level": self.from_level,
            "to_level": self.to_level,
            "choices": copy.deepcopy(dict(self.choices)),
            "hp_mode": self.hp_mode,
        }


@dataclass(frozen=True)
class AdvancementResult:
    """Observable result of one successful or idempotent apply."""

    plan: AdvancementPlan
    applied: bool
    hp_gain: int
    new_level: int
    features: tuple[Any, ...] = ()
    resource_changes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "applied": self.applied,
            "hp_gain": self.hp_gain,
            "new_level": self.new_level,
            "features": list(self.features),
            "resource_changes": dict(self.resource_changes),
        }


def _runtime(pack: Any) -> Any:
    runtime = getattr(pack, "runtime_spec", None)
    if runtime is None or not runtime.advancement:
        raise AdvancementError("rule pack has no advancement declaration")  # i18n-exempt: internal validation diagnostic
    return runtime


def _level(sheet: Any, pack: Any) -> int:
    declaration = _runtime(pack).advancement
    field_name = str(declaration.get("level_field") or "level")
    value = getattr(sheet, field_name, None)
    if value is None:
        try:
            from core.sheets import sheet_value

            value = sheet_value(sheet, pack, field_name)
        except Exception:
            value = 1
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdvancementError("sheet level is not numeric")  # i18n-exempt: internal validation diagnostic
    return max(1, int(value))


def _set_level(sheet: Any, pack: Any, value: int) -> None:
    field_name = str(_runtime(pack).advancement.get("level_field") or "level")
    if hasattr(sheet, field_name):
        setattr(sheet, field_name, int(value))
        return
    from core.sheets import set_sheet_value

    set_sheet_value(sheet, pack, field_name, int(value))


def _class_track(sheet: Any, pack: Any) -> Mapping[str, Any]:
    tracks = _runtime(pack).advancement.get("tracks") or {}
    if not isinstance(tracks, Mapping):
        raise AdvancementError("advancement tracks must be a mapping")  # i18n-exempt: internal validation diagnostic
    class_name = str(getattr(sheet, "character_class", "") or "")
    selected = tracks.get(class_name) or tracks.get("default") or {}
    if not isinstance(selected, Mapping):
        raise AdvancementError("advancement track must be a mapping")  # i18n-exempt: internal validation diagnostic
    return selected


def _threshold_for(level: int, thresholds: Any) -> int | None:
    if not isinstance(thresholds, list):
        return None
    index = max(0, level - 1)
    if index >= len(thresholds):
        return None
    value = thresholds[index]
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def eligible_level(sheet: Any, pack: Any, *, mode: str, xp: int | None = None) -> int | None:
    """Return the next level only when the pack-declared mode makes it eligible."""
    runtime = _runtime(pack)
    modes = {str(item) for item in runtime.advancement.get("modes", [])}
    if mode not in modes:
        raise AdvancementError(f"advancement mode {mode!r} is not enabled")  # i18n-exempt: internal validation diagnostic
    current = _level(sheet, pack)
    if mode == "milestone":
        return current + 1
    current_xp = int(xp if xp is not None else getattr(sheet, "xp", 0) or 0)
    threshold = _threshold_for(current + 1, runtime.advancement.get("xp_thresholds", []))
    return current + 1 if threshold is not None and current_xp >= threshold else None


def grant_advancement(sheet: Any, pack: Any, *, mode: str, xp: int | None = None) -> AdvancementPlan:
    """Keeper-side eligibility grant; the owning player still supplies choices."""
    target = eligible_level(sheet, pack, mode=mode, xp=xp)
    if target is None:
        raise AdvancementError("character is not eligible for advancement")  # i18n-exempt: internal validation diagnostic
    state = getattr(sheet, "advancement", None)
    if not isinstance(state, dict):
        state = {}
    plan = AdvancementPlan(
        id=str(uuid.uuid4()),
        mode=str(mode),
        from_level=_level(sheet, pack),
        to_level=target,
        choices={},
    )
    state["pending"] = plan.to_dict()
    sheet.advancement = state
    return plan


def choose_advancement(sheet: Any, pack: Any, choices: Mapping[str, Any], *, hp_mode: str = "fixed") -> AdvancementPlan:
    """Owner-side choice capture without applying any level effects."""
    pending = (getattr(sheet, "advancement", {}) or {}).get("pending")
    if not isinstance(pending, Mapping):
        raise AdvancementError("no advancement plan is pending")  # i18n-exempt: internal validation diagnostic
    if hp_mode not in {"fixed", "rolled"}:
        raise AdvancementError("hp mode must be fixed or rolled")  # i18n-exempt: internal validation diagnostic
    plan = AdvancementPlan(
        id=str(pending.get("id") or ""),
        mode=str(pending.get("mode") or ""),
        from_level=int(pending.get("from_level", 0)),
        to_level=int(pending.get("to_level", 0)),
        choices=copy.deepcopy(dict(choices)),
        hp_mode=hp_mode,
    )
    if not plan.id or plan.to_level != plan.from_level + 1:
        raise AdvancementError("pending advancement plan is invalid")  # i18n-exempt: internal validation diagnostic
    state = dict(getattr(sheet, "advancement", {}) or {})
    state["pending"] = plan.to_dict()
    sheet.advancement = state
    return plan


def _level_grant(track: Mapping[str, Any], level: int) -> Mapping[str, Any]:
    levels = track.get("levels") or {}
    if not isinstance(levels, Mapping):
        raise AdvancementError("track levels must be a mapping")  # i18n-exempt: internal validation diagnostic
    grant = levels.get(str(level), levels.get(level, {}))
    if not isinstance(grant, Mapping):
        raise AdvancementError("level grant must be a mapping")  # i18n-exempt: internal validation diagnostic
    return grant


def _hp_gain(grant: Mapping[str, Any], *, hp_mode: str, roller: DiceRoller, constitution_modifier: int = 0) -> tuple[int, dict[str, Any]]:
    hp = grant.get("hp")
    if hp is None:
        return 0, {}
    if isinstance(hp, (int, float)) and not isinstance(hp, bool):
        return max(0, int(hp)), {"mode": "fixed", "value": int(hp)}
    if not isinstance(hp, Mapping):
        return 0, {}
    if hp_mode == "rolled":
        expression = str(hp.get("roll") or hp.get("die") or "").strip()
        if not expression:
            raise AdvancementError("rolled HP requires a die expression")  # i18n-exempt: internal validation diagnostic
        roll = roller.roll_detail(expression)
        gain = int(roll.total) + int(constitution_modifier)
        return max(1, gain), {"mode": "rolled", "expression": expression, "dice": list(roll.dice), "total": roll.total}
    fixed = hp.get("fixed", hp.get("value"))
    if fixed is None:
        expression = str(hp.get("average") or "").strip()
        if expression:
            fixed = int(expression)
    if isinstance(fixed, bool) or not isinstance(fixed, (int, float)):
        raise AdvancementError("fixed HP requires a numeric value")  # i18n-exempt: internal validation diagnostic
    return max(1, int(fixed) + int(constitution_modifier)), {"mode": "fixed", "value": int(fixed)}


def apply_advancement(
    sheet: Any,
    pack: Any,
    *,
    roller: DiceRoller,
    constitution_modifier: int = 0,
) -> AdvancementResult:
    """Apply a complete pending plan exactly once."""
    pending = (getattr(sheet, "advancement", {}) or {}).get("pending")
    if not isinstance(pending, Mapping):
        raise AdvancementError("no advancement plan is pending")  # i18n-exempt: internal validation diagnostic
    plan = AdvancementPlan(
        id=str(pending.get("id") or ""),
        mode=str(pending.get("mode") or ""),
        from_level=int(pending.get("from_level", 0)),
        to_level=int(pending.get("to_level", 0)),
        choices=dict(pending.get("choices") or {}),
        hp_mode=str(pending.get("hp_mode") or "fixed"),
    )
    state = dict(getattr(sheet, "advancement", {}) or {})
    if state.get("applied_id") == plan.id:
        return AdvancementResult(plan, False, 0, _level(sheet, pack))
    if plan.to_level != _level(sheet, pack) + 1:
        raise AdvancementError("advancement level is stale")  # i18n-exempt: internal validation diagnostic
    track = _class_track(sheet, pack)
    grant = _level_grant(track, plan.to_level)
    required = grant.get("choices") or []
    if not isinstance(required, list) or any(str(item) not in plan.choices for item in required):
        raise AdvancementError("advancement choices are incomplete")  # i18n-exempt: internal validation diagnostic
    hp_gain, hp_roll = _hp_gain(grant, hp_mode=plan.hp_mode, roller=roller, constitution_modifier=constitution_modifier)
    from core.character_manager import get_hit_points, set_hit_points

    current_hp, current_max = get_hit_points(sheet)
    set_hit_points(sheet, maximum=current_max + hp_gain, current=current_hp + hp_gain, allow_raise_max=True)
    _set_level(sheet, pack, plan.to_level)
    features = tuple(copy.deepcopy(grant.get("features") or ()))
    existing_features = list(getattr(sheet, "features", []) or [])
    existing_features.extend(features)
    sheet.features = existing_features
    state["applied_id"] = plan.id
    state["last_result"] = {"hp_gain": hp_gain, "hp_roll": hp_roll, "features": list(features)}
    state.pop("pending", None)
    sheet.advancement = state
    return AdvancementResult(plan, True, hp_gain, plan.to_level, features, {"hp": hp_roll})


def cancel_advancement(sheet: Any) -> bool:
    state = getattr(sheet, "advancement", None)
    if not isinstance(state, dict) or "pending" not in state:
        return False
    state.pop("pending", None)
    sheet.advancement = state
    return True
