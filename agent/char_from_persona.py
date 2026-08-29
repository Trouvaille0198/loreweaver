"""Build rule-legal TRPG sheets from SillyTavern persona cards.

Lives in ``agent/`` (moved from ``core/`` 2026-08-19): the persona → numbers step is a
model call; only the validation against the rulepack (``core.character_rules``) is
deterministic, and that part stays in ``core/``."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from core.character_manager import CharacterManager, CharacterSheet
from core.character_rules import scale_skills_to_budget, skill_point_budget
from core.charcard import CharacterCard
from core.rulepacks import RulePack, load_rulepack
from core.sheets import has_check_value, refresh_sheet, set_sheet_value, sheet_value
from infra.i18n import t
from infra.model_call_trace import lane_scope
from infra.store import Store

# Gendered-pronoun markers for the deterministic gender/pronoun inference below. English is matched on
# word boundaries (he/she + their possessive/reflexive forms); CJK counts singular 他/她 while skipping
# the plural 们 forms (他们/她们 == "they"), which carry no personal-gender signal.
_EN_MALE_RE = re.compile(r"\b(?:he|him|his|himself)\b", re.IGNORECASE)
_EN_FEMALE_RE = re.compile(r"\b(?:she|her|hers|herself)\b", re.IGNORECASE)
_ZH_MALE_RE = re.compile(r"他(?!们)")
_ZH_FEMALE_RE = re.compile(r"她(?!们)")


def infer_pronoun_note(text: str) -> str:
    """Deterministically infer a compact pronoun note ('he/him' | 'she/her' | '') from persona text.

    Counts gendered pronoun markers -- English he/she (+ possessive/reflexive) and CJK 他/她
    (singular only) -- and returns the dominant one, or '' when there is no clear signal so the
    Keeper is handed a real pronoun hint or nothing at all, never a coin-flip guess. This is data
    inference over text the user supplied, not generation, so it lives in the deterministic core.
    """
    if not text:
        return ""
    male = len(_EN_MALE_RE.findall(text)) + len(_ZH_MALE_RE.findall(text))
    female = len(_EN_FEMALE_RE.findall(text)) + len(_ZH_FEMALE_RE.findall(text))
    if male > female:
        return "he/him"
    if female > male:
        return "she/her"
    return ""


async def build_sheet_from_persona(
    services: Any,
    card: CharacterCard,
    system: str,
    *,
    module_context: str = "",
    creation: str = "",
    chat_key: str = "",
) -> CharacterSheet:
    manager = _character_manager_from_services(services)
    pack = load_rulepack(system)  # unknown systems raise ValueError with a clear message
    concept = await _ask_concept(services, card, pack, module_context, chat_key=chat_key)
    sheet = manager.generate_character(pack.system, card.name or None)
    sheet.name = card.name or sheet.name

    if not concept:
        if creation == "pregen":
            # A pregen must stay deterministic even when the concept call failed:
            # place the pack's standard array with no emphasis rather than ship
            # the raw dice roll.
            _bias_sheet(manager, sheet, pack, {}, creation=creation)
        _apply_persona_text(sheet, card, {}, pack)
        return sheet

    _bias_sheet(manager, sheet, pack, concept, creation=creation)
    _apply_persona_text(sheet, card, concept, pack)
    _apply_race_data(sheet, pack)
    _fill_initial_spells(sheet, pack)
    return sheet


async def build_sheet_from_description(
    services: Any,
    description: str,
    system: str,
    *,
    name: str = "",
    module_context: str = "",
    creation: str = "",
    chat_key: str = "",
) -> CharacterSheet:
    text = description.strip()
    card = CharacterCard(name=name.strip(), description=text, personality=text)
    return await build_sheet_from_persona(
        services, card, system, module_context=module_context, creation=creation, chat_key=chat_key
    )


def _character_manager_from_services(services: Any) -> CharacterManager:
    manager = getattr(services, "characters", None)
    if isinstance(manager, CharacterManager):
        return manager
    store = getattr(services, "store", None)
    return CharacterManager(store if isinstance(store, Store) else Store(":memory:"))


async def _ask_concept(
    services: Any,
    card: CharacterCard,
    pack: RulePack,
    module_context: str,
    *,
    chat_key: str = "",
) -> dict[str, Any]:
    # The ROOM's own LLM lane, not the global `services.llm`: the per-room
    # provider (`.model`/runtime_config) is what actually carries credentials —
    # the global default can be an unconfigured stub, and a silently failing
    # concept call used to ship sheets with no class/race/backstory at all.
    if chat_key:
        try:
            llm = await services.main_llm(chat_key)
        except Exception:
            llm = getattr(services, "llm", None)
    else:
        llm = getattr(services, "llm", None)
    if llm is None:
        return {}

    prompt = _render_prompt(services, card, pack, module_context)
    try:
        with lane_scope("authoring"):
            result = await llm.chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": _persona_summary(card)},
                ],
                temperature=0,
            )
    except Exception:
        return {}
    return _parse_concept(getattr(result, "content", None))


def _render_prompt(services: Any, card: CharacterCard, pack: RulePack, module_context: str) -> str:
    i18n = getattr(services, "i18n", None)
    renderer = i18n.t if i18n is not None and hasattr(i18n, "t") else t
    return renderer(
        "charcard.concept_prompt",
        system=pack.system,
        module_context=module_context,
        persona=_persona_summary(card),
        skill_rules=_skill_rules_text(renderer, pack),
        identity_fields=_identity_fields_text(renderer, pack),
    )


# Concept keys the model may use for an identity field the pack declares under a
# different name (e.g. a pack spells it `occupation`, the sheet stores it as
# `character_class`). First key wins.
_IDENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "occupation": ("occupation", "character_class", "class"),
    "character_class": ("character_class", "occupation", "class"),
    "class": ("class", "occupation", "character_class"),
    "race": ("race", "ancestry"),
    "ancestry": ("ancestry", "race"),
}


def _identity_fields_text(renderer: Any, pack: RulePack) -> str:
    """The identity-fields contract for the concept prompt, generated FROM the pack's
    own sheet spec — a system that declares `race`/`alignment`/anything else gets it
    advertised automatically; one that declares none stays unadvertised. Identity
    fields are the sheet's non-numeric slots (default value is the empty string):
    occupation/class, race, alignment, … Numeric ones (level, proficiency) are
    derived or defaulted by the engine, never authored by the model."""
    spec = pack.sheet_spec
    if spec is None or not spec.fields:
        return ""
    names = [name for name, default in spec.fields.items() if default == ""]
    if not names:
        return ""
    text = renderer("charcard.concept_identity_fields", fields=", ".join(names))
    # When the pack declares a race table, the model must pick one of THOSE names —
    # a race the pack cannot resolve carries no mechanical data and no display facts.
    if pack.races and "race" in names:
        options = sorted({entry.display_name(loc) for entry in pack.races.values() for loc in ("zh", "en")})
        text += " " + renderer("charcard.concept_race_options", races=", ".join(options))
    return text


def _skill_rules_text(renderer: Any, pack: RulePack) -> str:
    """The `skill_allocations` contract for the concept prompt: value range plus the
    nominal point budget (pack defaults), so the model's proposal lands inside the
    budget the engine will enforce against the placed sheet. Empty when the pack
    declares no skills table or no budget — the field stays unadvertised."""
    spec = pack.sheet_spec
    if spec is None or not spec.skills:
        return ""
    budget = skill_point_budget(CharacterSheet(name="", system=pack.system), pack)
    if budget is None:
        return ""
    skills_rule = (pack.creation_constraints or {}).get("skills") or {}
    default = skills_rule.get("default") if isinstance(skills_rule, Mapping) else None
    skill_min = int(default.get("min", 0)) if isinstance(default, Mapping) else 0
    skill_max = int(default.get("max", 90)) if isinstance(default, Mapping) else 90
    return renderer("charcard.concept_skill_rules", budget=budget, min=skill_min, max=skill_max)


def _persona_summary(card: CharacterCard) -> str:
    parts = [
        f"name: {card.name}",
        f"description: {card.description}",
        f"personality: {card.personality}",
        f"scenario: {card.scenario}",
        f"tags: {', '.join(card.tags)}",
    ]
    return "\n".join(part for part in parts if not part.endswith(": "))


async def roster_character_concept(
    services: Any,
    system: str,
    module_context: str,
    *,
    reference: str = "",
) -> dict[str, str]:
    """One authoring-lane call: a claimable roster character's NAME + DESCRIPTION + APPEARANCE, fitted to the module's summary.
    `reference` is OPTIONAL keeper input (a name, a concept, or both — e.g.
    `.pc gen 阿岚 | 瘴雾镇的调查员`) that rides the prompt as a hint; the model may
    keep it verbatim or refine it. With no reference the model writes the whole
    character from the module context alone. Returns
    ``{"name": str, "description": str, "appearance": str}`` — ``appearance`` is the
    concrete look (build, hair, clothes, marks) the portrait lane folds into an
    image prompt; keys may be empty when the call fails."""

    llm = getattr(services, "llm", None)
    if llm is None:
        return {}
    pack = load_rulepack(system)
    i18n = getattr(services, "i18n", None)
    renderer = i18n.t if i18n is not None and hasattr(i18n, "t") else t
    prompt = renderer(
        "charcard.roster_concept_prompt",
        system=pack.system,
        module_context=module_context,
        reference=reference,
    )
    try:
        with lane_scope("authoring"):
            result = await llm.chat(
                [
                    {"role": "system", "content": prompt},
                ],
                temperature=0.7,
            )
    except Exception:
        return {}
    concept = _parse_concept(getattr(result, "content", None))
    return {
        "name": str(concept.get("name") or "").strip(),
        "description": str(concept.get("description") or "").strip(),
        "appearance": str(concept.get("appearance") or "").strip(),
    }


def _parse_concept(content: str | None) -> dict[str, Any]:
    if not content:
        return {}
    text = content.strip()
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _bias_sheet(
    manager: CharacterManager, sheet: CharacterSheet, pack: RulePack, concept: dict[str, Any], *, creation: str = ""
) -> None:
    """Bias a freshly generated sheet toward `concept`: reassign its rolled or
    arrayed attributes to favor the concept's emphasis, set its occupation/class
    meta field, and raise its signature skills to a competent floor.

    Entirely generic over the pack's own ``creation_constraints`` shape -- a
    pack that declares a ``standard_array`` method plus ``archetype_priorities``
    places values by best-first archetype list; one that only declares rolled
    attributes redistributes each same-roll group's already-rolled values
    (never re-rolling). A pack with neither section (or no ``sheet:`` at all)
    is simply left at its freshly generated values.

    ``creation`` selects the placement method: ``"pregen"`` (module cast
    sheets) ALWAYS takes the declared standard array when one exists -- a
    shipped pregen is an author-fixed sheet, not a dice roll; anything else
    follows the pack's declared ``default_method``, falling back to the array
    when declared and rolled redistribution otherwise.
    """
    constraints = pack.creation_constraints or {}
    attribute_rules: dict[str, Any] = constraints.get("attributes") or {}
    emphasis = _normalized_attrs(
        concept.get("attribute_emphasis") or concept.get("emphasis"), list(attribute_rules.keys())
    )
    role_text = _as_text(concept.get("occupation") or concept.get("class"))

    methods = constraints.get("methods") or {}
    array_values = (methods.get("standard_array") or {}).get("values")
    archetypes = constraints.get("archetype_priorities")
    if _creation_method(constraints, creation) == "standard_array":
        _assign_by_archetype(sheet, emphasis, array_values, archetypes, constraints.get("default_archetype"), role_text)
        _apply_attribute_tweaks(sheet, constraints, concept)
    else:
        _assign_rolled_groups(sheet, emphasis, attribute_rules)

    spec = pack.sheet_spec
    if spec is not None and spec.fields:
        # Identity fields (the sheet's non-numeric slots — occupation/class, race,
        # alignment, …) are written straight from the concept, matching through the
        # alias table, so a NEW system's declared fields work with no per-field code.
        for field_name, default in spec.fields.items():
            if default != "":
                continue
            aliases = _IDENTITY_ALIASES.get(field_name, (field_name,))
            value = _as_text(next((concept.get(k) for k in aliases if concept.get(k)), ""))
            if value:
                setattr(sheet, field_name, value)

    for skill in _list_text(concept.get("signature_skills") or concept.get("skills")):
        canonical = manager.find_skill_by_alias(sheet, skill) or skill
        if has_check_value(sheet, pack, canonical):
            trained = min(99, max(int(sheet_value(sheet, pack, canonical)), 60))
            set_sheet_value(sheet, pack, canonical, trained)
    _apply_skill_allocations(manager, sheet, pack, concept)

    # A full creation-style refresh: the reassignment above may have moved the
    # very attributes a current-pool vital (HP/SAN/MP-alike) derives from, so
    # this sheet -- never having been played -- starts fresh at its recomputed
    # full values, exactly like `CharacterManager.generate_character` itself.
    refresh_sheet(sheet, pack, initialize_vitals=True)


def _creation_method(constraints: Mapping[str, Any], creation: str) -> str:
    """Resolve which lane places attribute values: ``"standard_array"`` or ``"rolled"``.

    ``creation="pregen"`` forces the array whenever the pack declares one (with a
    rolled redistribution fallback for packs without); otherwise the pack's own
    ``default_method`` wins, with the legacy auto choice (array when declared) as
    the fallback for packs that never declared a preference.
    """
    methods = constraints.get("methods") or {}
    has_array = bool((methods.get("standard_array") or {}).get("values") and constraints.get("archetype_priorities"))
    if creation == "pregen":
        return "standard_array" if has_array else "rolled"
    declared = str(constraints.get("default_method") or "").strip().casefold()
    if declared in {"rolled", "standard_array"}:
        return declared
    return "standard_array" if has_array else "rolled"


def _apply_skill_allocations(
    manager: CharacterManager, sheet: CharacterSheet, pack: RulePack, concept: Mapping[str, Any]
) -> None:
    """Apply the concept's ``skill_allocations`` (skill name -> target value).

    Model-proposed numbers are untrusted: names resolve through the pack alias
    table (unknown names dropped, never an error), values floor at the skill's
    base and clamp to the pack's creation max, and when the pack declares a
    skill-point budget the SHEET's total spend — signature floor included —
    is held within it by scaling the allocation profile down (the signature
    floor keeps its points). No-op without allocations or a skills table.
    """
    raw = concept.get("skill_allocations")
    if not isinstance(raw, Mapping) or not raw:
        return
    spec = pack.sheet_spec
    if spec is None or not spec.skills:
        return
    base_skills: dict[str, int] = {}
    for key, value in (spec.skills or {}).items():
        try:
            base_skills[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    derived = set(spec.derived_skills)
    skills_rule = (pack.creation_constraints or {}).get("skills") or {}
    default = skills_rule.get("default") if isinstance(skills_rule, Mapping) else None
    skill_max = int(default.get("max", 90)) if isinstance(default, Mapping) else 90

    allocated: dict[str, int] = {}
    for key, value in raw.items():
        canonical = manager.find_skill_by_alias(sheet, str(key))
        if canonical is None or canonical in derived or canonical not in base_skills:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        allocated[canonical] = max(base_skills[canonical], min(skill_max, number))
    if not allocated:
        return

    # The prompt advertises the field only for packs with a declared budget, and
    # the engine enforces the same gate: no budget, no allocations.
    budget = skill_point_budget(sheet, pack)
    if budget is None:
        return
    for skill, value in allocated.items():
        set_sheet_value(sheet, pack, skill, value)
    other_spend = sum(
        max(0, int(sheet_value(sheet, pack, skill)) - base)
        for skill, base in base_skills.items()
        if skill not in allocated and skill not in derived
    )
    scaled = scale_skills_to_budget(
        {skill: int(sheet_value(sheet, pack, skill)) for skill in allocated},
        base_skills,
        max(0, budget - other_spend),
    )
    for skill, value in scaled.items():
        set_sheet_value(sheet, pack, skill, value)


def _tweak_policy_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _apply_attribute_tweaks(sheet: CharacterSheet, constraints: Mapping[str, Any], concept: Mapping[str, Any]) -> None:
    """Apply the concept's optional zero-sum attribute nudges to an array-placed sheet.

    The model proposes DELTAS; the engine enforces the pack's tweak policy
    (``methods.standard_array.tweak_step`` / ``tweak_max``): deltas must be whole
    multiples of the step, bounded in magnitude, sum to exactly zero, and keep
    every adjusted value inside the attribute's declared creation range. ANY
    violation discards the ENTIRE tweak set -- a malformed proposal degrades to
    the plain array, never to a partial or inflated sheet. A pack without the
    policy ignores tweaks altogether.
    """
    array_cfg = (constraints.get("methods") or {}).get("standard_array") or {}
    step = _tweak_policy_int(array_cfg.get("tweak_step"))
    limit = _tweak_policy_int(array_cfg.get("tweak_max"))
    raw = concept.get("attribute_tweaks")
    if step <= 0 or limit <= 0 or not isinstance(raw, Mapping) or not raw:
        return
    rules: Mapping[str, Any] = constraints.get("attributes") or {}
    tweaks: dict[str, int] = {}
    for key, value in raw.items():
        attr = str(key).strip().upper()
        rule = rules.get(attr)
        # bool is an int subclass -- accept real ints only.
        if not isinstance(rule, Mapping) or not isinstance(value, int) or isinstance(value, bool):
            return
        if value % step or abs(value) > limit:
            return
        tweaks[attr] = value
    if sum(tweaks.values()) != 0:
        return
    adjusted: dict[str, int] = {}
    for attr, delta in tweaks.items():
        rule = rules[attr]
        placed = int(sheet.attributes.get(attr, 0)) + delta
        if placed < int(rule.get("min", 0)) or placed > int(rule.get("max", 100)):
            return
        adjusted[attr] = placed
    sheet.attributes.update(adjusted)


def _assign_by_archetype(
    sheet: CharacterSheet,
    emphasis: list[str],
    values: list[Any],
    archetypes: Mapping[str, Any],
    default_archetype: Any,
    role_text: str,
) -> None:
    """Assign a declared standard array of values to attribute keys, following
    the archetype (a pack-declared, best-first attribute priority list) the
    concept's class/occupation text names -- falling back to the pack's
    declared default archetype, then to an arbitrary declared one."""
    base = archetypes.get(role_text.strip().casefold())
    if base is None:
        base = archetypes.get(str(default_archetype or "").strip().casefold())
    if base is None and archetypes:
        base = next(iter(archetypes.values()))
    if not base:
        return
    preferred = [attr for attr in emphasis if attr in base]
    priority = preferred + [attr for attr in base if attr not in preferred]
    for attr, value in zip(priority, values, strict=True):
        sheet.attributes[str(attr)] = value


def _assign_rolled_groups(sheet: CharacterSheet, emphasis: list[str], attribute_rules: Mapping[str, Any]) -> None:
    """Within each set of attribute keys sharing an identical roll/min/max (the
    pack's own rolled-attribute groups), redistribute their already-rolled
    values so the concept's emphasized attributes land on the group's highest
    rolls -- a same-distribution swap, never a re-roll."""
    groups: dict[tuple[Any, Any, Any], list[str]] = {}
    for key, rule in attribute_rules.items():
        if not isinstance(rule, Mapping):
            continue
        signature = (rule.get("roll"), rule.get("min"), rule.get("max"))
        groups.setdefault(signature, []).append(str(key))

    for attrs in groups.values():
        values = sorted((int(sheet.attributes.get(attr, 0)) for attr in attrs), reverse=True)
        preferred = [attr for attr in emphasis if attr in attrs]
        ordered_attrs = preferred + [attr for attr in attrs if attr not in preferred]
        for attr, value in zip(ordered_attrs, values, strict=True):
            sheet.attributes[attr] = value


def _normalized_attrs(value: Any, allowed: list[str]) -> list[str]:
    allowed_set = set(allowed)
    attrs = _list_text(value)
    normalized: list[str] = []
    for attr in attrs:
        key = attr.strip().upper()
        if key in allowed_set and key not in normalized:
            normalized.append(key)
    return normalized


def _list_text(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_as_text(item) for item in value if _as_text(item)]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _apply_race_data(sheet: CharacterSheet, pack: RulePack) -> None:
    """Apply the pack's race data to a freshly built sheet.

    The sheet's race field is free text the concept model wrote; the engine
    resolves it through the pack's ``races:`` table (aliases, zh/en names,
    parenthetical glosses stripped) and, when it matches, adds the race's
    ability bonuses to the BASE ability scores once — every derived stat
    (AC, HP, skills) then recomputes through the normal refresh lane. This is
    the race's only mechanical footprint; speed/darkvision/traits stay pack
    display data resolved at read time. Unknown or empty race names are a
    silent no-op, so homebrew and non-D&D systems are untouched.
    """
    race = pack.resolve_race(str(getattr(sheet, "race", "") or ""))
    if race is None or not race.bonuses:
        return
    spec = pack.sheet_spec
    if spec is None:
        return
    changed = False
    for attr_name, bonus in race.bonuses.items():
        canonical = pack.resolve_skill(str(attr_name)) or str(attr_name)
        attr_key = spec.attr_keys.get(canonical)
        if not attr_key:
            continue  # not an ability score on this sheet — display-only key
        try:
            current = int(sheet_value(sheet, pack, canonical))
        except (TypeError, ValueError):
            continue
        set_sheet_value(sheet, pack, canonical, current + int(bonus))
        changed = True
    if changed:
        # Re-derive vitals from the boosted constitution (a fresh sheet has
        # nothing to preserve — same semantics as the creation refresh above).
        refresh_sheet(sheet, pack, initialize_vitals=True)


def _fill_initial_spells(sheet: CharacterSheet, pack: RulePack) -> None:
    """Fill a caster's starting known_spells from the pack's class spellbook.

    Deterministic pack data: the AI only wrote the character's class, the
    engine picks the default spells for it (iron rule: spell lists are sheet
    data, never model-generated). No spellbook match leaves the list as-is.
    """
    catalog = getattr(pack, "spells", None)
    if catalog is None or not catalog.spellbook:
        return
    class_name = str(getattr(sheet, "character_class", "") or "").strip().casefold()
    defaults = catalog.spellbook.get(class_name)
    if not defaults:
        return
    known = [str(value) for value in (sheet.known_spells or [])]
    for spell_id in defaults:
        if spell_id not in known:
            known.append(spell_id)
    sheet.known_spells = known
    # A fresh caster starts with their slot pools topped to the level table's
    # maximums (like after a long rest); locked rings stay at 0 and hide.
    from core.resources import resource_values, set_resource

    try:
        for pool_id, value in resource_values(sheet, pack).items():
            if pool_id.startswith("spell_slot_") and value.maximum and value.maximum > 0:
                set_resource(sheet, pack, pool_id, value.maximum)
    except Exception:
        pass


def _apply_persona_text(sheet: CharacterSheet, card: CharacterCard, concept: dict[str, Any], pack: RulePack) -> None:
    backstory = _as_text(concept.get("backstory"))
    if backstory:
        sheet.background = backstory
    else:
        sheet.background = card.description

    notes = [
        card.description,
        card.personality,
        card.scenario,
        _as_text(concept.get("notes")),
    ]
    sheet.notes = "\n".join(part for part in notes if part).strip()

    # Identity fields (class/occupation/race/...): the concept prompt advertises
    # them and the model returns them under any of the pack's aliases — write the
    # winning value onto the sheet's declared field. This is what lets a D&D
    # sheet carry its class, which drives the spell-slot table at creation.
    spec = pack.sheet_spec
    if spec is not None and spec.fields:
        for canonical, aliases in _IDENTITY_ALIASES.items():
            if canonical not in spec.fields:
                continue
            value = next((_as_text(concept.get(key)) for key in aliases if concept.get(key)), "")
            if value:
                # Class names normalize to the pack's canonical id ("法师" -> wizard)
                # so the spell-slot table resolves; other identity fields verbatim.
                setattr(sheet, canonical, pack.normalize_class(value) if canonical == "character_class" else value)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
