"""Source-aware intermediate representation for native scenario conversion.

The IR is the boundary between semantic extraction and Loreweaver's native
content. The model may propose its fields, but this module owns caps, stable
IDs, provenance requirements and the conversion report. It deliberately does
not execute conditions or write room state.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from agent.scenario_bundle import ScenarioBundle
from core.pack import BuiltPack, build_pack


MAX_IR_BYTES = 4 * 1024 * 1024
MAX_ENTITIES = 512
MAX_ENTITY_TEXT = 16_000
MAX_SOURCE_QUOTE = 2_000
MAX_SOURCE_FILES = 8
MAX_SOURCE_PAGES = 32
MAX_ASSET_REFS = 128
MAX_WARNINGS = 256

SUPPORT_STATES = frozenset({"native", "keeper_judgment", "manual", "blocked"})
ENTITY_COLLECTIONS = (
    "scenes",
    "npcs",
    "clues",
    "items",
    "threats",
    "timeline",
    "objectives",
    "trackers",
    "endings",
    "rewards",
    "rules",
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ASSET_PRIORITIES = frozenset({"essential", "useful", "decorative", "unknown"})


class ScenarioIRError(ValueError):
    """The extracted scenario IR is structurally unusable."""


@dataclass(frozen=True)
class ScenarioIR:
    data: dict[str, Any]
    warnings: tuple[str, ...] = ()
    incomplete: bool = False

    @property
    def module(self) -> dict[str, Any]:
        value = self.data.get("module")
        return value if isinstance(value, dict) else {}

    @property
    def blocked_rules(self) -> tuple[dict[str, Any], ...]:
        rules = self.data.get("rules")
        if not isinstance(rules, list):
            return ()
        return tuple(rule for rule in rules if isinstance(rule, dict) and rule.get("support") == "blocked")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "loreweaver.scenario-ir",
            "format_version": 1,
            **self.data,
            "warnings": list(self.warnings),
            "incomplete": self.incomplete,
        }


@dataclass(frozen=True)
class NativeCardCompileReport:
    card: dict[str, Any]
    warnings: tuple[str, ...] = ()
    blocked_rules: tuple[str, ...] = ()
    selected_assets: tuple[str, ...] = ()

    @property
    def content_complete(self) -> bool:
        return not self.warnings

    @property
    def mechanics_complete(self) -> bool:
        return not self.blocked_rules


@dataclass(frozen=True)
class NativePackCompileReport:
    """A built native pack plus the content/mechanic report that produced it."""

    built: BuiltPack
    card_report: NativeCardCompileReport
    source_root: Path


def compile_native_pack(
    ir: ScenarioIR,
    bundle: ScenarioBundle,
    *,
    source_bytes: bytes | None = None,
    output_path: Path | None = None,
    pack_id: str = "",
    license_name: str = "",
) -> NativePackCompileReport:
    """Write and build a content-native pack from a normalized IR.

    The caller supplies an isolated temporary/source directory and owns the
    resulting ``source_root`` lifecycle. This function never installs the pack
    into a room. Unknown/decorative assets remain in the original source
    bundle; only selected assets enter the playable pack.
    """
    card_report = compile_native_card(ir)
    module = ir.module
    name = _text(module.get("name"), 160) or "Untitled Scenario"
    pack_id = pack_id or _pack_id(module, bundle)
    source_root = Path(tempfile.mkdtemp(prefix=f"{pack_id}-pack-src-"))
    try:
        cards_dir = source_root / "cards"
        assets_dir = source_root / "assets"
        cards_dir.mkdir(parents=True)
        assets_dir.mkdir(parents=True)
        card_path = cards_dir / f"{pack_id}.lorecard.json"
        card_path.write_text(json.dumps(card_report.card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        asset_paths: list[str] = []
        for asset in bundle.selected_assets:
            relative = f"assets/{asset.output_name}"
            (source_root / relative).write_bytes(asset.data)
            asset_paths.append(relative)
        source_path = "assets/source-bundle.zip"
        if source_bytes:
            (source_root / source_path).write_bytes(source_bytes)
            asset_paths.append(source_path)
        metadata_path = "assets/conversion-bundle.json"
        (source_root / metadata_path).write_text(
            json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        asset_paths.append(metadata_path)

        provenance = ir.data.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance = {}
        license_value = license_name or _text(provenance.get("license"), 300)
        if not license_value:
            license_value = "UNVERIFIED - author license review required"
        name_en = _text(module.get("name_en"), 160) or name
        description = _text(module.get("description"), 2_000) or "Converted native Loreweaver scenario."
        manifest = {
            "id": pack_id,
            "version": "0.1.0",
            "authors": _string_list(provenance.get("authors"), 16, 160) or ["Scenario converter"],
            "license": license_value,
            "name": {"en": name_en, "zh": name},
            "description": {"en": description, "zh": description},
            "contents": {"cards": [f"cards/{pack_id}.lorecard.json"]},
            "assets": [{"path": path} for path in asset_paths],
        }
        (source_root / "pack.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        built = build_pack(source_root, out_path=output_path)
        return NativePackCompileReport(built=built, card_report=card_report, source_root=source_root)
    except Exception:
        # A failed build must not leave a partially assembled source tree for a
        # caller that only knows about the returned report.
        import shutil

        shutil.rmtree(source_root, ignore_errors=True)
        raise


def normalize_scenario_ir(raw: Any, bundle: ScenarioBundle | None = None) -> ScenarioIR:
    """Normalize a model-produced IR without inventing missing facts.

    Missing provenance is retained as a warning and makes the result incomplete.
    The caller may show the draft to an author, but should not mark it as a
    complete conversion until those warnings are resolved.
    """
    if not isinstance(raw, Mapping):
        raise ScenarioIRError("scenario IR must be a JSON object")
    encoded = json.dumps(raw, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_IR_BYTES:
        raise ScenarioIRError(f"scenario IR exceeds {MAX_IR_BYTES} bytes")

    warnings: list[str] = []
    data: dict[str, Any] = {
        "module": _normalize_module(raw.get("module"), raw, warnings),
        "scenes": [],
        "npcs": [],
        "clues": [],
        "items": [],
        "threats": [],
        "timeline": [],
        "objectives": [],
        "trackers": [],
        "endings": [],
        "rewards": [],
        "rules": [],
        "assets": [],
        "conflicts": _string_list(raw.get("conflicts"), 64, 500),
    }
    for conflict in data["conflicts"]:
        warnings.append(f"unresolved conflict: {conflict}")

    seen_ids: set[str] = set()
    for collection in ENTITY_COLLECTIONS:
        raw_entries = raw.get(collection)
        if raw_entries is None:
            continue
        if not isinstance(raw_entries, list):
            warnings.append(f"{collection}: ignored because it is not a list")
            continue
        for index, entry in enumerate(raw_entries[:MAX_ENTITIES]):
            normalized = _normalize_entity(collection, entry, index, seen_ids, warnings)
            if normalized is not None:
                data[collection].append(normalized)
        if len(raw_entries) > MAX_ENTITIES:
            warnings.append(f"{collection}: truncated to {MAX_ENTITIES} entries")

    data["assets"] = _normalize_assets(raw.get("assets"), bundle, warnings)
    for warning in _string_list(raw.get("warnings"), MAX_WARNINGS, 1_000):
        warnings.append(warning)
    warnings = _dedupe(warnings)[:MAX_WARNINGS]
    return ScenarioIR(data=data, warnings=tuple(warnings), incomplete=bool(warnings))


def compile_native_card(ir: ScenarioIR, *, selected_asset_names: Mapping[str, str] | None = None) -> NativeCardCompileReport:
    """Compile content-native fields into the existing Loreweaver card shape.

    The ``module`` block is included as a versioned blueprint for the next
    runtime layer, while prompt-facing material is emitted as ordinary
    worldbook entries. Until the runtime consumes that blueprint, blocked
    rules remain visible in the report and are never advertised as active.
    """
    module = ir.module
    module_name = _text(module.get("name"), 160) or "Untitled Scenario"
    module_blueprint = dict(module)
    for collection in ("scenes", "npcs", "clues", "items", "threats", "timeline", "objectives", "trackers", "endings", "rewards", "rules"):
        module_blueprint[collection] = ir.data.get(collection, [])
    card: dict[str, Any] = {
        "format": "loreweaver.card",
        "format_version": 1,
        "name": module_name,
        "description": _text(module.get("description"), 2_000),
        "scenario": _text(module.get("scenario"), 4_000),
        "opening": _text(module.get("opening"), 8_000),
        "alternate_openings": _string_list(module.get("alternate_openings"), 8, 8_000),
        "tags": _string_list(module.get("tags"), 32, 80),
        "visual_world": _text(module.get("visual_world"), 1_200),
        "author_notes": _text(module.get("author_notes"), 4_000),
        "system": _text(module.get("system"), 80),
        "worldbook": [],
        "variables": _normalize_variables(module.get("variables"), ir.warnings),
        "items": _normalize_native_items(ir.data.get("items")),
        "pregens": _normalize_pregens(module.get("pregens")),
        # This is the versioned declarative blueprint consumed by the deterministic
        # module-runtime importer; it contains no executable code.
        "module": _public_module_blueprint(module_blueprint),
        "provenance": _normalize_provenance(module.get("provenance")),
    }

    for collection in ("scenes", "npcs", "clues", "threats", "timeline", "objectives", "endings", "rewards"):
        for entry in ir.data.get(collection, []):
            _append_worldbook_entries(card["worldbook"], collection, entry)

    blocked = tuple(
        str(rule.get("id") or rule.get("summary") or "rule")
        for rule in ir.data.get("rules", [])
        if isinstance(rule, dict) and rule.get("support") == "blocked"
    )
    selected = tuple(
        str(asset.get("output_name") or asset.get("path"))
        for asset in ir.data.get("assets", [])
        if isinstance(asset, dict) and asset.get("priority") in {"essential", "useful"}
    )
    warnings = _dedupe([*ir.warnings, *[f"blocked rule: {item}" for item in blocked]])
    return NativeCardCompileReport(
        card=card,
        warnings=tuple(warnings),
        blocked_rules=blocked,
        selected_assets=selected,
    )


def _normalize_module(raw: Any, root: Mapping[str, Any], warnings: list[str]) -> dict[str, Any]:
    value = dict(raw) if isinstance(raw, Mapping) else {}
    if not value:
        value = {key: root.get(key) for key in ("name", "description", "scenario", "opening", "tags", "system", "visual_world") if key in root}
    result = {
        "name": _text(value.get("name") or value.get("title"), 160),
        "name_en": _text(value.get("name_en"), 160),
        "description": _text(value.get("description"), 2_000),
        "scenario": _text(value.get("scenario"), 4_000),
        "opening": _text(value.get("opening"), 8_000),
        "alternate_openings": _string_list(value.get("alternate_openings"), 8, 8_000),
        "tags": _string_list(value.get("tags"), 32, 80),
        "visual_world": _text(value.get("visual_world"), 1_200),
        "author_notes": _text(value.get("author_notes"), 4_000),
        "system": _text(value.get("system"), 80),
        "start_scene": _text(value.get("start_scene"), 80),
        "variables": value.get("variables") if isinstance(value.get("variables"), list) else [],
        "pregens": value.get("pregens") if isinstance(value.get("pregens"), list) else [],
        "provenance": value.get("provenance") if isinstance(value.get("provenance"), Mapping) else root.get("provenance", {}),
    }
    if not result["name"]:
        warnings.append("module: missing title")
    if not result["system"]:
        warnings.append("module: missing rules system")
    return result


def _normalize_entity(collection: str, raw: Any, index: int, seen_ids: set[str], warnings: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        warnings.append(f"{collection}[{index}]: ignored because it is not an object")
        return None
    name = _text(raw.get("name") or raw.get("title"), 160)
    if not name:
        warnings.append(f"{collection}[{index}]: ignored because it has no name")
        return None
    entity_id = _stable_id(collection.rstrip("s"), raw.get("id"), name)
    if entity_id in seen_ids:
        suffix = hashlib.sha256(f"{collection}:{index}:{name}".encode("utf-8")).hexdigest()[:8]
        entity_id = f"{entity_id[:54]}-{suffix}"
        warnings.append(f"{collection}[{index}]: duplicate ID renamed to {entity_id}")
    seen_ids.add(entity_id)
    result: dict[str, Any] = {
        "id": entity_id,
        "name": name,
        "aliases": _string_list(raw.get("aliases"), 8, 80),
        "description": _text(raw.get("description") or raw.get("summary"), MAX_ENTITY_TEXT),
        "keeper_notes": _text(raw.get("keeper_notes") or raw.get("secret"), MAX_ENTITY_TEXT),
        "role": _text(raw.get("role") or raw.get("type"), 160),
        "location": _text(raw.get("location"), 160),
        "npcs_present": _string_list(raw.get("npcs_present"), 32, 160),
        "clues": _string_list(raw.get("clues"), 32, 160),
        "leads_to": _string_list(raw.get("leads_to"), 32, 160),
        "discovery_method": _text(raw.get("discovery_method"), 2_000),
        "condition": _text(raw.get("condition") or raw.get("when"), 500),
        "summary": _text(raw.get("summary"), 2_000),
        "stats": _bounded_mapping(raw.get("stats"), 64, 120),
        "attacks": _string_list(raw.get("attacks"), 16, 300),
        "special_abilities": _text(raw.get("special_abilities"), 4_000),
        "san_loss": _text(raw.get("san_loss"), 120),
        "involved": _string_list(raw.get("involved"), 32, 160),
        "source_pages": _pages(raw.get("source_pages")),
        "source_files": _string_list(raw.get("source_files"), MAX_SOURCE_FILES, 260),
        "source_quote": _text(raw.get("source_quote"), MAX_SOURCE_QUOTE),
        "confidence": _confidence(raw.get("confidence")),
        "support": _support(raw.get("support"), collection, warnings, entity_id),
        "kind": _text(raw.get("kind"), 40),
        "visibility": _text(raw.get("visibility"), 20),
        "actor_id": _text(raw.get("actor_id"), 80),
        "initial_status": _text(raw.get("initial_status"), 40),
        "statuses": _string_list(raw.get("statuses"), 32, 40),
        "player_visible": bool(raw.get("player_visible", True)),
        "default": raw.get("default"),
        "options": _string_list(raw.get("options"), 32, 80),
        "required_objectives": _string_list(raw.get("required_objectives"), 64, 80),
        "required_clues": _string_list(raw.get("required_clues"), 64, 80),
        "required_actors": _list_of_bounded(raw.get("required_actors"), 64),
    }
    if "minimum" in raw:
        result["minimum"] = _int(raw.get("minimum"), -1_000_000, 1_000_000)
    if "maximum" in raw:
        result["maximum"] = _int(raw.get("maximum"), -1_000_000, 1_000_000)
    if not result["source_pages"] and not result["source_files"] and not result["source_quote"]:
        warnings.append(f"{collection}:{entity_id}: missing source provenance")
    if collection == "rules" and not result["summary"]:
        result["summary"] = result["description"]
    return result


def _append_worldbook_entries(target: list[dict[str, Any]], collection: str, entry: dict[str, Any]) -> None:
    category = {
        "scenes": "lore",
        "npcs": "npc",
        "clues": "clue",
        "threats": "secret",
        "timeline": "secret",
        "objectives": "secret",
        "endings": "secret",
        "rewards": "secret",
    }.get(collection, "lore")
    keys = [entry["name"], *entry.get("aliases", [])]
    public_parts = [entry.get("description") or entry.get("summary")]
    if collection == "clues" and entry.get("discovery_method"):
        public_parts.append(f"Discovery method: {entry['discovery_method']}")
    public = "\n\n".join(str(value).strip() for value in public_parts if str(value or "").strip())
    if public:
        target.append(
            {
                "id": f"{entry['id']}-public",
                "title": entry["name"],
                "content": public,
                "keys": keys[:32],
                "aliases": entry.get("aliases", []),
                "category": category,
                "secret": False,
            }
        )
    private_parts = [entry.get("keeper_notes"), entry.get("condition"), entry.get("stats"), entry.get("attacks"), entry.get("special_abilities"), entry.get("san_loss")]
    private = "\n\n".join(
        json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value).strip()
        for value in private_parts
        if value not in (None, "", [], {})
    )
    if private:
        target.append(
            {
                "id": f"{entry['id']}-keeper",
                "title": f"{entry['name']} — Keeper",
                "content": private,
                "keys": keys[:32],
                "category": "secret",
                "secret": True,
            }
        )


def _normalize_variables(raw: Any, inherited_warnings: tuple[str, ...]) -> list[dict[str, Any]]:
    # Native variable validation remains authoritative at card parse/import time. Here we only
    # avoid copying arbitrary large objects into a draft card.
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw[:64] if isinstance(item, Mapping)]


def _normalize_native_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in raw[:32]:
        if not isinstance(entry, Mapping) or not _text(entry.get("name"), 60):
            continue
        item = dict(entry)
        item["name"] = _text(entry.get("name"), 60)
        item["description"] = _text(entry.get("description"), 2_000)
        item["origin"] = _text(entry.get("origin"), 200)
        item["original_holder"] = _text(entry.get("original_holder"), 100)
        item["reveals"] = _string_list(entry.get("reveals"), 16, 160)
        items.append(item)
    return items


def _normalize_pregens(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [dict(entry) for entry in raw[:8] if isinstance(entry, Mapping) and _text(entry.get("name"), 60)]


def _normalize_assets(raw: Any, bundle: ScenarioBundle | None, warnings: list[str]) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        values = raw[:MAX_ASSET_REFS]
    elif bundle is not None:
        values = [asset.to_dict() for asset in bundle.assets[:MAX_ASSET_REFS]]
    else:
        values = []
    output: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(values):
        if not isinstance(raw_entry, Mapping):
            warnings.append(f"assets[{index}]: ignored because it is not an object")
            continue
        path = _text(raw_entry.get("path"), 512)
        if not path:
            warnings.append(f"assets[{index}]: ignored because it has no path")
            continue
        priority = _text(raw_entry.get("priority"), 20) or "unknown"
        if priority not in _ASSET_PRIORITIES:
            warnings.append(f"assets[{index}]: unknown priority {priority!r}, using unknown")
            priority = "unknown"
        output.append(
            {
                "path": path,
                "output_name": _text(raw_entry.get("output_name"), 160),
                "role": _text(raw_entry.get("role"), 40) or "unknown",
                "priority": priority,
                "mime": _text(raw_entry.get("mime"), 120),
                "sha256": _text(raw_entry.get("sha256"), 64),
                "size": _int(raw_entry.get("size"), 0, 128 * 1024 * 1024),
                "player_visible": bool(raw_entry.get("player_visible", priority == "essential")),
            }
        )
    return output


def _public_module_blueprint(module: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only bounded, declarative module metadata into the draft card."""
    keys = ("schema_version", "start_scene", "scenes", "npcs", "clues", "items", "threats", "timeline", "objectives", "actors", "trackers", "encounters", "endings", "rewards", "rules", "handouts")
    result: dict[str, Any] = {"schema_version": _int(module.get("schema_version"), 1, 1)}
    for key in keys:
        if key == "schema_version":
            continue
        value = module.get(key)
        if isinstance(value, (str, int, float, bool)):
            result[key] = value
        elif isinstance(value, list):
            result[key] = [item for item in value[:MAX_ENTITIES] if isinstance(item, (Mapping, str, int, float, bool))]
    return result


def _normalize_provenance(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        "source_files": _string_list(raw.get("source_files"), MAX_SOURCE_FILES, 260),
        "source_sha256": _text(raw.get("source_sha256"), 64),
        "conversion_version": _text(raw.get("conversion_version"), 80),
        "distribution_policy": _text(raw.get("distribution_policy"), 80),
    }


def _stable_id(prefix: str, raw_id: Any, name: str) -> str:
    candidate = str(raw_id or "").strip().casefold()
    if _ID_RE.fullmatch(candidate):
        return candidate
    normalized = unicodedata.normalize("NFKC", name).casefold()
    digest = hashlib.sha256(f"{prefix}:{normalized}".encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _support(raw: Any, collection: str, warnings: list[str], entity_id: str) -> str:
    value = str(raw or "").strip().casefold()
    if value in SUPPORT_STATES:
        return value
    if collection == "rules":
        warnings.append(f"rules:{entity_id}: missing support state, marked blocked")
        return "blocked"
    return "native"


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _string_list(value: Any, limit: int, item_limit: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item, item_limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _pages(value: Any) -> list[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        page = _int(item, 0, 1_000_000)
        if page and page not in result:
            result.append(page)
        if len(result) >= MAX_SOURCE_PAGES:
            break
    return result


def _bounded_mapping(value: Any, limit: int, item_limit: int) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        _text(key, 80): _text(item, item_limit)
        for key, item in list(value.items())[:limit]
        if _text(key, 80) and _text(item, item_limit)
    }


def _list_of_bounded(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    result: list[Any] = []
    for item in value[:limit]:
        if isinstance(item, Mapping):
            result.append({str(key)[:80]: str(entry)[:160] for key, entry in list(item.items())[:16]})
        elif isinstance(item, (str, int, float, bool)):
            result.append(str(item)[:160])
    return result


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, number))


def _int(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return min(high, max(low, number))


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output


def _pack_id(module: Mapping[str, Any], bundle: ScenarioBundle) -> str:
    candidate = _text(module.get("name_en"), 64).casefold()
    candidate = re.sub(r"[^a-z0-9]+", "-", candidate).strip("-")
    if not candidate:
        candidate = "scenario"
    digest = bundle.sha256[:8] or hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:8]
    return f"{candidate[:55]}-{digest}"
