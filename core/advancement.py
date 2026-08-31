"""Deterministic D&D-style advancement transactions.

The rule pack owns progression tables. This module owns the transaction:
the keeper grants an eligible level, the character owner supplies choices, and
the keeper applies one complete plan atomically.
"""

from __future__ import annotations

import copy
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.dice_engine import DiceRoller


class AdvancementError(ValueError):
    """An advancement grant, choice, or apply operation is invalid."""


@dataclass(frozen=True)
class AdvancementPlan:
    id: str
    mode: str
    from_level: int
    to_level: int
    class_name: str = ""
    class_level_from: int = 0
    class_level_to: int = 0
    choices: Mapping[str, Any] = field(default_factory=dict)
    hp_mode: str = "fixed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "from_level": self.from_level,
            "to_level": self.to_level,
            "class_name": self.class_name,
            "class_level_from": self.class_level_from,
            "class_level_to": self.class_level_to,
            "choices": copy.deepcopy(dict(self.choices)),
            "hp_mode": self.hp_mode,
        }


@dataclass(frozen=True)
class AdvancementResult:
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
    field_name = str(_runtime(pack).advancement.get("level_field") or "level")
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


def _normal_class(sheet: Any, pack: Any, value: str = "") -> str:
    name = value or str(getattr(sheet, "character_class", "") or "")
    normalizer = getattr(pack, "normalize_class", None)
    return str(normalizer(name) if normalizer else name).strip().casefold()


def _class_levels(sheet: Any, pack: Any) -> dict[str, int]:
    raw = getattr(sheet, "class_levels", None)
    if isinstance(raw, Mapping):
        result = {
            str(key).casefold(): max(0, int(value))
            for key, value in raw.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        if result:
            return result
    return {_normal_class(sheet, pack): _level(sheet, pack)}


def _tracks(pack: Any) -> Mapping[str, Any]:
    tracks = _runtime(pack).advancement.get("tracks") or {}
    if not isinstance(tracks, Mapping) or not tracks:
        raise AdvancementError("rule pack has no class progression data")  # i18n-exempt: internal validation diagnostic
    return tracks


def _class_track(sheet: Any, pack: Any, class_name: str = "") -> Mapping[str, Any]:
    tracks = _tracks(pack)
    canonical = _normal_class(sheet, pack, class_name)
    selected = tracks.get(canonical) or tracks.get(str(canonical).casefold())
    if selected is None:
        raise AdvancementError(f"no progression data for class {canonical!r}")  # i18n-exempt: internal validation diagnostic
    if not isinstance(selected, Mapping):
        raise AdvancementError("advancement class track must be a mapping")  # i18n-exempt: internal validation diagnostic
    return selected


def _threshold_for(level: int, thresholds: Any) -> int | None:
    if not isinstance(thresholds, list):
        return None
    index = max(0, level - 1)
    if index >= len(thresholds):
        return None
    value = thresholds[index]
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _level_grant(track: Mapping[str, Any], level: int) -> Mapping[str, Any]:
    levels = track.get("levels") or {}
    if not isinstance(levels, Mapping):
        raise AdvancementError("track levels must be a mapping")  # i18n-exempt: internal validation diagnostic
    grant = levels.get(str(level), levels.get(level))
    if not isinstance(grant, Mapping):
        raise AdvancementError(f"class progression has no level {level} data")  # i18n-exempt: internal validation diagnostic
    return grant


def _next_class_level(sheet: Any, pack: Any, class_name: str) -> int:
    return _class_levels(sheet, pack).get(_normal_class(sheet, pack, class_name), 0) + 1


def synchronize_progression(sheet: Any, pack: Any) -> None:
    """Backfill declared class features and class-level state on a sheet.

    Character creation predates runtime advancement and therefore may have no
    feature records at all. This idempotent projection makes level-one features
    visible too, without granting a level or changing any mechanical value.
    """
    levels = _class_levels(sheet, pack)
    sheet.class_levels = levels
    existing = list(getattr(sheet, "features", []) or [])
    known = {
        (str(item.get("class")), str(item.get("id")))
        if isinstance(item, Mapping) and item.get("id")
        else ("", str(item))
        for item in existing
        if (isinstance(item, str) and item.strip()) or (isinstance(item, Mapping) and item.get("id"))
    }
    for class_name, class_level in levels.items():
        try:
            track = _class_track(sheet, pack, class_name)
        except AdvancementError:
            # A homebrew/blank class is valid sheet data, but has no pack-owned
            # progression to project or silently invent.
            continue
        for level in range(1, max(0, int(class_level)) + 1):
            try:
                feature_list = _level_grant(track, level).get("features") or ()
            except AdvancementError:
                break
            for feature in feature_list:
                feature_id = str(feature if isinstance(feature, str) else feature.get("id") or "").strip()
                if not feature_id or (class_name, feature_id) in known or ("", feature_id) in known:
                    continue
                existing.append({"class": class_name, "level": level, "id": feature_id})
                known.add((class_name, feature_id))
    sheet.features = existing


def eligible_level(sheet: Any, pack: Any, *, mode: str, xp: int | None = None) -> int | None:
    """Return the next total level eligible under the selected mode."""
    runtime = _runtime(pack)
    modes = {str(item) for item in runtime.advancement.get("modes", [])}
    if mode not in modes:
        raise AdvancementError(f"advancement mode {mode!r} is not enabled")  # i18n-exempt: internal validation diagnostic
    current = _level(sheet, pack)
    cap = int(runtime.advancement.get("level_cap") or len(runtime.advancement.get("xp_thresholds", []) or []) or 20)
    if current >= cap:
        return None
    if mode == "milestone":
        return current + 1
    current_xp = int(xp if xp is not None else getattr(sheet, "xp", 0) or 0)
    threshold = _threshold_for(current + 1, runtime.advancement.get("xp_thresholds", []))
    return current + 1 if threshold is not None and current_xp >= threshold else None


def grant_advancement(sheet: Any, pack: Any, *, mode: str, xp: int | None = None) -> AdvancementPlan:
    """Create a keeper authorization; no level effect is applied yet."""
    target = eligible_level(sheet, pack, mode=mode, xp=xp)
    if target is None:
        raise AdvancementError("character is not eligible for advancement")  # i18n-exempt: internal validation diagnostic
    state = getattr(sheet, "advancement", None)
    if isinstance(state, dict) and isinstance(state.get("pending"), Mapping):
        raise AdvancementError("an advancement plan is already pending")  # i18n-exempt: internal validation diagnostic
    class_name = _normal_class(sheet, pack)
    class_from = _class_levels(sheet, pack).get(class_name, 0)
    class_to = class_from + 1
    _level_grant(_class_track(sheet, pack, class_name), class_to)
    plan = AdvancementPlan(
        id=str(uuid.uuid4()), mode=str(mode), from_level=_level(sheet, pack), to_level=target,
        class_name=class_name, class_level_from=class_from, class_level_to=class_to,
    )
    state = dict(state) if isinstance(state, dict) else {}
    state["pending"] = plan.to_dict()
    sheet.advancement = state
    return plan


def _parse_asi(value: Any) -> list[tuple[str, int]]:
    if not isinstance(value, str):
        raise AdvancementError("ASI choice must be like STR+2 or STR+1,DEX+1")  # i18n-exempt: internal validation diagnostic
    result: list[tuple[str, int]] = []
    for item in value.split(","):
        match = re.fullmatch(r"\s*([A-Za-z\u4e00-\u9fff_ ]+)\s*([+-]\d+)\s*", item)
        if not match:
            raise AdvancementError("ASI choice must be like STR+2 or STR+1,DEX+1")  # i18n-exempt: internal validation diagnostic
        amount = int(match.group(2))
        if amount <= 0:
            raise AdvancementError("ASI increases must be positive")  # i18n-exempt: internal validation diagnostic
        result.append((match.group(1).strip(), amount))
    if sum(amount for _, amount in result) != 2 or any(amount > 2 for _, amount in result) or len(result) > 2:
        raise AdvancementError("ASI must total exactly +2, split between at most two abilities")  # i18n-exempt: internal validation diagnostic
    return result


def _validate_asi(sheet: Any, pack: Any, value: Any) -> list[tuple[str, int]]:
    from core.sheets import sheet_value

    result: list[tuple[str, int]] = []
    seen: set[str] = set()
    for raw_name, amount in _parse_asi(value):
        canonical = pack.resolve_skill(raw_name)
        if canonical not in {"力量", "敏捷", "体质", "智力", "感知", "魅力"}:
            raise AdvancementError(f"ASI target {raw_name!r} is not an ability score")  # i18n-exempt: internal validation diagnostic
        if canonical in seen:
            raise AdvancementError("ASI cannot list the same ability twice")  # i18n-exempt: internal validation diagnostic
        if sheet_value(sheet, pack, canonical) + amount > 20:
            raise AdvancementError("ASI cannot raise an ability score above 20")  # i18n-exempt: internal validation diagnostic
        seen.add(canonical)
        result.append((canonical, amount))
    return result


def _check_multiclass_prerequisite(sheet: Any, pack: Any, class_name: str) -> None:
    requirements = (_runtime(pack).advancement.get("multiclass_prerequisites") or {}).get(class_name)
    if not isinstance(requirements, Mapping):
        return
    from core.sheets import sheet_value

    for ability, minimum in requirements.items():
        if sheet_value(sheet, pack, str(ability)) < int(minimum):
            raise AdvancementError(f"multiclass prerequisite for {class_name!r} is not met")  # i18n-exempt: internal validation diagnostic


def choose_advancement(sheet: Any, pack: Any, choices: Mapping[str, Any], *, hp_mode: str = "fixed") -> AdvancementPlan:
    """Validate and persist the owner's choices without applying effects."""
    pending = (getattr(sheet, "advancement", {}) or {}).get("pending")
    if not isinstance(pending, Mapping):
        raise AdvancementError("no advancement plan is pending")  # i18n-exempt: internal validation diagnostic
    if hp_mode not in {"fixed", "rolled"}:
        raise AdvancementError("hp mode must be fixed or rolled")  # i18n-exempt: internal validation diagnostic
    base_class = str(pending.get("class_name") or _normal_class(sheet, pack))
    selected_class = _normal_class(sheet, pack, str(choices.get("class") or base_class))
    if selected_class != base_class:
        _check_multiclass_prerequisite(sheet, pack, selected_class)
    class_from = _class_levels(sheet, pack).get(selected_class, 0)
    class_to = class_from + 1
    grant = _level_grant(_class_track(sheet, pack, selected_class), class_to)
    required = [str(item) for item in (grant.get("choices") or [])]
    captured = {str(key): copy.deepcopy(value) for key, value in choices.items()}
    for key in required:
        if key not in captured or str(captured[key]).strip() == "":
            raise AdvancementError(f"advancement choice {key!r} is required")  # i18n-exempt: internal validation diagnostic
    if "asi" in required:
        _validate_asi(sheet, pack, captured["asi"])
    if "subclass" in required and not isinstance(captured["subclass"], str):
        raise AdvancementError("subclass choice must be text")  # i18n-exempt: internal validation diagnostic
    plan = AdvancementPlan(
        id=str(pending.get("id") or ""), mode=str(pending.get("mode") or ""),
        from_level=int(pending.get("from_level", 0)), to_level=int(pending.get("to_level", 0)),
        class_name=selected_class, class_level_from=class_from, class_level_to=class_to,
        choices=captured, hp_mode=hp_mode,
    )
    if not plan.id or plan.to_level != plan.from_level + 1:
        raise AdvancementError("pending advancement plan is invalid")  # i18n-exempt: internal validation diagnostic
    state = dict(getattr(sheet, "advancement", {}) or {})
    state["pending"] = plan.to_dict()
    sheet.advancement = state
    return plan


def _hp_gain(track: Mapping[str, Any], grant: Mapping[str, Any], *, hp_mode: str, roller: DiceRoller, constitution_modifier: int) -> tuple[int, dict[str, Any]]:
    expression = str(grant.get("hp_roll") or track.get("hit_die") or "").strip()
    if not expression:
        raise AdvancementError("class progression has no hit die")  # i18n-exempt: internal validation diagnostic
    if hp_mode == "rolled":
        roll = roller.roll_detail(expression)
        gain = max(1, int(roll.total) + constitution_modifier)
        return gain, {"mode": "rolled", "expression": expression, "dice": list(roll.dice), "total": roll.total, "constitution_modifier": constitution_modifier}
    average = {"1d6": 4, "1d8": 5, "1d10": 6, "1d12": 7}.get(expression)
    if average is None:
        raise AdvancementError(f"no fixed HP average for hit die {expression!r}")  # i18n-exempt: internal validation diagnostic
    gain = max(1, average + constitution_modifier)
    return gain, {"mode": "fixed", "expression": expression, "value": gain, "constitution_modifier": constitution_modifier}


def apply_advancement(sheet: Any, pack: Any, *, roller: DiceRoller, constitution_modifier: int | None = None) -> AdvancementResult:
    """Apply one complete pending plan exactly once."""
    pending = (getattr(sheet, "advancement", {}) or {}).get("pending")
    if not isinstance(pending, Mapping):
        raise AdvancementError("no advancement plan is pending")  # i18n-exempt: internal validation diagnostic
    plan = AdvancementPlan(
        id=str(pending.get("id") or ""), mode=str(pending.get("mode") or ""),
        from_level=int(pending.get("from_level", 0)), to_level=int(pending.get("to_level", 0)),
        class_name=str(pending.get("class_name") or _normal_class(sheet, pack)),
        class_level_from=int(pending.get("class_level_from", 0)), class_level_to=int(pending.get("class_level_to", 0)),
        choices=dict(pending.get("choices") or {}), hp_mode=str(pending.get("hp_mode") or "fixed"),
    )
    state = dict(getattr(sheet, "advancement", {}) or {})
    if state.get("applied_id") == plan.id:
        return AdvancementResult(plan, False, 0, _level(sheet, pack))
    if plan.to_level != _level(sheet, pack) + 1:
        raise AdvancementError("advancement level is stale")  # i18n-exempt: internal validation diagnostic
    levels = _class_levels(sheet, pack)
    if levels.get(plan.class_name, 0) != plan.class_level_from:
        raise AdvancementError("class progression is stale")  # i18n-exempt: internal validation diagnostic
    track = _class_track(sheet, pack, plan.class_name)
    grant = _level_grant(track, plan.class_level_to)
    required = [str(item) for item in (grant.get("choices") or [])]
    if any(key not in plan.choices for key in required):
        raise AdvancementError("advancement choices are incomplete")  # i18n-exempt: internal validation diagnostic
    asi = _validate_asi(sheet, pack, plan.choices["asi"]) if "asi" in required else []
    if constitution_modifier is None:
        from core.sheets import sheet_value

        constitution_modifier = sheet_value(sheet, pack, "体质调整值")
    hp_gain, hp_roll = _hp_gain(track, grant, hp_mode=plan.hp_mode, roller=roller, constitution_modifier=int(constitution_modifier))
    from core.character_manager import get_hit_points, set_hit_points
    from core.sheets import set_sheet_value, sheet_value

    current_hp, current_max = get_hit_points(sheet)
    old_constitution_modifier = sheet_value(sheet, pack, "体质调整值")
    set_hit_points(sheet, maximum=current_max + hp_gain, current=current_hp + hp_gain, allow_raise_max=True)
    for canonical, amount in asi:
        set_sheet_value(sheet, pack, canonical, sheet_value(sheet, pack, canonical) + amount)
    _set_level(sheet, pack, plan.to_level)
    # A Constitution modifier change raises or lowers hit-point maximum by
    # the character's total level, in addition to the new hit die.
    new_constitution_modifier = sheet_value(sheet, pack, "体质调整值")
    constitution_delta = new_constitution_modifier - old_constitution_modifier
    if constitution_delta:
        current_hp, current_max = get_hit_points(sheet)
        adjustment = constitution_delta * plan.to_level
        set_hit_points(sheet, maximum=current_max + adjustment, current=current_hp + adjustment, allow_raise_max=True)
    levels[plan.class_name] = plan.class_level_to
    sheet.class_levels = levels
    if "subclass" in plan.choices:
        sheet.subclass = str(plan.choices["subclass"]).strip()
    features = tuple(copy.deepcopy(grant.get("features") or ()))
    existing_features = list(getattr(sheet, "features", []) or [])
    existing_features.extend(
        {"class": plan.class_name, "level": plan.class_level_to, "id": item} if isinstance(item, str) else copy.deepcopy(item)
        for item in features
    )
    sheet.features = existing_features
    state["applied_id"] = plan.id
    state["last_result"] = {
        "hp_gain": hp_gain,
        "hp_roll": hp_roll,
        "features": list(features),
        "asi": [{"ability": key, "amount": amount} for key, amount in asi],
    }
    state.pop("pending", None)
    sheet.advancement = state
    return AdvancementResult(plan, True, hp_gain, plan.to_level, features, {"hp": hp_roll, "asi": state["last_result"]["asi"]})


def cancel_advancement(sheet: Any) -> bool:
    state = getattr(sheet, "advancement", None)
    if not isinstance(state, dict) or "pending" not in state:
        return False
    state.pop("pending", None)
    sheet.advancement = state
    return True
