"""Spell catalogs: deterministic spell data declared by a rulepack.

A spell catalog is pack data (``runtime.spells_file`` names a sibling YAML in
the rulepack's own directory), loaded the same way a pack's resolver scripts
are — confined to the pack directory, never arbitrary filesystem paths. The
catalog is the "world dictionary": every spell a system knows, with its
mechanical facts (level, school, components, duration, concentration, save or
attack resolution, damage, higher-level scaling). Which spells a CHARACTER
knows is a separate sheet concern (the ``known_spells`` list), enforced at
cast time by the engine — never by the model.

Data, not code: the engine validates shapes and keeps pack-owned ids and
labels as data. A pack without a spells_file simply has no catalog (spell
casting is unsupported there).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.runtime import ActionSpec, DamageComponentSpec, ResourceCostSpec

MAX_SPELLS = 1024
MAX_SPELL_LEVEL = 9


class SpellError(ValueError):
    """A malformed spell or spell catalog (raised at pack load time)."""


@dataclass(frozen=True)
class SpellSpec:
    """One spell's deterministic mechanical facts."""

    id: str
    name: Mapping[str, str]  # locale -> display name; "en" always present
    level: int  # 0 = cantrip, 1..9
    school: str = ""
    casting_time: str = ""
    range: str = ""
    components: tuple[str, ...] = ()
    material: str = ""
    duration: str = ""
    concentration: bool = False
    # Save resolution: {"ability": <canonical>, "success": "half"|"none"}
    save: Mapping[str, Any] | None = None
    attack: bool = False  # spell attack roll vs AC instead of a save
    damage: tuple[DamageComponentSpec, ...] = ()
    scaling: Mapping[str, Any] | None = None  # higher-level casting, see parse
    dc_ability: str = ""  # canonical attribute feeding the spell save DC
    description: Mapping[str, str] = field(default_factory=dict)

    def display_name(self, locale: str) -> str:
        base = str(locale or "").replace("_", "-").split("-")[0].casefold()
        return self.name.get(base) or self.name.get("en") or self.id

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": dict(self.name),
            "level": self.level,
        }
        for key in ("school", "casting_time", "range", "material", "duration", "dc_ability"):
            value = getattr(self, key)
            if value:
                result[key] = value
        if self.components:
            result["components"] = list(self.components)
        if self.concentration:
            result["concentration"] = True
        if self.save is not None:
            result["save"] = dict(self.save)
        if self.attack:
            result["attack"] = True
        if self.damage:
            result["damage"] = [component.to_dict() for component in self.damage]
        if self.scaling:
            result["scaling"] = self.scaling
        if self.description:
            result["description"] = dict(self.description)
        return result


@dataclass(frozen=True)
class SpellCatalog:
    """A rulepack's full spell directory, keyed by id (case-insensitive)."""

    spells: Mapping[str, SpellSpec]
    # Class → default known-spell ids (e.g. wizard's starting spellbook). The
    # engine fills a sheet's known_spells from this at creation; membership is
    # still enforced at cast time.
    spellbook: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.spells) > MAX_SPELLS:
            raise SpellError(f"spell catalog may contain at most {MAX_SPELLS} spells")

    def get(self, name: str) -> SpellSpec | None:
        key = str(name).strip().casefold()
        direct = self.spells.get(key)
        if direct is not None:
            return direct
        # Localized display names resolve too ("Magic Missile", "火球术").
        for spell in self.spells.values():
            if any(str(label).strip().casefold() == key for label in spell.name.values()):
                return spell
        return None

    def by_level(self, level: int) -> list[SpellSpec]:
        return [spell for spell in self.spells.values() if spell.level == level]

    def __len__(self) -> int:
        return len(self.spells)


def _mapping(pack_id: str, where: str, raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise SpellError(f"rulepack '{pack_id}': {where} must be a mapping")
    return raw


def _str_map(pack_id: str, where: str, raw: Any) -> dict[str, str]:
    mapping = _mapping(pack_id, where, raw)
    return {str(key): str(value).strip() for key, value in mapping.items() if str(value).strip()}


def _bounded_id(pack_id: str, where: str, value: Any) -> str:
    text = str(value).strip().casefold()
    if not text or any(char.isspace() for char in text):
        raise SpellError(f"rulepack '{pack_id}': {where} must be a non-empty id without spaces")
    return text


def _damage(pack_id: str, where: str, raw: Any) -> tuple[DamageComponentSpec, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise SpellError(f"rulepack '{pack_id}': {where} must be a list")
    result: list[DamageComponentSpec] = []
    for index, entry in enumerate(raw):
        item = _mapping(pack_id, f"{where}[{index}]", entry)
        roll = str(item.get("roll") or "").strip()
        if not roll:
            raise SpellError(f"rulepack '{pack_id}': {where}[{index}].roll must be a dice expression")
        damage_type = str(item.get("type") or "").strip()
        result.append(
            DamageComponentSpec(roll=roll, type=damage_type or "untyped")
        )
    return tuple(result)


def _parse_spell(pack_id: str, spell_id: Any, raw: Any) -> SpellSpec:
    mapping = _mapping(pack_id, f"spells[{spell_id!r}]", raw)
    spell_key = _bounded_id(pack_id, f"spells[{spell_id!r}] id", spell_id)
    names = _str_map(pack_id, f"spells[{spell_key}].name", mapping.get("name") or {spell_key: spell_key})
    if "en" not in names:
        raise SpellError(f"rulepack '{pack_id}': spells[{spell_key}].name must include an 'en' label")

    level = mapping.get("level", 0)
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= MAX_SPELL_LEVEL:
        raise SpellError(f"rulepack '{pack_id}': spells[{spell_key}].level must be an integer 0..{MAX_SPELL_LEVEL}")

    components_raw = mapping.get("components") or []
    if not isinstance(components_raw, Sequence) or isinstance(components_raw, (str, bytes, bytearray)):
        raise SpellError(f"rulepack '{pack_id}': spells[{spell_key}].components must be a list")
    components = tuple(str(component).strip().upper() for component in components_raw if str(component).strip())

    save_raw = mapping.get("save")
    save: Mapping[str, Any] | None = None
    if save_raw is not None:
        save_mapping = _mapping(pack_id, f"spells[{spell_key}].save", save_raw)
        ability = str(save_mapping.get("ability") or "").strip()
        if not ability:
            raise SpellError(f"rulepack '{pack_id}': spells[{spell_key}].save.ability is required")
        success = str(save_mapping.get("success") or "none").strip()
        if success not in {"half", "none"}:
            raise SpellError(f"rulepack '{pack_id}': spells[{spell_key}].save.success must be 'half' or 'none'")
        save = {"ability": ability, "success": success}

    attack = bool(mapping.get("attack", False))
    if save is not None and attack:
        raise SpellError(f"rulepack '{pack_id}': spells[{spell_key}] cannot declare both save and attack")

    damage = _damage(pack_id, f"spells[{spell_key}].damage", mapping.get("damage") or [])

    scaling_raw = mapping.get("scaling")
    scaling: Mapping[str, Any] | None = None
    if scaling_raw is not None:
        scaling_mapping = _mapping(pack_id, f"spells[{spell_key}].scaling", scaling_raw)
        every = scaling_mapping.get("every", 1)
        if isinstance(every, bool) or not isinstance(every, int) or every <= 0:
            raise SpellError(f"rulepack '{pack_id}': spells[{spell_key}].scaling.every must be a positive integer")
        add = _damage(pack_id, f"spells[{spell_key}].scaling.add", scaling_mapping.get("add") or [])
        scaling = {"every": every, "add": [component.to_dict() for component in add]}

    return SpellSpec(
        id=spell_key,
        name=names,
        level=level,
        school=str(mapping.get("school") or "").strip(),
        casting_time=str(mapping.get("casting_time") or "").strip(),
        range=str(mapping.get("range") or "").strip(),
        components=components,
        material=str(mapping.get("material") or "").strip(),
        duration=str(mapping.get("duration") or "").strip(),
        concentration=bool(mapping.get("concentration", False)),
        save=save,
        attack=attack,
        damage=damage,
        scaling=scaling,
        dc_ability=str(mapping.get("dc_ability") or "").strip(),
        description=_str_map(pack_id, f"spells[{spell_key}].description", mapping.get("description") or {}),
    )


def parse_spells_yaml(pack_id: str, raw: Any) -> SpellCatalog:
    """Shape-validate one spell catalog mapping (the ``spells:`` document).

    Unknown spell keys are data; unknown per-spell keys are rejected so a
    mistyped field never silently drops a mechanical fact. Level bounds,
    component vocabulary, save/attack exclusivity and dice-expression
    presence are enforced; the dice strings themselves are resolved by the
    dice engine at cast time, exactly like combat-action damage.
    """
    if raw is None:
        return SpellCatalog(spells={})
    mapping = _mapping(pack_id, "spells", raw.get("spells") if isinstance(raw, Mapping) else raw)
    spells: dict[str, SpellSpec] = {}
    for spell_id, value in mapping.items():
        spell = _parse_spell(pack_id, spell_id, value)
        if spell.id in spells:
            raise SpellError(f"rulepack '{pack_id}': duplicate spell id {spell.id!r}")
        spells[spell.id] = spell
    spellbook: dict[str, tuple[str, ...]] = {}
    if isinstance(raw, Mapping) and raw.get("spellbook"):
        book_mapping = _mapping(pack_id, "spellbook", raw.get("spellbook"))
        for class_name, spell_ids in book_mapping.items():
            if not isinstance(spell_ids, Sequence) or isinstance(spell_ids, (str, bytes, bytearray)):
                raise SpellError(f"rulepack '{pack_id}': spellbook[{class_name}] must be a list")
            resolved: list[str] = []
            for entry in spell_ids:
                spell = spells.get(str(entry))
                if spell is None:
                    raise SpellError(f"rulepack '{pack_id}': spellbook[{class_name}] names unknown spell {entry!r}")
                resolved.append(spell.id)
            spellbook[str(class_name).strip().casefold()] = tuple(resolved)
    return SpellCatalog(spells=spells, spellbook=spellbook)


def spell_to_action(spell: SpellSpec, *, slot_level: int | None = None) -> ActionSpec:
    """Build the generic combat ActionSpec a `.cast` / AI tool commits for `spell`.

    Higher-level casting (`slot_level` > spell.level) scales damage per the
    spell's `scaling` block and consumes the matching spell-slot pool. Save
    spells carry no resolution (the target rolls, not the caster); the caller
    applies the save ruling (half damage on success) around the resolution's
    damage. Attack spells resolve as a spell attack vs armor class.
    """
    level = spell.level if slot_level is None else slot_level
    if isinstance(level, bool) or not isinstance(level, int) or level < spell.level or level > MAX_SPELL_LEVEL:
        raise SpellError(f"spell {spell.id!r} cannot be cast at level {level!r}")
    damage: list[DamageComponentSpec] = list(spell.damage)
    if slot_level is not None and spell.scaling:
        every = int(spell.scaling.get("every", 1) or 1)
        steps = slot_level - spell.level
        for _ in range(max(0, steps // every) if every else 0):
            damage.extend(
                DamageComponentSpec(roll=str(entry.get("roll") or ""), type=str(entry.get("type") or "untyped"))
                for entry in (spell.scaling.get("add") or [])
            )
    resolution: Mapping[str, Any] | None = None
    if spell.attack:
        resolution = {"kind": "attack", "defense": "armor_class"}
    if spell.save:
        # Save-ruled damage carries the `save_half` tag: the caller sets that
        # factor on a target's defenses (0.5 on a successful half save, 0.0 on
        # a no-damage save) and untouched targets keep factor 1.0.
        damage = [DamageComponentSpec(roll=component.roll, type=component.type, tags=("save_half",)) for component in damage]
    return ActionSpec(
        id=spell.id,
        cost={"action": 1},
        targeting={"count": 1, "relationship": "enemy"},
        resolution=resolution,
        damage=tuple(damage),
        resource_costs=(ResourceCostSpec(pool=f"spell_slot_{level}", amount=1),),
        concentration=spell.concentration,
        label=dict(spell.name),
    )
