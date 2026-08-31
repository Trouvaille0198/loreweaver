"""Deterministic, pack-defined resource pools.

Resource pools are the authoritative mutable counters for runtime mechanics.  This
module intentionally knows only generic roles (health, recovery die, spell slot,
etc. as data) and delegates canonical sheet values to the existing sheet bridge.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.condexpr import CondExprError, compile_expression
from core.runtime import ResourcePoolSpec, resolve_display_label


class ResourceError(ValueError):
    """A resource operation or declaration cannot be applied."""


@dataclass(frozen=True)
class ResourceValue:
    """One pool's current bounded value and optimistic-write revision."""

    id: str
    role: str
    current: int
    maximum: int | None
    revision: int = 0
    group: str = ""
    die: str | None = None
    reset_tags: tuple[str, ...] = ()
    prominent: bool = False
    display: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "max": self.maximum,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class ResourceMutation:
    """One successful deterministic resource change."""

    pool: ResourceValue
    before: int
    after: int
    delta: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool.id,
            "before": self.before,
            "after": self.after,
            "delta": self.delta,
            "revision": self.pool.revision,
        }


def _as_int(value: Any, *, path: str) -> int:
    if isinstance(value, bool):
        raise ResourceError(f"{path} must be an integer")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise ResourceError(f"{path} must resolve to an integer")


def _raw_resources(sheet: Any) -> dict[str, dict[str, Any]]:
    value = getattr(sheet, "resources", None)
    if not isinstance(value, dict):
        value = {}
        sheet.resources = value
    return value


def _canonical_resolver(sheet: Any, pack: Any, pools: Mapping[str, ResourceValue], path: str) -> Any:
    if path in pools:
        return pools[path].current
    try:
        from core.sheets import canonical_values, sheet_value

        canonical = pack.resolve_skill(path) or path
        values = canonical_values(sheet, pack)
        if canonical in values or canonical in getattr(pack, "defaults", {}):
            return sheet_value(sheet, pack, canonical)
    except Exception as exc:
        raise ResourceError(f"cannot resolve resource reference {path!r}") from exc
    raise ResourceError(f"unknown resource reference {path!r}")


def _resolve_value(
    raw: Any,
    *,
    sheet: Any,
    pack: Any,
    pools: Mapping[str, ResourceValue],
    path: str,
    resolving: set[str] | None = None,
) -> int:
    if isinstance(raw, bool):
        raise ResourceError(f"{path} must not be boolean")
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ResourceError(f"{path} must not be empty")
        try:
            return int(text)
        except ValueError:
            return _as_int(_canonical_resolver(sheet, pack, pools, text), path=path)
    if isinstance(raw, Mapping):
        if set(raw) - {"expr", "ref", "value"}:
            raise ResourceError(f"{path} contains an unsupported value key")
        if "value" in raw:
            return _resolve_value(raw["value"], sheet=sheet, pack=pack, pools=pools, path=path)
        if "ref" in raw:
            ref = str(raw["ref"] or "").strip()
            if not ref:
                raise ResourceError(f"{path}.ref must not be empty")
            return _as_int(_canonical_resolver(sheet, pack, pools, ref), path=path)
        expression = raw.get("expr")
        if not isinstance(expression, str) or not expression.strip():
            raise ResourceError(f"{path}.expr must be a non-empty string")  # i18n-exempt: internal validation diagnostic
        try:
            compiled = compile_expression(
                expression,
                functions={"abs": abs, "max": max, "min": min, "round": round},
            )
            result = compiled(lambda name: _canonical_resolver(sheet, pack, pools, name))
        except (CondExprError, ResourceError, TypeError, ValueError) as exc:
            raise ResourceError(f"{path}.expr could not be evaluated: {exc}") from exc  # i18n-exempt: internal validation diagnostic
        return _as_int(result, path=path)
    raise ResourceError(f"{path} has unsupported type {type(raw).__name__}")


def _pool_specs(pack: Any) -> Mapping[str, ResourcePoolSpec]:
    runtime = getattr(pack, "runtime_spec", None)
    return {} if runtime is None else runtime.pools


def _slot_table_for(sheet: Any, runtime: Any) -> tuple[tuple[int, ...], ...]:
    """The slot table matching the sheet's class: `spell_slot_class` picks the
    half (paladin/ranger) or pact (warlock) table; everything else uses the
    full caster's `spell_slots_by_level`."""
    class_name = str(getattr(sheet, "character_class", "") or "").strip().casefold()
    levels = getattr(sheet, "class_levels", None)
    if isinstance(levels, Mapping) and len(levels) > 1:
        # Multiclass spellcasting uses one combined regular-caster level:
        # full casters contribute all levels, half casters contribute half
        # levels rounded down. Warlock pact slots remain a separate class
        # resource and are handled by its own table when it is the sole class.
        full = {name for name, kind in (runtime.spell_slot_class or {}).items() if kind == "full"}
        half = {name for name, kind in (runtime.spell_slot_class or {}).items() if kind == "half"}
        regular_level = sum(int(level) for name, level in levels.items() if str(name).casefold() in full)
        regular_level += sum(int(level) // 2 for name, level in levels.items() if str(name).casefold() in half)
        if regular_level > 0:
            table = tuple(runtime.spell_slots_by_level or ())
            return table if table else ()
    table_kind = (runtime.spell_slot_class or {}).get(class_name)
    if table_kind is None:
        for canonical, names in (runtime.class_aliases or {}).items():
            if class_name in {str(alias).casefold() for alias in names}:
                table_kind = (runtime.spell_slot_class or {}).get(canonical)
                break
    if table_kind in (None, "none"):
        return ()
    if table_kind == "half":
        return tuple(runtime.spell_slots_half or ())
    if table_kind == "pact":
        return tuple(runtime.spell_slots_pact or ())
    return tuple(runtime.spell_slots_by_level or ())


def _effective_spell_level(sheet: Any, runtime: Any) -> int:
    """Return the caster level used to index a slot table."""
    levels = getattr(sheet, "class_levels", None)
    if not isinstance(levels, Mapping) or len(levels) <= 1:
        return int(getattr(sheet, "level", 1) or 1)
    full = {name for name, kind in (runtime.spell_slot_class or {}).items() if kind == "full"}
    half = {name for name, kind in (runtime.spell_slot_class or {}).items() if kind == "half"}
    return sum(int(level) for name, level in levels.items() if str(name).casefold() in full) + sum(
        int(level) // 2 for name, level in levels.items() if str(name).casefold() in half
    )


def _initial_state(sheet: Any, pack: Any) -> dict[str, ResourceValue]:
    specs = _pool_specs(pack)
    raw = _raw_resources(sheet)
    resolved: dict[str, ResourceValue] = {}
    resolving: set[str] = set()

    def build(pool_id: str, spec: ResourcePoolSpec) -> ResourceValue:
        if pool_id in resolved:
            return resolved[pool_id]
        if pool_id in resolving:
            raise ResourceError(f"resource pool cycle at {pool_id!r}")
        resolving.add(pool_id)
        for declaration in (spec.maximum, spec.initial):
            if isinstance(declaration, Mapping) and "ref" in declaration:
                reference = str(declaration["ref"])
                dependency = specs.get(reference)
                if dependency is not None:
                    build(reference, dependency)
        # A maximum may reference a pool or canonical sheet value.  A null maximum
        # is intentionally unbounded (temporary pools use this shape).
        maximum = (
            None
            if spec.maximum is None
            else _resolve_value(spec.maximum, sheet=sheet, pack=pack, pools=resolved, path=f"resources.{pool_id}.max")
        )
        if maximum is not None:
            maximum = max(0, maximum)
        # A recovery-die pool's maximum is the number of class levels using
        # that die. This keeps multiclass characters' d6/d8/d10/d12 pools
        # separate, as required by D&D 5e.
        if spec.role == "recovery_die" and getattr(pack, "runtime_spec", None) is not None:
            hit_dice = (pack.runtime_spec.advancement or {}).get("hit_dice") or {}
            levels = getattr(sheet, "class_levels", None)
            if not isinstance(levels, Mapping) or not levels:
                class_name = str(getattr(sheet, "character_class", "") or "").strip().casefold()
                normalizer = getattr(pack, "normalize_class", None)
                if normalizer:
                    class_name = str(normalizer(class_name)).strip().casefold()
                levels = {class_name: int(getattr(sheet, "level", 1) or 1)}
            pool_die = str(spec.die or "")
            maximum = sum(
                int(level)
                for class_name, level in levels.items()
                if str(hit_dice.get(str(class_name).casefold()) or "") == pool_die
            )
            if maximum <= 0:
                maximum = 0
        # A spell_slot pool's maximum is driven by the pack's per-caster-type
        # slot tables (selected by the sheet's class), when declared — the
        # pack's static `max` is the pre-table fallback (0 = ring locked).
        if spec.role == "spell_slot" and getattr(pack, "runtime_spec", None) is not None:
            runtime = pack.runtime_spec
            table = _slot_table_for(sheet, runtime)
            if table:
                level = _effective_spell_level(sheet, runtime)
                ring_text = str(pool_id).rsplit("_", 1)[-1]
                ring = int(ring_text) if ring_text.isdigit() else 0
                if 1 <= ring <= 9 and 1 <= int(level) <= len(table):
                    maximum = max(0, int(table[int(level) - 1][ring - 1]))
        stored = raw.get(pool_id)
        # Keep the old universal d10 pool readable when migrating a sheet.
        if spec.role == "recovery_die" and stored is None and pool_id == "hit_die_d10":
            stored = raw.get("hit_die_d10")
        die = spec.die
        if isinstance(stored, Mapping) and "current" in stored:
            current = _as_int(stored.get("current"), path=f"resources.{pool_id}.current")
            revision = _as_int(stored.get("revision", 0), path=f"resources.{pool_id}.revision")
        else:
            # The health role adopts the existing field-backed value exactly once;
            # all other pools start from their declaration.
            if spec.role == "health":
                try:
                    from core.character_manager import get_hit_points

                    current, legacy_max = get_hit_points(sheet)
                    if maximum is None:
                        maximum = legacy_max
                except Exception:
                    current = _resolve_value(
                        spec.initial, sheet=sheet, pack=pack, pools=resolved, path=f"resources.{pool_id}.initial"
                    )
            elif spec.role == "recovery_die" and maximum is not None:
                current = maximum
            else:
                current = _resolve_value(
                    spec.initial, sheet=sheet, pack=pack, pools=resolved, path=f"resources.{pool_id}.initial"
                )
            revision = 0
        if maximum is not None:
            current = min(current, maximum)
        current = max(0, current)
        value = ResourceValue(
            id=pool_id,
            role=spec.role,
            current=current,
            maximum=maximum,
            revision=max(0, revision),
            group=spec.group,
            die=die,
            reset_tags=spec.reset_tags,
            prominent=spec.prominent,
            display=spec.display,
        )
        resolving.remove(pool_id)
        resolved[pool_id] = value
        return value

    for pool_id, spec in specs.items():
        build(pool_id, spec)
    return resolved


def _write_state(sheet: Any, pools: Mapping[str, ResourceValue]) -> None:
    raw = _raw_resources(sheet)
    for pool_id, value in pools.items():
        raw[pool_id] = value.to_dict()


def resource_values(sheet: Any, pack: Any) -> dict[str, ResourceValue]:
    """Load, normalize, and persist all declared runtime pools on ``sheet``."""
    values = _initial_state(sheet, pack)
    _write_state(sheet, values)
    return values


def get_resource(sheet: Any, pack: Any, pool_id: str) -> ResourceValue:
    """Return one declared pool, raising instead of silently creating a counter."""
    values = resource_values(sheet, pack)
    try:
        return values[str(pool_id)]
    except KeyError:
        raise ResourceError(f"unknown resource pool {pool_id!r}") from None


def _mutate(sheet: Any, pack: Any, pool_id: str, target: int) -> ResourceMutation:
    values = resource_values(sheet, pack)
    pool_key = str(pool_id)
    try:
        old = values[pool_key]
    except KeyError:
        raise ResourceError(f"unknown resource pool {pool_id!r}") from None
    if target < 0:
        target = 0
    if old.maximum is not None:
        target = min(target, old.maximum)
    updated = ResourceValue(
        id=old.id,
        role=old.role,
        current=target,
        maximum=old.maximum,
        revision=old.revision + (target != old.current),
        group=old.group,
        die=old.die,
        reset_tags=old.reset_tags,
        prominent=old.prominent,
        display=old.display,
    )
    values[pool_key] = updated
    _write_state(sheet, values)
    return ResourceMutation(pool=updated, before=old.current, after=target, delta=target - old.current)


def set_resource(sheet: Any, pack: Any, pool_id: str, value: int) -> ResourceMutation:
    """Set a pool after applying its lower/upper bounds."""
    return _mutate(sheet, pack, pool_id, _as_int(value, path=f"resource {pool_id!r}"))


def spend_resource(sheet: Any, pack: Any, pool_id: str, amount: int) -> ResourceMutation:
    """Spend a positive amount; insufficient resources fail without mutation."""
    amount_int = _as_int(amount, path=f"resource {pool_id!r} amount")
    if amount_int < 0:
        raise ResourceError("resource spend amount must be non-negative")  # i18n-exempt: internal validation diagnostic
    current = get_resource(sheet, pack, pool_id)
    if current.current < amount_int:
        raise ResourceError(f"resource pool {pool_id!r} is insufficient")
    return _mutate(sheet, pack, pool_id, current.current - amount_int)


def recover_resource(sheet: Any, pack: Any, pool_id: str, amount: int | None = None) -> ResourceMutation:
    """Recover a pool by an amount, or to its maximum when amount is omitted."""
    current = get_resource(sheet, pack, pool_id)
    if amount is None:
        if current.maximum is None:
            raise ResourceError(f"resource pool {pool_id!r} has no recovery maximum")
        target = current.maximum
    else:
        amount_int = _as_int(amount, path=f"resource {pool_id!r} amount")
        if amount_int < 0:
            raise ResourceError("resource recovery amount must be non-negative")  # i18n-exempt: internal validation diagnostic
        target = current.current + amount_int
    return _mutate(sheet, pack, pool_id, target)


def _is_pact_caster(sheet: Any, pack: Any) -> bool:
    """Whether the sheet's class is a pact caster (warlock) — its spell slots
    recover on a SHORT rest, unlike every other caster."""
    runtime = getattr(pack, "runtime_spec", None)
    if runtime is None:
        return False
    levels = getattr(sheet, "class_levels", None)
    if isinstance(levels, Mapping) and len(levels) > 1:
        return False
    class_name = str(getattr(sheet, "character_class", "") or "").strip().casefold()
    return (runtime.spell_slot_class or {}).get(class_name) == "pact"


def recover_by_reset(sheet: Any, pack: Any, tag: str) -> tuple[ResourceMutation, ...]:
    """Recover every pool carrying ``tag`` according to its declared maximum.

    Spell slots are the exception to the reset-tag rule: a pact caster (warlock)
    recovers them on a SHORT rest; every other caster only on a LONG rest (the
    pack's reset tag), even if a tag happens to match."""
    values = resource_values(sheet, pack)
    pact_short = tag == "short" and _is_pact_caster(sheet, pack)
    mutations: list[ResourceMutation] = []
    for pool_id, value in values.items():
        is_slot = value.role == "spell_slot"
        if is_slot and tag == "short" and not pact_short:
            continue
        if tag not in value.reset_tags and not (is_slot and pact_short):
            continue
        if value.maximum is None:
            continue
        mutations.append(_mutate(sheet, pack, pool_id, value.maximum))
    return tuple(mutations)


def resource_projection(sheet: Any, pack: Any, locale: str | None = None) -> dict[str, Any]:
    """Return grouped, generic wire data for all declared pools."""
    values = resource_values(sheet, pack)
    groups: dict[str, list[dict[str, Any]]] = {}
    for pool_id, value in values.items():
        label = resolve_display_label(value.display, pool_id, locale)
        item: dict[str, Any] = {
            "id": pool_id,
            "role": value.role,
            "value": value.current,
            "max": value.maximum,
            "revision": value.revision,
            "prominent": value.prominent,
        }
        if value.die is not None:
            item["die"] = value.die
        key = value.group or ""
        groups.setdefault(key, []).append({"label": str(label), **item})
    return {
        "groups": [{"id": group, "resources": items} for group, items in groups.items()],
        "resources": [item for items in groups.values() for item in items],
    }


class ResourceLedger:
    """Small object facade shared by commands, actors, and deterministic managers."""

    def __init__(self, sheet: Any, pack: Any) -> None:
        self.sheet = sheet
        self.pack = pack

    def show(self, pool_id: str | None = None, *, locale: str | None = None) -> Any:
        if pool_id:
            return get_resource(self.sheet, self.pack, pool_id)
        return resource_projection(self.sheet, self.pack, locale)

    def spend(self, pool_id: str, amount: int) -> ResourceMutation:
        return spend_resource(self.sheet, self.pack, pool_id, amount)

    def set(self, pool_id: str, value: int) -> ResourceMutation:
        return set_resource(self.sheet, self.pack, pool_id, value)

    def recover(self, pool_id: str, amount: int | None = None) -> ResourceMutation:
        return recover_resource(self.sheet, self.pack, pool_id, amount)
