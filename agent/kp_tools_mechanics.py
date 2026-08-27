"""AI-KP tools: character sheets, dice/skill checks, and initiative tracking.

Ported from ``nekro_trpg_dice_plugin``'s ``trpg_dice/plugin.py`` sandbox
methods (``create_character``, ``get_character_sheet``, ``skill_check``, ...
``initiative_tracker``) per ``docs/specs/M1.md`` §6.3. Each tool BODY is kept
faithful to the source; only the wiring changes:

- ``@plugin.mount_sandbox_method(...)`` -> ``@tool(...)`` (source AGENT /
  BEHAVIOR method types both collapse to a plain tool - none of the tools in
  this module are ``keeper_only``);
- ``_ctx: AgentCtx`` -> our ``ctx: AgentCtx``; user id via ``ctx.uid()``;
- managers/dice/store come from the injected ``Services`` bundle
  (``self.services.characters`` / ``.dice`` / ``.battles`` / ``.store`` /
  ``.i18n``), never module globals;
- ``DiceRoller.roll_expression(...)``-style staticmethod calls become
  ``self.services.dice.roll_expression(...)`` instance calls - the ported
  ``core.dice_engine.DiceRoller`` requires an instance (see its module
  docstring);
- check grading goes through the sheet system's COMPILED rulepack resolver
  (`core.resolution`): the engine rolls (`DiceRoller.roll_for_check`), the
  pack ladder interprets, and labels render via `RulePack.rank_label` — this
  module never re-implements a success ladder, never names a rule system, and
  only ever branches on the outcome contract's semantic flags plus the pack's
  declared shapes (`resolver.target_kind`, `resolver.check`, sheet spec).
  Name resolution (attribute aliases, bridged skills) is the pack alias table;
  check inputs read through `core.sheets.check_value`.

Every user-visible string is localized via ``self.services.i18n`` (see
``locales/{en,zh}/kp_tools.json``). CJK/EN game-data literals - the
``random_madness`` symptom tables - are exempt from i18n, the same convention
``core`` already uses (see ``core/prompt_sections.py``'s module docstring).
"""

from __future__ import annotations

import json

from agent.clue_log import find_worldbook_clue as _find_clue
from agent.clue_log import reveal_clue as _log_clue
from agent.tool_trace import active_module_id
from agent.context import AgentCtx
from agent.items import (
    aggregate_equipped_bonuses,
    canonicalize_bonus_keys,
    catalog_template,
    consume_instance,
    find_instance,
    get_item_catalog,
    grant_improvised_instance,
    grant_instance,
    improvised_template,
    instances_for_owner,
    item_active,
    module_source_id,
    parse_bonus_spec,
    render_held_items,
    render_item_views,
    reveal_linked_clues,
    set_equipped,
    template_is_consumable,
    template_with_source,
    validate_improvised_bonus,
)
from agent.module_lifecycle import active_module
from agent.npc import list_companions, sheet_reference
from agent.services import Services, room_rule_variant
from agent.tools import tool
from core.battle_recording import record_check, record_dice_roll
from core.battle_report import NPC_USER_ID
from core.character_manager import (
    CharacterDataError,
    CharacterSheet,
    character_resources,
    get_hit_points,
    has_character,
    set_hit_points,
)
from core.character_rules import render_validation_notice, validate_sheet
from core.check_outcome import CheckOutcome, outcome_wire
from core.check_roll import favor_modifiers, graded_roll
from core.combat import CombatManager, claim_turn, create_combat, end_combat, end_turn, join_combat, start_combat
from core.dice_engine import DiceResult
from core.rulepacks import RulePack, load_rulepack
from core.sheets import check_value, has_check_value, set_sheet_value, sheet_value
from infra.i18n import I18n
from infra.room_facets import STORAGE_ROOM_STATE, RoomStateFacet


async def _get_active_character(services: Services, ctx: AgentCtx) -> CharacterSheet:
    """Fetch `ctx`'s active character (a fresh, unsaved `"default"`-named sheet if none exists)."""
    return await services.characters.get_character(ctx.uid(), ctx.chat_key)


async def _sheet_pack(services: Services, ctx: AgentCtx, character: CharacterSheet) -> RulePack:
    """The rulepack governing `character`: its own system when resolvable,
    falling back to the room's active pack (bare/unset sheets)."""

    try:
        return load_rulepack(character.system)
    except Exception:
        return await services.room_rulepack(ctx)


def _characteristic_lines(sheet: CharacterSheet, i18n: I18n, locale: str | None) -> tuple[list[str], list[str]]:
    """A sheet's declared characteristics and its vital meters as text lines — the ONE
    rendering both `get_character_sheet` (the actor's sheet) and `list_party_sheets` (the
    whole table) print, so what the keeper reads is the same list either way. The pack's
    `sheet.attributes` selection in the pack's own order — the same list
    `state.character.attributes` puts on the wire — falling back to every stored key when
    the pack is unknown."""
    attrs = sheet.attributes
    try:
        spec = load_rulepack(sheet.system).sheet_spec
    except Exception:
        spec = None
    attribute_lines = [
        i18n.t("kp_tools.character.sheet.attr_line", attr=key, value=attrs[key])
        for key in (spec.attributes if spec is not None else attrs)
        if key in attrs
    ]
    meter_lines = [
        i18n.t("kp_tools.character.sheet.meter_line", label=meter["label"], value=meter["value"], max=meter["max"])
        for meter in character_resources(sheet, locale)
    ]
    return attribute_lines, meter_lines


async def _refresh_character_bonuses(
    services: Services, ctx: AgentCtx, char_name: str, owner_uid: str
) -> None:
    """Recompute `char_name`'s `equipped_bonuses` AND its display `equipment` list from
    its item instances, then persist. Called by every item mutation so checks/dice read
    the same bonuses and clients (via the roster) see the same items as the sheet."""
    try:
        items = await instances_for_owner(services.documents, ctx.chat_key, char_name)
        active = await active_module(services, ctx.chat_key)
        bonuses = aggregate_equipped_bonuses(items, active)
        sheet = await services.characters.get_character(owner_uid, ctx.chat_key, char_name)
        if not has_character(sheet):
            return
        sheet.equipped_bonuses = bonuses
        sheet.equipment = render_held_items(items)
        sheet.items = render_item_views(items)
        await services.characters.save_character(owner_uid, ctx.chat_key, sheet)
    except Exception:
        # A bonus refresh failure must never roll back the item operation itself.
        return


async def _resolve_actor_identity(
    services: Services,
    ctx: AgentCtx,
    active_name: str,
    actor: str | None,
) -> tuple[str, bool]:
    """Return the canonical actor name and whether it is outside the player roster."""
    actor_name = (actor or "").strip()
    if not actor_name:
        return active_name, False

    roster_names = {active_name.casefold(): active_name} if active_name else {}
    try:
        roster = await services.characters.get_party_roster(ctx.chat_key)
        roster_names.update(
            {
                str(member.get("name", "")).strip().casefold(): str(member.get("name", "")).strip()
                for member in roster
                if isinstance(member, dict) and str(member.get("name", "")).strip()
            }
        )
    except Exception:
        pass
    matched_name = roster_names.get(actor_name.casefold())
    return (matched_name, False) if matched_name else (actor_name, True)


class CharacterTools:
    """AI-KP tools for creating, inspecting and mutating player character sheets."""

    def __init__(self, services: Services) -> None:
        self.services = services

    @tool(prep_only=True)
    async def create_character(
        self, ctx: AgentCtx, name: str, system: str = "", auto_generate: bool = True
    ) -> str:
        """Create a new TRPG character sheet.

        Args:
            name: Character name.
            system: Rule system id; omit to use the room's active system.
            auto_generate: Whether to auto-roll attributes per the system's rules.
        """
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            if system.strip():
                pack = load_rulepack(system)
            else:

                pack = await self.services.room_rulepack(ctx)

            if auto_generate:
                character = self.services.characters.generate_character(pack.system, name)
            else:
                character = CharacterSheet(name=name, system=pack.system)

            character, violations = validate_sheet(
                character,
                pack.system,
                initialize_vitals=True,
                creation_method="rolled" if auto_generate else None,
            )
            await self.services.characters.save_character(ctx.uid(), ctx.chat_key, character)

            spec = pack.sheet_spec
            source_keys = list(spec.attributes) if spec is not None else list(character.attributes)
            attributes_str = ", ".join(
                f"{key} {character.attributes[key]}" for key in source_keys if key in character.attributes
            )
            meters_str = " | ".join(
                f"{meter['label']} {meter['value']}/{meter['max']}" for meter in character_resources(character)
            )
            result = i18n.t(
                "kp_tools.character.create.success",
                name=character.name,
                system=character.system,
                attributes=attributes_str,
                meters=meters_str or i18n.t("common.none"),
            )
            notice = render_validation_notice(i18n, violations)
            return f"{result}\n{notice}" if notice else result
        except Exception as exc:
            return i18n.t("kp_tools.character.create.failed", error=str(exc))

    @tool(read_only=True)
    async def get_character_sheet(self, ctx: AgentCtx) -> str:
        """Get the current user's character sheet details."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            character = await _get_active_character(self.services, ctx)
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        if not has_character(character):
            return i18n.t("kp_tools.character.none")

        lines = [
            i18n.t("kp_tools.character.sheet.title", name=character.name),
            i18n.t("kp_tools.character.sheet.system_line", system=character.system),
        ]

        try:
            pack = load_rulepack(character.system)
        except Exception:
            pack = None
        spec = pack.sheet_spec if pack is not None else None

        attribute_lines, meter_lines = _characteristic_lines(character, i18n, ctx.locale)
        if attribute_lines:
            lines.append("")
            lines.append(i18n.t("kp_tools.character.sheet.attributes_header"))
            lines.extend(attribute_lines)

        if meter_lines:
            lines.append("")
            lines.append(i18n.t("kp_tools.character.sheet.status_header"))
            lines.extend(meter_lines)
        elif spec is None:
            hp, hp_max = get_hit_points(character)
            if hp_max:
                lines.append("")
                lines.append(i18n.t("kp_tools.character.sheet.status_header"))
                lines.append(i18n.t("kp_tools.character.sheet.meter_line", label="HP", value=hp, max=hp_max))

        field_lines = [
            i18n.t("kp_tools.character.sheet.field_line", name=name, value=value)
            for name, value in character.field_values().items()
            if value not in (None, "")
        ]
        if field_lines:
            lines.append("")
            lines.extend(field_lines)

        skill_entries = dict(character.skills)
        if pack is not None and spec is not None:
            # Untrained derived skills are not stored; surface their computed
            # values so the sheet reads complete.
            for skill_key in spec.derived_skills:
                if skill_key not in skill_entries:
                    skill_entries[skill_key] = sheet_value(character, pack, skill_key)
        if skill_entries:
            lines.append("")
            lines.append(i18n.t("kp_tools.character.sheet.skills_header"))
            for skill, value in sorted(skill_entries.items(), key=lambda item: item[1], reverse=True):
                lines.append(i18n.t("kp_tools.character.sheet.skill_line", skill=skill, value=value))

        if character.equipment:
            lines.append("")
            lines.append(
                i18n.t("kp_tools.character.sheet.equipment_line", equipment=", ".join(character.equipment))
            )
        if character.background:
            lines.append("")
            lines.append(i18n.t("kp_tools.character.sheet.background_line", background=character.background))
        if character.notes:
            lines.append("")
            lines.append(i18n.t("kp_tools.character.sheet.notes_line", notes=character.notes))

        return "\n".join(lines)

    @tool(read_only=True)
    async def list_party_sheets(self, ctx: AgentCtx) -> str:
        """Every character sheet at this table — the WHOLE party, not only whoever is acting.

        The one sheet tool that crosses the acting-player boundary, and read-only for that
        reason. Every other one (get_character_sheet, update_character_attribute, …) acts on
        the member whose turn it is, so without this a second player's numbers are invisible
        to you — a module that asks for per-character bookkeeping (a daily dosage ledger, who
        is nearest a threshold) cannot be run from one seat. Shows each member's declared
        characteristics and vital meters, not their skills.

        To CHANGE one of these, narrate the new ABSOLUTE value and let that player set it on
        their own turn (`.st <key>=<value>`); writes never cross the boundary.

        Returns:
            One block per party member: name, rule system, characteristics, meters.
        """
        i18n = self.services.i18n.with_locale(ctx.locale)
        characters = self.services.characters
        try:
            roster = await characters.get_party_roster(ctx.chat_key)
        except Exception as exc:
            return i18n.t("kp_tools.character.list.failed", error=str(exc))
        try:
            companions = {
                sheet_reference(record) or record.name
                for record in await list_companions(self.services.documents, ctx.chat_key)
            }
        except Exception:
            companions = set()

        blocks: list[str] = []
        for member in roster:
            name = str(member.get("name") or "").strip()
            if not name:
                continue
            try:
                sheet = await characters.get_character(ctx.uid(), ctx.chat_key, name)
            except CharacterDataError:
                continue  # one unreadable row must not cost the keeper the whole roster
            if not has_character(sheet):
                continue
            attribute_lines, meter_lines = _characteristic_lines(sheet, i18n, ctx.locale)
            header = i18n.t(
                "kp_tools.character.party.member",
                name=sheet.name,
                system=sheet.system,
                ai=i18n.t("kp_tools.character.party.ai") if sheet.name in companions else "",
            )
            blocks.append("\n".join([header, *attribute_lines, *meter_lines]))

        if not blocks:
            return i18n.t("kp_tools.character.party.empty")
        return "\n".join(
            [i18n.t("kp_tools.character.party.header", count=len(blocks)), *blocks, i18n.t("kp_tools.character.party.write_hint")]
        )

    @tool(prep_only=True)
    async def update_character_skill(self, ctx: AgentCtx, skill_name: str, value: int) -> str:
        """Update a character's skill value.

        Args:
            skill_name: Skill name (CN/EN aliases supported, e.g. "侦查" or "spot hidden").
            value: The new skill value.
        """
        i18n = self.services.i18n.with_locale(ctx.locale)
        characters = self.services.characters
        try:
            character = await _get_active_character(self.services, ctx)
            if not has_character(character):
                return i18n.t("kp_tools.character.none")

            pack = await _sheet_pack(self.services, ctx, character)
            canonical = pack.resolve_skill(skill_name) or skill_name

            had_value = has_check_value(character, pack, canonical)
            old_value = sheet_value(character, pack, canonical) if had_value else i18n.t("kp_tools.character.value_unset")
            set_sheet_value(character, pack, canonical, value)
            character, violations = validate_sheet(character, pack.system)
            new_value = sheet_value(character, pack, canonical)
            target_skill = canonical

            await characters.save_character(ctx.uid(), ctx.chat_key, character)

            result = i18n.t(
                "kp_tools.character.skill.updated", name=character.name, skill=target_skill, old=old_value, new=new_value
            )
            notice = render_validation_notice(i18n, violations)
            return f"{result}\n{notice}" if notice else result
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        except Exception as exc:
            return i18n.t("kp_tools.character.skill.failed", error=str(exc))

    @tool(prep_only=True)
    async def update_character_attribute(self, ctx: AgentCtx, attribute: str, value: int) -> str:
        """Update a character's attribute value.

        Args:
            attribute: Attribute name (e.g. STR, DEX, POW).
            value: The new attribute value.
        """
        i18n = self.services.i18n.with_locale(ctx.locale)
        characters = self.services.characters
        try:
            character = await _get_active_character(self.services, ctx)
            if not has_character(character):
                return i18n.t("kp_tools.character.none")

            pack = await _sheet_pack(self.services, ctx, character)
            hp_field = attribute.strip().upper()
            canonical = pack.resolve_skill(attribute)
            if hp_field in {"HP", "HPMAX"}:
                hp, hp_max = get_hit_points(character)
                old_value = hp if hp_field == "HP" else hp_max
                if hp_field == "HP":
                    set_hit_points(character, current=value)
                else:
                    set_hit_points(character, maximum=value)
            elif canonical:
                old_value = sheet_value(character, pack, canonical)
                set_sheet_value(character, pack, canonical, value)
            else:
                old_value = character.attributes.get(attribute, i18n.t("kp_tools.character.value_unset"))
                character.attributes[attribute] = value

            character, violations = validate_sheet(character, pack.system)
            if hp_field in {"HP", "HPMAX"}:
                hp, hp_max = get_hit_points(character)
                new_value = hp if hp_field == "HP" else hp_max
            elif canonical:
                new_value = sheet_value(character, pack, canonical)
            else:
                new_value = character.attributes.get(attribute, value)

            await characters.save_character(ctx.uid(), ctx.chat_key, character)

            result = i18n.t(
                "kp_tools.character.attribute.updated",
                name=character.name,
                attribute=attribute,
                old=old_value,
                new=new_value,
            )
            notice = render_validation_notice(i18n, violations)
            return f"{result}\n{notice}" if notice else result
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        except Exception as exc:
            return i18n.t("kp_tools.character.attribute.failed", error=str(exc))

    @tool(read_only=True)
    async def list_characters(self, ctx: AgentCtx) -> str:
        """List all of the user's character sheets."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            characters = await self.services.characters.list_characters(ctx.uid(), ctx.chat_key)
            if not characters:
                return i18n.t("kp_tools.character.list.empty")

            lines = [i18n.t("kp_tools.character.list.header")]
            for index, char in enumerate(characters, 1):
                lines.append(
                    i18n.t("kp_tools.character.list.item", index=index, name=char["name"], system=char["system"])
                )
            return "\n".join(lines)
        except Exception as exc:
            return i18n.t("kp_tools.character.list.failed", error=str(exc))

    @tool(prep_only=True)
    async def switch_character(self, ctx: AgentCtx, name: str) -> str:
        """Switch to a different character sheet.

        Args:
            name: The character name to switch to.
        """
        i18n = self.services.i18n.with_locale(ctx.locale)
        characters = self.services.characters
        try:
            character = await characters.get_character(ctx.uid(), ctx.chat_key, name)
            if character.name == "default" and name != "default":
                return i18n.t("kp_tools.character.switch.not_found", name=name)

            # Only sheets the CALLING user owns are switchable. Without this the AI KP,
            # running in the acting player's ctx, can re-point that player's active sheet
            # to a companion/NPC it wants to see act (observed in live play) — silently
            # hijacking the player's character.
            owned = await characters.list_characters(ctx.uid(), ctx.chat_key)
            if not any(entry.get("name") == character.name for entry in owned):
                return i18n.t("kp_tools.character.switch.not_found", name=name)

            await characters.set_active_character(ctx.uid(), ctx.chat_key, name)
            return i18n.t("kp_tools.character.switch.success", name=character.name, system=character.system)
        except Exception as exc:
            return i18n.t("kp_tools.character.switch.failed", error=str(exc))

    @tool(prep_only=True)
    async def delete_character(self, ctx: AgentCtx, name: str) -> str:
        """Delete the named character sheet.

        Args:
            name: The character name to delete.
        """
        i18n = self.services.i18n.with_locale(ctx.locale)
        characters = self.services.characters
        try:
            # Sheets are room-scoped documents keyed by the character NAME, so a bare
            # name reaches every player's sheet. Only the CALLING user's own characters
            # are deletable — same ownership gate `switch_character` carries above.
            owned = await characters.list_characters(ctx.uid(), ctx.chat_key)
            if not any(entry.get("name") == name for entry in owned):
                return i18n.t("kp_tools.character.delete.not_yours", name=name)

            success = await characters.delete_character(ctx.uid(), ctx.chat_key, name)
            if success:
                return i18n.t("kp_tools.character.delete.success", name=name)
            return i18n.t("kp_tools.character.delete.failed_generic", name=name)
        except Exception as exc:
            return i18n.t("kp_tools.character.delete.failed", error=str(exc))

    @tool
    async def update_character_status(self, ctx: AgentCtx, status_effects: str) -> str:
        """Update the active character's status effects (poisoned, afraid, injured, insane, ...).

        Args:
            status_effects: A JSON array of status strings, e.g. '["Poisoned", "Afraid"]'. Synced into
                the shared party roster and injected into the AI's context on every turn.
        """
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            effects = json.loads(status_effects)
        except (json.JSONDecodeError, TypeError):
            return i18n.t("kp_tools.character.status.invalid")
        if not isinstance(effects, list):
            return i18n.t("kp_tools.character.status.invalid")

        try:
            character = await _get_active_character(self.services, ctx)
            if not has_character(character):
                return i18n.t("kp_tools.character.none")

            await self.services.characters.sync_party_roster(ctx.chat_key, character, status_effects=effects)
            return i18n.t("kp_tools.character.status.updated", effects=", ".join(str(effect) for effect in effects))
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        except Exception as exc:
            return i18n.t("kp_tools.character.status.failed", error=str(exc))

    # -- items / equipment --------------------------------------------------
    # Phase 2: items are `item` documents (agent.items); `grant` validates the room's
    # catalog (D6 - no template-less items), equip slots drive bonuses (D3), and every
    # mutation refreshes the sheet's equipped_bonuses. These verbs are the cross-owner
    # write path an AI Keeper uses to grant/move/consume gear on ANY member.

    @tool(read_only=True)
    async def list_item_catalog(self, ctx: AgentCtx) -> str:
        """List the room's item CATALOG — every designed item (name, kind, slot, plot role, effect, bonus) the loaded module ships.

        Check this BEFORE granting or improvising gear: catalog items carry the module's real mechanics (kind/effect/bonus/slot). When the party obtains one of them, grant it by its exact catalog name with grant_item — never improvise a substitute for a designed item. Improvise only when the object is genuinely off-catalog."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            items = await get_item_catalog(self.services.documents, ctx.chat_key)
            if not items:
                return i18n.t("kp_tools.item.catalog_empty")
            lines: list[str] = []
            for tpl in items:
                if not isinstance(tpl, dict):
                    continue
                parts = [str(tpl.get("name") or "?")]
                if tpl.get("kind"):
                    parts.append(f"kind={tpl.get('kind')}")
                if tpl.get("slot"):
                    parts.append(f"slot={tpl.get('slot')}")
                if tpl.get("plot_role"):
                    parts.append(f"role={tpl.get('plot_role')}")
                if tpl.get("effect"):
                    parts.append(f"effect: {tpl.get('effect')}")
                bonus = tpl.get("bonus")
                if isinstance(bonus, dict) and bonus:
                    parts.append("bonus: " + ", ".join(f"{key} {delta:+d}" for key, delta in bonus.items()))
                lines.append(" - " + " | ".join(parts))
            if not lines:
                return i18n.t("kp_tools.item.catalog_empty")
            return i18n.t("kp_tools.item.catalog_header", count=len(lines)) + "\n" + "\n".join(lines)
        except Exception as exc:
            return i18n.t("kp_tools.item.catalog_failed", error=str(exc))

    @tool
    async def grant_item(self, ctx: AgentCtx, character: str, item_id: str, qty: int = 1, common: bool = False) -> str:
        """Grant a real item to a character once the party has ACTUALLY obtained it in play (picked up, looted, bought, rewarded).

Args:
    character: target character name (any member of the party)
    item_id: the item's catalog template name (must exist in the room's item catalog)
    qty: how many to grant (default 1; same-owner same-name instances merge). ALWAYS
        write the quantity HERE — never embed the count in the item name. The name is
        the plain template name (e.g. "金币"), never a display string like "金币×300"
        or "金币 ×300" (the inventory display renders the count as a suffix; the stored
        name never contains it).
    common: set True for a GENERIC everyday good (coins, rations, arrows — the same
        thing no matter which scenario it came from). A common item merges into the
        holder's existing same-name instance (quantity stacks) instead of refusing
        a duplicate; narrative/plot items stay common=False.
Rules:
- Call ONLY when the item is genuinely in that character's hands in the story - never pre-award, never for narration alone.
- The item MUST be in the room's catalog; you cannot invent a template.
- A character who already holds this item cannot be granted it again (non-consumables are unique per holder; the tool refuses duplicates). Handovers use transfer_item, losses use remove_item - never re-grant an item that is simply moving around.
- Narrate that the character now holds it AFTER granting, and only if this tool succeeded."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            if qty is None or int(qty) < 1:
                return i18n.t("kp_tools.item.bad_qty")
        except (TypeError, ValueError):
            return i18n.t("kp_tools.item.bad_qty")
        try:
            character = (character or "").strip()
            item_id = (item_id or "").strip()
            if not character or not item_id:
                return i18n.t("kp_tools.item.bad_args")
            owner = await self.services.characters.get_character_owner(ctx.chat_key, character)
            if not owner:
                return i18n.t("kp_tools.item.character_not_found", name=character)
            template = await catalog_template(self.services.documents, ctx.chat_key, item_id)
            if template is None:
                return i18n.t("kp_tools.item.not_in_catalog", item=item_id)
            active = await active_module(self.services, ctx.chat_key)
            if not item_active(active, template):
                return i18n.t("kp_tools.item.module_mismatch", item=item_id)
            existing = await find_instance(self.services.documents, ctx.chat_key, character, item_id)
            if existing is not None and not template_is_consumable(template):
                held = int(existing.data.get("quantity", 1))
                return i18n.t("kp_tools.item.already_held", character=character, item=item_id, held=held)
            template = template_with_source(template, active)
            await grant_instance(self.services.documents, ctx.chat_key, character, template, int(qty))
            await reveal_linked_clues(self.services, ctx, template)
            await _refresh_character_bonuses(self.services, ctx, character, owner)
            ctx.emit_item_grant(character, item_id, i18n.t("kp_tools.item.granted", character=character, item=item_id))
            return i18n.t("kp_tools.item.granted", character=character, item=item_id)
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        except Exception as exc:
            return i18n.t("kp_tools.item.failed", error=str(exc))

    @tool  # off-catalog improv lane must be live during play: evidence trinkets and
    # scene objects a party picks up mid-session need a real grant channel, or the
    # model can only narrate a holding claim that the item system never records.
    async def improvise_item(self, ctx: AgentCtx, character: str, name: str, description: str = "", bonus: str = "", qty: int = 1, common: bool = False) -> str:
        """Give a character an OFF-CATALOG item the Keeper improvises on the spot (a trinket found in a pocket, a curious stone, a small reward, a few doses of something).

Args:
    character: target character name (any member of the party)
    name: the item's name
    description: short description of what it is (optional)
    bonus: optional small mechanical edge, format "stat=value,stat=value" (e.g. "spot_hidden=1" or "侦查=1"); each stat capped at +/-2, total at 4 points; names resolve to the character's real skills/attributes (any spelling or alias works); leave empty for narrative-only items
    qty: how many to grant (default 1; same-owner same-name instances merge). ALWAYS
        write the quantity HERE — never embed the count in the item name. The name is
        the plain item name (e.g. "金币"), never a display string like "金币×300" or
        "金币 ×300".

Rules:
- Improvised items are a LIGHT channel: narrative trinkets, small rewards, consumables. NEVER use it for strong gear or scenario-critical artifacts - those must exist in the room's catalog (use grant_item).
- If `name` already exists in the room's catalog, the real catalog template is granted instead (its kind/effect/bonus win over this call's bonus) - improvising a designed item's name must never degrade it.
- Give a bonus whenever the improvised object grants a real edge (a lucky charm, a sharpened tool, a warm cloak); a bonus-bearing item is EQUIPPED automatically, so its edge applies immediately. The player can unequip it later.
- The bonus cap is enforced; oversized bonuses are refused. A bonus key the character's sheet cannot resolve is reported as a warning and kept as-is (it will not apply).
- A character who already holds the item cannot be granted it again (the tool refuses duplicates; a second dose of a consumable is granted via qty, not by calling twice). Handovers use transfer_item, losses use remove_item - never re-grant an item that is simply moving around.
- Narrate that the character now holds it after granting.
- common=True marks a GENERIC everyday good (coins, rations, arrows — the same thing
  no matter which scenario it came from): it merges into the holder's existing
  same-name instance (quantity stacks) instead of refusing a duplicate. Narrative
  or plot items keep common=False."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            if qty is None or int(qty) < 1:
                return i18n.t("kp_tools.item.bad_qty")
        except (TypeError, ValueError):
            return i18n.t("kp_tools.item.bad_qty")
        try:
            character = (character or "").strip()
            name = (name or "").strip()
            if not character or not name:
                return i18n.t("kp_tools.item.bad_args")
            owner = await self.services.characters.get_character_owner(ctx.chat_key, character)
            if not owner:
                return i18n.t("kp_tools.item.character_not_found", name=character)
            sheet = await self.services.characters.get_character(owner, ctx.chat_key, character)
            pack = await _sheet_pack(self.services, ctx, sheet) if has_character(sheet) else None
            template = await catalog_template(self.services.documents, ctx.chat_key, name)
            if template is not None:
                # The catalog already designs this item — grant the real template
                # (kind/effect/bonus) instead of a stripped one-off: improvising a
                # name that exists must never degrade a designed item.
                active = await active_module(self.services, ctx.chat_key)
                if not item_active(active, template):
                    return i18n.t("kp_tools.item.module_mismatch", item=name)
                canonical_name = str(template.get("name") or name).strip()
                existing = await find_instance(self.services.documents, ctx.chat_key, character, canonical_name)
                if existing is not None and not template_is_consumable(template):
                    # A common item (coins, rations, arrows — the same thing from any
                    # scenario) merges into the existing instance instead of refusing:
                    # the keeper/AI marks it common, and quantity stacks.
                    if common or existing.data.get("common"):
                        template = {**template_with_source(template, active), "common": True}
                        await grant_instance(self.services.documents, ctx.chat_key, character, template, int(qty))
                        await _refresh_character_bonuses(self.services, ctx, character, owner)
                        ctx.emit_item_grant(character, canonical_name, i18n.t("kp_tools.item.granted", character=character, item=canonical_name))
                        return i18n.t("kp_tools.item.granted", character=character, item=canonical_name)
                    if existing.data.get("archived"):
                        return i18n.t("kp_tools.item.archived_held", character=character, item=canonical_name)
                    held = int(existing.data.get("quantity", 1))
                    return i18n.t("kp_tools.item.already_held", character=character, item=canonical_name, held=held)
                template = template_with_source(template, active)
                await grant_instance(self.services.documents, ctx.chat_key, character, template, int(qty))
                await reveal_linked_clues(self.services, ctx, template)
                await _refresh_character_bonuses(self.services, ctx, character, owner)
                ctx.emit_item_grant(
                    character,
                    canonical_name,
                    i18n.t("kp_tools.item.granted", character=character, item=canonical_name),
                )
                return i18n.t("kp_tools.item.granted", character=character, item=canonical_name)
            try:
                bonus_map = parse_bonus_spec(bonus or "")
            except ValueError:
                return i18n.t("kp_tools.item.improv_invalid_value")
            error = validate_improvised_bonus(bonus_map)
            if error:
                return i18n.t(f"kp_tools.item.improv_{error}")
            bonus_map, unresolved = canonicalize_bonus_keys(bonus_map, pack)
            active = await active_module(self.services, ctx.chat_key)
            template = improvised_template(
                name,
                description=description or "",
                bonus=bonus_map,
                source_module_id=module_source_id(active),
                common=common,
            )
            existing = await find_instance(self.services.documents, ctx.chat_key, character, name)
            if existing is not None:
                # A common item merges into the existing instance (quantity stacks)
                # instead of refusing — the keeper/AI marked it common.
                if common or existing.data.get("common"):
                    template = {**template, "common": True}
                    await grant_improvised_instance(self.services.documents, ctx.chat_key, character, template, int(qty))
                    await _refresh_character_bonuses(self.services, ctx, character, owner)
                    ctx.emit_item_grant(character, name, i18n.t("kp_tools.item.improvised_granted", character=character, item=name))
                    return i18n.t("kp_tools.item.improvised_granted", character=character, item=name)
                if existing.data.get("archived"):
                    return i18n.t("kp_tools.item.archived_held", character=character, item=name)
                held = int(existing.data.get("quantity", 1))
                return i18n.t("kp_tools.item.already_held", character=character, item=name, held=held)
            await grant_improvised_instance(self.services.documents, ctx.chat_key, character, template, int(qty))
            await _refresh_character_bonuses(self.services, ctx, character, owner)
            ctx.emit_item_grant(character, name, i18n.t("kp_tools.item.improvised_granted", character=character, item=name))
            result = i18n.t("kp_tools.item.improvised_granted", character=character, item=name)
            if unresolved:
                result += "\n" + i18n.t("kp_tools.item.improv_unresolved_bonus", keys=", ".join(unresolved))
            return result
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        except Exception as exc:
            return i18n.t("kp_tools.item.failed", error=str(exc))

    @tool
    async def transfer_item(self, ctx: AgentCtx, source: str, target: str, item: str, qty: int = 1) -> str:
        """Move a real item between two characters (handed over, sold, given away). Args: source, target, item (name), qty (default 1). Source must hold it; both must exist; narrate the handover.
        IMPORTANT: transfer ONLY when the characters genuinely do it in play — a handover,
        a payment the player described. NEVER invent a transfer the player didn't ask for
        (no surprise "X hands Y coins to Z"), and never narrate the handover before this
        tool succeeds."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            if qty is None or int(qty) < 1:
                return i18n.t("kp_tools.item.bad_qty")
        except (TypeError, ValueError):
            return i18n.t("kp_tools.item.bad_qty")
        try:
            source = (source or "").strip()
            target = (target or "").strip()
            item = (item or "").strip()
            if not source or not target or not item:
                return i18n.t("kp_tools.item.bad_args")
            if source.casefold() == target.casefold():
                return i18n.t("kp_tools.item.same_character")
            characters = self.services.characters
            src_owner = await characters.get_character_owner(ctx.chat_key, source)
            dst_owner = await characters.get_character_owner(ctx.chat_key, target)
            if not src_owner:
                return i18n.t("kp_tools.item.character_not_found", name=source)
            if not dst_owner:
                return i18n.t("kp_tools.item.character_not_found", name=target)
            doc = await find_instance(self.services.documents, ctx.chat_key, source, item)
            if doc is None:
                return i18n.t("kp_tools.item.not_found", name=source, item=item)
            src_qty = int(doc.data.get("quantity", 1))
            move = min(int(qty), src_qty)
            if src_qty <= move:
                await self.services.documents.delete(ctx.chat_key, "item", doc.id)
            else:
                await self.services.documents.put(
                    ctx.chat_key, "item", doc.id, {**doc.data, "quantity": src_qty - move}
                )
            await grant_instance(self.services.documents, ctx.chat_key, target, doc.data, move)
            await _refresh_character_bonuses(self.services, ctx, source, src_owner)
            await _refresh_character_bonuses(self.services, ctx, target, dst_owner)
            ctx.emit_item_grant(target, item, i18n.t("kp_tools.item.transferred", item=item, source=source, target=target))
            return i18n.t("kp_tools.item.transferred", item=item, source=source, target=target)
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        except Exception as exc:
            return i18n.t("kp_tools.item.failed", error=str(exc))

    @tool
    async def remove_item(self, ctx: AgentCtx, character: str, item: str) -> str:
        """Remove an item from a character (lost, destroyed, taken away). Args: character, item. Character must hold it."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            character = (character or "").strip()
            item = (item or "").strip()
            if not character or not item:
                return i18n.t("kp_tools.item.bad_args")
            owner = await self.services.characters.get_character_owner(ctx.chat_key, character)
            if not owner:
                return i18n.t("kp_tools.item.character_not_found", name=character)
            doc = await find_instance(self.services.documents, ctx.chat_key, character, item)
            if doc is None:
                return i18n.t("kp_tools.item.not_found", name=character, item=item)
            await self.services.documents.delete(ctx.chat_key, "item", doc.id)
            await _refresh_character_bonuses(self.services, ctx, character, owner)
            ctx.emit_item_grant(character, item, i18n.t("kp_tools.item.removed", item=item, character=character))
            return i18n.t("kp_tools.item.removed", item=item, character=character)
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        except Exception as exc:
            return i18n.t("kp_tools.item.failed", error=str(exc))

    @tool
    async def update_item(self, ctx: AgentCtx, character: str, item: str, *, description: str = "", effect: str = "", bonus: str = "", common: bool | None = None, merge_into: str = "") -> str:
        """Update a held item — description, effect, bonus, the common flag, or a merge.
        Only the fields you pass change; pass ""/omit to leave one untouched.
        description: new flavor text.
        effect: new in-play effect line.
        bonus: new mechanical edge "stat=value,stat=value" (replaces the existing bonus;
            recomputes the holder's equipped bonuses).
        common: True marks a COMMON good (coins/rations/arrows — the same thing from any
            scenario; future same-name grants auto-merge quantity), False unmarks it,
            omit to leave unchanged.
        merge_into: name of the entry to MERGE this item INTO — quantities stack into one
            instance and duplicates collapse (e.g. the player says "金币" and "50枚金币"
            are the same thing; pass item="50枚金币", merge_into="金币"). Any other
            fields you pass apply to the merged entry.
        item/merge_into take PLAIN stored names only (e.g. "金币") — never display
            strings that carry the count ("金币 ×300", "金币×300"); the quantity lives
            in the instance's quantity field, never in the name.
        Use when the player says an item's description/effect/status is wrong or asks to
        combine duplicate/equivalent entries; narrate the result.
        IMPORTANT: never announce the outcome ("已合并", "现在是500枚") BEFORE calling
        this tool and seeing it succeed — narrate only what the engine actually did,
        with the quantity it actually returned."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            character = (character or "").strip()
            item = (item or "").strip()
            merge_into = (merge_into or "").strip()
            if not character or not item:
                return i18n.t("kp_tools.item.bad_args")
            owner = await self.services.characters.get_character_owner(ctx.chat_key, character)
            if not owner:
                return i18n.t("kp_tools.item.character_not_found", name=character)

            # Resolve the bonus spec ONCE in async context (the sheet lookup is awaitable).
            bonus_map: dict | None = None
            bonus_changed = False
            if bonus.strip():
                try:
                    bonus_map = parse_bonus_spec(bonus)
                except ValueError:
                    return i18n.t("kp_tools.item.improv_invalid_value")
                error = validate_improvised_bonus(bonus_map)
                if error:
                    return i18n.t(f"kp_tools.item.improv_{error}")
                sheet = await self.services.characters.get_character(owner, ctx.chat_key, character)
                pack = await _sheet_pack(self.services, ctx, sheet) if has_character(sheet) else None
                bonus_map, _unresolved = canonicalize_bonus_keys(bonus_map, pack)
                bonus_changed = True

            def _apply_updates(data: dict, changed: list[str]) -> None:
                if description.strip():
                    data["description"] = description.strip()
                    changed.append(i18n.t("kp_tools.item.field_description"))
                if effect.strip():
                    data["effect"] = effect.strip()
                    changed.append(i18n.t("kp_tools.item.field_effect"))
                if bonus_changed and bonus_map is not None:
                    data["bonus"] = dict(bonus_map)
                    data["effect"] = ", ".join(f"{k} {v:+d}" for k, v in bonus_map.items()) or data.get("effect", "")
                    changed.append(i18n.t("kp_tools.item.field_bonus"))

            if merge_into:
                from agent.items import instances_for_owner

                instances = await instances_for_owner(self.services.documents, ctx.chat_key, character)
                folded = item.casefold()
                srcs = [d for d in instances if str(d.data.get("name", "")).casefold() == folded]
                target_name = merge_into
                target_folded = target_name.casefold()
                src_ids = {s.id for s in srcs}
                targets = [
                    d for d in instances
                    if str(d.data.get("name", "")).casefold() == target_folded and d.id not in src_ids
                ]
                if targets:
                    target = targets[0]
                    drops = srcs
                elif srcs:
                    target = srcs[0]
                    drops = [d for d in srcs if d.id != target.id]
                else:
                    return i18n.t("kp_tools.item.not_found", name=character, item=item)
                total = int(target.data.get("quantity", 1)) + sum(
                    int(d.data.get("quantity", 1)) for d in drops
                )
                merged_data = dict(target.data)
                merged_data["quantity"] = total
                if common is not None:
                    merged_data["common"] = bool(common)
                elif bool(target.data.get("common")) or any(bool(d.data.get("common")) for d in srcs):
                    merged_data["common"] = True
                if target_name != item:
                    merged_data["name"] = target_name
                changed: list[str] = []
                _apply_updates(merged_data, changed)
                await self.services.documents.put(ctx.chat_key, "item", target.id, merged_data)
                for d in drops:
                    await self.services.documents.delete(ctx.chat_key, "item", d.id)
                if bonus_changed:
                    await _refresh_character_bonuses(self.services, ctx, character, owner)
                if changed:
                    changed_text = "（" + "、".join(changed) + "）" if str(ctx.locale).startswith("zh") else " (" + ", ".join(changed) + ")"
                    return i18n.t("kp_tools.item.merged_updated", character=character, item=target_name, qty=total, changed=changed_text)
                return i18n.t("kp_tools.item.merged", character=character, item=target_name, qty=total)

            doc = await find_instance(self.services.documents, ctx.chat_key, character, item)
            if doc is None:
                return i18n.t("kp_tools.item.not_found", name=character, item=item)
            data = dict(doc.data)
            changed: list[str] = []
            if common is not None:
                data["common"] = bool(common)
                changed.append(i18n.t("kp_tools.item.field_common"))
            _apply_updates(data, changed)
            if not changed:
                return i18n.t("kp_tools.item.updated_nothing")
            await self.services.documents.put(ctx.chat_key, "item", doc.id, data)
            if bonus_changed:
                await _refresh_character_bonuses(self.services, ctx, character, owner)
            changed_text = "（" + "、".join(changed) + "）" if str(ctx.locale).startswith("zh") else " (" + ", ".join(changed) + ")"
            return i18n.t("kp_tools.item.updated", character=character, item=item, changed=changed_text)
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        except Exception as exc:
            return i18n.t("kp_tools.item.failed", error=str(exc))

    @tool
    async def use_item(self, ctx: AgentCtx, character: str, item: str) -> str:
        """Consume one unit of a held item (drinks a potion, spends a token):
        quantity decreases and the item disappears at zero. Args: character,
        item. Character must hold it."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            character = (character or "").strip()
            item = (item or "").strip()
            if not character or not item:
                return i18n.t("kp_tools.item.bad_args")
            owner = await self.services.characters.get_character_owner(ctx.chat_key, character)
            if not owner:
                return i18n.t("kp_tools.item.character_not_found", name=character)
            found, remaining = await consume_instance(
                self.services.documents, ctx.chat_key, character, item, 1
            )
            if not found:
                return i18n.t("kp_tools.item.not_found", name=character, item=item)
            await _refresh_character_bonuses(self.services, ctx, character, owner)
            if remaining is None:
                ctx.emit_item_grant(character, item, i18n.t("kp_tools.item.used_up", character=character, item=item))
                return i18n.t("kp_tools.item.used_up", character=character, item=item)
            ctx.emit_item_grant(character, item, i18n.t("kp_tools.item.used", character=character, item=item, remaining=remaining))
            return i18n.t("kp_tools.item.used", character=character, item=item, remaining=remaining)
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        except Exception as exc:
            return i18n.t("kp_tools.item.failed", error=str(exc))

    @tool
    async def equip_item(self, ctx: AgentCtx, character: str, item: str, slot: str = "") -> str:
        """Equip an item into a slot so its mechanical bonus applies. Args: character, item, slot (optional; defaults to the item's declared slot). Character must hold it; unequip_item stops the bonus."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            character = (character or "").strip()
            item = (item or "").strip()
            if not character or not item:
                return i18n.t("kp_tools.item.bad_args")
            owner = await self.services.characters.get_character_owner(ctx.chat_key, character)
            if not owner:
                return i18n.t("kp_tools.item.character_not_found", name=character)
            doc = await find_instance(self.services.documents, ctx.chat_key, character, item)
            if doc is None:
                return i18n.t("kp_tools.item.not_found", name=character, item=item)
            effective_slot = slot.strip() or str(doc.data.get("slot") or "equipped")
            await set_equipped(self.services.documents, ctx.chat_key, doc.id, effective_slot)
            await _refresh_character_bonuses(self.services, ctx, character, owner)
            ctx.emit_item_grant(character, item, i18n.t("kp_tools.item.equipped", item=item, character=character, slot=effective_slot))
            return i18n.t("kp_tools.item.equipped", item=item, character=character, slot=effective_slot)
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        except Exception as exc:
            return i18n.t("kp_tools.item.failed", error=str(exc))

    @tool
    async def unequip_item(self, ctx: AgentCtx, character: str, item: str) -> str:
        """Unequip an item, stopping its mechanical bonus. Args: character, item. Character must hold it."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            character = (character or "").strip()
            item = (item or "").strip()
            if not character or not item:
                return i18n.t("kp_tools.item.bad_args")
            owner = await self.services.characters.get_character_owner(ctx.chat_key, character)
            if not owner:
                return i18n.t("kp_tools.item.character_not_found", name=character)
            doc = await find_instance(self.services.documents, ctx.chat_key, character, item)
            if doc is None:
                return i18n.t("kp_tools.item.not_found", name=character, item=item)
            await set_equipped(self.services.documents, ctx.chat_key, doc.id, None)
            await _refresh_character_bonuses(self.services, ctx, character, owner)
            ctx.emit_item_grant(character, item, i18n.t("kp_tools.item.unequipped", item=item, character=character))
            return i18n.t("kp_tools.item.unequipped", item=item, character=character)
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        except Exception as exc:
            return i18n.t("kp_tools.item.failed", error=str(exc))

    @tool
    async def reveal_clue(self, ctx: AgentCtx, name: str) -> str:
        """Record a worldbook CLUE as discovered by the party — the structural half of
        clue tracking (the discovered-clue log players see).

Args:
    name: the clue's title or one of its trigger keys (e.g. "录音带" or "田中")

Rules:
- Call ONLY when the party has GENUINELY obtained this clue in play — read the letter, found the tape, examined the scene. Never pre-reveal.
- Check list_discovered_clues first so you never re-grant a clue that is already in the log.
- The entry is snapshotted now; players' clue list shows it from here on.
- Secret clues (the hidden truth) stay out of the log until you reveal them — this is the moment the party earns the knowledge."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            entry = await _find_clue(self.services.worldbook, ctx.chat_key, name)
            if entry is None:
                return i18n.t("kp_tools.clue.not_found", name=name)
            added = await _log_clue(
                self.services.documents,
                ctx.chat_key,
                module=await active_module_id(self.services, ctx.chat_key),
                **entry,
            )
            if not added:
                return i18n.t("kp_tools.clue.already_added", name=entry["title"])
            return i18n.t("kp_tools.clue.revealed", name=entry["title"])
        except Exception as exc:
            return i18n.t("kp_tools.clue.failed", error=str(exc))


class DiceTools:
    """AI-KP tools for dice rolls, graded checks, HP management and dice pools."""

    def __init__(self, services: Services) -> None:
        self.services = services

    async def _record_dice_roll(
        self,
        ctx: AgentCtx,
        expression: str,
        result: DiceResult,
        actor: str | None = None,
        *,
        hidden: bool = False,
    ) -> None:
        """Best-effort battle-report recording, mirroring plugin.py's `/r` command handler.

        The manager lazily starts a session when needed. A recording failure
        never breaks the roll.
        """
        try:
            character = await _get_active_character(self.services, ctx)
            active_name = character.name if character else ""
            char_name, is_npc = await _resolve_actor_identity(
                self.services,
                ctx,
                active_name,
                actor,
            )
            user_id = NPC_USER_ID if is_npc else ctx.uid()
            await record_dice_roll(
                self.services.battles,
                ctx.chat_key,
                user_id,
                char_name,
                expression,
                result,
                hidden=hidden,
            )
        except Exception:
            pass

    async def _record_check(
        self,
        ctx: AgentCtx,
        char_name: str,
        skill: str,
        outcome: CheckOutcome,
        *,
        label: str = "",
        actor: str | None = None,
        actor_is_npc: bool | None = None,
        hidden: bool = False,
        **details: object,
    ) -> None:
        """Best-effort structured battle-report recording for one check."""
        try:
            actor_name, resolved_is_npc = await _resolve_actor_identity(
                self.services,
                ctx,
                char_name,
                actor,
            )
            is_npc = resolved_is_npc if actor_is_npc is None else actor_is_npc
            await record_check(
                self.services.battles,
                ctx.chat_key,
                NPC_USER_ID if is_npc else ctx.uid(),
                actor_name,
                skill,
                outcome,
                label=label,
                hidden=hidden,
                **details,
            )
        except Exception:
            pass

    @tool
    async def roll_dice(
        self, ctx: AgentCtx, expression: str, actor: str | None = None, hidden: bool = False
    ) -> str:
        """Roll dice and return the result.

        Args:
            expression: Dice expression, e.g. '1d100', '3d6+2', '2d6*5'.
            actor: Set to the NPC/creature name when rolling for a non-player actor.
            hidden: True for a behind-the-screen roll: the dice frame reaches the
                KEEPER only — players never see the number or even that a roll
                happened — and the roll is recorded as hidden (excluded from every
                player-facing report). Use it for secret world rulings (a hidden
                perception check, an NPC's private contest, a consequence that must
                not be revealed yet, e.g. whether foraged herbs are poisonous);
                NEVER for a player's own declared action, which must stay public.
        """
        i18n = self.services.i18n.with_locale(ctx.locale)
        try:
            result = self.services.dice.roll_expression(expression)
        except ValueError as exc:
            return i18n.t("kp_tools.dice.roll.invalid_expression", error=str(exc))
        except Exception as exc:
            return i18n.t("kp_tools.dice.roll.failed", error=str(exc))

        response = i18n.t("kp_tools.dice.roll.result", result=result.format_result(i18n=i18n))
        if result.is_critical_success():
            response += i18n.t("kp_tools.dice.critical_success_suffix")
        elif result.is_critical_failure():
            response += i18n.t("kp_tools.dice.critical_failure_suffix")

        payload: dict[str, object] = {
            "kind": "roll",
            "expr": expression,
            "rolls": list(result.rolls),
            "total": result.total,
            "detail": {
                "modifier": result.modifier,
                "critical_success": result.is_critical_success(),
                "critical_failure": result.is_critical_failure(),
            },
        }
        if actor and actor.strip():
            payload["actor"] = actor.strip()
        if hidden:
            payload["hidden"] = True
        ctx.emit_dice(payload)
        await self._record_dice_roll(ctx, expression, result, actor=actor, hidden=hidden)
        return response

    async def _pool_check(self, ctx: AgentCtx, i18n, params: dict, actor: str | None, *, hidden: bool = False) -> str:
        """Graded pool check for parameterized systems, under the ROOM's pack."""

        pack = await self.services.room_rulepack(ctx)
        resolver = pack.resolver
        if resolver is None or not resolver.params:
            return i18n.t("kp_tools.dice.pool.not_parameterized")
        bounds = {spec.id: spec for spec in resolver.params}
        cleaned: dict[str, int] = {}
        for key, spec in bounds.items():
            raw = params.get(key, spec.default)
            if raw is None:
                return i18n.t("kp_tools.dice.pool.missing_param", param=key)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return i18n.t("kp_tools.dice.pool.missing_param", param=key)
            if isinstance(raw, bool) or not spec.minimum <= value <= spec.maximum:
                return i18n.t(
                    "kp_tools.dice.pool.out_of_range",
                    param=key,
                    minimum=spec.minimum,
                    maximum=spec.maximum,
                )
            cleaned[key] = value
        unknown = set(params) - set(bounds)
        if unknown:
            return i18n.t("kp_tools.dice.pool.unknown_param", param=", ".join(sorted(unknown)))

        rolled = self.services.dice.roll_for_check(resolver, params=cleaned)
        outcome = resolver.interpret(rolled, None)
        level = pack.rank_label(outcome.rank.id, ctx.locale)
        rolls_str = ", ".join(str(face) for face in rolled.dice)
        ctx.emit_dice(
            {
                "kind": "check",
                **({"actor": actor} if actor and actor.strip() else {}),
                "expr": rolled.expression,
                "rolls": list(rolled.dice),
                "total": rolled.total,
                "outcome": outcome_wire(outcome, level),
                "detail": {**dict(rolled.modifiers), **cleaned},
                **({"hidden": True} if hidden else {}),
            }
        )
        lines = [
            i18n.t("kp_tools.dice.pool.header", expr=rolled.expression),
            i18n.t("kp_tools.dice.pool.rolls_line", rolls=rolls_str),
            i18n.t("kp_tools.dice.pool.margin_line", count=outcome.margin if outcome.margin is not None else 0),
            level,
        ]
        return "\n".join(lines)

    @tool
    async def skill_check(
        self,
        ctx: AgentCtx,
        skill_name: str,
        bonus: int = 0,
        penalty: int = 0,
        dc: int | None = None,
        proficient: bool = False,
        actor: str | None = None,
        npc_target: int | None = None,
        params: dict | None = None,
        hidden: bool = False,
    ) -> str:
        """Run a skill check for the active character (attribute names and bridged skills resolve too).

        Args:
            skill_name: Skill or attribute name (CN/EN aliases supported).
            bonus: Count of the system's favorable roll modifier (bonus dice / advantage).
            penalty: Count of the system's unfavorable roll modifier (penalty dice / disadvantage).
            dc: Difficulty target, for systems whose checks roll against a declared DC; omit to use
                the system's default. Ignored by systems that roll against the sheet value.
            proficient: Whether the sheet's proficiency bonus applies (systems that declare one).
            params: Roll parameters for rule systems whose check declares them (e.g. a dice-pool
                size and threshold), as an integer mapping. Omit for systems that don't.
            actor: ONLY for a non-player actor: copy the NPC/creature's exact stated name, without
                added titles or roles. For a player character's check OMIT actor entirely — never
                send actor="" or the player's name.
            npc_target: Required with actor: the NPC's real check number — its skill/target value or
                its total check modifier, whichever this system's checks use — as a real integer.
                Omit for player checks — never send 0.
            hidden: True for a behind-the-screen check: the dice frame reaches the KEEPER only —
                players never see the number or even that a roll happened — and the check is
                recorded as hidden (excluded from every player-facing report). Use it for secret
                world rulings (a hidden perception check, an NPC's private contest, a consequence
                that must not be revealed yet); NEVER for a player's own declared action, which
                must stay public.
        """
        i18n = self.services.i18n.with_locale(ctx.locale)
        dice = self.services.dice

        try:
            if params:
                # Pool-parameterized systems (the resolver declares {slot}s):
                # the params ARE the whole input — no sheet required.
                return await self._pool_check(ctx, i18n, params, actor, hidden=hidden)
            character = await _get_active_character(self.services, ctx)
            if not has_character(character):
                return i18n.t("kp_tools.character.none")
            display_name, is_npc = await _resolve_actor_identity(
                self.services,
                ctx,
                character.name,
                actor,
            )
            if is_npc and npc_target is None:
                return i18n.t("kp_tools.dice.skill_check.npc_target_required")

            pack = await _sheet_pack(self.services, ctx, character)
            resolver = pack.resolver
            if resolver is None:
                return i18n.t("kp_tools.dice.skill_check.unknown_skill", name=skill_name)
            if resolver.params:
                return i18n.t("kp_tools.dice.pool.missing_param", param=resolver.params[0].id)
            check = resolver.check

            canonical = pack.resolve_skill(skill_name) or skill_name.strip()
            if not is_npc and not has_check_value(character, pack, canonical):
                # Unknown name (no alias, not on the sheet): refuse the roll
                # instead of running a degenerate target-0 check where a
                # minimal roll reads as a critical success.
                return i18n.t("kp_tools.dice.skill_check.unknown_skill", name=skill_name)
            sheet_check_value = None if is_npc else check_value(character, pack, canonical)

            # The pack routes the favorable/unfavorable counts to its declared roll
            # modifiers (opposing counts cancel) — `core.check_roll`, shared with the
            # typed-command lane so the two cannot drift on how a check is rolled.
            net_favor = bonus - penalty
            modifiers, applied = favor_modifiers(check, bonus, penalty)
            favor_label = pack.display_name(applied, ctx.locale) if applied else ""

            variant = await room_rule_variant(self.services.store, ctx.chat_key)

            # What the roll is graded against, and the flat sheet modifier — the tool
            # lane's inputs (an explicit DC or the pack default; an NPC's stated number
            # or the sheet's own value).
            if resolver.target_kind == "dc":
                # Roll + sheet modifier against an external difficulty target.
                target = int(dc) if dc is not None else int(check.default_target or 0)
                modifier = int(npc_target) if is_npc else int(sheet_check_value or 0)
                if not is_npc and proficient and check.proficiency:
                    modifier += sheet_value(character, pack, check.proficiency)
            else:
                # Roll against the sheet's own value as the target.
                target = int(npc_target) if is_npc else int(sheet_check_value or 0)
                modifier = 0

            graded = graded_roll(dice, resolver, modifiers=modifiers, target=target, modifier=modifier, variant=variant)
            rolled, outcome, total = graded.rolled, graded.outcome, graded.total  # graded: target is an int
            level_label = pack.rank_label(outcome.rank.id, ctx.locale)
            skill_label = pack.display_name(canonical, ctx.locale)

            prof_label = i18n.t("kp_tools.dice.skill_check.proficient_label") if proficient and check.proficiency else ""
            lines = [i18n.t("kp_tools.dice.skill_check.header", name=display_name, skill=skill_label, extra=prof_label)]
            if resolver.target_kind == "dc":
                if favor_label:
                    lines.append(
                        i18n.t("kp_tools.dice.skill_check.modifier_line", label=favor_label, count=abs(net_favor))
                    )
                lines.append(
                    i18n.t(
                        "kp_tools.dice.skill_check.roll_vs_line",
                        roll=rolled.total,
                        modifier=modifier,
                        total=total,
                        target=target,
                    )
                )
            else:
                target_line = i18n.t("kp_tools.dice.skill_check.target_line", value=target)
                if favor_label:
                    target_line += i18n.t(
                        "kp_tools.dice.skill_check.modifier_suffix", label=favor_label, count=abs(net_favor)
                    )
                lines.append(target_line)
                base_roll = int(rolled.modifiers.get("base_roll", rolled.total))
                lines.append(i18n.t("kp_tools.dice.skill_check.raw_roll_line", roll=base_roll))
                if favor_label and "final_tens" in rolled.modifiers:
                    lines.append(
                        i18n.t(
                            "kp_tools.dice.skill_check.tens_line",
                            label=favor_label,
                            extra=list(rolled.modifiers.get("extra_tens", [])),
                            final=rolled.modifiers.get("final_tens", rolled.total // 10 % 10),
                        )
                    )
                lines.append(i18n.t("kp_tools.dice.skill_check.final_line", final=rolled.total))

            outcome_key = (
                "kp_tools.dice.skill_check.outcome_success"
                if outcome.rank.success
                else "kp_tools.dice.skill_check.outcome_failure"
            )
            lines.append(i18n.t(outcome_key, level=level_label))

            candidate_rolls = list(rolled.modifiers.get("dice_all", rolled.dice)) or [rolled.total]
            ctx.emit_dice(
                {
                    "kind": "check",
                    **({"actor": display_name} if actor and actor.strip() else {}),
                    "expr": skill_label,
                    "skill": canonical,
                    "rolls": candidate_rolls,
                    "total": total,
                    "target": target,
                    "effective_target": resolver.effective_target(target),
                    "outcome": outcome_wire(outcome, level_label),
                    "detail": {
                        "bonus": bonus,
                        "penalty": penalty,
                        "modifier": modifier,
                        "proficient": proficient,
                        **dict(rolled.modifiers),
                    },
                    **({"hidden": True} if hidden else {}),
                }
            )
            await self._record_check(
                ctx,
                character.name,
                canonical,
                outcome,
                label=level_label,
                actor=display_name if actor and actor.strip() else None,
                actor_is_npc=is_npc,
                hidden=hidden,
                bonus=bonus,
                penalty=penalty,
                modifier=modifier,
                **({"variant": variant} if variant else {}),
            )
            return "\n".join(lines)
        except Exception as exc:
            return i18n.t("kp_tools.dice.skill_check.failed", error=str(exc))

    @tool
    async def hp_manager(self, ctx: AgentCtx, action: str, value: int = 0) -> str:
        """Manage the active character's hit points.

        Args:
            action: Operation type (show/add/sub/set).
            value: The amount to add/subtract, or the value to set.
        """
        i18n = self.services.i18n.with_locale(ctx.locale)
        characters = self.services.characters
        try:
            character = await _get_active_character(self.services, ctx)
            if not has_character(character):
                return i18n.t("kp_tools.character.none")

            hp, hp_max = get_hit_points(character)

            if action == "show":
                pass
            elif action == "add":
                hp, hp_max = set_hit_points(character, delta=value)
            elif action == "sub":
                hp, hp_max = set_hit_points(character, delta=-value)
            elif action == "set":
                hp, hp_max = set_hit_points(character, current=value)
            else:
                return i18n.t("kp_tools.dice.hp.unknown_action", action=action)

            await characters.save_character(ctx.uid(), ctx.chat_key, character)

            ratio = hp / hp_max if hp_max > 0 else 1
            if ratio >= 0.75:
                status_key = "kp_tools.dice.hp.status_healthy"
            elif ratio >= 0.5:
                status_key = "kp_tools.dice.hp.status_light"
            elif ratio >= 0.25:
                status_key = "kp_tools.dice.hp.status_heavy"
            elif hp > 0:
                status_key = "kp_tools.dice.hp.status_dying"
            else:
                status_key = "kp_tools.dice.hp.status_dead"

            return i18n.t(
                "kp_tools.dice.hp.status_line", name=character.name, hp=hp, hpmax=hp_max, status=i18n.t(status_key)
            )
        except CharacterDataError:
            return i18n.t("kp_tools.character.data_error")
        except Exception as exc:
            return i18n.t("kp_tools.dice.hp.failed", error=str(exc))

def roll_initiative(services: Services, character: CharacterSheet) -> DiceResult:
    """Roll the pack-declared initiative expression for `character`.

    ``{name}`` slots in the expression read the sheet's canonical values; a
    system with no declaration falls back to the engine's plain d100 order.
    """
    import re as _re

    try:
        pack = load_rulepack(character.system)
    except Exception:
        pack = None
    expression = pack.initiative_roll if pack is not None else ""
    if expression and pack is not None:
        filled = _re.sub(
            r"\{([^{}]+)\}",
            lambda match: str(sheet_value(character, pack, match.group(1))),
            expression,
        )
        return services.dice.roll_expression(filled, is_check=True)
    return services.dice.roll_expression("1d100", is_check=True)


class InitiativeTools:
    """AI-KP tool for tracking combat initiative order and casting spells."""

    def __init__(self, services: Services, *, command_router: Any | None = None) -> None:
        self.services = services
        self._command_router = command_router

    @tool(read_only=False, needs="spells")
    async def cast_spell(
        self, ctx: AgentCtx, *, spell: str, target: str = "", slot_level: int = 0
    ) -> str:
        """Cast `spell` for the CURRENT combat actor through the real cast lane.

        The engine resolves the spell catalog, enforces known-spells and slot
        availability, rolls the save/attack and damage, spends the slot pool and
        records the action in the combat log — the AI only picks the spell and
        narrates the outcome. `slot_level` casts at a higher level (scaling
        damage, consuming that slot pool); omit it to cast at the spell's own
        level. `target` names one combatant; omit it only when the spell needs
        no target (a buff or a no-target effect). Requires an active combat and
        the caster's turn, exactly like `.cast`."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        if self._command_router is None:
            return i18n.t("kp_tools.cast.unavailable")
        parts = [str(spell)]
        if slot_level and int(slot_level) > 0:
            parts.append(f"@{int(slot_level)}")
        if target:
            parts.append(str(target))
        result = await self._command_router.dispatch(ctx, ".cast " + " ".join(parts))
        return result if result is not None else i18n.t("kp_tools.cast.empty")

    @tool(read_only=False, needs="runtime")
    async def rest_manager(self, ctx: AgentCtx, *, kind: str = "long", recovery_dice: str = "") -> str:
        """Complete a short or long rest through the real rest lane.

        A long rest restores HP and recovers spell slots to the level-table
        maximums and advances the game clock; a short rest spends hit dice to
        heal (and, for a pact caster like a warlock, recovers pact slots).
        `kind` is "short" or "long"; `recovery_dice` optionally names hit-dice
        pools to spend on a short rest. Call this when the story calls for the
        party to rest — never narrate "you rested" without settling the real
        resources."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        if self._command_router is None:
            return i18n.t("kp_tools.rest.unavailable")
        kind = str(kind).strip().casefold()
        if kind not in {"short", "long"}:
            return i18n.t("kp_tools.rest.usage")
        parts = [kind]
        if recovery_dice:
            parts.extend(str(recovery_dice).split())
        result = await self._command_router.dispatch(ctx, ".rest " + " ".join(parts))
        return result if result is not None else i18n.t("kp_tools.rest.empty")

    async def _dispatch_command(self, ctx: AgentCtx, command: str, fallback_key: str) -> str:
        """Run a gateway command through the real lane (keeper context assumed)."""
        i18n = self.services.i18n.with_locale(ctx.locale)
        if self._command_router is None:
            return i18n.t(fallback_key)
        result = await self._command_router.dispatch(ctx, command)
        return result if result is not None else i18n.t("kp_tools.cast.empty")

    @tool(read_only=False, needs="runtime")
    async def attack_target(self, ctx: AgentCtx, *, action: str = "attack", target: str = "") -> str:
        """Resolve a real attack action in the active combat through the engine.

        `action` is the runtime combat action id (attack, dash, dodge, spell...);
        `target` names one combatant. The engine rolls the attack vs AC, applies
        damage, spends the action budget and records the combat event — never
        narrate a hit or miss without resolving it here (mirror of `.attack`)."""
        return await self._dispatch_command(
            ctx, f".attack {action} {target}".rstrip(), "kp_tools.cast.unavailable"
        )

    @tool(read_only=False, needs="runtime")
    async def advance_level(self, ctx: AgentCtx, *, mode: str = "", choice: str = "") -> str:
        """Drive character advancement (leveling up) through the engine.

        `mode` is "status" to inspect, "grant" (with `choice` naming milestone or
        xp) to open an advancement, or "apply" to commit a pending one. The engine
        raises the level, grows HP and unlocks spell slots per the level table —
        never narrate "you leveled up" without settling the real sheet."""
        command = ".advance"
        if mode:
            command += f" {mode}"
            if choice:
                command += f" {choice}"
        return await self._dispatch_command(ctx, command, "kp_tools.cast.unavailable")

    @tool(read_only=False, needs="runtime")
    async def manage_resource(self, ctx: AgentCtx, *, pool: str = "", action: str = "show", amount: int = 0) -> str:
        """Inspect or mutate the active character's resource pools through the
        engine — HP, spell slots, hit dice, and any pack-declared pool.

        `action` is show (default), spend, set or recover; `pool` names the pool
        (spell_slot_1..9, hp, temp_hp, hit_die_d10, ...); `amount` is the spend/set
        value. Setting spell slots is how a keeper tops a caster up when the
        story grants it (`.resource` mirror)."""
        command = ".resource"
        if action != "show" and pool:
            command += f" {action} {pool}"
            if action != "recover" and amount:
                command += f" {amount}"
        elif pool:
            command += f" show {pool}"
        return await self._dispatch_command(ctx, command, "kp_tools.cast.unavailable")

    @tool(read_only=False, needs="spells")
    async def manage_spells(self, ctx: AgentCtx, *, spell: str = "", action: str = "list") -> str:
        """Manage the active character's known spells through the engine.

        `action` is list (default), learn or forget; `spell` names the spell (id or
        localized display name). Learning records real sheet data enforced at cast
        time — use this when the story grants a new spell, never just narrate it
        (`.spells` mirror)."""
        command = ".spells"
        if action != "list" and spell:
            command += f" {action} {spell}"
        return await self._dispatch_command(ctx, command, "kp_tools.cast.unavailable")

    async def _runtime_tracker(
        self,
        ctx: AgentCtx,
        *,
        action: str,
        name: str | None,
        initiative: int | None,
        pack: RulePack,
    ) -> str:
        i18n = self.services.i18n.with_locale(ctx.locale)
        manager = CombatManager(self.services.store, ctx.chat_key)
        state = await manager.get()
        if action == "add":
            if name is None:
                character = await _get_active_character(self.services, ctx)
                name = character.name
                if initiative is None:
                    initiative = roll_initiative(self.services, character).total
            if initiative is None:
                initiative = 0
            if state is None or state.phase == "ended":
                state = create_combat(
                    f"{ctx.chat_key}:combat",
                    budget={
                        str(key): int(value)
                        for key, value in pack.runtime_spec.budgets.items()
                        if isinstance(value, (int, float)) and not isinstance(value, bool)
                    },
                )
                expected_raw = None
            else:
                expected_raw = state.json()
            controller = "keeper" if ctx.platform == "cli" else "human"
            controller_id = "keeper" if controller == "keeper" else ctx.uid()
            updated = join_combat(
                state,
                str(name),
                name=str(name),
                initiative=int(initiative),
                controller=controller,
                controller_id=controller_id,
                budget={
                    str(key): int(value)
                    for key, value in pack.runtime_spec.budgets.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                },
            )
            ctx.emit_dice({"kind": "init", "actor": str(name), "expr": pack.runtime_spec.initiative, "rolls": [], "total": int(initiative)})
            if not await manager.save(updated, expected_raw=expected_raw):
                return i18n.t("kp_tools.initiative.failed", error="combat_state_changed")
            return i18n.t("kp_tools.initiative.added", name=name, initiative=initiative)
        if state is None or not state.order:
            return i18n.t("kp_tools.initiative.empty")
        if action in {"list", "show"}:
            lines = [
                i18n.t("kp_tools.initiative.list_header"),
                i18n.t(
                    "kp_tools.initiative.status",
                    round=max(1, state.round),
                    current=state.current or state.order[0],
                ),
            ]
            for index, combatant_id in enumerate(state.order, 1):
                combatant = state.combatants[combatant_id]
                lines.append(
                    i18n.t(
                        "kp_tools.initiative.list_item",
                        index=index,
                        name=combatant.get("name", combatant_id),
                        initiative=combatant.get("initiative", 0),
                    )
                )
            return "\n".join(lines)
        if action == "clear":
            updated = end_combat(state) if state.phase in {"pending", "active"} else state
            if updated is not state and not await manager.save(updated, expected_raw=state.json()):
                return i18n.t("kp_tools.initiative.failed", error="combat_state_changed")
            return i18n.t("kp_tools.initiative.cleared")
        if action == "next":
            if state.phase == "pending":
                started = start_combat(
                    state,
                    budget={
                        str(key): int(value)
                        for key, value in pack.runtime_spec.budgets.items()
                        if isinstance(value, (int, float)) and not isinstance(value, bool)
                    },
                )
                if not await manager.save(started, expected_raw=state.json()):
                    return i18n.t("kp_tools.initiative.failed", error="combat_state_changed")
                state = started
            if state.phase != "active" or state.current is None:
                return i18n.t("kp_tools.initiative.empty")
            claimed = claim_turn(state, state.current, "keeper", keeper_override=True)
            updated = end_turn(claimed, state.current, claim_token=str(claimed.claim["token"]))
            if not await manager.save(updated, expected_raw=state.json()):
                return i18n.t("kp_tools.initiative.failed", error="combat_state_changed")
            return i18n.t("kp_tools.initiative.next_turn", name=updated.current or "-")
        return i18n.t("kp_tools.initiative.unknown_action", action=action)

    @tool
    async def initiative_tracker(
        self, ctx: AgentCtx, action: str, name: str | None = None, initiative: int | None = None
    ) -> str:
        """Manage the combat initiative order.

        Args:
            action: Operation (add/list/clear/next).
            name: Character/NPC name (defaults to the active character when adding).
            initiative: Initiative value (auto-rolled for the active character when adding, if omitted).
        """
        try:
            pack = await self.services.room_rulepack(ctx)
        except Exception:
            pack = None
        if pack is not None and pack.runtime_spec is not None:
            try:
                return await self._runtime_tracker(
                    ctx,
                    action=action,
                    name=name,
                    initiative=initiative,
                    pack=pack,
                )
            except Exception as exc:
                return self.services.i18n.with_locale(ctx.locale).t(
                    "kp_tools.initiative.failed",
                    error=str(exc),
                )
        i18n = self.services.i18n.with_locale(ctx.locale)
        chat_key = ctx.chat_key
        store_key = "initiative"
        meta_key = "initiative_meta"

        try:
            init_data = await self.services.store.state_get(chat_key, store_key)
            init_list = json.loads(init_data) if init_data else []
            meta_data = await self.services.store.state_get(chat_key, meta_key)
            parsed_meta = json.loads(meta_data) if meta_data else {}
            meta = parsed_meta if isinstance(parsed_meta, dict) else {}
            round_number = max(1, int(meta.get("round", 1)))
            turns_in_round = max(0, int(meta.get("turns", 0)))

            if action == "add":
                starting_combat = not init_list
                if name is None:
                    character = await _get_active_character(self.services, ctx)
                    name = character.name
                    if initiative is None:
                        initiative = roll_initiative(self.services, character).total

                init_list.append({"name": name, "init": initiative})
                init_list.sort(key=lambda entry: entry["init"], reverse=True)
                ctx.emit_dice({"kind": "init", "actor": name, "expr": name, "rolls": [], "total": initiative})
                await self.services.store.state_set(
                    chat_key, store_key, json.dumps(init_list, ensure_ascii=False)
                )
                if starting_combat:
                    round_number = 1
                    turns_in_round = 0
                await self.services.store.state_set(
                    chat_key, meta_key, json.dumps({"round": round_number, "turns": turns_in_round})
                )
                return i18n.t("kp_tools.initiative.added", name=name, initiative=initiative)

            if action in {"list", "show"}:
                if not init_list:
                    return i18n.t("kp_tools.initiative.empty")
                lines = [
                    i18n.t("kp_tools.initiative.list_header"),
                    i18n.t(
                        "kp_tools.initiative.status",
                        round=round_number,
                        current=init_list[0]["name"],
                    ),
                ]
                for index, entry in enumerate(init_list, 1):
                    lines.append(
                        i18n.t(
                            "kp_tools.initiative.list_item",
                            index=index,
                            name=entry["name"],
                            initiative=entry["init"],
                        )
                    )
                return "\n".join(lines)

            if action == "clear":
                await self.services.store.state_set(chat_key, store_key, "[]")
                await self.services.store.state_delete(chat_key, meta_key)
                return i18n.t("kp_tools.initiative.cleared")

            if action == "next":
                # The pointer lives in exactly two rows — the order and its meta — and
                # they advance together or not at all. (They used to CAS against the
                # session record too, purely to mirror the round into the battle report;
                # the report no longer holds combat state, and `initiative_meta` was
                # always the authority `net.state` reads.)
                for _attempt in range(3):
                    current_init_data = await self.services.store.state_get(chat_key, store_key)
                    current_meta_data = await self.services.store.state_get(chat_key, meta_key)
                    current_list = json.loads(current_init_data) if current_init_data else []
                    current_meta = json.loads(current_meta_data) if current_meta_data else {}
                    if not current_list:
                        return i18n.t("kp_tools.initiative.empty")

                    next_round = max(1, int(current_meta.get("round", 1)))
                    next_turn = max(0, int(current_meta.get("turns", 0))) + 1
                    finished = current_list.pop(0)
                    current_list.append(finished)
                    if next_turn >= len(current_list):
                        next_round += 1
                        next_turn = 0
                    next_name = str(current_list[0]["name"])
                    next_list_data = json.dumps(current_list, ensure_ascii=False)
                    next_meta_data = json.dumps(
                        {"round": next_round, "turns": next_turn, "current": next_name},
                        ensure_ascii=False,
                    )
                    committed = await self.services.store.state_set_if_values(
                        chat_key,
                        expected=[
                            (store_key, current_init_data),
                            (meta_key, current_meta_data),
                        ],
                        updates=[
                            (store_key, next_list_data),
                            (meta_key, next_meta_data),
                        ],
                    )
                    if not committed:
                        continue
                    return i18n.t("kp_tools.initiative.next_turn", name=next_name)
                raise RuntimeError("initiative_state_changed")

            return i18n.t("kp_tools.initiative.unknown_action", action=action)
        except Exception as exc:
            return i18n.t("kp_tools.initiative.failed", error=str(exc))


# --- Room lifecycle (M23 WS1) -----------------------------------------------
ROOM_FACETS = (
    RoomStateFacet(
        name="initiative",
        owner="agent.kp_tools_mechanics",
        reset_scope="story",
        state_keys=frozenset({"initiative", "initiative_meta"}),
        storages=frozenset({STORAGE_ROOM_STATE}),
    ),
)
