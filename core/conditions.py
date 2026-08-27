"""Structured conditions and deterministic duration handling."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.runtime import ConditionSpec, EffectSpec

MAX_ACTIVE_CONDITIONS = 64


class ConditionError(ValueError):
    """A condition state cannot be created or transitioned."""


@dataclass(frozen=True)
class ConditionState:
    """One applied condition instance, including provenance and timing."""

    id: str
    source: str
    target: str
    start_round: int
    start_turn: int
    duration: Any = None
    end_trigger: Any = None
    stacks: int = 1
    visibility: str = "public"
    effects: tuple[EffectSpec, ...] = ()
    display: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ConditionState:
        if not isinstance(raw, Mapping):
            raise ConditionError("condition state must be a mapping")  # i18n-exempt: internal validation diagnostic
        identifier = str(raw.get("id") or "").strip()
        if not identifier:
            raise ConditionError("condition id must not be empty")  # i18n-exempt: internal validation diagnostic
        stacks = raw.get("stacks", 1)
        if isinstance(stacks, bool) or not isinstance(stacks, int) or stacks < 1:
            raise ConditionError("condition stacks must be a positive integer")  # i18n-exempt: internal validation diagnostic
        visibility = str(raw.get("visibility") or "public")
        if visibility not in {"public", "keeper", "private"}:
            raise ConditionError("condition visibility is invalid")
        return cls(
            id=identifier,
            source=str(raw.get("source") or ""),
            target=str(raw.get("target") or ""),
            start_round=max(0, int(raw.get("start_round", 0) or 0)),
            start_turn=max(0, int(raw.get("start_turn", 0) or 0)),
            duration=copy.deepcopy(raw.get("duration")),
            end_trigger=copy.deepcopy(raw.get("end_trigger", raw.get("end"))),
            stacks=stacks,
            visibility=visibility,
            effects=tuple(),
            display=copy.deepcopy(dict(raw.get("display") or {})),
        )

    @classmethod
    def from_spec(
        cls,
        spec: ConditionSpec,
        *,
        source: str,
        target: str,
        round: int,
        turn: int,
        duration: Any = None,
        stacks: int = 1,
    ) -> ConditionState:
        actual_duration = spec.duration if duration is None else duration
        if isinstance(stacks, bool) or stacks < 1:
            raise ConditionError("condition stacks must be a positive integer")  # i18n-exempt: internal validation diagnostic
        if spec.max_stacks is not None:
            stacks = min(stacks, spec.max_stacks)
        return cls(
            id=spec.id,
            source=str(source),
            target=str(target),
            start_round=max(0, int(round)),
            start_turn=max(0, int(turn)),
            duration=copy.deepcopy(actual_duration),
            end_trigger=copy.deepcopy(spec.end),
            stacks=int(stacks),
            visibility=spec.visibility,
            effects=spec.effects,
            display=copy.deepcopy(dict(spec.display)),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "start_round": self.start_round,
            "start_turn": self.start_turn,
            "stacks": self.stacks,
            "visibility": self.visibility,
        }
        if self.duration is not None:
            result["duration"] = copy.deepcopy(self.duration)
        if self.end_trigger is not None:
            result["end_trigger"] = copy.deepcopy(self.end_trigger)
        if self.effects:
            result["effects"] = [effect.to_dict() for effect in self.effects]
        if self.display:
            result["display"] = copy.deepcopy(dict(self.display))
        return result


def _same_identity(left: ConditionState, right: ConditionState) -> bool:
    return left.id == right.id and left.target == right.target


def add_condition(
    conditions: Iterable[ConditionState],
    condition: ConditionState,
    *,
    stacking: str = "replace",
    max_stacks: int | None = None,
) -> tuple[ConditionState, ...]:
    """Apply one condition according to its declared stacking policy."""
    current = list(conditions)
    match_index = next((index for index, item in enumerate(current) if _same_identity(item, condition)), None)
    if match_index is None:
        if len(current) >= MAX_ACTIVE_CONDITIONS:
            raise ConditionError("condition limit reached")
        current.append(condition)
        return tuple(current)
    old = current[match_index]
    if stacking == "ignore":
        return tuple(current)
    if stacking == "refresh":
        current[match_index] = ConditionState(
            id=old.id,
            source=condition.source,
            target=old.target,
            start_round=condition.start_round,
            start_turn=condition.start_turn,
            duration=condition.duration,
            end_trigger=condition.end_trigger,
            stacks=old.stacks,
            visibility=condition.visibility,
            effects=condition.effects,
            display=condition.display,
        )
        return tuple(current)
    if stacking == "stack":
        stacks = old.stacks + condition.stacks
        if max_stacks is not None:
            stacks = min(stacks, max_stacks)
        current[match_index] = ConditionState(
            id=old.id,
            source=old.source,
            target=old.target,
            start_round=old.start_round,
            start_turn=old.start_turn,
            duration=old.duration,
            end_trigger=old.end_trigger,
            stacks=stacks,
            visibility=old.visibility,
            effects=old.effects,
            display=old.display,
        )
        return tuple(current)
    if stacking != "replace":
        raise ConditionError(f"unknown condition stacking policy {stacking!r}")
    current[match_index] = condition
    return tuple(current)


def remove_condition(
    conditions: Iterable[ConditionState],
    condition_id: str,
    *,
    target: str | None = None,
) -> tuple[ConditionState, ...]:
    """Remove matching condition instances without touching other targets."""
    identifier = str(condition_id)
    return tuple(
        item
        for item in conditions
        if not (item.id == identifier and (target is None or item.target == str(target)))
    )


def _duration_rounds(duration: Any) -> int | None:
    if isinstance(duration, bool):
        return None
    if isinstance(duration, int):
        return max(0, duration)
    if isinstance(duration, Mapping):
        value = duration.get("rounds")
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    return None


def expire_conditions(
    conditions: Iterable[ConditionState],
    *,
    round: int,
    turn: int,
    trigger: str | None = None,
) -> tuple[ConditionState, ...]:
    """Drop instances whose declared duration or end trigger has elapsed."""
    result: list[ConditionState] = []
    for item in conditions:
        if trigger is not None and item.end_trigger is not None and str(item.end_trigger) == str(trigger):
            continue
        duration = _duration_rounds(item.duration)
        if duration is not None and round - item.start_round >= duration:
            continue
        result.append(item)
    return tuple(result)


def project_conditions(
    conditions: Iterable[ConditionState],
    *,
    keeper: bool = False,
    actor_id: str | None = None,
) -> list[dict[str, Any]]:
    """Project condition state without exposing private conditions."""
    result: list[dict[str, Any]] = []
    for condition in conditions:
        if not keeper and condition.visibility != "public" and condition.target != str(actor_id or ""):
            continue
        result.append(condition.to_dict())
    return result


def custom_condition(text: str, *, target: str = "", source: str = "") -> ConditionState:
    """Preserve unknown imported status text as display-only custom data."""
    label = str(text).strip()
    if not label:
        raise ConditionError("custom condition text must not be empty")  # i18n-exempt: internal validation diagnostic
    return ConditionState(
        id="custom",
        source=str(source),
        target=str(target),
        start_round=0,
        start_turn=0,
        visibility="public",
        display={"text": label, "mechanical": False},
    )
