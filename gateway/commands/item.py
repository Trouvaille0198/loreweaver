"""The `.item` command — manage items on character sheets (phase 2).

Items are `item` documents (agent.items); grants come from the room's catalog
(D6) or — keeper-only — from the improvised off-catalog lane (narrative
trinkets, light capped bonuses, consumables), equip slots drive bonuses (D3),
and every mutation refreshes the sheet's equipped_bonuses. The verbs address
any member's holdings (table-level read, D5) and the keeper-gated
cross-character `give`.

Subcommands:
    .item inv [name]            — show a character's items (any member; table-level read)
    .item add <name> [qty]      — add an item to your active character (from the catalog)
    .item drop <name> [qty]     — remove an item from your active character
    .item give <item> <to>      — keeper: give an item to another character
                                  (off-catalog names become improvised one-offs;
                                  flags: --desc <text> --bonus stat=n,… --qty n --secret)
    .item use <name> [qty]      — consume a held item (quantity decreases, zero removes it)
    .item equip <name> [slot]   — equip an item (bonus applies)
    .item unequip <name>        — unequip an item (bonus stops)

    `.item drop/archive/unarchive` accept `--on <character>` to address one of
    your OWN characters by name (a retired sheet's bag can be cleaned up); other
    characters' holdings are only readable (`inv`) or keeper-granted (`give`).

Every user-facing string routes through `infra.i18n` + `locales/{en,zh}/commands.json`.
"""

from __future__ import annotations

import re

from agent.items import (
    aggregate_equipped_bonuses,
    canonicalize_bonus_keys,
    catalog_template,
    consume_instance,
    find_instance,
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
    set_archived,
    set_equipped,
    template_with_source,
    validate_improvised_bonus,
)
from agent.module_lifecycle import active_module
from core.character_manager import CharacterDataError, has_character
from core.rulepacks import load_rulepack
from gateway.commands.rooms import _is_keeper
from gateway.commands.types import CommandCtx
from gateway.hub import Event
from gateway.turn import publish_state
from gateway.turn import publish_state


def _parse_item_qty(rest: str) -> tuple[str, int]:
    """Split `<name> [qty]` — a trailing all-digits token is the quantity."""
    parts = rest.strip().split()
    if not parts:
        return "", 1
    if len(parts) > 1 and parts[-1].isdigit():
        return " ".join(parts[:-1]), int(parts[-1])
    return " ".join(parts), 1


def _split_owner_flag(rest: str) -> tuple[str, str | None]:
    """Split a trailing `--on <character>` target off `rest`. The character name
    may contain spaces — everything after `--on` is taken as the name. Returns
    `(rest, target)` with target `None` when the flag is absent."""
    rest = rest.strip()
    marker = "--on"
    idx = rest.find(marker)
    if idx < 0:
        return rest, None
    target = rest[idx + len(marker):].strip()
    return rest[:idx].strip(), target or None

def _parse_give_flags(rest: str) -> tuple[str, str, dict[str, int], str, int, bool]:
    """Split `.item give` args: `<item> <to> [--desc <text>] [--bonus stat=n,…] [--qty n] [--secret]`.
    `--desc` consumes the rest of the line; every other flag takes one token.
    Returns (item, target, bonus, desc, qty, secret); raises ValueError on bad flags."""
    parts = rest.split()
    flag_at = next((i for i, tok in enumerate(parts) if tok.startswith("--")), None)
    if flag_at is None:
        head, tail = rest.strip(), ""
    else:
        head, tail = " ".join(parts[:flag_at]), " ".join(parts[flag_at:])
    if head.strip():
        item, target = head.strip().rsplit(None, 1)
    else:
        item = target = ""
    bonus: dict[str, int] = {}
    desc = ""
    qty = 1
    secret = False
    tokens = tail.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--secret":
            secret = True
            i += 1
        elif tok == "--qty":
            if i + 1 >= len(tokens):
                raise ValueError("missing --qty value")
            try:
                qty = int(tokens[i + 1])
            except ValueError:
                raise ValueError("--qty must be an integer") from None
            if qty < 1:
                raise ValueError("--qty must be a positive integer")  # i18n-exempt: internal flag diagnostic, surfaced as commands.item.bad_flags
            i += 2
        elif tok == "--bonus":
            if i + 1 >= len(tokens):
                raise ValueError("missing --bonus value")
            bonus = parse_bonus_spec(tokens[i + 1])
            i += 2
        elif tok == "--desc":
            desc_parts = []
            j = i + 1
            while j < len(tokens) and not tokens[j].startswith("--"):
                desc_parts.append(tokens[j])
                j += 1
            desc = " ".join(desc_parts)
            i = j
        else:
            raise ValueError(f"unknown flag: {tok}")
    return item, target, bonus, desc, qty, secret


def _render_instances(ctx: CommandCtx, name: str, instances) -> str:
    labels = []
    for doc in instances:
        data = doc.data
        label = str(data.get("name", ""))
        qty = data.get("quantity")
        if qty is not None and int(qty) > 1:
            label = f"{label} ×{int(qty)}"
        slot = data.get("equipped_slot")
        if slot:
            label = f"{label} [equipped: {slot}]"
        labels.append(label)
    if not labels:
        return ctx.i18n.t("commands.item.inv_empty", name=name)
    return ctx.i18n.t("commands.item.inv_header", name=name, items=", ".join(labels))


async def _publish(ctx: CommandCtx) -> None:
    """Broadcast the updated room snapshot to connected clients (no-op off-hub)."""
    if ctx.router.hub is not None:
        await publish_state(ctx.router.hub, ctx.services, ctx.raw_ctx)


async def _broadcast(ctx: CommandCtx, text: str) -> None:
    """Fan an item-state notice to the whole room (D5: who holds what is table talk).
    No-op off-hub (standalone CLI/tests), where the command reply is the only channel.
    `secret` items are never broadcast — their reveal belongs to the Keeper alone."""
    if ctx.router.hub is not None:
        await ctx.router.hub.publish(
            ctx.chat_key,
            Event.narrative(speaker="system", text=text, fmt="plain"),
        )


async def _refresh_bonuses(ctx: CommandCtx, char_name: str, owner_uid: str) -> None:
    """Recompute `char_name`'s equipped_bonuses AND display `equipment` list from its
    item instances and persist (so checks read the bonuses and the roster shows items)."""
    try:
        items = await instances_for_owner(ctx.services.documents, ctx.chat_key, char_name)
        sheet = await ctx.services.characters.get_character(owner_uid, ctx.chat_key, char_name)
        if not has_character(sheet):
            return
        active = await active_module(ctx.services, ctx.chat_key)
        sheet.equipped_bonuses = aggregate_equipped_bonuses(items, active)
        sheet.equipment = render_held_items(items)
        sheet.items = render_item_views(items)
        await ctx.services.characters.save_character(owner_uid, ctx.chat_key, sheet)
    except Exception:
        return


async def _own_active_name(ctx: CommandCtx) -> str | None:
    sheet = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
    if not has_character(sheet):
        return None
    return sheet.name


async def _resolve_own_or_target(ctx: CommandCtx, target: str | None) -> str | None:
    """The character an item verb operates on: the caller's active character, or
    `target` when `--on` names one of the caller's OWN characters (a retired sheet
    the caller still owns may be cleaned up; anyone else's sheet is refused)."""
    if target is None:
        return await _own_active_name(ctx)
    owner = await ctx.services.characters.get_character_owner(ctx.chat_key, target)
    if owner != ctx.user_id:
        return None
    return target


class ItemCommands:
    """`.item` — phase 2: view/grant/drop/equip gear on `item` documents."""

    async def cmd_item(self, ctx: CommandCtx) -> str:
        args = ctx.args.strip()
        parts = args.split(maxsplit=1)
        sub = parts[0].casefold() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if not sub:
            return ctx.i18n.t("commands.item.usage")
        if sub in ("inv", "list", "show", "背包", "查看"):
            return await self._item_inv(ctx, rest)
        if sub in ("add", "gain", "获得", "添加"):
            return await self._item_add(ctx, rest)
        if sub in ("drop", "remove", "丢失", "移除", "丢弃"):
            return await self._item_drop(ctx, rest)
        if sub in ("use", "consume", "使用", "消耗"):
            return await self._item_use(ctx, rest)
        if sub in ("give", "给", "贈予"):
            return await self._item_give(ctx, rest)
        if sub in ("equip", "装备"):
            return await self._item_equip(ctx, rest)
        if sub in ("unequip", "卸下", "卸裝"):
            return await self._item_unequip(ctx, rest)
        if sub in ("archive", "归档", "封存"):
            return await self._item_archive(ctx, rest)
        if sub in ("unarchive", "restore", "恢复", "解封", "還原"):
            return await self._item_unarchive(ctx, rest)
    async def _item_inv(self, ctx: CommandCtx, rest: str) -> str:
        target = rest.strip()
        archived_only = target.casefold() in ("--archived", "已归档", "归档")
        if archived_only:
            target = ""
        try:
            if not target:
                name = await _own_active_name(ctx)
                if name is None:
                    return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name="?"))
                owner_uid = ctx.user_id
            else:
                name = target
                owner_uid = await ctx.services.characters.get_character_owner(ctx.chat_key, name)
                if not owner_uid:
                    return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name=name))
            instances = await instances_for_owner(ctx.services.documents, ctx.chat_key, name)
            # Archived items stay out of the active bag; `--archived` lists them.
            if archived_only:
                instances = [doc for doc in instances if doc.data.get("archived")]
            else:
                instances = [doc for doc in instances if not doc.data.get("archived")]
            # D5: `secret` items are keeper/owner-only — a player viewing another
            # member's bag sees none of them; the keeper and the owner see all.
            if not _is_keeper(ctx.raw_ctx) and owner_uid != ctx.user_id:
                instances = [doc for doc in instances if not doc.data.get("secret")]
            return _render_instances(ctx, name, instances)
        except CharacterDataError:
            return ctx.fail(ctx.i18n.t("kp_tools.character.data_error"))

    async def _item_add(self, ctx: CommandCtx, rest: str) -> str:
        item, qty = _parse_item_qty(rest)
        if not item or qty < 1:
            return ctx.fail(ctx.i18n.t("commands.item.usage"))
        name = await _own_active_name(ctx)
        if name is None:
            return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name="?"))
        try:
            template = await catalog_template(ctx.services.documents, ctx.chat_key, item)
            if template is None:
                return ctx.fail(ctx.i18n.t("commands.item.not_in_catalog", item=item))
            active = await active_module(ctx.services, ctx.chat_key)
            if not item_active(active, template):
                return ctx.fail(ctx.i18n.t("commands.item.module_mismatch", item=item))
            template = template_with_source(template, active)
            await grant_instance(ctx.services.documents, ctx.chat_key, name, template, qty)
            await reveal_linked_clues(ctx.services, ctx.raw_ctx, template)
            await _refresh_bonuses(ctx, name, ctx.user_id)
            await _publish(ctx)
            if not template.get("secret"):
                await _broadcast(ctx, ctx.i18n.t("commands.item.added", item=item, name=name))
            return ctx.i18n.t("commands.item.added", item=item, name=name)
        except CharacterDataError:
            return ctx.fail(ctx.i18n.t("kp_tools.character.data_error"))

    async def _item_drop(self, ctx: CommandCtx, rest: str) -> str:
        rest, target = _split_owner_flag(rest)
        item, _qty = _parse_item_qty(rest)
        if not item:
            return ctx.fail(ctx.i18n.t("commands.item.usage"))
        name = await _resolve_own_or_target(ctx, target)
        if name is None:
            if target is not None:
                return ctx.fail(ctx.i18n.t("commands.item.not_owned", name=target))
            return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name="?"))
        try:
            doc = await find_instance(ctx.services.documents, ctx.chat_key, name, item)
            if doc is None:
                return ctx.fail(ctx.i18n.t("commands.item.not_found", name=name, item=item))
            await ctx.services.documents.delete(ctx.chat_key, "item", doc.id)
            await _refresh_bonuses(ctx, name, ctx.user_id)
            await _publish(ctx)
            return ctx.i18n.t("commands.item.dropped", item=item, name=name)
        except CharacterDataError:
            return ctx.fail(ctx.i18n.t("kp_tools.character.data_error"))

    async def _item_give(self, ctx: CommandCtx, rest: str) -> str:
        # `.item give <item> <to> [--desc <text>] [--bonus stat=n,…] [--qty n] [--secret]`
        # — keeper-only. A catalog item grants from its template (its description/
        # bonus/secret are the module's, not overridable; --qty still applies);
        # any other name becomes an IMPROVISED one-off — narrative trinket, light
        # capped bonus or consumable — capped so it can never rival a designed
        # item. Players still cannot add off-catalog items: D6 keeps the player lane.
        if not _is_keeper(ctx.raw_ctx):
            return ctx.fail(ctx.i18n.t("commands.item.denied"))
        try:
            item, target, bonus, desc, qty, secret = _parse_give_flags(rest)
        except ValueError:
            return ctx.fail(ctx.i18n.t("commands.item.bad_flags"))
        if not item or not target:
            return ctx.fail(ctx.i18n.t("commands.item.usage"))
        try:
            owner = await ctx.services.characters.get_character_owner(ctx.chat_key, target)
            if not owner:
                return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name=target))
            template = await catalog_template(ctx.services.documents, ctx.chat_key, item)
            if template is not None:
                if bonus or desc or secret:
                    return ctx.fail(ctx.i18n.t("commands.item.catalog_flag_denied", item=item))
                active = await active_module(ctx.services, ctx.chat_key)
                if not item_active(active, template):
                    return ctx.fail(ctx.i18n.t("commands.item.module_mismatch", item=item))
                template = template_with_source(template, active)
                await grant_instance(ctx.services.documents, ctx.chat_key, target, template, qty)
                await reveal_linked_clues(ctx.services, ctx.raw_ctx, template)
                await _refresh_bonuses(ctx, target, owner)
                await _publish(ctx)
                if not template.get("secret"):
                    await _broadcast(ctx, ctx.i18n.t("commands.item.given", item=item, name=target))
                return ctx.i18n.t("commands.item.given", item=item, name=target)
            error = validate_improvised_bonus(bonus)
            if error:
                return ctx.fail(ctx.i18n.t(f"commands.item.improv_{error}"))
            sheet = await ctx.services.characters.get_character(owner, ctx.chat_key, target)
            pack = None
            if has_character(sheet):
                try:
                    pack = load_rulepack(sheet.system)
                except Exception:
                    pack = None
            bonus, unresolved = canonicalize_bonus_keys(bonus, pack)
            active = await active_module(ctx.services, ctx.chat_key)
            template = improvised_template(
                item,
                description=desc,
                bonus=bonus,
                secret=secret,
                source_module_id=module_source_id(active),
            )
            await grant_improvised_instance(ctx.services.documents, ctx.chat_key, target, template, qty)
            await _refresh_bonuses(ctx, target, owner)
            await _publish(ctx)
            if not secret:
                await _broadcast(ctx, ctx.i18n.t("commands.item.improvised_given", item=item, name=target))
            result = ctx.i18n.t("commands.item.improvised_given", item=item, name=target)
            if unresolved:
                result += "\n" + ctx.i18n.t("commands.item.improv_unresolved_bonus", keys=", ".join(unresolved))
            return result
        except CharacterDataError:
            return ctx.fail(ctx.i18n.t("kp_tools.character.data_error"))

    async def _item_use(self, ctx: CommandCtx, rest: str) -> str:
        # `.item use <name> [qty]` — consume a held item on your active character
        # (drinks a potion, spends a token): quantity decreases, zero removes it.
        item, qty = _parse_item_qty(rest)
        if not item or qty < 1:
            return ctx.fail(ctx.i18n.t("commands.item.usage"))
        name = await _own_active_name(ctx)
        if name is None:
            return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name="?"))
        try:
            found, remaining = await consume_instance(ctx.services.documents, ctx.chat_key, name, item, qty)
            if not found:
                return ctx.fail(ctx.i18n.t("commands.item.not_found", name=name, item=item))
            await _refresh_bonuses(ctx, name, ctx.user_id)
            await _publish(ctx)
            if remaining is None:
                await _broadcast(ctx, ctx.i18n.t("commands.item.used_up", item=item, name=name))
                return ctx.i18n.t("commands.item.used_up", item=item, name=name)
            await _broadcast(ctx, ctx.i18n.t("commands.item.used", item=item, name=name, remaining=remaining))
            return ctx.i18n.t("commands.item.used", item=item, name=name, remaining=remaining)
        except CharacterDataError:
            return ctx.fail(ctx.i18n.t("kp_tools.character.data_error"))
        # `.item use <name> [qty]` — consume a held item on your active character
        # (drinks a potion, spends a token): quantity decreases, zero removes it.
        item, qty = _parse_item_qty(rest)
        if not item or qty < 1:
            return ctx.fail(ctx.i18n.t("commands.item.usage"))
        name = await _own_active_name(ctx)
        if name is None:
            return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name="?"))
        try:
            doc = await find_instance(ctx.services.documents, ctx.chat_key, name, item)
            if doc is None:
                return ctx.fail(ctx.i18n.t("commands.item.not_found", name=name, item=item))
            if doc.data.get("archived"):
                return ctx.fail(ctx.i18n.t("commands.item.archived_first", item=item))
            found, remaining = await consume_instance(ctx.services.documents, ctx.chat_key, name, item, qty)
            await _refresh_bonuses(ctx, name, ctx.user_id)
            await _publish(ctx)
            if remaining is None:
                return ctx.i18n.t("commands.item.used_up", item=item, name=name)
            return ctx.i18n.t("commands.item.used", item=item, name=name, remaining=remaining)
        except CharacterDataError:
            return ctx.fail(ctx.i18n.t("kp_tools.character.data_error"))
    async def _item_equip(self, ctx: CommandCtx, rest: str) -> str:
        rest = rest.strip()
        if not rest:
            return ctx.fail(ctx.i18n.t("commands.item.usage"))
        # Multi-word item names are common ("Fencing Sword"); an explicit slot is
        # given as `.item equip <name> as <slot>`, else the item's declared slot.
        match = re.match(r"^(.*?)\s+as\s+(\S+)$", rest)
        if match:
            item, slot = match.group(1).strip(), match.group(2)
        else:
            item, slot = rest, ""
        name = await _own_active_name(ctx)
        if name is None:
            return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name="?"))
        try:
            doc = await find_instance(ctx.services.documents, ctx.chat_key, name, item)
            if doc is None:
                return ctx.fail(ctx.i18n.t("commands.item.not_found", name=name, item=item))
            if doc.data.get("archived"):
                return ctx.fail(ctx.i18n.t("commands.item.archived_first", item=item))
            effective_slot = slot or str(doc.data.get("slot") or "equipped")
            await set_equipped(ctx.services.documents, ctx.chat_key, doc.id, effective_slot)
            await _refresh_bonuses(ctx, name, ctx.user_id)
            await _publish(ctx)
            return ctx.i18n.t("commands.item.equipped", item=item, name=name, slot=effective_slot)
        except CharacterDataError:
            return ctx.fail(ctx.i18n.t("kp_tools.character.data_error"))

    async def _item_unequip(self, ctx: CommandCtx, rest: str) -> str:
        item = rest.strip()
        if not item:
            return ctx.fail(ctx.i18n.t("commands.item.usage"))
        name = await _own_active_name(ctx)
        if name is None:
            return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name="?"))
        try:
            doc = await find_instance(ctx.services.documents, ctx.chat_key, name, item)
            if doc is None:
                return ctx.fail(ctx.i18n.t("commands.item.not_found", name=name, item=item))
            await set_equipped(ctx.services.documents, ctx.chat_key, doc.id, None)
            await _refresh_bonuses(ctx, name, ctx.user_id)
            await _publish(ctx)
            return ctx.i18n.t("commands.item.unequipped", item=item, name=name)
        except CharacterDataError:
            return ctx.fail(ctx.i18n.t("kp_tools.character.data_error"))

    async def _item_archive(self, ctx: CommandCtx, rest: str) -> str:
        """`.item archive <name> [--on <character>]` — shelf an item on your active
        character (or one of your own characters via `--on`, e.g. a retired sheet):
        out of the active bag, the wire views and the bonus aggregation, so it stops
        mattering in play and does not carry into another scenario. The record
        survives — `.item unarchive <name>` brings it back."""
        rest, target = _split_owner_flag(rest)
        item = rest.strip()
        if not item:
            return ctx.fail(ctx.i18n.t("commands.item.usage"))
        name = await _resolve_own_or_target(ctx, target)
        if name is None:
            if target is not None:
                return ctx.fail(ctx.i18n.t("commands.item.not_owned", name=target))
            return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name="?"))
        try:
            doc = await find_instance(ctx.services.documents, ctx.chat_key, name, item)
            if doc is None:
                return ctx.fail(ctx.i18n.t("commands.item.not_found", name=name, item=item))
            if doc.data.get("archived"):
                return ctx.i18n.t("commands.item.already_archived", item=item, name=name)
            await set_archived(ctx.services.documents, ctx.chat_key, doc.id, True)
            await _refresh_bonuses(ctx, name, ctx.user_id)
            await _publish(ctx)
            return ctx.i18n.t("commands.item.archived", item=item, name=name)
        except CharacterDataError:
            return ctx.fail(ctx.i18n.t("kp_tools.character.data_error"))

    async def _item_unarchive(self, ctx: CommandCtx, rest: str) -> str:
        """`.item unarchive <name> [--on <character>]` — bring a shelved item back
        into the active bag (or one of your own characters via `--on`)."""
        rest, target = _split_owner_flag(rest)
        item = rest.strip()
        if not item:
            return ctx.fail(ctx.i18n.t("commands.item.usage"))
        name = await _resolve_own_or_target(ctx, target)
        if name is None:
            if target is not None:
                return ctx.fail(ctx.i18n.t("commands.item.not_owned", name=target))
            return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name="?"))
        try:
            doc = await find_instance(ctx.services.documents, ctx.chat_key, name, item)
            if doc is None:
                return ctx.fail(ctx.i18n.t("commands.item.not_found", name=name, item=item))
            if not doc.data.get("archived"):
                return ctx.i18n.t("commands.item.not_archived", item=item, name=name)
            await set_archived(ctx.services.documents, ctx.chat_key, doc.id, False)
            await _refresh_bonuses(ctx, name, ctx.user_id)
            await _publish(ctx)
            return ctx.i18n.t("commands.item.unarchived", item=item, name=name)
        except CharacterDataError:
            return ctx.fail(ctx.i18n.t("kp_tools.character.data_error"))
