"""The `.item` command — manage items on character sheets (phase 2).

Items are `item` documents (agent.items); grant requires the room's catalog
(D6), equip slots drive bonuses (D3), and every mutation refreshes the sheet's
equipped_bonuses. The verbs address any member's holdings (table-level read,
D5) and the keeper-gated cross-character `give`.

Subcommands:
    .item inv [name]            — show a character's items (any member; table-level read)
    .item add <name> [qty]      — add an item to your active character (from the catalog)
    .item drop <name> [qty]     — remove an item from your active character
    .item give <item> <to>      — keeper: give an item to another character
    .item equip <name> [slot]   — equip an item (bonus applies)
    .item unequip <name>        — unequip an item (bonus stops)

Every user-facing string routes through `infra.i18n` + `locales/{en,zh}/commands.json`.
"""

from __future__ import annotations

import re

from agent.items import (
    aggregate_equipped_bonuses,
    catalog_template,
    find_instance,
    grant_instance,
    instances_for_owner,
    render_held_items,
    render_item_views,
    set_equipped,
)
from core.character_manager import CharacterDataError, has_character
from gateway.commands.rooms import _is_keeper
from gateway.commands.types import CommandCtx


def _parse_item_qty(rest: str) -> tuple[str, int]:
    """Split `<name> [qty]` — a trailing all-digits token is the quantity."""
    parts = rest.split()
    if not parts:
        return "", 1
    if len(parts) >= 2 and parts[-1].isdigit():
        return " ".join(parts[:-1]), int(parts[-1])
    return " ".join(parts), 1


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
        await _publish(ctx)


async def _refresh_bonuses(ctx: CommandCtx, char_name: str, owner_uid: str) -> None:
    """Recompute `char_name`'s equipped_bonuses AND display `equipment` list from its
    item instances and persist (so checks read the bonuses and the roster shows items)."""
    try:
        items = await instances_for_owner(ctx.services.documents, ctx.chat_key, char_name)
        sheet = await ctx.services.characters.get_character(owner_uid, ctx.chat_key, char_name)
        if not has_character(sheet):
            return
        sheet.equipped_bonuses = aggregate_equipped_bonuses(items)
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
        if sub in ("give", "给", "贈予"):
            return await self._item_give(ctx, rest)
        if sub in ("equip", "装备"):
            return await self._item_equip(ctx, rest)
        if sub in ("unequip", "卸下", "卸裝"):
            return await self._item_unequip(ctx, rest)
        return ctx.i18n.t("commands.item.usage")

    async def _item_inv(self, ctx: CommandCtx, rest: str) -> str:
        target = rest.strip()
        try:
            if not target:
                name = await _own_active_name(ctx)
                if name is None:
                    return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name="?"))
            else:
                name = target
                if not await ctx.services.characters.get_character_owner(ctx.chat_key, name):
                    return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name=name))
            instances = await instances_for_owner(ctx.services.documents, ctx.chat_key, name)
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
            await grant_instance(ctx.services.documents, ctx.chat_key, name, template, qty)
            await _refresh_bonuses(ctx, name, ctx.user_id)
            await _publish(ctx)
            return ctx.i18n.t("commands.item.added", item=item, name=name)
        except CharacterDataError:
            return ctx.fail(ctx.i18n.t("kp_tools.character.data_error"))

    async def _item_drop(self, ctx: CommandCtx, rest: str) -> str:
        item, _qty = _parse_item_qty(rest)
        if not item:
            return ctx.fail(ctx.i18n.t("commands.item.usage"))
        name = await _own_active_name(ctx)
        if name is None:
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
        # `.item give <item> <to>` — keeper-only, gives to ANY character in the room.
        if not _is_keeper(ctx.raw_ctx):
            return ctx.fail(ctx.i18n.t("commands.item.denied"))
        parts = rest.rsplit(None, 1)
        if len(parts) != 2:
            return ctx.fail(ctx.i18n.t("commands.item.usage"))
        item, target = parts[0].strip(), parts[1].strip()
        if not item or not target:
            return ctx.fail(ctx.i18n.t("commands.item.usage"))
        try:
            owner = await ctx.services.characters.get_character_owner(ctx.chat_key, target)
            if not owner:
                return ctx.fail(ctx.i18n.t("commands.item.character_not_found", name=target))
            template = await catalog_template(ctx.services.documents, ctx.chat_key, item)
            if template is None:
                return ctx.fail(ctx.i18n.t("commands.item.not_in_catalog", item=item))
            await grant_instance(ctx.services.documents, ctx.chat_key, target, template, 1)
            await _refresh_bonuses(ctx, target, owner)
            await _publish(ctx)
            return ctx.i18n.t("commands.item.given", item=item, name=target)
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
