"""Deterministic runtime for a native scenario blueprint.

The native card contains authored, mostly static content.  This module owns the
small mutable part: which scene is active, what objectives/clues/actors have
changed, which ending was resolved, and which rewards were recorded.  Conditions
are deliberately a closed expression language; model prose is never allowed to
choose an ending or mutate this document.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from typing import Any, Mapping

from infra.room_facets import STORAGE_DOCUMENTS, RoomStateFacet

MODULE_RUNTIME_DOC_TYPE = "module_runtime"
MODULE_RUNTIME_ID = "module_runtime"
RUNTIME_SCHEMA_VERSION = 1
MAX_ACTIONS = 256
MAX_HISTORY = 128
MAX_REVEALED_CLUES = 256
MAX_REWARDS = 128
MAX_TEXT = 2_000

OBJECTIVE_STATUSES = frozenset({"pending", "active", "complete", "failed", "blocked"})
DEFAULT_ACTOR_STATUSES = frozenset(
    {"active", "safe", "injured", "unconscious", "dead", "missing", "asleep", "transformed", "rescued", "hostile", "surrendered"}
)
TRACKER_KINDS = frozenset({"number", "bool", "enum"})
ACTION_TYPES = frozenset(
    {
        "enter_scene",
        "reveal_clue",
        "update_objective",
        "update_actor_state",
        "set_tracker",
        "adjust_tracker",
        "resolve_ending",
        "grant_reward",
    }
)

_LOCKS: dict[str, asyncio.Lock] = {}
_COMPARE_RE = re.compile(r"^\s*([^\s]+)\s*(==|!=|>=|<=|>|<)\s*(.*?)\s*$")
_ID_RE = re.compile(r"[^a-z0-9_-]+")
_MISSING = object()


class ModuleRuntimeError(ValueError):
    """A deterministic module action was refused."""


def _lock(chat_key: str) -> asyncio.Lock:
    value = _LOCKS.get(chat_key)
    if value is None:
        value = asyncio.Lock()
        _LOCKS[chat_key] = value
    return value


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _id(value: Any, fallback: str) -> str:
    candidate = _text(value, 80).casefold()
    candidate = _ID_RE.sub("-", candidate).strip("-")
    if candidate:
        return candidate[:64]
    digest = hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:12]
    return f"entry-{digest}"


def _list(value: Any, limit: int = 128) -> list[Any]:
    return list(value[:limit]) if isinstance(value, list) else []


def _entities(raw: Any, *, fallback_collection: str = "entry") -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw[:512]):
        if not isinstance(item, Mapping):
            continue
        name = _text(item.get("name") or item.get("title"), 160)
        entity_id = _id(item.get("id"), f"{fallback_collection}:{index}:{name}")
        if entity_id in seen:
            continue
        seen.add(entity_id)
        result.append(dict(item) | {"id": entity_id, "name": name or entity_id})
    return result


def normalize_blueprint(raw: Any) -> dict[str, Any]:
    """Normalize only declarative runtime fields from a native card blueprint."""
    source = dict(raw) if isinstance(raw, Mapping) else {}
    result: dict[str, Any] = {
        "schema_version": 1,
        "start_scene": _text(source.get("start_scene"), 80),
        "scenes": _entities(source.get("scenes"), fallback_collection="scene"),
        "clues": _entities(source.get("clues"), fallback_collection="clue"),
        "objectives": _entities(source.get("objectives"), fallback_collection="objective"),
        "actors": _entities(source.get("actors"), fallback_collection="actor"),
        "trackers": _entities(source.get("trackers"), fallback_collection="tracker"),
        "endings": _entities(source.get("endings"), fallback_collection="ending"),
        "rewards": _entities(source.get("rewards"), fallback_collection="reward"),
        "edges": _list(source.get("edges")),
        "encounters": _list(source.get("encounters")),
        "handouts": _list(source.get("handouts")),
    }
    # A converter commonly extracts NPCs as `npcs`, while the runtime blueprint
    # uses `actors`. Keep the source's stable ids and public fields intact.
    if not result["actors"]:
        result["actors"] = _entities(source.get("npcs"), fallback_collection="actor")
    for actor in result["actors"]:
        statuses = actor.get("statuses")
        cleaned_statuses = [
            _text(value, 40)
            for value in statuses[:32]
            if _text(value, 40)
        ] if isinstance(statuses, list) else []
        actor["statuses"] = cleaned_statuses or list(DEFAULT_ACTOR_STATUSES)
        initial = _text(actor.get("initial_status") or actor.get("status"), 40) or "active"
        actor["initial_status"] = initial if initial in actor["statuses"] else actor["statuses"][0]
        actor["player_visible"] = bool(actor.get("player_visible", True))
    for objective in result["objectives"]:
        initial = _text(objective.get("initial_status") or objective.get("status"), 20) or "pending"
        objective["initial_status"] = initial if initial in OBJECTIVE_STATUSES else "pending"
        objective["player_visible"] = bool(objective.get("player_visible", True))
    for tracker in result["trackers"]:
        kind = _text(tracker.get("kind"), 20) or "number"
        tracker["kind"] = kind if kind in TRACKER_KINDS else "number"
        tracker["visibility"] = "keeper" if tracker.get("visibility") == "keeper" else "player"
        tracker["actor_id"] = _text(tracker.get("actor_id"), 64)
        try:
            tracker["minimum"] = int(tracker["minimum"])
        except (KeyError, TypeError, ValueError):
            tracker.pop("minimum", None)
        try:
            tracker["maximum"] = int(tracker["maximum"])
        except (KeyError, TypeError, ValueError):
            tracker.pop("maximum", None)
        if tracker["kind"] == "enum":
            options = tracker.get("options")
            tracker["options"] = [_text(value, 80) for value in options[:32] if _text(value, 80)] if isinstance(options, list) else []
            if not tracker["options"]:
                tracker["kind"] = "number"
    return result


def _find(blueprint: Mapping[str, Any], collection: str, entity_id: str) -> dict[str, Any] | None:
    wanted = _text(entity_id, 80).casefold()
    for item in blueprint.get(collection, []):
        if isinstance(item, Mapping) and str(item.get("id", "")).casefold() == wanted:
            return dict(item)
    return None


def _initial_tracker(spec: Mapping[str, Any]) -> Any:
    if spec.get("kind") == "bool":
        return bool(spec.get("default", False))
    if spec.get("kind") == "enum":
        options = spec.get("options")
        return spec.get("default") if spec.get("default") in options else options[0]
    try:
        value = int(spec.get("default", 0))
    except (TypeError, ValueError):
        value = 0
    if "minimum" in spec:
        value = max(value, int(spec["minimum"]))
    if "maximum" in spec:
        value = min(value, int(spec["maximum"]))
    return value


def _validate_tracker(spec: Mapping[str, Any], value: Any) -> Any:
    kind = spec.get("kind")
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if value in (0, 1, "0", "1", "true", "false"):
            return str(value).casefold() in {"1", "true"}
        raise ModuleRuntimeError("tracker value must be true or false")
    if kind == "enum":
        options = [str(item) for item in spec.get("options", [])]
        match = next((item for item in options if item.casefold() == str(value).casefold()), None)
        if match is None:
            raise ModuleRuntimeError("tracker value is not an allowed option")
        return match
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ModuleRuntimeError("tracker value must be a whole number") from None
    if spec.get("minimum") is not None:
        result = max(result, int(spec["minimum"]))
    if spec.get("maximum") is not None:
        result = min(result, int(spec["maximum"]))
    return result


def initial_runtime(blueprint: Any, module_id: str) -> dict[str, Any]:
    """Create a fresh runtime state from one imported native blueprint."""
    normalized = normalize_blueprint(blueprint)
    scenes = normalized["scenes"]
    start = normalized.get("start_scene") or (str(scenes[0]["id"]) if scenes else "")
    if start and not _find(normalized, "scenes", start):
        start = str(scenes[0]["id"]) if scenes else ""
    objectives = {
        str(item["id"]): {"status": item.get("initial_status", "pending"), "progress": 0}
        for item in normalized["objectives"]
    }
    actors = {
        str(item["id"]): {"status": item.get("initial_status", "active"), "trackers": {}}
        for item in normalized["actors"]
    }
    trackers = {str(item["id"]): _initial_tracker(item) for item in normalized["trackers"] if not item.get("actor_id")}
    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "module_id": _text(module_id, 160),
        "blueprint": normalized,
        "current_scene": start,
        "scene_history": [start] if start else [],
        "objectives": objectives,
        "actors": actors,
        "trackers": trackers,
        "revealed_clues": [],
        "ending": None,
        "rewards": [],
        "actions": [],
    }


def normalize_runtime(raw: Any, *, blueprint: Any = None, module_id: str = "") -> dict[str, Any]:
    source = dict(raw) if isinstance(raw, Mapping) else {}
    if isinstance(blueprint, Mapping):
        source["blueprint"] = blueprint
    normalized = initial_runtime(source.get("blueprint", {}), module_id or str(source.get("module_id") or ""))
    for key in ("current_scene", "scene_history", "objectives", "actors", "trackers", "revealed_clues", "ending", "rewards", "actions"):
        if key in source:
            normalized[key] = copy.deepcopy(source[key])
    normalized["module_id"] = _text(module_id or source.get("module_id"), 160)
    normalized["blueprint"] = normalize_blueprint(source.get("blueprint", {}))
    normalized["actions"] = _list(normalized.get("actions"), MAX_ACTIONS)
    return normalized


def _path_value(runtime: Mapping[str, Any], path: str) -> Any:
    bits = [bit for bit in str(path).split(".") if bit]
    if not bits:
        return _MISSING
    root: Any = runtime
    if bits[0] == "tracker":
        root = runtime.get("trackers", {})
        bits = bits[1:]
    elif bits[0] == "objective":
        root = runtime.get("objectives", {})
        bits = bits[1:]
    elif bits[0] == "actor":
        root = runtime.get("actors", {})
        bits = bits[1:]
    elif bits[0] == "scene":
        return runtime.get("current_scene", "") == (bits[1] if len(bits) > 1 else "")
    elif bits[0] == "clue":
        return bits[1] in {str(item.get("id")) for item in runtime.get("revealed_clues", []) if isinstance(item, Mapping)} if len(bits) > 1 else False
    for bit in bits:
        if isinstance(root, Mapping) and bit in root:
            root = root[bit]
        else:
            return _MISSING
    return root


def evaluate_condition(condition: Any, runtime: Mapping[str, Any]) -> bool:
    """Evaluate the closed condition format used by converted module entities."""
    if condition in (None, "", [], {}):
        return True
    if isinstance(condition, Mapping):
        if isinstance(condition.get("all"), list):
            return all(evaluate_condition(item, runtime) for item in condition["all"])
        if isinstance(condition.get("any"), list):
            return any(evaluate_condition(item, runtime) for item in condition["any"])
        if "not" in condition:
            return not evaluate_condition(condition["not"], runtime)
        if "clue" in condition:
            return evaluate_condition(f"clue.{condition['clue']}", runtime)
        if "scene" in condition:
            return str(runtime.get("current_scene", "")) == _text(condition["scene"], 80)
        path = condition.get("path") or condition.get("field")
        if path:
            actual = _path_value(runtime, str(path))
            op = str(condition.get("op") or "==")
            expected = condition.get("value")
            return _compare(actual, op, expected)
        for key, op in (("gte", ">="), ("lte", "<="), ("gt", ">"), ("lt", "<"), ("equals", "==")):
            if key in condition and isinstance(condition[key], Mapping):
                item = condition[key]
                return _compare(_path_value(runtime, str(item.get("path") or item.get("tracker") or "")), op, item.get("value"))
        return False
    if not isinstance(condition, str):
        return False
    text = condition.strip()
    if text.casefold().startswith("not "):
        return not evaluate_condition(text[4:], runtime)
    for connector, fn in ((" or ", any), (" and ", all)):
        if connector in text.casefold():
            parts = re.split(connector, text, flags=re.IGNORECASE)
            return fn(evaluate_condition(part, runtime) for part in parts)
    match = _COMPARE_RE.match(text)
    if match:
        left, op, raw_right = match.groups()
        expected: Any = raw_right.strip().strip("\"'")
        try:
            expected = int(expected)
        except ValueError:
            if expected.casefold() in {"true", "false"}:
                expected = expected.casefold() == "true"
        return _compare(_path_value(runtime, left), op, expected)
    return bool(_path_value(runtime, text) not in {_MISSING, None, False, "", 0})


def _compare(actual: Any, op: str, expected: Any) -> bool:
    if actual is _MISSING:
        return False
    try:
        if op == "==": return actual == expected or str(actual).casefold() == str(expected).casefold()
        if op == "!=": return not (actual == expected or str(actual).casefold() == str(expected).casefold())
        if op == ">=": return actual >= expected
        if op == "<=": return actual <= expected
        if op == ">": return actual > expected
        if op == "<": return actual < expected
    except TypeError:
        return False
    return False


def _audit_snapshot(runtime: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(runtime.get(key)) for key in ("current_scene", "objectives", "actors", "trackers", "revealed_clues", "ending", "rewards")}


def apply_action(raw: Any, action: Mapping[str, Any], *, actor: str = "keeper", reason: str = "", evidence: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one validated action and append an auditable before/after record."""
    runtime = normalize_runtime(raw)
    blueprint = runtime["blueprint"]
    kind = _text(action.get("action") or action.get("type"), 40)
    if kind not in ACTION_TYPES:
        raise ModuleRuntimeError("unknown module action")
    before = _audit_snapshot(runtime)

    if kind == "enter_scene":
        scene_id = _text(action.get("scene_id") or action.get("id"), 80)
        scene = _find(blueprint, "scenes", scene_id)
        if scene is None:
            raise ModuleRuntimeError("scene not found")
        if not evaluate_condition(action.get("condition") or scene.get("condition"), runtime):
            raise ModuleRuntimeError("scene condition is not satisfied")
        runtime["current_scene"] = str(scene["id"])
        if runtime["scene_history"][-1:] != [scene["id"]]:
            runtime["scene_history"] = [*runtime["scene_history"], scene["id"]][-MAX_HISTORY:]
    elif kind == "reveal_clue":
        clue_id = _text(action.get("clue_id") or action.get("id"), 80)
        clue = _find(blueprint, "clues", clue_id)
        if clue is None:
            raise ModuleRuntimeError("clue not found")
        if not evaluate_condition(action.get("condition") or clue.get("condition"), runtime):
            raise ModuleRuntimeError("clue condition is not satisfied")
        if not any(str(item.get("id")) == str(clue["id"]) for item in runtime["revealed_clues"] if isinstance(item, Mapping)):
            runtime["revealed_clues"] = [*runtime["revealed_clues"], {key: clue.get(key) for key in ("id", "name", "description", "summary", "image") if clue.get(key)}][-MAX_REVEALED_CLUES:]
    elif kind == "update_objective":
        objective_id = _text(action.get("objective_id") or action.get("id"), 80)
        objective = _find(blueprint, "objectives", objective_id)
        if objective is None:
            raise ModuleRuntimeError("objective not found")
        status = _text(action.get("status"), 20)
        if status not in OBJECTIVE_STATUSES:
            raise ModuleRuntimeError("invalid objective status")
        if status == "complete" and not evaluate_condition(objective.get("condition"), runtime):
            raise ModuleRuntimeError("objective condition is not satisfied")
        previous = runtime["objectives"].setdefault(str(objective["id"]), {"status": "pending", "progress": 0})
        old_status = str(previous.get("status", "pending"))
        if old_status in {"complete", "failed"} and status not in {old_status}:
            raise ModuleRuntimeError("finished objective cannot be reopened")
        previous["status"] = status
        if "progress" in action:
            try:
                previous["progress"] = max(0, min(100, int(action["progress"])))
            except (TypeError, ValueError):
                raise ModuleRuntimeError("objective progress must be a whole number") from None
        elif status == "complete":
            previous["progress"] = 100
    elif kind == "update_actor_state":
        actor_id = _text(action.get("actor_id") or action.get("id"), 80)
        definition = _find(blueprint, "actors", actor_id)
        if definition is None:
            raise ModuleRuntimeError("actor not found")
        status = _text(action.get("status"), 40)
        if status not in set(definition.get("statuses") or DEFAULT_ACTOR_STATUSES):
            raise ModuleRuntimeError("invalid actor status")
        runtime["actors"].setdefault(str(definition["id"]), {"status": "active", "trackers": {}})["status"] = status
    elif kind in {"set_tracker", "adjust_tracker"}:
        tracker_id = _text(action.get("tracker_id") or action.get("id"), 80)
        definition = _find(blueprint, "trackers", tracker_id)
        if definition is None:
            raise ModuleRuntimeError("tracker not found")
        actor_id = _text(action.get("actor_id"), 80)
        owner = _text(definition.get("actor_id"), 80)
        if owner and actor_id != owner:
            raise ModuleRuntimeError("tracker belongs to another actor")
        if owner and not _find(blueprint, "actors", actor_id):
            raise ModuleRuntimeError("tracker actor not found")
        bucket = runtime["actors"].setdefault(actor_id, {"status": "active", "trackers": {}})["trackers"] if owner else runtime["trackers"]
        current = bucket.get(str(definition["id"]), _initial_tracker(definition))
        value = action.get("value")
        if kind == "adjust_tracker":
            if definition.get("kind") != "number":
                raise ModuleRuntimeError("only number trackers can be adjusted")
            try:
                value = int(current) + int(action.get("delta"))
            except (TypeError, ValueError):
                raise ModuleRuntimeError("tracker delta must be a whole number") from None
        bucket[str(definition["id"])] = _validate_tracker(definition, value)
    elif kind == "resolve_ending":
        ending_id = _text(action.get("ending_id") or action.get("id"), 80)
        ending = _find(blueprint, "endings", ending_id)
        if ending is None:
            raise ModuleRuntimeError("ending not found")
        if runtime.get("ending") is not None:
            raise ModuleRuntimeError("an ending has already been resolved")
        if not evaluate_condition(action.get("condition") or ending.get("condition"), runtime):
            raise ModuleRuntimeError("ending condition is not satisfied")
        for objective_id in ending.get("required_objectives", []):
            if runtime["objectives"].get(str(objective_id), {}).get("status") != "complete":
                raise ModuleRuntimeError("ending objectives are not complete")
        for clue_id in ending.get("required_clues", []):
            if not any(str(item.get("id")) == str(clue_id) for item in runtime["revealed_clues"] if isinstance(item, Mapping)):
                raise ModuleRuntimeError("ending clues are not revealed")
        required_actors = ending.get("required_actors", [])
        if isinstance(required_actors, list):
            for required in required_actors:
                if isinstance(required, Mapping):
                    actor_id = str(required.get("id") or required.get("actor_id") or "")
                    expected_status = str(required.get("status") or "")
                else:
                    actor_id = str(required)
                    expected_status = ""
                if not actor_id or actor_id not in runtime["actors"] or (expected_status and runtime["actors"][actor_id].get("status") != expected_status):
                    raise ModuleRuntimeError("ending actor conditions are not satisfied")
        runtime["ending"] = {"id": ending["id"], "name": ending.get("name", ending["id"])}
    elif kind == "grant_reward":
        reward_id = _text(action.get("reward_id") or action.get("id"), 80)
        reward = _find(blueprint, "rewards", reward_id)
        if reward is None:
            raise ModuleRuntimeError("reward not found")
        if runtime.get("ending") is None and not bool(reward.get("available_before_ending")):
            raise ModuleRuntimeError("resolve an ending before granting this reward")
        if not evaluate_condition(reward.get("condition"), runtime):
            raise ModuleRuntimeError("reward condition is not satisfied")
        if any(str(item.get("reward_id")) == str(reward["id"]) and str(item.get("recipient")) == _text(action.get("recipient"), 120) for item in runtime["rewards"] if isinstance(item, Mapping)):
            raise ModuleRuntimeError("reward already granted to this recipient")
        try:
            quantity = max(1, int(action.get("quantity", 1)))
        except (TypeError, ValueError):
            raise ModuleRuntimeError("reward quantity must be a whole number") from None
        runtime["rewards"] = [*runtime["rewards"], {"reward_id": reward["id"], "name": reward.get("name", reward["id"]), "recipient": _text(action.get("recipient"), 120), "quantity": quantity, "description": reward.get("description", "")}][-MAX_REWARDS:]

    after = _audit_snapshot(runtime)
    if before == after:
        raise ModuleRuntimeError("module action made no change")
    sequence = len(runtime["actions"]) + 1
    audit = {"sequence": sequence, "action": kind, "actor": _text(actor, 120) or "keeper", "reason": _text(reason, 500), "evidence": _text(evidence, 1_000), "before": before, "after": after}
    runtime["actions"] = [*runtime["actions"], audit][-MAX_ACTIONS:]
    return runtime, audit


def project_runtime(runtime: Any, *, keeper: bool = False) -> dict[str, Any] | None:
    """Build a player-safe view; the full blueprint and audit trail are keeper-only."""
    if not isinstance(runtime, Mapping):
        return None
    value = normalize_runtime(runtime)
    blueprint = value["blueprint"]
    if keeper:
        return value
    scene = _find(blueprint, "scenes", value.get("current_scene", ""))
    objectives = []
    for definition in blueprint["objectives"]:
        if definition.get("player_visible", True):
            current = value["objectives"].get(str(definition["id"]), {"status": "pending", "progress": 0})
            objectives.append({"id": definition["id"], "name": definition.get("name", definition["id"]), "status": current.get("status", "pending"), "progress": current.get("progress", 0)})
    actors = []
    for definition in blueprint["actors"]:
        if definition.get("player_visible", True):
            current = value["actors"].get(str(definition["id"]), {"status": definition.get("initial_status", "active")})
            actors.append({"id": definition["id"], "name": definition.get("name", definition["id"]), "status": current.get("status", "active")})
    trackers = []
    for definition in blueprint["trackers"]:
        if definition.get("visibility") == "player":
            actor_id = str(definition.get("actor_id") or "")
            bucket = value["actors"].get(actor_id, {}).get("trackers", {}) if actor_id else value["trackers"]
            entry = {"id": definition["id"], "name": definition.get("name", definition["id"]), "value": bucket.get(str(definition["id"]), _initial_tracker(definition))}
            if actor_id:
                actor = _find(blueprint, "actors", actor_id)
                if actor:
                    entry["actor"] = actor.get("name", actor_id)
            trackers.append(entry)
    return {
        "module_id": value.get("module_id", ""),
        "scene": {key: scene.get(key) for key in ("id", "name", "description", "summary", "image") if scene and scene.get(key)},
        "objectives": objectives,
        "actors": actors,
        "trackers": trackers,
        "clues": copy.deepcopy(value.get("revealed_clues", [])),
        "ending": copy.deepcopy(value.get("ending")),
        "rewards": [{key: item.get(key) for key in ("name", "recipient", "quantity", "description") if item.get(key)} for item in value.get("rewards", []) if isinstance(item, Mapping)],
    }


def project_document(doc: Any, viewer: Any) -> dict[str, Any] | None:
    """Document-layer adapter that keeps the projection chokepoint intact."""
    return project_runtime(getattr(doc, "data", None), keeper=bool(getattr(viewer, "is_keeper", False)))


async def install_runtime(documents: Any, chat_key: str, blueprint: Any, module_id: str, *, source: str = "", preserve: bool = True) -> dict[str, Any]:
    """Install a module blueprint, preserving progress only for the same module."""
    async with _lock(chat_key):
        existing = await documents.get_singleton(chat_key, MODULE_RUNTIME_DOC_TYPE)
        if preserve and existing is not None and existing.data.get("module_id") == module_id:
            runtime = normalize_runtime(existing.data, blueprint=blueprint, module_id=module_id)
        else:
            runtime = initial_runtime(blueprint, module_id)
        await documents.put_singleton(chat_key, MODULE_RUNTIME_DOC_TYPE, runtime, source=source)
        return runtime


async def apply_runtime_action(documents: Any, chat_key: str, action: Mapping[str, Any], *, actor: str = "keeper", reason: str = "", evidence: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """The one persistence lane for every module-runtime mutation."""
    async with _lock(chat_key):
        existing = await documents.get_singleton(chat_key, MODULE_RUNTIME_DOC_TYPE)
        if existing is None:
            raise ModuleRuntimeError("no native module runtime is installed")
        runtime, audit = apply_action(existing.data, action, actor=actor, reason=reason, evidence=evidence)
        await documents.put_singleton(chat_key, MODULE_RUNTIME_DOC_TYPE, runtime, source=existing.source)
        return runtime, audit


ROOM_FACETS = (
    RoomStateFacet(
        name="module_runtime",
        owner="core.module_runtime",
        reset_scope="all",
        doc_types=frozenset({MODULE_RUNTIME_DOC_TYPE}),
        storages=frozenset({STORAGE_DOCUMENTS}),
    ),
)
