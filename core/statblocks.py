"""Validated reusable stat blocks and encounter-local combat instances."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.runtime import ActionSpec, parse_action_spec
from infra.room_facets import STORAGE_DOCUMENTS, RoomStateFacet

STATBLOCK_SCHEMA_VERSION = 1
MAX_STATBLOCK_ACTIONS = 64


class StatBlockError(ValueError):
    """A stat block is malformed or cannot produce an instance."""


@dataclass(frozen=True)
class StatBlock:
    """Immutable reusable mechanics template."""

    id: str
    name: str
    public: Mapping[str, Any] = field(default_factory=dict)
    description: str = ""
    mechanics_ref: str = ""
    resources: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    defenses: Mapping[str, Any] = field(default_factory=dict)
    actions: Mapping[str, ActionSpec] = field(default_factory=dict)
    traits: Mapping[str, Any] = field(default_factory=dict)
    saves: Mapping[str, Any] = field(default_factory=dict)
    challenge_weight: float = 0.0
    source: str = ""

    def to_dict(self, *, keeper: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": STATBLOCK_SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "public": copy.deepcopy(dict(self.public)),
            "description": self.description,
            "mechanics_ref": self.mechanics_ref,
            "challenge_weight": self.challenge_weight,
            "source": self.source,
        }
        if keeper:
            result.update(
                {
                    "resources": copy.deepcopy(dict(self.resources)),
                    "defenses": copy.deepcopy(dict(self.defenses)),
                    "actions": {key: value.to_dict() for key, value in self.actions.items()},
                    "traits": copy.deepcopy(dict(self.traits)),
                    "saves": copy.deepcopy(dict(self.saves)),
                }
            )
        return result

    def instance(self, instance_id: str, *, controller: str = "", controller_id: str = "") -> dict[str, Any]:
        """Create independent mutable encounter state from this template."""
        identifier = str(instance_id).strip()
        if not identifier:
            raise StatBlockError("stat block instance id must not be empty")  # i18n-exempt: internal validation diagnostic
        return {
            "id": identifier,
            "name": self.name,
            "template_id": self.id,
            "mechanics_ref": f"statblock:{self.id}",
            "controller": str(controller),
            "controller_id": str(controller_id),
            "initiative": 0,
            "resources": copy.deepcopy(dict(self.resources)),
            "defenses": copy.deepcopy(dict(self.defenses)),
            "conditions": [],
            "state": "ready",
            "public": copy.deepcopy(dict(self.public)),
            "challenge_weight": self.challenge_weight,
        }


def _mapping(path: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StatBlockError(f"{path} must be a mapping")  # i18n-exempt: internal validation diagnostic
    return value


def parse_statblock(pack_id: str, raw: Any, *, statblock_id: str | None = None) -> StatBlock:
    """Validate one stat block; action mechanics use the shared runtime parser."""
    mapping = _mapping("statblock", raw)
    allowed = {
        "schema_version",
        "id",
        "name",
        "public",
        "description",
        "mechanics_ref",
        "resources",
        "defenses",
        "actions",
        "traits",
        "saves",
        "challenge_weight",
        "source",
    }
    unknown = set(map(str, mapping)) - allowed
    if unknown:
        raise StatBlockError(f"statblock has unknown keys {sorted(unknown)}")  # i18n-exempt: internal validation diagnostic
    version = mapping.get("schema_version", STATBLOCK_SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version != STATBLOCK_SCHEMA_VERSION:
        raise StatBlockError("unsupported stat block schema version")  # i18n-exempt: internal validation diagnostic
    identifier = str(mapping.get("id") or statblock_id or "").strip()
    name = str(mapping.get("name") or "").strip()
    if not identifier or not name:
        raise StatBlockError("statblock requires id and name")  # i18n-exempt: internal validation diagnostic
    public = dict(_mapping("statblock.public", mapping.get("public") or {}))
    resources = dict(_mapping("statblock.resources", mapping.get("resources") or {}))
    defenses = dict(_mapping("statblock.defenses", mapping.get("defenses") or {}))
    traits = dict(_mapping("statblock.traits", mapping.get("traits") or {}))
    saves = dict(_mapping("statblock.saves", mapping.get("saves") or {}))
    challenge = mapping.get("challenge_weight", 0)
    if isinstance(challenge, bool) or not isinstance(challenge, (int, float)) or challenge < 0:
        raise StatBlockError("statblock challenge weight must be non-negative")  # i18n-exempt: internal validation diagnostic
    action_raw = mapping.get("actions") or {}
    actions: dict[str, ActionSpec] = {}
    if isinstance(action_raw, Mapping):
        if len(action_raw) > MAX_STATBLOCK_ACTIONS:
            raise StatBlockError("statblock has too many actions")  # i18n-exempt: internal validation diagnostic
        for action_id, value in action_raw.items():
            actions[str(action_id)] = parse_action_spec(pack_id, str(action_id), value)
    elif isinstance(action_raw, Sequence) and not isinstance(action_raw, (str, bytes, bytearray)):
        if len(action_raw) > MAX_STATBLOCK_ACTIONS:
            raise StatBlockError("statblock has too many actions")  # i18n-exempt: internal validation diagnostic
        for value in action_raw:
            action_id = str(value).strip()
            if not action_id:
                raise StatBlockError("statblock action id must not be empty")  # i18n-exempt: internal validation diagnostic
            actions[action_id] = ActionSpec(id=action_id)
    else:
        raise StatBlockError("statblock actions must be a mapping or list")  # i18n-exempt: internal validation diagnostic
    return StatBlock(
        id=identifier,
        name=name,
        public=public,
        description=str(mapping.get("description") or ""),
        mechanics_ref=str(mapping.get("mechanics_ref") or f"statblock:{identifier}"),
        resources=resources,
        defenses=defenses,
        actions=actions,
        traits=traits,
        saves=saves,
        challenge_weight=float(challenge),
        source=str(mapping.get("source") or ""),
    )


def project_statblock(statblock: StatBlock, *, keeper: bool = False, owner: bool = False) -> dict[str, Any] | None:
    """Keeper/owner view versus public identity-only view."""
    if keeper or owner:
        return statblock.to_dict(keeper=True)
    result = {
        "schema_version": STATBLOCK_SCHEMA_VERSION,
        "id": statblock.id,
        "name": statblock.name,
        "public": copy.deepcopy(dict(statblock.public)),
        "description": statblock.description,
    }
    return result


@dataclass(frozen=True)
class StatBlockDocument:
    """Document-layer adapter retaining source metadata outside the payload."""

    statblock: StatBlock
    source: str = ""

    def project(self, *, keeper: bool = False, owner: bool = False) -> dict[str, Any] | None:
        return project_statblock(self.statblock, keeper=keeper, owner=owner)


ROOM_FACETS = (
    RoomStateFacet(
        name="statblock_catalog",
        owner="core.statblocks",
        reset_scope="all",
        doc_types=frozenset({"statblock"}),
        storages=frozenset({STORAGE_DOCUMENTS}),
    ),
)
