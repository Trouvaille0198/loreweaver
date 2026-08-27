"""Pure, system-neutral combat state transitions.

The state machine owns turn order, claims, budgets, reaction windows, and mutable
combatant instances.  Pack-specific actions and effects are resolved by callers,
then applied here as one transition.  No transition performs I/O or model calls.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from infra.room_facets import STORAGE_ROOM_STATE, RoomStateFacet

COMBAT_SCHEMA_VERSION = 1
DEFAULT_BUDGET_KEYS = ("action", "bonus", "movement", "reaction")


class CombatError(ValueError):
    """A combat transition is invalid for the current state."""


class StaleCombatError(CombatError):
    """The caller used a state revision or claim token that is no longer current."""


class TurnOwnershipError(CombatError):
    """The requested mutation is not controlled by the authenticated actor."""


@dataclass(frozen=True)
class ActionApplication:
    """Structured facts to apply after an action resolver has rolled."""

    action_id: str
    actor_id: str
    budget_cost: Mapping[str, int] = field(default_factory=dict)
    target_updates: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    actor_updates: Mapping[str, Any] = field(default_factory=dict)
    reaction_window: Mapping[str, Any] | None = None
    event: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class CombatState:
    """Versioned JSON-compatible combat state."""

    schema_version: int = COMBAT_SCHEMA_VERSION
    id: str = ""
    revision: int = 0
    phase: str = "pending"
    round: int = 0
    turn_index: int = 0
    current: str | None = None
    claim: Mapping[str, Any] | None = None
    order: tuple[str, ...] = ()
    combatants: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    budget: Mapping[str, int] = field(default_factory=dict)
    reaction_window: Mapping[str, Any] | None = None
    event_seq: int = 0
    events: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CombatState:
        if not isinstance(raw, Mapping):
            raise CombatError("combat state must be a mapping")  # i18n-exempt: internal validation diagnostic
        version = raw.get("schema_version", COMBAT_SCHEMA_VERSION)
        if isinstance(version, bool) or not isinstance(version, int) or version != COMBAT_SCHEMA_VERSION:
            raise CombatError(f"unsupported combat schema version {version!r}")
        order_raw = raw.get("order") or []
        if not isinstance(order_raw, Sequence) or isinstance(order_raw, (str, bytes, bytearray)):
            raise CombatError("combat order must be a list")  # i18n-exempt: internal validation diagnostic
        order = tuple(str(item) for item in order_raw)
        if len(set(order)) != len(order):
            raise CombatError("combat order contains duplicate combatants")
        combatants_raw = raw.get("combatants") or {}
        if not isinstance(combatants_raw, Mapping):
            raise CombatError("combat combatants must be a mapping")  # i18n-exempt: internal validation diagnostic
        combatants: dict[str, dict[str, Any]] = {}
        for key, value in combatants_raw.items():
            if not isinstance(value, Mapping):
                raise CombatError("combatant entries must be mappings")  # i18n-exempt: internal validation diagnostic
            combatants[str(key)] = copy.deepcopy(dict(value))
        if set(order) != set(combatants):
            raise CombatError("combat order and combatants must contain the same ids")  # i18n-exempt: internal validation diagnostic
        current = raw.get("current")
        if current is not None:
            current = str(current)
            if current not in combatants:
                raise CombatError("combat current is not a combatant")  # i18n-exempt: internal validation diagnostic
        phase = str(raw.get("phase") or "pending")
        if phase not in {"pending", "active", "ended", "aborted"}:
            raise CombatError("combat phase is invalid")
        revision = raw.get("revision", 0)
        round_number = raw.get("round", 0)
        turn_index = raw.get("turn_index", 0)
        event_seq = raw.get("event_seq", 0)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (revision, round_number, turn_index, event_seq)):
            raise CombatError("combat counters must be non-negative integers")  # i18n-exempt: internal validation diagnostic
        budget = _budget_map(raw.get("budget") or {})
        claim = copy.deepcopy(raw.get("claim")) if isinstance(raw.get("claim"), Mapping) else None
        reaction = copy.deepcopy(raw.get("reaction_window")) if isinstance(raw.get("reaction_window"), Mapping) else None
        events_raw = raw.get("events") or []
        if not isinstance(events_raw, Sequence) or isinstance(events_raw, (str, bytes, bytearray)):
            raise CombatError("combat events must be a list")  # i18n-exempt: internal validation diagnostic
        events: list[dict[str, Any]] = []
        for item in events_raw:
            if not isinstance(item, Mapping):
                raise CombatError("combat event entries must be mappings")  # i18n-exempt: internal validation diagnostic
            events.append(copy.deepcopy(dict(item)))
        return cls(
            schema_version=version,
            id=str(raw.get("id") or ""),
            revision=revision,
            phase=phase,
            round=round_number,
            turn_index=turn_index,
            current=current,
            claim=claim,
            order=order,
            combatants=combatants,
            budget=budget,
            reaction_window=reaction,
            event_seq=event_seq,
            events=events,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "revision": self.revision,
            "phase": self.phase,
            "round": self.round,
            "turn_index": self.turn_index,
            "current": self.current,
            "claim": copy.deepcopy(self.claim),
            "order": list(self.order),
            "combatants": copy.deepcopy(dict(self.combatants)),
            "budget": dict(self.budget),
            "reaction_window": copy.deepcopy(self.reaction_window),
            "event_seq": self.event_seq,
            "events": copy.deepcopy(list(self.events)),
        }

    def json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


# ---------------------------------------------------------------------------
# Pure transition helpers
# ---------------------------------------------------------------------------


def _budget_map(raw: Mapping[str, Any], *, defaults: Mapping[str, int] | None = None) -> dict[str, int]:
    source = dict(defaults or {})
    source.update(raw)
    result: dict[str, int] = {}
    for key, value in source.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CombatError(f"budget {key!r} must be numeric")
        result[str(key)] = max(0, int(value))
    return result


def _bump(state: CombatState, **changes: Any) -> CombatState:
    payload = state.to_dict()
    payload.update(changes)
    payload["revision"] = state.revision + 1
    return CombatState.from_dict(payload)


def _combatant_payload(
    combatant_id: str,
    *,
    name: str | None = None,
    initiative: int = 0,
    mechanics_ref: str = "",
    controller: str = "",
    controller_id: str = "",
    budget: Mapping[str, int] | None = None,
    public: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    identifier = str(combatant_id).strip()
    if not identifier:
        raise CombatError("combatant id must not be empty")  # i18n-exempt: internal validation diagnostic
    if isinstance(initiative, bool) or not isinstance(initiative, (int, float)):
        raise CombatError("initiative must be numeric")
    current_budget = _budget_map(budget or {})
    return {
        "id": identifier,
        "name": str(name if name is not None else identifier),
        "initiative": int(initiative),
        "mechanics_ref": str(mechanics_ref),
        "controller": str(controller),
        "controller_id": str(controller_id),
        "budget": current_budget,
        "budget_max": dict(current_budget),
        "conditions": [],
        "state": "ready",
        "public": copy.deepcopy(dict(public or {})),
    }


def _ordered_ids(combatants: Mapping[str, Mapping[str, Any]], existing: Sequence[str] = ()) -> list[str]:
    positions = {str(identifier): index for index, identifier in enumerate(existing)}
    return sorted(
        (str(identifier) for identifier in combatants),
        key=lambda identifier: (-int(combatants[identifier].get("initiative", 0) or 0), positions.get(identifier, len(positions))),
    )


def create_combat(
    encounter_id: str,
    combatants: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]] = (),
    *,
    budget: Mapping[str, int] | None = None,
) -> CombatState:
    """Create an explicit pending encounter; ``start_combat`` activates it."""
    identifier = str(encounter_id).strip()
    if not identifier:
        raise CombatError("encounter id must not be empty")  # i18n-exempt: internal validation diagnostic
    normalized: dict[str, Mapping[str, Any]] = {}
    if isinstance(combatants, Mapping):
        entries = combatants.items()
        for key, value in entries:
            if not isinstance(value, Mapping):
                raise CombatError("combatant entries must be mappings")
            payload = dict(value)
            payload.setdefault("id", key)
            cid = str(payload.get("id") or key)
            normalized[cid] = _combatant_payload(
                cid,
                name=payload.get("name"),
                initiative=payload.get("initiative", 0),
                mechanics_ref=str(payload.get("mechanics_ref", "")),
                controller=str(payload.get("controller", "")),
                controller_id=str(payload.get("controller_id", "")),
                budget=payload.get("budget", budget or {}),
                public=payload.get("public"),
            )
    else:
        for value in combatants:
            if not isinstance(value, Mapping):
                raise CombatError("combatant entries must be mappings")
            cid = str(value.get("id") or "")
            if cid in normalized:
                raise CombatError(f"duplicate combatant {cid!r}")
            normalized[cid] = _combatant_payload(
                cid,
                name=value.get("name"),
                initiative=value.get("initiative", 0),
                mechanics_ref=str(value.get("mechanics_ref", "")),
                controller=str(value.get("controller", "")),
                controller_id=str(value.get("controller_id", "")),
                budget=value.get("budget", budget or {}),
                public=value.get("public"),
            )
    order = _ordered_ids(normalized)
    return CombatState(id=identifier, order=tuple(order), combatants=normalized, budget={})


def start_combat(state: CombatState, *, budget: Mapping[str, int] | None = None) -> CombatState:
    """Start a pending encounter and select its first eligible combatant."""
    if state.phase != "pending":
        raise CombatError("combat can only start from pending")  # i18n-exempt: internal validation diagnostic
    if not state.order:
        raise CombatError("combat needs at least one combatant")  # i18n-exempt: internal validation diagnostic
    template = _budget_map(budget or {})
    combatants = copy.deepcopy(dict(state.combatants))
    for _cid, combatant in combatants.items():
        current = _budget_map(combatant.get("budget_max") or combatant.get("budget") or template, defaults=template)
        combatant["budget"] = current
        combatant["budget_max"] = dict(current)
        combatant["state"] = "ready"
    first_index, first = _next_eligible(state.order, combatants, start=0)
    if first is None:
        raise CombatError("combat has no eligible combatant")
    payload = state.to_dict()
    payload.update(
        {
            "phase": "active",
            "round": 1,
            "turn_index": first_index,
            "current": first,
            "claim": None,
            "combatants": combatants,
            "budget": dict(combatants[first].get("budget") or {}),
        }
    )
    return _bump(state, **payload)


def end_combat(state: CombatState, *, reason: str = "") -> CombatState:
    """Explicitly end a pending or active encounter."""
    if state.phase not in {"pending", "active"}:
        raise CombatError("combat is already ended")
    payload = state.to_dict()
    payload.update({"phase": "ended", "current": None, "claim": None, "reaction_window": None, "budget": {}})
    if reason:
        payload["end_reason"] = str(reason)
    return _bump(state, **payload)


def join_combat(
    state: CombatState,
    combatant_id: str,
    *,
    name: str | None = None,
    initiative: int = 0,
    mechanics_ref: str = "",
    controller: str = "",
    controller_id: str = "",
    budget: Mapping[str, int] | None = None,
) -> CombatState:
    """Add one independent encounter instance without changing the current claim."""
    if state.phase not in {"pending", "active"}:
        raise CombatError("cannot join an ended combat")
    cid = str(combatant_id)
    if cid in state.combatants:
        raise CombatError(f"combatant {cid!r} already joined")
    combatants = copy.deepcopy(dict(state.combatants))
    combatants[cid] = _combatant_payload(
        cid,
        name=name,
        initiative=initiative,
        mechanics_ref=mechanics_ref,
        controller=controller,
        controller_id=controller_id,
        budget=budget or state.budget,
    )
    order = _ordered_ids(combatants, state.order)
    turn_index = state.turn_index
    if state.current is not None and state.current in order:
        turn_index = order.index(state.current)
    payload = state.to_dict()
    payload.update({"combatants": combatants, "order": order, "turn_index": turn_index})
    return _bump(state, **payload)


def remove_combatant(state: CombatState, combatant_id: str) -> CombatState:
    """Remove an encounter instance and repair the current turn index."""
    if state.phase not in {"pending", "active"}:
        raise CombatError("cannot remove from an ended combat")  # i18n-exempt: internal validation diagnostic
    cid = str(combatant_id)
    if cid not in state.combatants:
        raise CombatError(f"unknown combatant {cid!r}")
    combatants = copy.deepcopy(dict(state.combatants))
    del combatants[cid]
    order = [item for item in state.order if item != cid]
    payload = state.to_dict()
    payload["combatants"] = combatants
    payload["order"] = order
    if cid == state.current:
        if order and state.phase == "active":
            index, next_id = _next_eligible(order, combatants, start=state.turn_index % len(order))
            payload.update({"turn_index": index, "current": next_id, "claim": None, "budget": dict(combatants[next_id].get("budget") or {})})
        else:
            payload.update({"turn_index": 0, "current": None, "claim": None, "budget": {}})
    elif state.current in order:
        payload["turn_index"] = order.index(state.current)
    return _bump(state, **payload)


def set_initiative(state: CombatState, combatant_id: str, initiative: int) -> CombatState:
    """Set one initiative value; ties preserve their previous relative order."""
    cid = str(combatant_id)
    if cid not in state.combatants:
        raise CombatError(f"unknown combatant {cid!r}")
    if isinstance(initiative, bool) or not isinstance(initiative, (int, float)):
        raise CombatError("initiative must be numeric")
    combatants = copy.deepcopy(dict(state.combatants))
    combatants[cid]["initiative"] = int(initiative)
    order = _ordered_ids(combatants, state.order)
    payload = state.to_dict()
    payload.update({"combatants": combatants, "order": order})
    if state.current in order:
        payload["turn_index"] = order.index(state.current)
    return _bump(state, **payload)


def _next_eligible(order: Sequence[str], combatants: Mapping[str, Mapping[str, Any]], *, start: int) -> tuple[int, str | None]:
    if not order:
        return 0, None
    for offset in range(len(order)):
        index = (start + offset) % len(order)
        cid = order[index]
        status = str(combatants[cid].get("state", "ready"))
        if status not in {"defeated", "dead", "escaped"}:
            return index, cid
    return 0, None


def _require_active(state: CombatState) -> None:
    if state.phase != "active" or state.current is None:
        raise CombatError("combat has no active turn")


def _require_revision(state: CombatState, expected_revision: int | None) -> None:
    if expected_revision is not None and expected_revision != state.revision:
        raise StaleCombatError("combat revision is stale")


def controller_can_act(
    state: CombatState,
    actor_id: str,
    controller_id: str,
    *,
    keeper_override: bool = False,
) -> bool:
    """Check current-turn ownership without changing state."""
    if state.current != str(actor_id):
        return False
    combatant = state.combatants.get(str(actor_id))
    if combatant is None:
        return False
    if keeper_override and str(controller_id) == "keeper":
        return True
    expected = str(combatant.get("controller_id") or "")
    return bool(expected) and expected == str(controller_id)


def claim_turn(
    state: CombatState,
    actor_id: str,
    controller_id: str,
    *,
    expected_revision: int | None = None,
    token: str | None = None,
    keeper_override: bool = False,
) -> CombatState:
    """CAS-ready claim of exactly the current controller's turn."""
    _require_revision(state, expected_revision)
    _require_active(state)
    if state.claim is not None:
        raise StaleCombatError("combat turn is already claimed")
    if not controller_can_act(state, actor_id, controller_id, keeper_override=keeper_override):
        raise TurnOwnershipError("actor is not the current controller")  # i18n-exempt: internal validation diagnostic
    claim_token = str(token or f"{state.id}:{state.revision}:{actor_id}")
    payload = state.to_dict()
    payload["claim"] = {
        "token": claim_token,
        "actor": str(actor_id),
        "controller": str(controller_id),
        "revision": state.revision,
    }
    return _bump(state, **payload)


def release_claim(state: CombatState, token: str, *, expected_revision: int | None = None) -> CombatState:
    _require_revision(state, expected_revision)
    if state.claim is None or state.claim.get("token") != str(token):
        raise StaleCombatError("claim token is stale")
    payload = state.to_dict()
    payload["claim"] = None
    return _bump(state, **payload)


def _require_claim(state: CombatState, actor_id: str, claim_token: str | None) -> None:
    if state.claim is None:
        raise StaleCombatError("turn must be claimed before a mechanical mutation")  # i18n-exempt: internal validation diagnostic
    if state.claim.get("actor") != str(actor_id) or state.claim.get("token") != str(claim_token):
        raise TurnOwnershipError("claim does not belong to actor")  # i18n-exempt: internal validation diagnostic


def spend_budget(
    state: CombatState,
    actor_id: str,
    costs: Mapping[str, int],
    *,
    claim_token: str | None = None,
    expected_revision: int | None = None,
) -> CombatState:
    """Spend action/bonus/movement/reaction budget without allowing negatives."""
    _require_revision(state, expected_revision)
    _require_active(state)
    _require_claim(state, actor_id, claim_token)
    if state.current != str(actor_id):
        raise TurnOwnershipError("only the current actor can spend budget")  # i18n-exempt: internal validation diagnostic
    if not isinstance(costs, Mapping):
        raise CombatError("budget costs must be a mapping")  # i18n-exempt: internal validation diagnostic
    current = _budget_map(state.budget)
    normalized: dict[str, int] = {}
    for key, value in costs.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) < 0:
            raise CombatError(f"budget cost {key!r} must be non-negative")
        amount = int(value)
        normalized[str(key)] = amount
        if current.get(str(key), 0) < amount:
            raise CombatError(f"budget {key!r} is insufficient")
    remaining = dict(current)
    for key, amount in normalized.items():
        remaining[key] = remaining.get(key, 0) - amount
    combatants = copy.deepcopy(dict(state.combatants))
    combatants[str(actor_id)]["budget"] = dict(remaining)
    payload = state.to_dict()
    payload.update({"budget": remaining, "combatants": combatants})
    return _bump(state, **payload)


def open_reaction_window(
    state: CombatState,
    *,
    trigger: str,
    eligible: Iterable[str],
    expires: Mapping[str, Any] | None = None,
) -> CombatState:
    """Open one bounded reaction opportunity; only one window can be active."""
    _require_active(state)
    if state.reaction_window is not None:
        raise CombatError("a reaction window is already open")  # i18n-exempt: internal validation diagnostic
    eligible_ids = tuple(dict.fromkeys(str(item) for item in eligible if str(item) in state.combatants))
    if not eligible_ids:
        raise CombatError("reaction window needs an eligible controller")  # i18n-exempt: internal validation diagnostic
    payload = state.to_dict()
    payload["reaction_window"] = {
        "trigger": str(trigger),
        "eligible": list(eligible_ids),
        "expires": copy.deepcopy(dict(expires or {})),
        "resolved": None,
    }
    return _bump(state, **payload)


def resolve_reaction(state: CombatState, actor_id: str, *, result: Mapping[str, Any] | None = None) -> CombatState:
    """Resolve and close a reaction window for an eligible actor."""
    window = state.reaction_window
    if window is None or str(actor_id) not in {str(item) for item in window.get("eligible", [])}:
        raise TurnOwnershipError("actor is not eligible for the reaction window")  # i18n-exempt: internal validation diagnostic
    payload = state.to_dict()
    payload["reaction_window"] = {**dict(window), "resolved": {"actor": str(actor_id), **dict(result or {})}}
    return _bump(state, **payload)


def expire_reaction(state: CombatState) -> CombatState:
    """Close an unresolved reaction window at its declared boundary."""
    if state.reaction_window is None:
        return state
    payload = state.to_dict()
    payload["reaction_window"] = None
    return _bump(state, **payload)


def apply_action_result(
    state: CombatState,
    application: ActionApplication,
    *,
    claim_token: str | None = None,
    expected_revision: int | None = None,
) -> CombatState:
    """Apply one already-resolved action to combatant instances atomically."""
    _require_revision(state, expected_revision)
    _require_active(state)
    _require_claim(state, application.actor_id, claim_token)
    if state.current != str(application.actor_id):
        raise TurnOwnershipError("only the current actor can apply an action")  # i18n-exempt: internal validation diagnostic
    if not str(application.action_id).strip():
        raise CombatError("action id must not be empty")  # i18n-exempt: internal validation diagnostic
    combatants = copy.deepcopy(dict(state.combatants))
    actor = combatants.get(str(application.actor_id))
    if actor is None:
        raise CombatError("action actor is not in combat")  # i18n-exempt: internal validation diagnostic
    current_budget = _budget_map(state.budget)
    costs = {str(key): int(value) for key, value in application.budget_cost.items()}
    for key, amount in costs.items():
        if amount < 0 or current_budget.get(key, 0) < amount:
            raise CombatError(f"budget {key!r} is insufficient")
    remaining = dict(current_budget)
    for key, amount in costs.items():
        remaining[key] = remaining.get(key, 0) - amount
    actor["budget"] = dict(remaining)
    for key, value in application.actor_updates.items():
        actor[str(key)] = copy.deepcopy(value)
    for target_id, updates in application.target_updates.items():
        target = combatants.get(str(target_id))
        if target is None:
            raise CombatError(f"action target {target_id!r} is not in combat")
        if not isinstance(updates, Mapping):
            raise CombatError("target updates must be mappings")
        for key, value in updates.items():
            target[str(key)] = copy.deepcopy(value)
    event = dict(application.event or {})
    event.setdefault("action_id", application.action_id)
    event.setdefault("actor", application.actor_id)
    event.setdefault("targets", list(application.target_updates))
    event.setdefault("round", state.round)
    event.setdefault("turn_index", state.turn_index)
    event.setdefault("visibility", "public")
    event["revision"] = state.revision + 1
    events = [dict(item) for item in state.events]
    events.append(event)
    payload = state.to_dict()
    payload.update(
        {
            "combatants": combatants,
            "budget": remaining,
            "claim": state.claim,
            "event_seq": state.event_seq + 1,
            "events": events[-256:],
        }
    )
    if application.reaction_window is not None:
        payload["reaction_window"] = copy.deepcopy(dict(application.reaction_window))
    return _bump(state, **payload)


def end_turn(
    state: CombatState,
    actor_id: str,
    *,
    claim_token: str | None = None,
    expected_revision: int | None = None,
) -> CombatState:
    """Release the current claim and reset the next actor's declared budget."""
    _require_revision(state, expected_revision)
    _require_active(state)
    _require_claim(state, actor_id, claim_token)
    if state.current != str(actor_id):
        raise TurnOwnershipError("only the current actor can end the turn")  # i18n-exempt: internal validation diagnostic
    combatants = copy.deepcopy(dict(state.combatants))
    from core.conditions import ConditionState, expire_conditions

    for combatant in combatants.values():
        structured = []
        for condition in combatant.get("conditions", []):
            if isinstance(condition, Mapping):
                try:
                    structured.append(ConditionState.from_dict(condition))
                except Exception:
                    continue
        combatant["conditions"] = [
            item.to_dict()
            for item in expire_conditions(structured, round=state.round, turn=state.turn_index, trigger="turn_end")
        ]
    next_start = (state.turn_index + 1) % len(state.order) if state.order else 0
    next_index, next_id = _next_eligible(state.order, combatants, start=next_start)
    payload = state.to_dict()
    payload["claim"] = None
    payload["reaction_window"] = None
    if next_id is None:
        payload.update({"phase": "ended", "current": None, "budget": {}, "turn_index": 0, "combatants": combatants})
    else:
        wrapped = bool(next_index <= state.turn_index)
        if wrapped:
            payload["round"] = state.round + 1
        next_budget = _budget_map(combatants[next_id].get("budget_max") or {})
        combatants[next_id]["budget"] = dict(next_budget)
        payload.update(
            {
                "current": next_id,
                "turn_index": next_index,
                "budget": next_budget,
                "combatants": combatants,
            }
        )
    return _bump(state, **payload)


def mark_combatant(state: CombatState, combatant_id: str, status: str, *, updates: Mapping[str, Any] | None = None) -> CombatState:
    """Set a generic encounter status such as defeated, stabilized, or escaped."""
    cid = str(combatant_id)
    if cid not in state.combatants:
        raise CombatError(f"unknown combatant {cid!r}")
    status_text = str(status).strip()
    if not status_text:
        raise CombatError("combatant status must not be empty")  # i18n-exempt: internal validation diagnostic
    combatants = copy.deepcopy(dict(state.combatants))
    combatants[cid]["state"] = status_text
    for key, value in (updates or {}).items():
        combatants[cid][str(key)] = copy.deepcopy(value)
    payload = state.to_dict()
    payload["combatants"] = combatants
    if cid == state.current and state.order and status_text in {"defeated", "dead", "escaped"} and state.claim is None:
        index, next_id = _next_eligible(state.order, combatants, start=(state.turn_index + 1) % len(state.order))
        if next_id is not None:
            payload.update({"current": next_id, "turn_index": index, "budget": dict(combatants[next_id].get("budget") or {})})
    return _bump(state, **payload)


def make_claim_token(state: CombatState, actor_id: str) -> str:
    """Generate a caller token without making it part of state-machine randomness."""
    return f"{state.id}:{state.revision}:{str(actor_id)}:{uuid.uuid4().hex}"


def project_combat(
    state: CombatState,
    *,
    keeper: bool = False,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Project combat once for a keeper, player, or knowledge-scoped actor.

    Keeper views retain mechanics; player/actor views contain only public
    identity, order, visible conditions, and an explicitly declared health
    presentation.  Callers must not filter raw ``combatants`` themselves.
    """
    projected: list[dict[str, Any]] = []
    for index, combatant_id in enumerate(state.order):
        combatant = state.combatants[combatant_id]
        own = actor_id is not None and str(actor_id) == combatant_id
        if keeper or own:
            entry = copy.deepcopy(dict(combatant))
        else:
            public = combatant.get("public")
            public = dict(public) if isinstance(public, Mapping) else {}
            entry = {
                "id": combatant_id,
                "name": str(combatant.get("name") or combatant_id),
                "initiative": int(combatant.get("initiative", 0) or 0),
                "position": index,
                "state": str(combatant.get("state") or "ready"),
                "conditions": [
                    copy.deepcopy(condition)
                    for condition in combatant.get("conditions", [])
                    if isinstance(condition, Mapping) and condition.get("visibility", "public") == "public"
                ],
            }
            if "health" in public:
                entry["health"] = copy.deepcopy(public["health"])
            if "health_presentation" in public:
                entry["health_presentation"] = copy.deepcopy(public["health_presentation"])
        entry.setdefault("position", index)
        projected.append(entry)
    visible_events = [
        copy.deepcopy(dict(event))
        for event in state.events
        if keeper or event.get("visibility", "public") == "public"
    ]
    return {
        "schema_version": state.schema_version,
        "id": state.id,
        "revision": state.revision,
        "phase": state.phase,
        "round": state.round,
        "turn_index": state.turn_index,
        "current": state.current,
        "budget": dict(state.budget),
        "reaction_window": copy.deepcopy(state.reaction_window),
        "order": list(state.order),
        "combatants": projected,
        "event_seq": state.event_seq,
        "events": visible_events,
    }


class CombatManager:
    """Persistence facade using room-state CAS; transition formulas stay pure."""

    state_key = "combat_state"

    def __init__(self, store: Any, room: str) -> None:
        self.store = store
        self.room = str(room)

    async def get(self) -> CombatState | None:
        raw = await self.store.state_get(self.room, self.state_key)
        if raw is None:
            return None
        try:
            return CombatState.from_dict(json.loads(raw))
        except (json.JSONDecodeError, CombatError) as exc:
            raise CombatError("stored combat state is invalid") from exc

    async def save(self, state: CombatState, *, expected_raw: str | None = None) -> bool:
        """Persist one state only if the caller's raw snapshot is still current."""
        if expected_raw is None:
            current = await self.store.state_get(self.room, self.state_key)
            expected_raw = current
        return await self.store.state_set_if_values(
            self.room,
            expected=[("combat_state", expected_raw)],
            updates=[("combat_state", state.json())],
        )

    async def transition(self, transform: Callable[[CombatState], CombatState]) -> CombatState:
        """Read, transform, and CAS once; callers retry stale transitions explicitly."""
        raw = await self.store.state_get(self.room, self.state_key)
        if raw is None:
            raise CombatError("combat state does not exist")
        state = CombatState.from_dict(json.loads(raw))
        updated = transform(state)
        if not await self.save(updated, expected_raw=raw):
            raise StaleCombatError("combat state changed during transition")
        return updated


ROOM_FACETS = (
    RoomStateFacet(
        name="combat_state",
        owner="core.combat",
        reset_scope="story",
        state_keys=frozenset({"combat_state"}),
        state_prefixes=frozenset({"action_result:"}),
        storages=frozenset({STORAGE_ROOM_STATE}),
    ),
)
