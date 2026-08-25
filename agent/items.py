"""The item lane's catalog + instance helpers (phase 2).

A room's item CATALOG is a singleton `item_catalog` document seeded from the module
script's `items` analysis (and, when the active rulepack declares one, its `items:`
section). Item INSTANCES are `item` documents (one per holding, doc_id = a unique id,
`owner` = the character's sheet name — unique within a room).

This module is the ONE place the tools and CLI read/write both, so grant validation
(D6: no template-less items) and the equipped-bonus aggregation (D3) live here once.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from core.documents import Document, DocumentStore
from infra.room_facets import STORAGE_DOCUMENTS, RoomStateFacet

ITEM_CATALOG_ID = "item_catalog"

# Room lifecycle: item instances and the catalog are room content that ships with the
# module's world — cleared on `reset all` (they install with the script's items and
# leave with the room), never on a narrower scope.
ROOM_FACETS = (
    RoomStateFacet(
        name="items",
        owner="agent.items",
        reset_scope="all",
        doc_types=frozenset({"item", "item_catalog"}),
        state_keys=frozenset(),
        storages=frozenset({STORAGE_DOCUMENTS}),
    ),
)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


async def get_item_catalog(documents: DocumentStore, chat_key: str) -> list[dict]:
    """The room's item templates as a list (empty when none seeded)."""
    doc = await documents.get_singleton(chat_key, "item_catalog")
    if doc is None:
        return []
    items = doc.data.get("items")
    return items if isinstance(items, list) else []


async def catalog_template(documents: DocumentStore, chat_key: str, name: str) -> dict | None:
    """One catalog template by (case-insensitive) name, or None."""
    folded = name.casefold()
    for entry in await get_item_catalog(documents, chat_key):
        if isinstance(entry, dict) and str(entry.get("name", "")).casefold() == folded:
            return entry
    return None


async def ensure_catalog(documents: DocumentStore, chat_key: str, templates: list[dict]) -> None:
    """Merge `templates` into the room's catalog, preserving existing entries.
    Idempotent by name — an existing template is left untouched."""
    existing = await get_item_catalog(documents, chat_key)
    names = {str(e.get("name", "")).casefold() for e in existing if isinstance(e, dict)}
    merged = list(existing)
    for tpl in templates:
        if not isinstance(tpl, dict):
            continue
        name = str(tpl.get("name", "")).strip()
        if name and name.casefold() not in names:
            merged.append(tpl)
            names.add(name.casefold())
    await documents.put_singleton(chat_key, "item_catalog", {"items": merged})


# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------


def _instance_data(owner: str, template: dict, qty: int) -> dict[str, Any]:
    name = str(template.get("name") or template.get("template_id") or "").strip()
    return {
        "template_id": str(template.get("template_id") or name),
        "name": name,
        "owner": owner,
        "quantity": int(qty),
        "state": {},
        "equipped_slot": None,
        "kind": str(template.get("kind") or ""),
        "slot": str(template.get("slot") or ""),
        "description": str(template.get("description") or ""),
        "lore": str(template.get("lore") or ""),
        "effect": str(template.get("effect") or ""),
        "bonus": template.get("bonus") if isinstance(template.get("bonus"), dict) else {},
        "origin": str(template.get("origin") or ""),
        "original_holder": str(template.get("original_holder") or ""),
        "secret": bool(template.get("secret", False)),
    }


async def find_instance(
    documents: DocumentStore, chat_key: str, owner: str, name: str
) -> Document | None:
    """The owner's instance matching `name` (case-insensitive), or None."""
    folded = name.casefold()
    for doc in await documents.list(chat_key, "item"):
        data = doc.data
        if data.get("owner") == owner and str(data.get("name", "")).casefold() == folded:
            return doc
    return None


async def instances_for_owner(documents: DocumentStore, chat_key: str, owner: str) -> list[Document]:
    return [doc for doc in await documents.list(chat_key, "item") if doc.data.get("owner") == owner]


async def grant_instance(
    documents: DocumentStore, chat_key: str, owner: str, template: dict, qty: int = 1
) -> Document:
    """Create or merge an item instance for `owner` from a catalog `template`.
    Same-owner same-name instances merge their quantity."""
    name = str(template.get("name") or template.get("template_id") or "").strip()
    existing = await find_instance(documents, chat_key, owner, name)
    if existing is not None:
        new_qty = int(existing.data.get("quantity", 1)) + int(qty)
        return await documents.put(
            chat_key, "item", existing.id, {**existing.data, "quantity": new_qty}
        )
    return await documents.put(
        chat_key, "item", uuid4().hex, _instance_data(owner, template, qty)
    )


async def set_equipped(
    documents: DocumentStore, chat_key: str, instance_id: str, slot: str | None
) -> Document | None:
    """Set an instance's equip slot (None = unequip). Returns the updated doc, or
    None if the instance is gone."""
    doc = await documents.get(chat_key, "item", instance_id)
    if doc is None:
        return None
    return await documents.put(
        chat_key, "item", instance_id, {**doc.data, "equipped_slot": slot}
    )


def render_held_items(items: list[Document]) -> list[str]:
    """Render a character's held items as display strings (for the sheet's
    `equipment` field, which the wire/roster surfaces to clients). `secret` items
    are omitted — they are keeper-side only and never appear in player-facing lists."""
    out: list[str] = []
    for doc in items:
        data = doc.data
        if data.get("secret"):
            continue
        name = str(data.get("name", "")).strip()
        if not name:
            continue
        qty = data.get("quantity")
        if qty is not None and int(qty) > 1:
            name = f"{name} ×{int(qty)}"
        slot = data.get("equipped_slot")
        if slot:
            name = f"{name} ({slot})"
        out.append(name)
    return out


_ITEM_VIEW_FIELDS = (
    "name",
    "kind",
    "slot",
    "description",
    "lore",
    "effect",
    "origin",
    "original_holder",
    "quantity",
    "equipped_slot",
    "bonus",
)


def render_item_views(items: list[Document]) -> list[dict[str, Any]]:
    """Structured, player-safe item views for a sheet's `items` field (which the wire/
    roster surfaces to clients for an item-detail section). `secret` items are omitted."""
    out: list[dict[str, Any]] = []
    for doc in items:
        data = doc.data
        if data.get("secret"):
            continue
        out.append({key: data.get(key) for key in _ITEM_VIEW_FIELDS})
    return out


def aggregate_equipped_bonuses(items: list[Document]) -> dict[str, int]:
    """Sum the structured `bonus` maps ({canonical: delta}) of every EQUIPPED item
    (equipped_slot set). Used to refresh a sheet's `equipped_bonuses`."""
    total: dict[str, int] = {}
    for doc in items:
        data = doc.data
        if data.get("equipped_slot") is None:
            continue
        bonus = data.get("bonus")
        if not isinstance(bonus, dict):
            continue
        for canon, delta in bonus.items():
            try:
                total[str(canon)] = total.get(str(canon), 0) + int(delta)
            except (TypeError, ValueError):
                continue
    return total
