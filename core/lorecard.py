"""Loreweaver native card bundle (``*.lorecard.json``) — the parsing half of the M14 importer.

The card studio's lossless export ("imported cards adapt to us; forged cards are born native and
still play everywhere") is a flat JSON object tagged ``format: "loreweaver.card"``. Unlike a
SillyTavern card it keeps everything ST has no safe shape for: keeper-only variables, ``secret``
lore, typed variable specs (``core.modvars`` shape verbatim), per-entry ``condition`` expressions,
optional stable entry ``id``s (the cross-pack reference handle — ``<pack-id>#<entry-id>``), and
top-level ``hooks`` scripts. ``docs/plugins.md`` and the studio's ``docs/FORMATS.md`` document the
shape; this module is the engine side of that contract.

Format v1 (the M16 2.0 consolidation) is native-optimal: ``opening`` /
``alternate_openings`` / ``dialogue_examples`` / ``author_notes`` replace the ST-copied
``first_mes`` / ``alternate_greetings`` / ``mes_example`` / ``creator_notes`` names, and hook
scripts are the first-class top-level ``hooks`` list instead of hiding under ``extensions``.
ST compatibility remains an IMPORT-boundary concern (``core.charcard``), never a shape this
format carries. ``format_version`` is the schema version: older versions upgrade through
``_FORMAT_MIGRATIONS`` step by step (v0, the pre-freeze provisional shape, is deliberately
unmigratable), and newer versions are refused.

It ONLY parses. No I/O, no network, no ``exec`` — bytes in, a :class:`Lorecard` out. Every trust
decision (who may bring world machinery into a room, whether hooks get installed, whether
``secret`` survives) stays with the caller, i.e. the keeper-gated ``.import … world`` path. Two
consequences are worth stating up front:

- the worldbook half is re-emitted in the ST-ENTRY dict shape ``core.worldbook.import_entries``
  already consumes, so the native path reuses that audited importer instead of growing a second
  one. A typed ``condition`` rides back as a leading ``@@if <expr>`` decorator line (the same
  representation the studio's ST export writes, and the only one that importer reads), and
  ``secret`` rides as a plain ``secret: True`` key — honored only for a keeper import and dropped
  structurally for anyone else, because iron rule #3 lives in the importer, not here;
- the original document is kept verbatim on ``card.raw``, so ``core.card_split`` classifies a
  native bundle like any other card: hooks under the root-level ``extensions`` are found by
  ``card_hook_codes``, and a bundle carrying hooks / variables / secret lore is world-kind by
  construction.

Structural garbage (not JSON, wrong ``format`` tag, unsupported ``format_version``, past a hard
cap) raises ``ValueError`` with an author-actionable message. Entry-level junk (a malformed lore
entry, an invalid variable spec) is SKIPPED and reported through :attr:`Lorecard.warnings`, so one
bad row never costs an author the whole bundle.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from core.charcard import MAX_CARD_FILE_BYTES, CharacterCard
from core.modvars import normalize_spec

LORECARD_FORMAT = "loreweaver.card"
# The schema version this build writes/reads natively. Older documents upgrade through
# `_FORMAT_MIGRATIONS` (version -> migration(raw) -> raw for version+1, applied until the
# document reaches CURRENT); a version with no registered migration — v0, the pre-freeze
# provisional shape — is refused, as is anything newer than this build.
CURRENT_FORMAT_VERSION = 1
_FORMAT_MIGRATIONS: dict[int, Any] = {}
SUPPORTED_FORMAT_VERSIONS = frozenset({CURRENT_FORMAT_VERSION})

# v1: hooks are the top-level ``hooks`` list. ``core.card_split.card_hook_codes`` reads the
# same key off ``card.raw`` for native bundles.
HOOKS_KEY = "hooks"

# Hard caps against a hostile or simply broken bundle fed through `.import`. The file cap is the
# character-card cap (same upload path, same OOM concern); the rest bound prompt-injection surface
# and parse cost. Passing one is FATAL: a document that far out of shape is not an author typo
# worth half-importing. Note these are PARSE caps — a room's own limits (``core.modvars.MAX_VARS``,
# ``core.worldbook.MAX_IMPORT_ENTRIES``) still apply when the parsed bundle is actually installed.
MAX_LORECARD_FILE_BYTES = MAX_CARD_FILE_BYTES
MAX_LORECARD_ENTRIES = 512
MAX_LORECARD_ENTRY_CONTENT_BYTES = 128 * 1024
MAX_LORECARD_VARIABLES = 256
MAX_LORECARD_PREGENS = 8
MAX_LORECARD_ITEMS = 32
MAX_ITEM_REVEALS = 16
MAX_ITEM_REVEAL_REF_CHARS = 160
# Starter-gear detection. The module item pool holds things the party must FIND in the
# world (a place, an NPC's hands) — never the investigators' personal starting gear.
# When `origin`/`original_holder` reads like initial equipment (investigators carry
# it, 随身携带/自备, starter gear), the entry is skipped with a warning: that payload
# belongs to the character sheet (pregens' equipment), not the findable pool.
_INITIAL_GEAR_MARKS = (
    "调查员", "玩家角色", "随身携带", "自备", "自带",
    "初始装备", "初始物品", "开局装备", "开局物品",
    "investigator", "player character", "starter", "starting",
    "carried by the", "personal gear",
)


def _is_starter_gear(origin: str, original_holder: str) -> bool:
    """Whether `origin`/`original_holder` describe the investigators' own starting
    gear rather than something findable in the world."""
    text = f"{origin} {original_holder}".casefold()
    return any(mark in text for mark in _INITIAL_GEAR_MARKS)
# Mirrors ``core.condexpr.MAX_EXPR_LEN`` (not imported — see HOOKS_EXTENSION_KEY). A longer
# condition still rides along, but fails closed downstream, so the author gets a warning here.
MAX_CONDITION_CHARS = 500

_SELECTIVE_LOGICS = ("and_any", "and_all", "not_any", "not_all")
# Native ``"" | "before" | "after"`` → the ST names ``core.worldbook._normalize_import_entry``
# reads. This is the one field where the two engine consumers disagree (``LoreEntry.from_dict``
# wants the bare native words), and the importer is the documented consumer of these dicts.
_POSITIONS = {"before": "before_char", "after": "after_char"}


@dataclass(frozen=True)
class Lorecard:
    """One parsed native bundle: a character-card view plus the native-only extras.

    ``card`` is a plain :class:`core.charcard.CharacterCard` whose ``character_book`` holds
    importer-shaped entry dicts (see the module docstring) and whose ``raw`` is the original
    document. ``variable_specs`` are ``core.modvars`` specs, already normalized. ``warnings``
    lists every tolerated problem, in document order, for the caller to echo to the author.
    """

    card: CharacterCard
    alternate_greetings: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    variable_specs: tuple[dict[str, Any], ...] = ()
    pregens: tuple[dict[str, Any], ...] = ()
    items: tuple[dict[str, Any], ...] = ()
    system: str = ""
    warnings: tuple[str, ...] = ()


def looks_like_lorecard(data: bytes) -> bool:
    """Cheap sniff: does ``data`` look like a native bundle rather than an ST card?

    Total function — never raises, whatever the bytes are. A verbatim substring test gates the
    JSON parse so an unrelated 16MB upload is rejected without paying for a full decode.
    """
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_LORECARD_FILE_BYTES:
        return False
    if LORECARD_FORMAT.encode("utf-8") not in data:
        return False
    try:
        parsed = json.loads(bytes(data).decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    return isinstance(parsed, dict) and parsed.get("format") == LORECARD_FORMAT


def parse_lorecard_bytes(data: bytes, filename: str = "") -> Lorecard:
    """Parse one ``*.lorecard.json`` document.

    Raises ``ValueError`` with an author-actionable message when the document is structurally
    unusable (not JSON, not a native bundle, an unsupported ``format_version``, or past a hard
    cap). Anything smaller — a lore entry that isn't an object, an unusable variable spec — is
    skipped and recorded in :attr:`Lorecard.warnings`.
    """
    label = filename or "lorecard"
    if len(data) > MAX_LORECARD_FILE_BYTES:
        raise _fail(label, f"native card bundle exceeds the {MAX_LORECARD_FILE_BYTES}-byte size limit")

    try:
        raw = json.loads(bytes(data).decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _fail(label, f"not a readable JSON document ({exc})") from exc
    if not isinstance(raw, dict):
        raise _fail(label, "native card bundle must be a JSON object")  # i18n-exempt: author diagnostic, wrapped in a localized import summary

    declared = raw.get("format")
    if declared != LORECARD_FORMAT:
        raise _fail(label, f"not a Loreweaver native card: format is {declared!r}, want {LORECARD_FORMAT!r}")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
    version = raw.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _fail(label, f"format_version must be an integer, got {version!r}")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
    if version > CURRENT_FORMAT_VERSION:
        raise _fail(
            label,
            f"unsupported format_version {version}; this build reads up to {CURRENT_FORMAT_VERSION}",  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        )
    while version < CURRENT_FORMAT_VERSION:
        migrate = _FORMAT_MIGRATIONS.get(version)
        if migrate is None:
            raise _fail(label, f"unsupported format_version {version}; this build reads: {CURRENT_FORMAT_VERSION}")
        raw = migrate(raw)
        version += 1

    warnings: list[str] = []
    entries = _parse_worldbook(raw.get("worldbook"), label, warnings)
    specs = _parse_variables(raw.get("variables"), label, warnings)
    hooks = _parse_hooks(raw.get(HOOKS_KEY), warnings)
    pregens = _parse_pregens(raw.get("pregens"), warnings)
    items = _parse_items(raw.get("items"), warnings)

    card = CharacterCard(
        name=_text(raw.get("name")).strip(),
        description=_text(raw.get("description")),
        personality=_text(raw.get("personality")),
        scenario=_text(raw.get("scenario")),
        first_mes=_text(raw.get("opening")),
        mes_example=_text(raw.get("dialogue_examples")),
        creator_notes=_text(raw.get("author_notes")),
        tags=_text_list(raw.get("tags")),
        character_book=entries,
        raw=raw,
    )
    return Lorecard(
        card=card,
        alternate_greetings=tuple(_text_list(raw.get("alternate_openings"))),
        hooks=hooks,
        variable_specs=specs,
        pregens=pregens,
        items=items,
        system=_text(raw.get("system")).strip(),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _parse_pregens(raw: Any, warnings: list[str]) -> tuple[dict[str, Any], ...]:
    """Native pregen-cast list → normalized entries the world importer registers.

    Shape: ``[{name, occupation?, background|notes?, skills?: {canonical: int}}]``.
    ``occupation`` (the character's job, e.g. "Detective" / "考古研究员") lands in the
    sheet's occupation field when the system declares one; ``background`` (the persona
    paragraph, legacy name ``notes``) lands in the sheet's background. Sheets are built
    downstream from the target system's DEFAULTS plus these overrides — deterministic,
    no LLM — so a module ships a claimable multi-investigator cast."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        warnings.append("pregens: ignored (must be a list of entries)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        return ()
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if len(out) >= MAX_LORECARD_PREGENS:
            warnings.append(f"pregens: truncated to {MAX_LORECARD_PREGENS} entries")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
            break
        if not isinstance(item, dict):
            warnings.append(f"pregens[{index}]: ignored (must be an object)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
            continue
        name = _text(item.get("name")).strip()[:60]
        if not name:
            warnings.append(f"pregens[{index}]: ignored (missing name)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
            continue
        # The roster one-liner (`blurb`) is derived from the persona paragraph's
        # first sentence — one field of truth, no separate `concept`. Legacy
        # hand-authored packs may still spell it `concept`/`blurb`.
        blurb = _first_sentence(_text(item.get("background") or item.get("notes")).strip()) or _text(
            item.get("concept") or item.get("blurb")
        ).strip()
        blurb = blurb[:200]
        occupation = _text(item.get("occupation")).strip()[:60]
        # `background` is the forge's persona paragraph (history/personality/voice/
        # secret); hand-authored packs may use the legacy `notes` name instead.
        notes = _text(item.get("background") or item.get("notes")).strip()[:400]
        skills: dict[str, int] = {}
        skills_raw = item.get("skills")
        if isinstance(skills_raw, dict):
            for key, value in list(skills_raw.items())[:32]:
                try:
                    skills[str(key).strip()[:60]] = int(value)
                except (TypeError, ValueError):
                    warnings.append(f"pregens[{index}].skills.{key}: ignored (not an integer)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        elif skills_raw is not None:
            warnings.append(f"pregens[{index}].skills: ignored (must be a mapping)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        avatar = str(item.get("avatar") or "").strip()[:200]
        out.append({"name": name, "blurb": blurb, "occupation": occupation, "notes": notes, "skills": skills, "avatar": avatar})
    return tuple(out)


def _parse_items(raw: Any, warnings: list[str]) -> tuple[dict[str, Any], ...]:
    """Native item-catalog list → normalized templates the room importer seeds.

    Shape: ``[{name, kind?, slot?, scope?, description?, effect?, lore?, origin?,
    original_holder?, quantity?, bonus?: {canonical: int}, plot_role?, reveals?}]``.
    ``name`` is the only
    required field; a missing name drops the entry with a warning. ``slot`` names the
    equip slot the item occupies when equipped (empty = not equippable), and ``bonus``
    is the equipped mechanical delta map (sheet canonical -> int) that the engine
    aggregates into the sheet's derived bonuses — this is what makes a designed item
    grant a real effect, not just a prop.     ``origin`` is where the item starts (a
    place); ``original_holder`` is who holds it first (a person or group) — both ride
    the template so the module page can show where each item begins play. Entries
    whose origin reads as the investigators' OWN starting gear (随身携带/自备/
    investigator) are skipped with a warning — the item pool is for things the party
    must find in the world, not what they begin with.

    ``plot_role`` describes the item's narrative role, while ``reveals`` lists
    clue ids or clue names that become known when the party actually obtains the
    item. The item remains a physical entity; the linked clue is the information
    it carries. ``scope`` marks whether the item is ``universal`` (works in ANY module — a
    handgun, a healing salve, a toolkit) or ``module`` (bound to the module it
    ships with — a quest artifact, a plot device). The importer stamps module
    items with the room's active module id; an item whose module no longer
    matches the room's active module contributes nothing in play. The default
    is ``module`` — a module-bound item failing closed is safer than a
    module-only prop leaking across campaigns.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        warnings.append("items: ignored (must be a list of entries)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        return ()
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if len(out) >= MAX_LORECARD_ITEMS:
            warnings.append(f"items: truncated to {MAX_LORECARD_ITEMS} entries")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
            break
        if not isinstance(item, dict):
            warnings.append(f"items[{index}]: ignored (must be an object)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
            continue
        name = _text(item.get("name")).strip()[:60]
        if not name:
            warnings.append(f"items[{index}]: ignored (missing name)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
            continue
        origin = _text(item.get("origin")).strip()[:200]
        original_holder = _text(item.get("original_holder")).strip()[:100]
        if _is_starter_gear(origin, original_holder):
            warnings.append(  # i18n-exempt: author diagnostic, wrapped in a localized import summary
                f"items[{index}] {name!r}: skipped — origin reads like the investigators' "
                "starting gear; the item pool holds what the party must FIND (a place, "
                "an NPC's hands), not what they begin with"
            )
            continue
        scope_raw = _text(item.get("scope")).strip().casefold()
        if scope_raw not in {"universal", "module"}:
            if scope_raw:
                warnings.append(f"items[{index}].scope: {scope_raw!r} ignored (want universal|module)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
            scope = "module"
        else:
            scope = scope_raw
        bonus: dict[str, int] = {}
        bonus_raw = item.get("bonus")
        if isinstance(bonus_raw, dict):
            for key, value in list(bonus_raw.items())[:32]:
                try:
                    bonus[str(key).strip()[:60]] = int(value)
                except (TypeError, ValueError):
                    warnings.append(f"items[{index}].bonus.{key}: ignored (not an integer)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        elif bonus_raw is not None:
            warnings.append(f"items[{index}].bonus: ignored (must be a mapping)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        quantity = item.get("quantity")
        try:
            quantity = max(1, int(quantity)) if quantity is not None else 1
        except (TypeError, ValueError):
            quantity = 1
        reveals_raw = item.get("reveals")
        if isinstance(reveals_raw, str):
            reveals_values = [reveals_raw]
        elif isinstance(reveals_raw, list):
            reveals_values = reveals_raw
        else:
            reveals_values = []
        # ``clue`` was the original analysis/card spelling. Keep accepting it at
        # the boundary so existing modules gain the new runtime behavior without
        # needing a rewrite.
        legacy_clue = _text(item.get("clue")).strip()
        if not reveals_values and legacy_clue:
            reveals_values = [legacy_clue]
        reveals = [
            _text(value).strip()[:MAX_ITEM_REVEAL_REF_CHARS]
            for value in reveals_values[:MAX_ITEM_REVEALS]
            if _text(value).strip()
        ]
        plot_role = _text(item.get("plot_role")).strip()[:40]
        if reveals and not plot_role:
            plot_role = "evidence"
        out.append(
            {
                "name": name,
                "kind": _text(item.get("kind")).strip()[:40],
                "slot": _text(item.get("slot")).strip()[:40],
                "scope": scope,
                "description": _text(item.get("description")).strip()[:500],
                "lore": _text(item.get("lore")).strip()[:2000],
                "effect": _text(item.get("effect")).strip()[:500],
                "origin": origin,
                "original_holder": original_holder,
                "quantity": quantity,
                "bonus": bonus,
                "secret": bool(item.get("secret", False)),
                "plot_role": plot_role,
                "reveals": reveals,
            }
        )
    return tuple(out)


def _parse_worldbook(raw: Any, label: str, warnings: list[str]) -> list[dict[str, Any]]:
    """Native worldbook list → importer-shaped entry dicts, junk rows skipped."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append("worldbook: ignored (must be a list of entries)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        return []
    if len(raw) > MAX_LORECARD_ENTRIES:
        raise _fail(label, f"worldbook has {len(raw)} entries; at most {MAX_LORECARD_ENTRIES} are allowed")

    entries: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        entry = _parse_entry(item, index, label, warnings)
        if entry is not None:
            entries.append(entry)
    return entries


def _parse_entry(raw: Any, index: int, label: str, warnings: list[str]) -> dict[str, Any] | None:
    where = f"worldbook[{index}]"
    if not isinstance(raw, dict):
        warnings.append(f"{where}: skipped (entry must be a JSON object)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        return None

    body = raw.get("content")
    if isinstance(body, (dict, list, tuple, set)):
        warnings.append(f"{where}: skipped (content must be text)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        return None
    content = _text(body)
    if len(content.encode("utf-8")) > MAX_LORECARD_ENTRY_CONTENT_BYTES:
        raise _fail(label, f"{where} content exceeds the {MAX_LORECARD_ENTRY_CONTENT_BYTES}-byte limit")
    if not content.strip():
        warnings.append(f"{where}: skipped (empty content)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        return None

    title = _text(raw.get("title") or raw.get("comment") or raw.get("name")).strip()
    if not title:
        # Native pack authors are expected to provide `title`, but older generated
        # cards omitted it. Derive a readable snapshot title instead of exposing
        # the storage fallback "Untitled Lore" to players.
        title = next((line.strip() for line in content.splitlines() if line.strip()), "")
        title = title.lstrip("# ").strip()[:120]
    if not title:
        title = f"Lore entry {index + 1}"
    # A typed `condition` becomes a leading `@@if` decorator line: that is the ONLY form
    # `core.worldbook._normalize_import_entry` maps back onto `LoreEntry.condition`, and it is
    # exactly what the studio's SillyTavern export writes. Whitespace is collapsed because a
    # decorator is a single line by definition.
    condition = " ".join(_text(raw.get("condition")).split())
    if len(condition) > MAX_CONDITION_CHARS:
        warnings.append(  # i18n-exempt: author diagnostic, wrapped in a localized import summary
            f"{where}: condition is longer than {MAX_CONDITION_CHARS} characters and will never fire"
        )
    if condition:
        content = f"@@if {condition}\n{content}"

    secondary_keys = _text_list(raw.get("secondary_keys"))
    logic = _text(raw.get("selective_logic")).strip()
    # The optional stable entry id — the cross-pack reference handle
    # (`<pack-id>#<entry-id>`). Carried verbatim; uniqueness is warned about at
    # parse time so authors catch collisions before anyone references them.
    entry_id = _text(raw.get("id")).strip()
    return {
        **({"id": entry_id} if entry_id else {}),
        "comment": title,
        "content": content,
        "keys": _text_list(raw.get("keys")),
        "secondary_keys": secondary_keys,
        # Optional illustration asset filename (forge binds generated scene/NPC/item
        # portraits onto the worldbook entry they depict). Carried verbatim into the
        # room's lore document via `core.worldbook.LoreEntry.image`.
        "image": _text(raw.get("image")).strip(),
        # V2's gate flag, stated explicitly rather than left to the importer's default —
        # the same thing the studio's SillyTavern export writes.
        "selective": bool(secondary_keys),
        "selective_logic": logic if logic in _SELECTIVE_LOGICS else "and_any",
        "category": _text(raw.get("category")).strip() or "lore",
        # Keeper-only lore. The importer honors this ONLY for `is_keeper=True`; a player-path
        # import drops it structurally, so carrying it here cannot widen anyone's visibility.
        "secret": _flag(raw.get("secret")),
        # Carried for fidelity; the importer forces it off for any uploaded file (an always-on
        # entry would inject itself into every prompt regardless of keywords).
        "constant": _flag(raw.get("constant")),
        "priority": _int(raw.get("priority"), 0),
        "enabled": _flag(raw.get("enabled"), default=True),
        "probability": _int(raw.get("probability"), 100, low=0, high=100),
        "case_sensitive": _flag(raw.get("case_sensitive")),
        "match_whole_words": _flag(raw.get("match_whole_words")),
        "scan_depth": _int(raw.get("scan_depth"), 0, low=0, high=200),
        "position": _POSITIONS.get(_text(raw.get("position")).strip(), ""),
        "sticky": _int(raw.get("sticky"), 0, low=0, high=999),
        "cooldown": _int(raw.get("cooldown"), 0, low=0, high=999),
        "delay": _int(raw.get("delay"), 0, low=0, high=9999),
    }


def _parse_variables(raw: Any, label: str, warnings: list[str]) -> tuple[dict[str, Any], ...]:
    """Native variable list → normalized ``core.modvars`` specs, invalid ones skipped.

    Normalization is ``core.modvars.normalize_spec``, the same tolerant path stored state goes
    through, so a bundle can never introduce a spec the engine would not have accepted itself.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        warnings.append("variables: ignored (must be a list of variable specs)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        return ()
    if len(raw) > MAX_LORECARD_VARIABLES:
        raise _fail(label, f"{len(raw)} variables declared; at most {MAX_LORECARD_VARIABLES} are allowed")

    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"variables[{index}]"
        if not isinstance(item, dict):
            warnings.append(f"{where}: skipped (variable spec must be a JSON object)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
            continue
        spec = normalize_spec(item.get("id"), item)
        if spec is None:
            warnings.append(f"{where}: skipped (unusable id, kind, bounds or default)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
            continue
        if spec["id"] in seen:
            warnings.append(f"{where}: skipped (duplicate variable id {spec['id']!r})")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
            continue
        seen.add(spec["id"])
        specs.append(spec)
    return tuple(specs)


def _parse_hooks(entries: Any, warnings: list[str]) -> tuple[str, ...]:
    """The top-level ``hooks`` list → hook sources. Tolerates code strings and ``{code: …}``
    dicts, matching ``core.card_split.card_hook_codes``; installing them is the caller's call."""
    if entries is None:
        return ()
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, list):
        warnings.append(f"{HOOKS_KEY}: ignored (must be a list of scripts)")  # i18n-exempt: author diagnostic, wrapped in a localized import summary
        return ()
    codes = []
    for item in entries:
        code = item if isinstance(item, str) else item.get("code") if isinstance(item, dict) else None
        if isinstance(code, str) and code.strip():
            codes.append(code)
    return tuple(codes)


# ---------------------------------------------------------------------------
# Coercion helpers — total functions, defensive against author/attacker garbage
# ---------------------------------------------------------------------------
def _fail(label: str, message: str) -> ValueError:
    return ValueError(f"{label}: {message}" if label else message)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value)


_SENTENCE_BREAK_RE = re.compile(r"[。！？!?；;\n]")


def _first_sentence(text: str) -> str:
    """The first sentence of `text` (split on CJK/ASCII sentence punctuation or
    newlines) — the roster one-liner derived from the persona paragraph."""
    if not text:
        return ""
    return _SENTENCE_BREAK_RE.split(text.strip(), maxsplit=1)[0].strip()


def _text_list(value: Any) -> list[str]:
    """A list of non-empty trimmed strings; a bare string counts as a one-item list."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [text for text in (_text(item).strip() for item in value) if text]


def _flag(value: Any, *, default: bool = False) -> bool:
    return default if value is None else bool(value)


def _int(value: Any, default: int, *, low: int | None = None, high: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if low is not None:
        parsed = max(low, parsed)
    if high is not None:
        parsed = min(high, parsed)
    return parsed
