"""Pack-declared runtime mechanics contracts.

This module is deliberately rules-system neutral.  It validates the generic runtime
vocabulary used by a rule pack and keeps pack-owned ids and labels as data.  The
runtime manager modules consume these frozen specifications; they never infer game
rules from prose or from a system name.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.condexpr import CondExprError, compile_expression

RUNTIME_SCHEMA_VERSION = 1

MAX_RUNTIME_ENTRIES = 256
MAX_RESOURCE_POOLS = 128
MAX_ACTIONS = 128
MAX_CONDITIONS = 128
MAX_EFFECTS = 32
MAX_DAMAGE_COMPONENTS = 16
MAX_RESET_TAGS = 16
MAX_ID_LENGTH = 96
MAX_LABEL_LENGTH = 256
MAX_DICE_EXPRESSION_LENGTH = 128

GENERIC_EFFECT_OPS = frozenset(
    {
        "resource_delta",
        "roll_modifier",
        "action_block",
        "speed_factor",
        "damage_factor",
        "condition_add",
        "condition_remove",
    }
)

_EXPR_FUNCTIONS = {
    "abs": abs,
    "ceil": math.ceil,
    "floor": math.floor,
    "max": max,
    "min": min,
    "round": round,
}


def resolve_display_label(display: Mapping[str, Any], fallback: str, locale: str | None) -> str:
    """A pack-owned display map's label for ``locale`` (exact match, then ``en``,
    then any declared locale), falling back to ``fallback``."""
    raw = display.get("label", fallback)
    if isinstance(raw, Mapping):
        short = (locale or "en").split("-", 1)[0].split("_", 1)[0]
        return raw.get(short) or raw.get("en") or next(iter(raw.values()), fallback)
    return str(raw)


class RuntimeSpecError(ValueError):
    """A malformed or unsafe ``runtime:`` declaration."""


@dataclass(frozen=True)
class EffectSpec:
    """One closed generic effect operation with pack-owned arguments."""

    op: str
    args: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, **dict(self.args)}


@dataclass(frozen=True)
class ResourceCostSpec:
    """One resource pool cost attached to an action."""

    pool: str
    amount: Any

    def to_dict(self) -> dict[str, Any]:
        return {"pool": self.pool, "amount": self.amount}


@dataclass(frozen=True)
class DamageComponentSpec:
    """One typed damage/effect roll component."""

    roll: str
    type: str
    tags: tuple[str, ...] = ()
    critical: Any = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"roll": self.roll, "type": self.type}
        if self.tags:
            result["tags"] = list(self.tags)
        if self.critical is not None:
            result["critical"] = self.critical
        return result


@dataclass(frozen=True)
class ResourcePoolSpec:
    """A persistent, bounded, pack-named resource pool."""

    id: str
    role: str
    initial: Any
    maximum: Any | None
    die: str | None = None
    group: str = ""
    reset_tags: tuple[str, ...] = ()
    prominent: bool = False
    display: Mapping[str, Any] = field(default_factory=dict)

    @property
    def reset(self) -> tuple[str, ...]:
        """The declared reset tags (short spelling used by older callers)."""
        return self.reset_tags

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": self.role,
            "initial": self.initial,
            "max": self.maximum,
        }
        if self.die is not None:
            result["die"] = self.die
        if self.group:
            result["group"] = self.group
        if self.reset_tags:
            result["reset"] = list(self.reset_tags)
        if self.prominent:
            result["prominent"] = True
        if self.display:
            result["display"] = dict(self.display)
        return result
    def display_label(self, locale: str | None) -> str:
        """The pool's display label for ``locale`` (exact match, then ``en``,
        then any declared locale), falling back to the pool id."""
        return resolve_display_label(self.display, self.id, locale)


@dataclass(frozen=True)
class ConditionSpec:
    """A structured condition declaration and its generic effects."""

    id: str
    duration: Any = None
    end: Any = None
    effects: tuple[EffectSpec, ...] = ()
    visibility: str = "public"
    stacking: str = "replace"
    max_stacks: int | None = None
    display: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"visibility": self.visibility, "stacking": self.stacking}
        if self.duration is not None:
            result["duration"] = self.duration
        if self.end is not None:
            result["end"] = self.end
        if self.effects:
            result["effects"] = [effect.to_dict() for effect in self.effects]
        if self.max_stacks is not None:
            result["max_stacks"] = self.max_stacks
        if self.display:
            result["display"] = dict(self.display)
        return result


@dataclass(frozen=True)
class ActionSpec:
    """A generic action used by sheets, items, spells, and stat blocks."""

    id: str
    cost: Mapping[str, Any] = field(default_factory=dict)
    targeting: Mapping[str, Any] = field(default_factory=dict)
    resolution: Any = None
    roll_modifier: Any = None
    range: Any = None
    on_success: tuple[EffectSpec, ...] = ()
    on_failure: tuple[EffectSpec, ...] = ()
    damage: tuple[DamageComponentSpec, ...] = ()
    resource_costs: tuple[ResourceCostSpec, ...] = ()
    concentration: Any = False
    conditions: tuple[EffectSpec, ...] = ()
    visibility: str = "public"
    label: Mapping[str, str] = field(default_factory=dict)

    @property
    def budget_costs(self) -> Mapping[str, Any]:
        return self.cost

    @property
    def target(self) -> Mapping[str, Any]:
        return self.targeting

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"cost": dict(self.cost), "targeting": dict(self.targeting)}
        if self.resolution is not None:
            result["resolution"] = self.resolution
        if self.roll_modifier is not None:
            result["roll_modifier"] = self.roll_modifier
        if self.range is not None:
            result["range"] = self.range
        if self.on_success:
            result["on_success"] = [effect.to_dict() for effect in self.on_success]
        if self.on_failure:
            result["on_failure"] = [effect.to_dict() for effect in self.on_failure]
        if self.damage:
            result["damage"] = [component.to_dict() for component in self.damage]
        if self.resource_costs:
            result["resource_costs"] = [cost.to_dict() for cost in self.resource_costs]
        if self.concentration:
            result["concentration"] = self.concentration
        if self.conditions:
            result["conditions"] = [effect.to_dict() for effect in self.conditions]
        if self.visibility != "public":
            result["visibility"] = self.visibility
        if self.label:
            result["label"] = dict(self.label)
        return result


@dataclass(frozen=True)
class RuntimeSpec:
    """The complete versioned runtime declaration for one rule pack."""

    version: int
    pools: Mapping[str, ResourcePoolSpec] = field(default_factory=dict)
    initiative: str = ""
    budgets: Mapping[str, Any] = field(default_factory=dict)
    actions: Mapping[str, ActionSpec] = field(default_factory=dict)
    conditions: Mapping[str, ConditionSpec] = field(default_factory=dict)
    damage_types: tuple[str, ...] = ()
    cover: Mapping[str, Any] = field(default_factory=dict)
    dying: Mapping[str, Any] = field(default_factory=dict)
    rests: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    advancement: Mapping[str, Any] = field(default_factory=dict)
    encounters: Mapping[str, Any] = field(default_factory=dict)

    @property
    def resources(self) -> Mapping[str, ResourcePoolSpec]:
        return self.pools

    def action(self, action_id: str) -> ActionSpec | None:
        return self.actions.get(str(action_id))

    def pool(self, pool_id: str) -> ResourcePoolSpec | None:
        return self.pools.get(str(pool_id))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "version": self.version,
            "resources": {"pools": {key: value.to_dict() for key, value in self.pools.items()}},
            "combat": {
                "budgets": dict(self.budgets),
                "actions": {key: value.to_dict() for key, value in self.actions.items()},
                "conditions": {key: value.to_dict() for key, value in self.conditions.items()},
                "damage_types": list(self.damage_types),
                "cover": dict(self.cover),
                "dying": dict(self.dying),
            },
            "rests": {key: dict(value) for key, value in self.rests.items()},
            "advancement": dict(self.advancement),
            "encounters": dict(self.encounters),
        }
        if self.initiative:
            result["combat"]["initiative"] = self.initiative
        return result


# ---------------------------------------------------------------------------
# Shape and safe-expression helpers
# ---------------------------------------------------------------------------


def _fail(pack_id: str, path: str, message: str) -> RuntimeSpecError:
    return RuntimeSpecError(f"rulepack '{pack_id}': runtime.{path} {message}")


def _mapping(pack_id: str, path: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(pack_id, path, "must be a mapping")
    return value


def _known_keys(pack_id: str, path: str, value: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = {str(key) for key in value} - allowed
    if unknown:
        raise _fail(pack_id, path, f"has unknown keys {sorted(unknown)}; allowed: {sorted(allowed)}")


def _bounded_id(pack_id: str, path: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result or len(result) > MAX_ID_LENGTH or any(char.isspace() for char in result):
        raise _fail(pack_id, path, f"must be a non-empty id of at most {MAX_ID_LENGTH} non-space characters")  # i18n-exempt: internal validation diagnostic
    return result


def _label_map(pack_id: str, path: str, value: Any) -> dict[str, str]:
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > MAX_LABEL_LENGTH:
            raise _fail(pack_id, path, "must be a non-empty short label")  # i18n-exempt: internal validation diagnostic
        return {"en": text}
    mapping = _mapping(pack_id, path, value)
    labels = {str(locale): str(text).strip() for locale, text in mapping.items() if str(text).strip()}
    if not labels or any(len(text) > MAX_LABEL_LENGTH for text in labels.values()):
        raise _fail(pack_id, path, "must contain at least one short non-empty label")  # i18n-exempt: internal validation diagnostic
    return labels


def _validate_expr(pack_id: str, path: str, expression: str) -> str:
    text = expression.strip()
    if not text:
        raise _fail(pack_id, path, "expression must not be empty")
    try:
        compile_expression(text, functions=_EXPR_FUNCTIONS)
    except (CondExprError, TypeError, ValueError) as exc:
        raise _fail(pack_id, path, f"has invalid safe expression: {exc}") from exc  # i18n-exempt: internal validation diagnostic
    return text


def _value(pack_id: str, path: str, raw: Any, *, allow_none: bool = False) -> Any:
    if raw is None:
        if allow_none:
            return None
        raise _fail(pack_id, path, "is required")
    if isinstance(raw, Mapping):
        _known_keys(pack_id, path, raw, {"expr", "ref", "value"})
        if "expr" in raw:
            if not isinstance(raw["expr"], str):
                raise _fail(pack_id, f"{path}.expr", "must be a string")
            return {"expr": _validate_expr(pack_id, f"{path}.expr", raw["expr"])}
        if "ref" in raw:
            return {"ref": _bounded_id(pack_id, f"{path}.ref", raw["ref"])}
        if "value" in raw:
            return raw["value"]
        raise _fail(pack_id, path, "must contain expr, ref, or value")  # i18n-exempt: internal validation diagnostic
    if isinstance(raw, str):
        text = raw.strip()
        if not text or len(text) > MAX_LABEL_LENGTH:
            raise _fail(pack_id, path, "must be a non-empty short value")  # i18n-exempt: internal validation diagnostic
        return text
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw
    raise _fail(pack_id, path, f"has unsupported value type {type(raw).__name__}")


def _reset_tags(pack_id: str, path: str, raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    values: Sequence[Any] = [raw] if isinstance(raw, str) else raw
    if not isinstance(values, Sequence) or isinstance(values, (bytes, bytearray)):
        raise _fail(pack_id, path, "must be a string or list")  # i18n-exempt: internal validation diagnostic
    if len(values) > MAX_RESET_TAGS:
        raise _fail(pack_id, path, f"may contain at most {MAX_RESET_TAGS} tags")
    result = tuple(_bounded_id(pack_id, f"{path}[{index}]", item) for index, item in enumerate(values))
    if len(set(result)) != len(result):
        raise _fail(pack_id, path, "must not contain duplicate tags")
    return result


def _effects(pack_id: str, path: str, raw: Any) -> tuple[EffectSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise _fail(pack_id, path, "must be a list")
    if len(raw) > MAX_EFFECTS:
        raise _fail(pack_id, path, f"may contain at most {MAX_EFFECTS} effects")
    return tuple(_effect(pack_id, f"{path}[{index}]", entry) for index, entry in enumerate(raw))


def _effect(pack_id: str, path: str, raw: Any) -> EffectSpec:
    mapping = _mapping(pack_id, path, raw)
    _known_keys(
        pack_id,
        path,
        mapping,
        {
            "op",
            "resource",
            "amount",
            "modifier",
            "action",
            "factor",
            "damage_type",
            "condition",
            "target",
            "duration",
            "stacks",
            "source",
            "tags",
        },
    )
    op = str(mapping.get("op") or "").strip()
    if op not in GENERIC_EFFECT_OPS:
        raise _fail(pack_id, f"{path}.op", f"must be one of {sorted(GENERIC_EFFECT_OPS)}")
    args = {str(key): value for key, value in mapping.items() if key != "op"}
    for key in ("amount", "factor", "duration", "stacks"):
        if key not in args:
            continue
        args[key] = _value(pack_id, f"{path}.{key}", args[key])
    for key in ("resource", "modifier", "action", "damage_type", "condition", "target", "source"):
        if key in args:
            args[key] = _bounded_id(pack_id, f"{path}.{key}", args[key])
    if "tags" in args:
        tags = args["tags"]
        if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes, bytearray)):
            raise _fail(pack_id, f"{path}.tags", "must be a list")
        args["tags"] = tuple(_bounded_id(pack_id, f"{path}.tags[{index}]", item) for index, item in enumerate(tags))
    return EffectSpec(op=op, args=args)


def _damage(pack_id: str, path: str, raw: Any) -> tuple[DamageComponentSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise _fail(pack_id, path, "must be a list")
    if len(raw) > MAX_DAMAGE_COMPONENTS:
        raise _fail(pack_id, path, f"may contain at most {MAX_DAMAGE_COMPONENTS} components")
    result: list[DamageComponentSpec] = []
    for index, entry in enumerate(raw):
        item_path = f"{path}[{index}]"
        mapping = _mapping(pack_id, item_path, entry)
        _known_keys(pack_id, item_path, mapping, {"roll", "type", "tags", "critical", "critical_policy"})
        roll = str(mapping.get("roll") or "").strip()
        if not roll or len(roll) > MAX_DICE_EXPRESSION_LENGTH:
            raise _fail(pack_id, f"{item_path}.roll", "must be a non-empty short dice expression")  # i18n-exempt: internal validation diagnostic
        damage_type = _bounded_id(pack_id, f"{item_path}.type", mapping.get("type"))
        tags_raw = mapping.get("tags") or []
        if not isinstance(tags_raw, Sequence) or isinstance(tags_raw, (str, bytes, bytearray)):
            raise _fail(pack_id, f"{item_path}.tags", "must be a list")
        tags = tuple(_bounded_id(pack_id, f"{item_path}.tags[{tag_index}]", tag) for tag_index, tag in enumerate(tags_raw))
        critical = mapping.get("critical", mapping.get("critical_policy"))
        if critical is not None and not isinstance(critical, (str, bool, Mapping)):
            raise _fail(pack_id, f"{item_path}.critical", "must be a string, mapping, or boolean")  # i18n-exempt: internal validation diagnostic
        result.append(DamageComponentSpec(roll=roll, type=damage_type, tags=tags, critical=critical))
    return tuple(result)


def _resource_costs(pack_id: str, path: str, raw: Any) -> tuple[ResourceCostSpec, ...]:
    if raw is None:
        return ()
    result: list[ResourceCostSpec] = []
    if isinstance(raw, Mapping):
        items = [{"pool": key, "amount": value} for key, value in raw.items()]
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        items = list(raw)
    else:
        raise _fail(pack_id, path, "must be a mapping or list")  # i18n-exempt: internal validation diagnostic
    if len(items) > MAX_RESOURCE_POOLS:
        raise _fail(pack_id, path, f"may contain at most {MAX_RESOURCE_POOLS} costs")
    for index, entry in enumerate(items):
        item_path = f"{path}[{index}]"
        mapping = _mapping(pack_id, item_path, entry)
        _known_keys(pack_id, item_path, mapping, {"pool", "amount"})
        pool = _bounded_id(pack_id, f"{item_path}.pool", mapping.get("pool"))
        amount = _value(pack_id, f"{item_path}.amount", mapping.get("amount"))
        result.append(ResourceCostSpec(pool=pool, amount=amount))
    return tuple(result)


def _parse_pool(pack_id: str, pool_id: Any, raw: Any) -> ResourcePoolSpec:
    path = f"resources.pools.{pool_id}"
    mapping = _mapping(pack_id, path, raw)
    _known_keys(pack_id, path, mapping, {"role", "initial", "max", "die", "group", "reset", "prominent", "display", "label"})
    pool = _bounded_id(pack_id, path, pool_id)
    role = _bounded_id(pack_id, f"{path}.role", mapping.get("role"))
    if "initial" not in mapping or "max" not in mapping:
        raise _fail(pack_id, path, "requires initial and max")
    initial = _value(pack_id, f"{path}.initial", mapping["initial"])
    maximum = _value(pack_id, f"{path}.max", mapping["max"], allow_none=True)
    die = mapping.get("die")
    if die is not None:
        die = str(die).strip()
        if not die or len(die) > MAX_DICE_EXPRESSION_LENGTH:
            raise _fail(pack_id, f"{path}.die", "must be a short dice expression")  # i18n-exempt: internal validation diagnostic
    group = str(mapping.get("group") or "").strip()
    if len(group) > MAX_ID_LENGTH:
        raise _fail(pack_id, f"{path}.group", "is too long")
    display_raw = mapping.get("display")
    display = dict(_mapping(pack_id, f"{path}.display", display_raw)) if display_raw is not None else {}
    if "label" in mapping:
        display["label"] = _label_map(pack_id, f"{path}.label", mapping["label"])
    if len(display) > MAX_RUNTIME_ENTRIES:
        raise _fail(pack_id, f"{path}.display", "has too many entries")
    return ResourcePoolSpec(
        id=pool,
        role=role,
        initial=initial,
        maximum=maximum,
        die=die,
        group=group,
        reset_tags=_reset_tags(pack_id, f"{path}.reset", mapping.get("reset")),
        prominent=bool(mapping.get("prominent", False)),
        display=display,
    )


def _parse_condition(pack_id: str, condition_id: Any, raw: Any) -> ConditionSpec:
    path = f"combat.conditions.{condition_id}"
    mapping = _mapping(pack_id, path, raw)
    _known_keys(pack_id, path, mapping, {"duration", "end", "effects", "visibility", "stacking", "max_stacks", "display"})
    condition = _bounded_id(pack_id, path, condition_id)
    duration = _value(pack_id, f"{path}.duration", mapping["duration"], allow_none=True) if "duration" in mapping else None
    end = _value(pack_id, f"{path}.end", mapping["end"], allow_none=True) if "end" in mapping else None
    visibility = str(mapping.get("visibility") or "public").strip()
    if visibility not in {"public", "keeper", "private"}:
        raise _fail(pack_id, f"{path}.visibility", "must be public, keeper, or private")  # i18n-exempt: internal validation diagnostic
    stacking = str(mapping.get("stacking") or "replace").strip()
    if stacking not in {"replace", "stack", "refresh", "ignore"}:
        raise _fail(pack_id, f"{path}.stacking", "must be replace, stack, refresh, or ignore")  # i18n-exempt: internal validation diagnostic
    max_stacks = mapping.get("max_stacks")
    if max_stacks is not None:
        if isinstance(max_stacks, bool) or not isinstance(max_stacks, int) or max_stacks < 1:
            raise _fail(pack_id, f"{path}.max_stacks", "must be a positive integer")
    display_raw = mapping.get("display")
    display = dict(_mapping(pack_id, f"{path}.display", display_raw)) if display_raw is not None else {}
    return ConditionSpec(
        id=condition,
        duration=duration,
        end=end,
        effects=_effects(pack_id, f"{path}.effects", mapping.get("effects")),
        visibility=visibility,
        stacking=stacking,
        max_stacks=max_stacks,
        display=display,
    )


def _parse_action(pack_id: str, action_id: Any, raw: Any) -> ActionSpec:
    path = f"combat.actions.{action_id}"
    mapping = _mapping(pack_id, path, raw)
    _known_keys(
        pack_id,
        path,
        mapping,
        {
            "id",
            "cost",
            "targeting",
            "target",
            "resolution",
            "roll_modifier",
            "modifier",
            "range",
            "on_success",
            "on_failure",
            "damage",
            "resource_costs",
            "resources",
            "concentration",
            "conditions",
            "visibility",
            "label",
        },
    )
    action = _bounded_id(pack_id, path, mapping.get("id", action_id))
    cost_raw = mapping.get("cost") or {}
    cost_mapping = _mapping(pack_id, f"{path}.cost", cost_raw)
    if len(cost_mapping) > MAX_RUNTIME_ENTRIES:
        raise _fail(pack_id, f"{path}.cost", "has too many entries")
    cost = {str(key): _value(pack_id, f"{path}.cost.{key}", value) for key, value in cost_mapping.items()}
    targeting_raw = mapping.get("targeting", mapping.get("target")) or {}
    targeting = dict(_mapping(pack_id, f"{path}.targeting", targeting_raw))
    for key in ("min", "max", "count"):
        if key in targeting:
            value = targeting[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _fail(pack_id, f"{path}.targeting.{key}", "must be a non-negative integer")  # i18n-exempt: internal validation diagnostic
    resolution = mapping.get("resolution")
    if isinstance(resolution, Mapping):
        resolution = dict(resolution)
    elif resolution is not None and not isinstance(resolution, str):
        raise _fail(pack_id, f"{path}.resolution", "must be a string or mapping")  # i18n-exempt: internal validation diagnostic
    roll_modifier = mapping.get("roll_modifier", mapping.get("modifier"))
    if isinstance(roll_modifier, Mapping):
        roll_modifier = _value(pack_id, f"{path}.roll_modifier", roll_modifier)
    elif roll_modifier is not None and not isinstance(roll_modifier, (str, int, float, bool)):
        raise _fail(pack_id, f"{path}.roll_modifier", "has an unsupported value")
    range_value = mapping.get("range")
    if isinstance(range_value, Mapping):
        range_value = dict(range_value)
    elif range_value is not None and not isinstance(range_value, (str, int, float)):
        raise _fail(pack_id, f"{path}.range", "must be a scalar or mapping")  # i18n-exempt: internal validation diagnostic
    visibility = str(mapping.get("visibility") or "public").strip()
    if visibility not in {"public", "keeper", "private"}:
        raise _fail(pack_id, f"{path}.visibility", "must be public, keeper, or private")  # i18n-exempt: internal validation diagnostic
    return ActionSpec(
        id=action,
        cost=cost,
        targeting=targeting,
        resolution=resolution,
        roll_modifier=roll_modifier,
        range=range_value,
        on_success=_effects(pack_id, f"{path}.on_success", mapping.get("on_success")),
        on_failure=_effects(pack_id, f"{path}.on_failure", mapping.get("on_failure")),
        damage=_damage(pack_id, f"{path}.damage", mapping.get("damage")),
        resource_costs=_resource_costs(
            pack_id, f"{path}.resource_costs", mapping.get("resource_costs", mapping.get("resources"))
        ),
        concentration=mapping.get("concentration", False),
        conditions=_effects(pack_id, f"{path}.conditions", mapping.get("conditions")),
        visibility=visibility,
        label=_label_map(pack_id, f"{path}.label", mapping["label"]) if "label" in mapping else {},
    )


def _named_maps(pack_id: str, path: str, raw: Any, allowed_names: set[str]) -> dict[str, Mapping[str, Any]]:
    if raw is None:
        return {}
    mapping = _mapping(pack_id, path, raw)
    _known_keys(pack_id, path, mapping, allowed_names)
    result: dict[str, Mapping[str, Any]] = {}
    for name, value in mapping.items():
        item_path = f"{path}.{name}"
        item = _mapping(pack_id, item_path, value)
        # Procedures intentionally keep pack-defined nested data, but all values are
        # copied and expression wrappers are validated recursively at their leaves.
        result[str(name)] = _validate_nested_values(pack_id, item_path, item)
    return result


def _validate_nested_values(pack_id: str, path: str, value: Any) -> Any:
    if isinstance(value, Mapping):
        _known_keys(pack_id, path, value, {str(key) for key in value})
        return {str(key): _validate_nested_values(pack_id, f"{path}.{key}", item) for key, item in value.items()}
    if isinstance(value, list):
        if len(value) > MAX_RUNTIME_ENTRIES:
            raise _fail(pack_id, path, f"may contain at most {MAX_RUNTIME_ENTRIES} entries")
        return [_validate_nested_values(pack_id, f"{path}[{index}]", item) for index, item in enumerate(value)]
    if isinstance(value, str) and len(value) > MAX_LABEL_LENGTH:
        raise _fail(pack_id, path, "contains an overlong string")
    return value


def _parse_rest_block(pack_id: str, path: str, raw: Any) -> Mapping[str, Any]:
    mapping = _mapping(pack_id, path, raw)
    allowed = {
        "duration",
        "cooldown",
        "eligibility",
        "participants",
        "recovery_dice",
        "reset",
        "recover",
        "effects",
        "advance_time",
    }
    _known_keys(pack_id, path, mapping, allowed)
    result = dict(_validate_nested_values(pack_id, path, mapping))
    for key in ("duration", "cooldown", "advance_time"):
        if key in result and isinstance(result[key], str):
            result[key] = _validate_expr(pack_id, f"{path}.{key}", result[key])
    if "effects" in mapping:
        result["effects"] = [effect.to_dict() for effect in _effects(pack_id, f"{path}.effects", mapping["effects"])]
    return result


def parse_action_spec(pack_id: str, action_id: str, raw: Any) -> ActionSpec:
    """Validate one reusable action using the runtime contract."""
    return _parse_action(pack_id, action_id, raw)


def parse_condition_spec(pack_id: str, condition_id: str, raw: Any) -> ConditionSpec:
    """Validate one reusable condition using the runtime contract."""
    return _parse_condition(pack_id, condition_id, raw)


def parse_runtime_section(pack_id: str, raw: Any) -> RuntimeSpec | None:
    """Shape-validate and compile one rulepack ``runtime:`` section.

    Unknown keys are rejected before the pack enters discovery. Expressions are
    compiled with ``core.condexpr`` and arbitrary Python/rule code cannot enter a
    runtime contract.
    """
    if raw is None:
        return None
    mapping = _mapping(pack_id, "", raw)
    _known_keys(pack_id, "", mapping, {"version", "resources", "combat", "rests", "advancement", "encounters"})
    version = mapping.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != RUNTIME_SCHEMA_VERSION:
        raise _fail(pack_id, "version", f"must be integer version {RUNTIME_SCHEMA_VERSION}")

    resources_raw = mapping.get("resources") or {}
    resources_mapping = _mapping(pack_id, "resources", resources_raw)
    _known_keys(pack_id, "resources", resources_mapping, {"pools"})
    pools_raw = resources_mapping.get("pools") or {}
    pools_mapping = _mapping(pack_id, "resources.pools", pools_raw)
    if len(pools_mapping) > MAX_RESOURCE_POOLS:
        raise _fail(pack_id, "resources.pools", f"may contain at most {MAX_RESOURCE_POOLS} pools")
    pools = {str(pool_id): _parse_pool(pack_id, pool_id, value) for pool_id, value in pools_mapping.items()}

    combat_raw = mapping.get("combat") or {}
    combat = _mapping(pack_id, "combat", combat_raw)
    _known_keys(pack_id, "combat", combat, {"initiative", "budgets", "actions", "conditions", "damage_types", "cover", "dying"})
    initiative = combat.get("initiative", "")
    if initiative is not None and not isinstance(initiative, str):
        raise _fail(pack_id, "combat.initiative", "must be a string")
    initiative = str(initiative or "").strip()
    budgets_raw = combat.get("budgets") or {}
    budgets_mapping = _mapping(pack_id, "combat.budgets", budgets_raw)
    budgets = {str(key): _value(pack_id, f"combat.budgets.{key}", value) for key, value in budgets_mapping.items()}
    actions_raw = combat.get("actions") or {}
    actions_mapping = _mapping(pack_id, "combat.actions", actions_raw)
    if len(actions_mapping) > MAX_ACTIONS:
        raise _fail(pack_id, "combat.actions", f"may contain at most {MAX_ACTIONS} actions")
    actions = {str(action_id): _parse_action(pack_id, action_id, value) for action_id, value in actions_mapping.items()}
    conditions_raw = combat.get("conditions") or {}
    conditions_mapping = _mapping(pack_id, "combat.conditions", conditions_raw)
    if len(conditions_mapping) > MAX_CONDITIONS:
        raise _fail(pack_id, "combat.conditions", f"may contain at most {MAX_CONDITIONS} conditions")
    conditions = {
        str(condition_id): _parse_condition(pack_id, condition_id, value)
        for condition_id, value in conditions_mapping.items()
    }
    damage_types_raw = combat.get("damage_types") or []
    if not isinstance(damage_types_raw, Sequence) or isinstance(damage_types_raw, (str, bytes, bytearray)):
        raise _fail(pack_id, "combat.damage_types", "must be a list")
    damage_types = tuple(_bounded_id(pack_id, f"combat.damage_types[{index}]", value) for index, value in enumerate(damage_types_raw))
    if len(set(damage_types)) != len(damage_types):
        raise _fail(pack_id, "combat.damage_types", "must not contain duplicates")
    cover = _validate_nested_values(pack_id, "combat.cover", _mapping(pack_id, "combat.cover", combat.get("cover") or {}))
    dying = _validate_nested_values(pack_id, "combat.dying", _mapping(pack_id, "combat.dying", combat.get("dying") or {}))

    rests_raw = mapping.get("rests") or {}
    rests_mapping = _mapping(pack_id, "rests", rests_raw)
    _known_keys(pack_id, "rests", rests_mapping, {"short", "long"})
    rests = {
        str(key): _parse_rest_block(pack_id, f"rests.{key}", value)
        for key, value in rests_mapping.items()
    }

    advancement_raw = mapping.get("advancement") or {}
    advancement = dict(_mapping(pack_id, "advancement", advancement_raw))
    _known_keys(pack_id, "advancement", advancement, {"modes", "level_field", "xp_thresholds", "tracks", "hp", "features"})
    advancement = _validate_nested_values(pack_id, "advancement", advancement)
    modes = advancement.get("modes")
    if modes is not None:
        if not isinstance(modes, list) or not modes or any(str(mode) not in {"milestone", "xp"} for mode in modes):
            raise _fail(pack_id, "advancement.modes", "must be a non-empty list containing only milestone or xp")  # i18n-exempt: internal validation diagnostic
        advancement["modes"] = [str(mode) for mode in modes]
    if "level_field" in advancement:
        advancement["level_field"] = _bounded_id(pack_id, "advancement.level_field", advancement["level_field"])
    if "xp_thresholds" in advancement:
        thresholds = advancement["xp_thresholds"]
        if not isinstance(thresholds, list):
            raise _fail(pack_id, "advancement.xp_thresholds", "must be a list")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in thresholds):
            raise _fail(pack_id, "advancement.xp_thresholds", "must contain non-negative integers")
        if list(thresholds) != sorted(thresholds):
            raise _fail(pack_id, "advancement.xp_thresholds", "must be sorted ascending")

    encounters_raw = mapping.get("encounters") or {}
    encounters = dict(_mapping(pack_id, "encounters", encounters_raw))
    _known_keys(pack_id, "encounters", encounters, {"budget", "visibility"})
    encounters = _validate_nested_values(pack_id, "encounters", encounters)

    return RuntimeSpec(
        version=version,
        pools=pools,
        initiative=initiative,
        budgets=budgets,
        actions=actions,
        conditions=conditions,
        damage_types=damage_types,
        cover=cover,
        dying=dying,
        rests=rests,
        advancement=advancement,
        encounters=encounters,
    )
