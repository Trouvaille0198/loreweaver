"""Build the WebSocket `state` frame's payload for one room (M4 spec §1).

`build_room_state` is a read-only snapshot: the caller's own active
character, the shared party roster, the game clock, the initiative order,
the current scene, and the room's rolling LLM token/cache usage. Every piece
is independently optional — a brand-new room has none of them yet — so a
missing/unset piece is simply left out of the returned dict (or reduced to
an empty list for `party`/`initiative`) instead of raising, letting
`net.tui_server.TuiServer` call this unconditionally on join and after
every turn.

`online` is left at `0` here: a room's live connection count (and which
party members are currently connected) is `TuiServer`'s concern, not this
module's — the server overlays the real numbers before broadcasting.

`resolve_active_character` (below) is the single, canonical "what character is
this caller playing right now" lookup: `gateway.turn._display_name` (the turn
echo's actor name) reuses it too, rather than re-implementing the same
lookup + `"default"`-sentinel fallback a second time, so the echoed actor name
and this module's `state.character` can never diverge on the same caller.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from agent.context import AgentCtx
from agent.npc import list_companions
from agent.services import Services
from core.character_manager import CharacterSheet, character_resources, has_character, resource_label_map
from core.combat import CombatManager, project_combat
from core.documents import (
    CLUE_LOG_ID,
    KEEPER_VIEWER,
    MODULE_POOL_ID,
    MVU_ID,
    PLAYER_VIEWER,
    SCENE_ID,
)
from core.modvars import MODVARS_DOC_ID, MODVARS_DOC_TYPE, wire_entries
from core.sheets import projected_skills
from infra.usage_stats import USAGE_STATS_KEY


async def build_room_state(
    services: Services,
    ctx: AgentCtx,
    *,
    members: list[Any] | None = None,
    claimant_name_resolver: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Assemble one `state` frame's payload (including `type`) for `ctx`'s room.

    `members` (the room hub's registered connections) lets the pregen cast resolve a
    claimer's internal member id to its display name for the wire. The optional
    `claimant_name_resolver` handles offline claims from the authentication store.
    """
    sheet = await resolve_active_character(services, ctx)
    party = await _party(services, ctx.chat_key, locale=ctx.locale)
    initiative = await _initiative(services, ctx.chat_key)
    initiative_by_name = {entry["name"]: entry["value"] for entry in initiative}

    active_name = sheet.name if sheet is not None else ""
    for member in party:
        member["active"] = bool(active_name) and member["name"] == active_name
        if member["name"] in initiative_by_name:
            member["initiative"] = initiative_by_name[member["name"]]

    state: dict[str, Any] = {"type": "state", "party": party, "initiative": initiative, "online": 0}
    combat_manager = CombatManager(services.store, ctx.chat_key)
    try:
        combat = await combat_manager.get()
    except Exception:
        combat = None
    if combat is not None:
        role = (ctx.extra.get("role") if isinstance(ctx.extra, dict) else "") or ""
        state["combat"] = project_combat(combat, keeper=role == "keeper")
    state["room_system"] = (await services.room_rulepack(ctx)).system

    # `.share` publishes a player-facing module link: the public face rides the
    # state frame so ANY member (no keeper admin round trip) can render the page.
    module_share = await services.store.state_get(ctx.chat_key, "module_share")
    if module_share:
        try:
            state["module_share"] = json.loads(module_share)
        except (TypeError, ValueError):
            pass

    if sheet is not None:
        state["character"] = await _character_payload(services, ctx.chat_key, sheet, ctx.locale)

    scene = await _scene(services, ctx.chat_key)
    if scene is not None:
        state["scene"] = scene

    clues = await _clues(services, ctx.chat_key)
    if clues:
        state["clues"] = clues

    clock = await _clock(services, ctx.chat_key)
    combat_round = await _combat_round(services, ctx.chat_key)
    if combat_round is not None:
        clock = clock or {"time": ""}
        clock["round"] = combat_round
    if clock is not None:
        state["clock"] = clock

    usage = await _usage(services, ctx.chat_key)
    if usage is not None:
        state["usage"] = usage

    variables = await _variables(services, ctx)
    if variables:
        state["variables"] = variables

    pregens = await _pregens(
        services,
        ctx.chat_key,
        members,
        claimant_name_resolver=claimant_name_resolver,
        locale=ctx.locale,
    )
    if pregens:
        state["pregens"] = pregens
    characters = await _owned_characters(services, ctx, locale=ctx.locale)
    if characters:
        state["characters"] = characters

    systems = _rule_systems()
    if systems:
        state["systems"] = systems

    image_names = await _image_names(services, ctx.chat_key)
    if image_names:
        state["image_names"] = image_names

    return state


async def _image_names(services: Services, chat_key: str) -> dict[str, list[str]]:
    """Player-visible noun lists for `.image` completions.

    NPC names and clue names from the module knowledge pool's PLAYER view —
    keeper-secret-free (iron rule #3) — plus titled pack illustrations registered
    for the active room. The client offers these as argument completions for
    `.image portrait` / `.image clue`."""
    from core.documents import MODULE_POOL_ID, PLAYER_VIEWER, DocumentStore

    docs = DocumentStore(services.store)
    try:
        pool = await docs.get_view(chat_key, "module_pool", MODULE_POOL_ID, PLAYER_VIEWER)
    except Exception:
        pool = None
    names: dict[str, list[str]] = {}
    npcs = [
        str(n.get("name") or "").strip()
        for n in ((pool or {}).get("npcs") or [])
        if str(n.get("name") or "").strip()
    ]
    if npcs:
        names["npcs"] = npcs
    clues = [
        str(c.get("name") or "").strip()
        for c in ((pool or {}).get("clues") or [])
        if str(c.get("name") or "").strip()
    ]
    if clues:
        names["clues"] = clues
    try:
        raw_media = await services.store.state_get(chat_key, "module_media_index")
        media_entries = json.loads(raw_media or "[]")
    except Exception:
        media_entries = []
    for entry in media_entries if isinstance(media_entries, list) else []:
        if not isinstance(entry, dict):
            continue
        subject = str(entry.get("subject") or "").strip()
        kind = str(entry.get("kind") or "")
        target = "npcs" if kind == "npcs" else "clues" if kind == "items" else ""
        if subject and target and subject not in names.setdefault(target, []):
            names[target].append(subject)
    return names


def _rule_systems() -> list[dict[str, str]]:
    """Every discoverable rule system, with the command word that makes a character in it.

    What a client needs to offer "create a character" WITHOUT knowing any rule system:
    the id to name and the dialect word to send. A pack that ships its own system
    therefore appears in every client's picker with no client release — the same reason
    resolution, sheets and commands are pack data (iron rule #1). Nothing secret rides
    here: the install banner prints the packs and `.help` prints their command words.

    A system with no `make_char` binding still lists (it can be imported into); it simply
    carries no word to create with. Both lookups are cached in `core.rulepacks`, so
    rebuilding this on every state snapshot costs a dict walk.

    The word advertised is one that ROUTES BACK to this pack (`own_make_char_word`): an
    `extends:` pack inherits its base's words and dispatch gives those to the base, so
    only the pack's own word (`antu` for `coc7-antu`) is its entry point; a patch that
    declares none carries no `make_char` rather than the base's.
    """
    from core.rulepacks import available_systems, load_rulepack, own_make_char_word

    entries: list[dict[str, str]] = []
    for system in available_systems():
        try:
            pack = load_rulepack(system)
        except Exception:
            continue  # a pack that will not load cannot be offered; the doctor reports it
        entry = {"id": pack.system}
        word = own_make_char_word(pack)
        if word is not None:
            entry["make_char"] = word
        entries.append(entry)
    return entries


async def resolve_active_character(services: Services, ctx: AgentCtx) -> CharacterSheet | None:
    """`ctx.uid()`'s active character for `ctx.chat_key`, or `None` when unset.

    `CharacterManager.get_character` never raises for "no character" — it
    defaults the unresolved active-character pointer to the fixed sentinel
    slot name `"default"` and returns a fresh, unsaved sheet for it — so
    "unset" here means: the lookup itself failed (best-effort — treated the
    same as unset), or the resolved sheet is that `"default"` sentinel.
    """
    try:
        sheet = await services.characters.get_character(ctx.uid(), ctx.chat_key)
    except Exception:
        return None
    if not has_character(sheet):
        return None
    return sheet


async def _character_payload(
    services: Services, chat_key: str, sheet: CharacterSheet, locale: str | None = None
) -> dict[str, Any]:
    """Protocol 2.0: vitals ride a generic ``resources`` list ({id,label,value,max})
    instead of per-system field names — a client renders meters without knowing
    any rule system. The sheet layer declares its own resources (M16 stage B:
    `core.character_manager.character_resources`, pack-driven); the WIRE shape
    is final. Labels resolve to ``locale`` here, at the per-viewer boundary (M19)."""
    attrs = _wire_attributes(sheet)
    resources = character_resources(sheet, locale)
    resource_groups: list[dict[str, Any]] = []
    pack: Any = None
    try:
        from core.resources import resource_projection
        from core.rulepacks import load_rulepack

        pack = load_rulepack(sheet.system)
        if pack.runtime_spec is not None:
            resource_groups = [
                group for group in resource_projection(sheet, pack, locale).get("groups", []) if group.get("id")
            ]
    except Exception:
        resource_groups = []
    status_effects: list[Any] = []
    try:
        roster = await services.characters.get_party_roster(chat_key)
        member = next((item for item in roster if item.get("name") == sheet.name), None)
        if member:
            status_effects = list(member.get("status_effects") or [])
    except Exception:
        pass

    payload = {
        "name": sheet.name,
        "system": sheet.system,
        "resources": resources,
        "attributes": attrs,
        "skills": projected_skills(sheet, pack) if pack is not None else _wire_skills(sheet),
        "status_effects": status_effects,
        # Retired = out of this scenario's party, sheet kept. The character
        # library renders a "join" affordance on retired cards and a "retire"
        # one on active cards; the party roster excludes them structurally.
        "retired": bool(getattr(sheet, "retired", False)),
    }
    # Character prose and the pack-declared secondary surfaces are private to
    # the owning player's sheet. They are additive wire fields: old clients
    # ignore them, while a character page can show the complete card without
    # making a second command round-trip. The portrait rides the same MediaRef
    # shape the party roster uses, so a client's one avatar renderer serves both.
    if resource_groups:
        payload["resource_groups"] = resource_groups
    avatar = getattr(sheet, "avatar", None)
    if isinstance(avatar, dict):
        payload["avatar"] = avatar
    for key in ("background", "notes"):
        value = getattr(sheet, key, "")
        if isinstance(value, str) and value.strip():
            payload[key] = value
    equipment = getattr(sheet, "equipment", [])
    if isinstance(equipment, list) and equipment:
        payload["equipment"] = list(equipment)
    items = getattr(sheet, "items", [])
    if isinstance(items, list) and items:
        payload["items"] = list(items)
    secondary = getattr(sheet, "secondary_attributes", {})
    if isinstance(secondary, dict) and secondary:
        payload["secondary_attributes"] = dict(secondary)
    fields = sheet.field_values()
    if fields:
        payload["fields"] = fields
    # The module source of a claimed pregen (which scenario this character came
    # from), read off the roster document — only pregen characters carry one.
    try:
        from core.pregen_roster import slug_for

        pregen_doc = await services.documents.get(chat_key, "pregen", slug_for(sheet.name))
        if pregen_doc is not None and pregen_doc.data.get("source"):
            payload["source"] = str(pregen_doc.data["source"])[:200]
    except Exception:
        pass
    # Character memory (player projection): the scenario-level PLAYTHROUGH
    # memories (`.settle apply` writes one per completed scenario, tagged).
    # The raw per-turn Scribe journal stays server-side (it feeds the
    # settlement lane and the AI's character context); the wire carries only
    # the tagged playthrough entries, newest first, bounded tail.
    try:
        from core.character_memory import CHARACTER_MEMORY_DOC_TYPE, project_character_memory

        memory_doc = await services.documents.get(chat_key, CHARACTER_MEMORY_DOC_TYPE, sheet.name)
        if memory_doc is not None:
            memory = project_character_memory(memory_doc, PLAYER_VIEWER) or {}
            memory_payload: dict[str, Any] = {}
            raw_entries = [
                str(entry.get("text") or "").strip()
                for entry in (memory.get("entries") or [])
                # kind=="playthrough" is the current settle format; entries
                # written by OLDER settlements carry no kind at all — both are
                # scenario-level memories the player should see.
                if isinstance(entry, dict)
                and entry.get("kind") in (None, "playthrough")
            ]
            entries = [text for text in raw_entries if text][-10:]
            entries.reverse()  # newest first, like a journal
            if entries:
                memory_payload["entries"] = entries
            if memory_payload:
                payload["memory"] = memory_payload
    except Exception:
        pass
    # The relationship tracks THIS character holds toward each named entity —
    # only non-default values ride the wire, labeled server-side by the caller.
    try:
        from core.relationships import TRACKS, RelationshipManager

        relationship_state = await RelationshipManager(services.store).load(chat_key)
        relationships: list[dict[str, Any]] = []
        for target, tracks in (relationship_state.get(sheet.name) or {}).items():
            entries: list[dict[str, Any]] = []
            for track_id, value in tracks.items():
                spec = TRACKS.get(track_id)
                if spec is None or value == spec.default:
                    continue
                entries.append({"track": str(track_id), "value": int(value)})
            if entries:
                relationships.append({"target": str(target), "tracks": entries})
        if relationships:
            payload["relationships"] = relationships
    except Exception:
        pass
    return payload


async def _owned_characters(
    services: Services, ctx: AgentCtx, *, locale: str | None = None
) -> list[dict[str, Any]]:
    """The full character roster owned by this state-frame recipient.

    Sheets are filtered by owner before they are serialized. The payload uses the
    same pack-shaped projection as the active `state.character`, including private
    notes because this list is only sent to the owning viewer.
    """
    try:
        sheets = await services.characters.list_character_sheets(ctx.uid(), ctx.chat_key)
    except Exception:
        return []
    payloads: list[dict[str, Any]] = []
    for sheet in sheets:
        try:
            payloads.append(await _character_payload(services, ctx.chat_key, sheet, locale))
        except Exception:
            continue
    return payloads

def _wire_attributes(sheet: CharacterSheet) -> dict[str, Any]:
    """`state.character.attributes`: the sheet's CHARACTERISTICS, in the pack's order.

    The stored attributes dict also carries what the sheet layer writes beside them —
    the vitals (`HP`/`SAN`/`MP` and their maxima) and derived values (`IDEA`, `KNOW`,
    …). Those are not attributes to a table: the vitals ride `resources` as meters, and
    a derived value is computed, not owned. Sending them here forced every client to
    know, per system, which keys to hide and how to order the rest — the TUI kept a
    CoC table and a D&D table for exactly that. So the wire carries the keys the pack's
    `sheet.attributes` declares, in declaration order; a pack that declares none (a
    system with no sheet spec) sends the dict as stored, since nothing else can say
    what it means.
    """
    from core.rulepacks import load_rulepack

    stored = dict(sheet.attributes)
    try:
        spec = load_rulepack(sheet.system).sheet_spec
        declared = list(spec.attributes.keys()) if spec is not None else []
    except Exception:
        declared = []
    if not declared:
        return stored
    return {key: stored[key] for key in declared if key in stored}


def _wire_skills(sheet: CharacterSheet) -> dict[str, Any]:
    """`state.character.skills`: the sheet's trained skills, name → current value.

    Skills are a long, system-specific list (a CoC sheet carries dozens of them) —
    a secondary surface, not something to paint beside the vitals. The wire sends
    the stored dict as-is: names and values come from the sheet layer, and clients
    fold them into a collapsible "skills" section of the character card instead of
    the main grid. No pack-order filtering like `_wire_attributes`: every system's
    skill list IS its storage order (there is no declaration-order concept to keep).
    """
    skills = getattr(sheet, "skills", None)
    if not isinstance(skills, dict):
        return {}
    return {str(key): value for key, value in skills.items() if value is not None}


async def _party(
    services: Services,
    chat_key: str,
    *,
    locale: str | None = None,
) -> list[dict[str, Any]]:
    """The room's whole roster, every member with their own meters.

    Mixed-system rooms are real (a module's `extends:` rulepack is a different system
    from the base it patches, and a keeper on one and a player on the other share a
    table), and nothing about the wire needs the members to agree: `resources` is the
    generic ``{id,label,value,max}`` list, its labels resolve from EACH member's own pack
    (``label_maps`` is keyed by the member's system), and every client renders one
    member's bars from that member's list alone. So no member is dropped for their
    system, and none loses their meters for it — a d20 sheet's HP bar beside a CoC
    sheet's HP/MP/SAN bars is exactly what that table looks like.
    """
    try:
        roster = await services.characters.get_party_roster(chat_key)
    except Exception:
        return []
    companion_names = await _companion_sheet_names(services, chat_key)
    label_maps: dict[str, dict[str, str]] = {}
    members: list[dict[str, Any]] = []
    for member in roster:
        payload = {
            "name": member.get("name", ""),
            "online": True,
            "active": False,
            # M10: tag AI-companion party members so clients can render an "AI" badge.
            "ai": member.get("name", "") in companion_names,
        }
        avatar = member.get("avatar")
        if isinstance(avatar, dict):
            payload["avatar"] = avatar
        for key in (
            "system",
            "attributes",
            "secondary_attributes",
            "fields",
            "equipment",
            "items",
            "background",
            "status_effects",
        ):
            value = member.get(key)
            if value not in (None, "", [], {}):
                payload[key] = value
        # Skills: stored (trained) values plus the recomputed derived skills —
        # a fully-derived system (D&D 5e) never persists its skills, so a plain
        # copy would show an empty panel in the party view.
        try:
            from core.rulepacks import load_rulepack

            system = str(member.get("system", "") or "")
            skills = projected_skills(member, load_rulepack(system) if system else None)
        except Exception:
            skills = None
        if skills:
            payload["skills"] = skills
        system = str(member.get("system", "") or "")
        if system not in label_maps:
            label_maps[system] = resource_label_map(system, locale)
        # Grouped pools (spell slots, hit dice) ride the same wire shape the
        # character card uses, so ANY detail view can show them — rebuilt from
        # the roster's public sheet fields through the pack projection.
        try:
            from core.resources import resource_projection
            from core.rulepacks import load_rulepack

            member_pack = load_rulepack(system) if system else None
            if member_pack is not None:
                member_sheet = CharacterSheet.from_dict(dict(member))
                groups = [
                    group
                    for group in resource_projection(member_sheet, member_pack, locale).get("groups", [])
                    if group.get("id")
                ]
                if groups:
                    payload["resource_groups"] = groups
        except Exception:
            pass
        members.append(payload)
    return members


def _party_member_resources(member: dict[str, Any], labels: dict[str, str]) -> list[dict[str, Any]]:
    """Protocol 2.0 party vitals: the same generic ``resources`` list shape as
    ``state.character`` -- read straight off the roster entry. M17:
    `CharacterManager.sync_party_roster` already stores the pack-declared
    meter list (`core.character_manager.character_resources`) verbatim; this
    only validates the wire shape survived the JSON round-trip.

    M19: the STORED label froze whatever locale was current when the roster was
    synced, so ``labels`` (this viewer's, from the member's own system) wins when it
    knows the id; the stored string stays the fallback for a system that no longer
    resolves to a pack."""
    resources: list[dict[str, Any]] = []
    for entry in member.get("resources") or []:
        if not isinstance(entry, dict):
            continue
        res_id, label = entry.get("id"), entry.get("label")
        if not res_id or not label:
            continue
        value = _int_value(entry.get("value"))
        maximum = _int_value(entry.get("max"))
        if value is None or maximum is None:
            continue
        resources.append({"id": res_id, "label": labels.get(res_id, label), "value": value, "max": maximum})
    return resources


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


async def _companion_sheet_names(services: Services, chat_key: str) -> set[str]:
    """Character-sheet names belonging to AI player companions in this room (best-effort, may be empty)."""
    try:
        records = await list_companions(services.documents, chat_key)
    except Exception:
        return set()
    return {record.stat_char or record.name for record in records}


async def _initiative(services: Services, chat_key: str) -> list[dict[str, Any]]:
    try:
        combat = await CombatManager(services.store, chat_key).get()
    except Exception:
        combat = None
    if combat is not None:
        return [
            {
                "name": str(combat.combatants[combatant_id].get("name") or combatant_id),
                "value": int(combat.combatants[combatant_id].get("initiative", 0) or 0),
                "current": combatant_id == combat.current,
            }
            for combatant_id in combat.order
        ]
    # Rooms created before the runtime state contract retain their check-only
    # initiative display until an explicit runtime migration is requested.
    try:
        raw = await services.store.state_get(chat_key, "initiative")
        entries = json.loads(raw) if raw else []
    except Exception:
        return []
    if not isinstance(entries, list):
        return []
    return [
        {"name": entry.get("name", ""), "value": entry.get("init", 0), "current": index == 0}
        for index, entry in enumerate(entries)
        if isinstance(entry, dict)
    ]


async def _clues(services: Services, chat_key: str) -> list[dict[str, Any]] | None:
    """The room's discovered-clue log (player projection). Every entry in the log
    is a clue the table has already found, so the player view is the whole list —
    an unrevealed secret clue never exists here at all."""
    try:
        view = await services.documents.get_view(chat_key, "clue_log", CLUE_LOG_ID, PLAYER_VIEWER)
    except Exception:
        view = None
    clues = (view or {}).get("clues")
    if not (isinstance(clues, list) and clues):
        return None
    # Clue log is shared across the room's scenario switches; each entry carries the
    # module it was discovered in, and the projection shows only the CURRENT module's
    # clues (sandbox rooms with no module keep every entry).
    try:
        from agent.module_lifecycle import active_module

        active = await active_module(services, chat_key)
        module = str(active.get("pack_id") or active.get("source_id") or "") if active else ""
    except Exception:
        module = ""
    if module:
        clues = [c for c in clues if str(c.get("module") or "") == module]
        if not clues:
            return None
    return clues


async def _scene_image(services: Services, chat_key: str, name: str) -> dict[str, Any] | None:
    """The enabled packs' illustration whose `title` names THIS scene, as a wire
    `{hash, mime, name}` ref — the scene strip's thumbnail. Scene plates ship with
    their scene's name as the asset title (深水城/漫游塔/…), so the match is the
    name itself; a `scenes`-kind plate (by the `module-<id>-<kind>-<n>` provenance
    stem) wins over any other kind. Metadata only: the client pulls bytes through
    the content-addressed asset channel, which serves ENABLED packs' assets to the
    whole table."""
    from pathlib import PurePosixPath

    from gateway.panels import enabled_packs

    wanted = name.strip()
    if not wanted:
        return None
    fallback: dict[str, Any] | None = None
    for _pack_id, _home, manifest in await enabled_packs(services, chat_key):
        for asset in manifest.assets:
            if not str(asset.mime or "").lower().startswith("image/"):
                continue
            # Scene plates ship with the scene's short name as the asset title
            # (深水城/漫游塔/…), while `scene.name` is the fuller display form
            # ("中央广场·仲夏展台（深水城港口区）") — match by containment so
            # the thumbnail resolves instead of silently vanishing.
            asset_title = (asset.title or "").strip()
            if not asset_title or asset_title not in wanted:
                continue
            ref = {"hash": asset.sha256, "mime": asset.mime, "name": PurePosixPath(asset.path).name}
            if "scenes" in PurePosixPath(asset.path).stem:
                return ref
            fallback = fallback or ref
    return fallback


async def _scene(services: Services, chat_key: str) -> dict[str, Any] | None:
    """The `scene` singleton document (all-viewer projection), falling back to
    the module pool's first PLAYER-visible scene for rooms the keeper hasn't
    scened yet."""
    try:
        view = await services.documents.get_view(chat_key, "scene", SCENE_ID, PLAYER_VIEWER)
    except Exception:
        view = None
    name = (view or {}).get("name")
    if name:
        scene: dict[str, Any] = {"name": name}
        focus = (view or {}).get("focus")
        if focus:
            scene["focus"] = focus
        image = await _scene_image(services, chat_key, str(name))
        if image is not None:
            scene["image"] = image
        return scene

    try:
        pool = await services.documents.get_view(chat_key, "module_pool", MODULE_POOL_ID, PLAYER_VIEWER)
    except Exception:
        pool = None

    scenes = (pool or {}).get("scenes")
    if scenes:
        first = scenes[0]
        scene = {"name": first.get("name", "")}
        if first.get("focus"):
            scene["focus"] = first["focus"]
        image = await _scene_image(services, chat_key, str(scene["name"]))
        if image is not None:
            scene["image"] = image
        return scene
    return None


async def _clock(services: Services, chat_key: str) -> dict[str, Any] | None:
    try:
        raw = await services.store.state_get(chat_key, "game_clock")
        clock = json.loads(raw) if raw else {}
    except Exception:
        clock = {}

    time_value = clock.get("current_time") if isinstance(clock, dict) else None
    return {"time": time_value} if time_value else None


async def _combat_round(services: Services, chat_key: str) -> int | None:
    try:
        combat = await CombatManager(services.store, chat_key).get()
    except Exception:
        combat = None
    if combat is not None:
        return combat.round if combat.round > 0 else None
    try:
        raw = await services.store.state_get(chat_key, "initiative_meta")
        meta = json.loads(raw) if raw else {}
        value = int(meta.get("round", 0)) if isinstance(meta, dict) else 0
    except Exception:
        return None
    return value if value > 0 else None


_MVU_PANEL_CAP = 32
_KEEPER_ROLE = "keeper"
# The single-operator platform set (mirrors `gateway.commands.rooms._AUTO_MASTER_PLATFORMS`): a
# `--cli` session is the box's owner running their own table, keeper by construction.
_LOCAL_OPERATOR_PLATFORMS = {"cli"}


def _viewer_is_keeper(ctx: AgentCtx) -> bool:
    """Whether THIS state frame's recipient is the keeper: the local operator platform, or a
    connection whose keystore-authenticated role was threaded into ``ctx.extra["role"]`` by
    `gateway.turn.publish_state` (networked members) / `net.session._ctx_for` (commands)."""
    if ctx.platform in _LOCAL_OPERATOR_PLATFORMS:
        return True
    extra = ctx.extra if isinstance(ctx.extra, dict) else {}
    return extra.get("role") == _KEEPER_ROLE


async def _variables(services: Services, ctx: AgentCtx) -> list[dict[str, Any]]:
    """Module variables for THIS viewer (state frames are built per member), on one wire shape.

    Both sources are consumed as DOCUMENT PROJECTIONS — the one structural
    visibility discipline (iron rule #3, fail-closed):

    - the `modvars` document's PLAYER projection drops keeper-only trackers spec and
      value, so no state frame — any viewer, any transport — ever carries them (the
      panel deliberately shows the player set even to the keeper; keeper-only values
      live in the keeper's prompt, not the HUD);
    - the `mvu_tree` document's player projection ships ONLY keeper-exposed leaves
      (`.var expose <prefix>`); the keeper projection carries every leaf tagged with
      its exposure, so a keeper viewer sees the unexposed remainder flagged
      ``"hidden": true`` and can watch their module's internals live.

    Empty (→ field omitted) when the room has neither; best-effort like every other piece of
    this snapshot.
    """
    try:
        modvar_view = await services.documents.get_view(
            ctx.chat_key, MODVARS_DOC_TYPE, MODVARS_DOC_ID, PLAYER_VIEWER
        )
        entries = wire_entries(modvar_view or {}, ctx.locale)
    except Exception:
        entries = []
    try:
        keeper_view = _viewer_is_keeper(ctx)
        viewer = KEEPER_VIEWER if keeper_view else PLAYER_VIEWER
        mvu_view = await services.documents.get_view(ctx.chat_key, "mvu_tree", MVU_ID, viewer)
        # The projection already filtered a player's leaves (fail-closed) and tagged the
        # keeper's with per-leaf exposure; the panel cap applies to what the viewer SEES.
        shown = 0
        for leaf in (mvu_view or {}).get("leaves", []):
            if shown >= _MVU_PANEL_CAP:
                break
            value = leaf["value"]
            if isinstance(value, bool):
                kind = "bool"
            elif isinstance(value, (int, float)):
                kind = "number"
            elif isinstance(value, str):
                kind = "text"
            else:
                continue  # nested/list leaves are prompt-side detail, not panel material
            entry: dict[str, Any] = {"id": f"mvu.{leaf['path']}", "label": leaf["path"], "kind": kind, "value": value}
            if keeper_view and not leaf.get("exposed", False):
                entry["hidden"] = True
            entries.append(entry)
            shown += 1
    except Exception:
        pass
    return entries


async def _pregens(
    services: Services,
    chat_key: str,
    members: list[Any] | None = None,
    *,
    claimant_name_resolver: Callable[[str], str] | None = None,
    locale: str = "en",
) -> list[dict[str, Any]]:
    """The claimable pregen cast, v1.9 additive: one ``{name, claimed_by, …sheet}`` per
    entry, insertion-ordered, consumed from the `pregen` documents' PLAYER projection
    (the cast list is table talk). Omitted (never an empty list) for roster-less rooms.
    Best-effort like the rest of this snapshot.

    Each entry carries the pregen's PUBLIC sheet fields — attributes / skills / fields /
    background / avatar — so the roster can open the same detail dialog a claimed party
    member opens, without a claim. The derived one-liner (`blurb`) and the pristine
    sheet internals (equipment, items, notes) deliberately do NOT ride the wire: they
    are claim-time copies, not cast-table data.

    `claimed_by` goes out as the claiming member's DISPLAY NAME: clients render it
    verbatim ("已被 {name} 认领") and compare it against ``welcome.you.name`` to mark
    "yours" — a raw internal member id (``tui:…``) would read badly and never match.
    The claim-time display name (``claimed_name``) is authoritative and survives the
    claimer going offline; the room hub's members and authentication store are fallbacks.
    Internal member ids are never exposed in a player-facing frame."""
    try:
        pairs = await services.documents.list_views(chat_key, "pregen", PLAYER_VIEWER)
    except Exception:
        return []
    member_names = {
        str(getattr(member, "id", "")): str(getattr(member, "name", "") or "")
        for member in (members or [])
        if getattr(member, "id", None)
    }

    def wire_claimer(view: dict[str, Any]) -> str:
        claimed_by = str(view.get("claimed_by", ""))
        if not claimed_by:
            return ""
        resolved_name = ""
        if claimant_name_resolver is not None:
            try:
                resolved_name = str(claimant_name_resolver(claimed_by) or "")
            except Exception:
                resolved_name = ""
        return (
            str(view.get("claimed_name", ""))
            or member_names.get(claimed_by, "")
            or resolved_name
            or ("玩家" if str(locale).lower().startswith("zh") else "player")
        )

    entries: list[dict[str, Any]] = []
    for _doc, view in pairs:
        name = str(view.get("name", ""))
        if not name:
            continue
        entry = {"name": name, "claimed_by": wire_claimer(view)}
        # Where the character came from — a module import, `.pc gen` (`room`), or a
        # card import — rides the roster so the client can tell a room-born character
        # from a module's own cast (the delete gate reads it).
        source = str(view.get("source", ""))
        if source:
            entry["source"] = source
        # Public sheet fields, mirroring the `PartyMember` shape a claimed character
        # already exposes (protocol types.ts): enough to render the detail dialog.
        sheet = (_doc.data or {}).get("sheet") if isinstance(getattr(_doc, "data", None), dict) else None
        if isinstance(sheet, dict):
            for key in (
                "system",
                "attributes",
                "secondary_attributes",
                "fields",
                "background",
                "avatar",
            ):
                value = sheet.get(key)
                if value not in (None, "", {}):
                    entry[key] = value
            # Grouped pools (spell slots) so a pregen's detail dialog shows its
            # caster resources like any claimed character's.
            try:
                from core.resources import resource_projection
                from core.rulepacks import load_rulepack

                system = str(sheet.get("system", "") or "")
                pack = load_rulepack(system) if system else None
                if pack is not None:
                    pregen_sheet = CharacterSheet.from_dict(dict(sheet))
                    groups = [
                        group
                        for group in resource_projection(pregen_sheet, pack, locale).get("groups", [])
                        if group.get("id")
                    ]
                    if groups:
                        entry["resource_groups"] = groups
            except Exception:
                pass
        entries.append(entry)
    return entries


async def _usage(services: Services, chat_key: str) -> dict[str, Any] | None:
    """The room's rolling token/cache usage aggregate (`infra.usage_stats.record_usage_stats`
    writes it), translated to the wire's snake_case shape -- `None` when unset (a
    brand-new room, or one that has never completed a real AI-KP turn), so
    `build_room_state` leaves `state.usage` out entirely rather than sending zeros.

    The stored `last` block also records whether its `prompt` figure was MEASURED by
    the provider or ESTIMATED by `agent.loop` (an endpoint that reports no usage on a
    streamed turn). That flag deliberately does NOT cross the wire: describing it
    would be an additive protocol field, and the version bump that entitles one is a
    heavier, owner-facing change than the meter warrants. Nothing is lost by keeping
    it server-side — the only consumer that ACTS on the number is the chronicle fold,
    which reads the stored payload directly. What the HUD renders is a fullness
    percentage, and it was already an approximation in both sources: the meter is the
    previous turn's prompt, and the denominator is a table lookup. Before this, a
    streaming room had NO usage block at all; an approximate meter is what it gains.
    The `session` totals stay measured-only, so a room whose provider never reports
    honestly shows a context figure with zero cumulative tokens beside it.
    """
    try:
        raw = await services.store.state_get(chat_key, USAGE_STATS_KEY)
        stats = json.loads(raw) if raw else {}
    except Exception:
        stats = {}

    if not isinstance(stats, dict) or not stats:
        return None

    last = stats.get("last")
    last = last if isinstance(last, dict) else {}
    session = stats.get("session")
    session = session if isinstance(session, dict) else {}

    return {
        "context_tokens": last.get("prompt", 0),
        "context_window": last.get("context_window", 0),
        "input_tokens": session.get("prompt", 0),
        "output_tokens": session.get("completion", 0),
        "cache_hit_tokens": session.get("cache_hit", 0),
        "cache_miss_tokens": session.get("cache_miss", 0),
    }
