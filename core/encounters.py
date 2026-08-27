"""Validated encounter definitions and deterministic budget arithmetic."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.statblocks import StatBlock, StatBlockError
from infra.room_facets import STORAGE_DOCUMENTS, RoomStateFacet

ENCOUNTER_SCHEMA_VERSION = 1


class EncounterError(ValueError):
    """An encounter definition or budget request is invalid."""


@dataclass(frozen=True)
class EncounterEntry:
    """One stat-block reference and its independent encounter count."""

    reference: str
    count: int = 1
    side: str = ""
    position: Any = None
    category: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ref": self.reference, "count": self.count}
        if self.side:
            result["side"] = self.side
        if self.position is not None:
            result["position"] = copy.deepcopy(self.position)
        if self.category:
            result["category"] = self.category
        return result


@dataclass(frozen=True)
class BudgetResult:
    """Arithmetic evidence for one party-versus-encounter comparison."""

    party_size: int
    base_weight: float
    count_multiplier: float
    adjusted_weight: float
    threshold: Any
    band: str
    evidence: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "party_size": self.party_size,
            "base_weight": self.base_weight,
            "count_multiplier": self.count_multiplier,
            "adjusted_weight": self.adjusted_weight,
            "threshold": self.threshold,
            "band": self.band,
            "evidence": [dict(item) for item in self.evidence],
        }


@dataclass(frozen=True)
class Encounter:
    """A reusable encounter definition, not mutable combat state."""

    id: str
    name: str
    public: Mapping[str, Any] = field(default_factory=dict)
    keeper_notes: str = ""
    entries: tuple[EncounterEntry, ...] = ()
    sides: Mapping[str, Any] = field(default_factory=dict)
    surprise: Any = None
    initiative_policy: Any = None
    environment: tuple[str, ...] = ()
    objectives: tuple[Any, ...] = ()
    end_conditions: tuple[Any, ...] = ()
    rewards: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self, *, keeper: bool = True) -> dict[str, Any]:
        if not keeper:
            return {
                "schema_version": ENCOUNTER_SCHEMA_VERSION,
                "id": self.id,
                "name": self.name,
                "public": copy.deepcopy(dict(self.public)),
            }
        return {
            "schema_version": ENCOUNTER_SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "public": copy.deepcopy(dict(self.public)),
            "keeper_notes": self.keeper_notes,
            "entries": [entry.to_dict() for entry in self.entries],
            "sides": copy.deepcopy(dict(self.sides)),
            "surprise": copy.deepcopy(self.surprise),
            "initiative_policy": copy.deepcopy(self.initiative_policy),
            "environment": list(self.environment),
            "objectives": copy.deepcopy(list(self.objectives)),
            "end_conditions": copy.deepcopy(list(self.end_conditions)),
            "rewards": copy.deepcopy(dict(self.rewards)),
        }

    def budget(self, statblocks: Mapping[str, StatBlock], party_size: int, declaration: Mapping[str, Any]) -> BudgetResult:
        return calculate_budget(self, statblocks, party_size=party_size, declaration=declaration)


def _entry(path: str, raw: Any) -> EncounterEntry:
    if isinstance(raw, str):
        reference, count = raw, 1
        mapping: Mapping[str, Any] = {}
    elif isinstance(raw, Mapping):
        mapping = raw
        reference = str(mapping.get("ref") or mapping.get("statblock") or "").strip()
        count = mapping.get("count", 1)
    else:
        raise EncounterError(f"{path} must be a stat-block reference")  # i18n-exempt: internal validation diagnostic
    if not reference:
        raise EncounterError(f"{path} requires ref")  # i18n-exempt: internal validation diagnostic
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise EncounterError(f"{path}.count must be a positive integer")  # i18n-exempt: internal validation diagnostic
    unknown = set(map(str, mapping)) - {"ref", "statblock", "count", "side", "position", "category"}
    if unknown:
        raise EncounterError(f"{path} has unknown keys {sorted(unknown)}")  # i18n-exempt: internal validation diagnostic
    return EncounterEntry(
        reference=reference,
        count=count,
        side=str(mapping.get("side") or ""),
        position=copy.deepcopy(mapping.get("position")),
        category=str(mapping.get("category") or ""),
    )


def parse_encounter(raw: Any, *, encounter_id: str | None = None) -> Encounter:
    """Validate an encounter document and preserve only structured content."""
    if not isinstance(raw, Mapping):
        raise EncounterError("encounter must be a mapping")  # i18n-exempt: internal validation diagnostic
    allowed = {
        "schema_version",
        "id",
        "name",
        "public",
        "keeper_notes",
        "entries",
        "statblocks",
        "sides",
        "surprise",
        "initiative_policy",
        "environment",
        "objectives",
        "end_conditions",
        "rewards",
    }
    unknown = set(map(str, raw)) - allowed
    if unknown:
        raise EncounterError(f"encounter has unknown keys {sorted(unknown)}")  # i18n-exempt: internal validation diagnostic
    version = raw.get("schema_version", ENCOUNTER_SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version != ENCOUNTER_SCHEMA_VERSION:
        raise EncounterError("unsupported encounter schema version")  # i18n-exempt: internal validation diagnostic
    identifier = str(raw.get("id") or encounter_id or "").strip()
    name = str(raw.get("name") or "").strip()
    if not identifier or not name:
        raise EncounterError("encounter requires id and name")  # i18n-exempt: internal validation diagnostic
    entries_raw = raw.get("entries", raw.get("statblocks", [])) or []
    if not isinstance(entries_raw, Sequence) or isinstance(entries_raw, (str, bytes, bytearray)):
        raise EncounterError("encounter entries must be a list")  # i18n-exempt: internal validation diagnostic
    entries = tuple(_entry(f"encounter.entries[{index}]", value) for index, value in enumerate(entries_raw))
    environment = raw.get("environment", []) or []
    if isinstance(environment, str) or not isinstance(environment, Sequence):
        raise EncounterError("encounter environment must be a list")  # i18n-exempt: internal validation diagnostic
    return Encounter(
        id=identifier,
        name=name,
        public=dict(raw.get("public") or {}) if isinstance(raw.get("public") or {}, Mapping) else {},
        keeper_notes=str(raw.get("keeper_notes") or ""),
        entries=entries,
        sides=dict(raw.get("sides") or {}) if isinstance(raw.get("sides") or {}, Mapping) else {},
        surprise=copy.deepcopy(raw.get("surprise")),
        initiative_policy=copy.deepcopy(raw.get("initiative_policy")),
        environment=tuple(str(item) for item in environment),
        objectives=tuple(copy.deepcopy(raw.get("objectives") or [])),
        end_conditions=tuple(copy.deepcopy(raw.get("end_conditions") or [])),
        rewards=dict(raw.get("rewards") or {}) if isinstance(raw.get("rewards") or {}, Mapping) else {},
    )


def _count_multiplier(count: int, declaration: Mapping[str, Any]) -> tuple[float, Any]:
    raw = declaration.get("count_multipliers", {})
    if not isinstance(raw, Mapping):
        raise EncounterError("count_multipliers must be a mapping")  # i18n-exempt: internal validation diagnostic
    candidates: list[tuple[int, float, Any]] = []
    for key, value in raw.items():
        try:
            lower = int(str(key).split("-", 1)[0])
        except ValueError:
            continue
        if "-" in str(key):
            try:
                upper = int(str(key).split("-", 1)[1])
            except ValueError:
                continue
            if not lower <= count <= upper:
                continue
        elif lower != count:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise EncounterError(f"count multiplier {key!r} must be non-negative")  # i18n-exempt: internal validation diagnostic
        candidates.append((lower, float(value), key))
    if not candidates:
        return 1.0, None
    _, multiplier, key = max(candidates, key=lambda item: item[0])
    return multiplier, key


def _threshold(party_size: int, adjusted_weight: float, declaration: Mapping[str, Any]) -> tuple[Any, str]:
    raw = declaration.get("party_thresholds", {})
    if not isinstance(raw, Mapping):
        raise EncounterError("party_thresholds must be a mapping")  # i18n-exempt: internal validation diagnostic
    selected: tuple[int, Any] | None = None
    for key, value in raw.items():
        try:
            size = int(str(key))
        except ValueError:
            continue
        if size <= party_size and (selected is None or size > selected[0]):
            selected = (size, value)
    if selected is None:
        return None, "unrated"
    threshold = selected[1]
    if isinstance(threshold, Mapping):
        bands = []
        for band, limit in threshold.items():
            if isinstance(limit, (int, float)) and not isinstance(limit, bool):
                bands.append((float(limit), str(band)))
        bands.sort()
        chosen = "unrated"
        chosen_limit: float | None = None
        for limit, band in bands:
            if adjusted_weight >= limit:
                chosen_limit, chosen = limit, band
        return {"party_size": selected[0], "thresholds": dict(threshold), "matched": chosen_limit}, chosen
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        return {"party_size": selected[0], "value": threshold}, "above" if adjusted_weight >= threshold else "below"
    return threshold, str(threshold)


def calculate_budget(
    encounter: Encounter,
    statblocks: Mapping[str, StatBlock],
    *,
    party_size: int,
    declaration: Mapping[str, Any],
) -> BudgetResult:
    """Sum challenge weights, apply count multipliers, and expose arithmetic evidence."""
    if isinstance(party_size, bool) or not isinstance(party_size, int) or party_size < 1:
        raise EncounterError("party size must be a positive integer")  # i18n-exempt: internal validation diagnostic
    base = 0.0
    evidence: list[Mapping[str, Any]] = []
    for entry in encounter.entries:
        statblock = statblocks.get(entry.reference)
        if statblock is None:
            raise EncounterError(f"unknown stat block {entry.reference!r}")  # i18n-exempt: internal validation diagnostic
        subtotal = float(statblock.challenge_weight) * entry.count
        base += subtotal
        evidence.append({"ref": entry.reference, "count": entry.count, "unit_weight": statblock.challenge_weight, "subtotal": subtotal})
    multiplier, multiplier_key = _count_multiplier(sum(entry.count for entry in encounter.entries), declaration)
    adjusted = base * multiplier
    threshold, band = _threshold(party_size, adjusted, declaration)
    evidence.append({"count_multiplier": multiplier, "matched": multiplier_key})
    evidence.append({"party_size": party_size, "threshold": threshold, "band": band})
    return BudgetResult(
        party_size=party_size,
        base_weight=base,
        count_multiplier=multiplier,
        adjusted_weight=adjusted,
        threshold=threshold,
        band=band,
        evidence=tuple(evidence),
    )


def encounter_instances(encounter: Encounter, statblocks: Mapping[str, StatBlock]) -> list[dict[str, Any]]:
    """Expand counted references into independent mutable combatant instances."""
    instances: list[dict[str, Any]] = []
    for entry in encounter.entries:
        template = statblocks.get(entry.reference)
        if template is None:
            raise StatBlockError(f"unknown stat block {entry.reference!r}")  # i18n-exempt: internal validation diagnostic
        for index in range(entry.count):
            instance_id = f"{encounter.id}:{entry.reference}:{index + 1}"
            instance = template.instance(instance_id)
            instance.update({"side": entry.side, "position": copy.deepcopy(entry.position), "category": entry.category})
            instances.append(instance)
    return instances


def project_encounter(encounter: Encounter, *, keeper: bool = False) -> dict[str, Any]:
    return encounter.to_dict(keeper=keeper)


ROOM_FACETS = (
    RoomStateFacet(
        name="encounter_catalog",
        owner="core.encounters",
        reset_scope="all",
        doc_types=frozenset({"encounter"}),
        storages=frozenset({STORAGE_DOCUMENTS}),
    ),
)
