"""The item lane's catalog + instance helpers (phase 2).

A room's item CATALOG is a singleton `item_catalog` document seeded from the module
script's `items` analysis (and, when the active rulepack declares one, its `items:`
section). Item INSTANCES are `item` documents (one per holding, doc_id = a unique id,
`owner` = the character's sheet name — unique within a room).

This module is the ONE place the tools and CLI read/write both, so grant validation
(D6: catalog-gated grants, with the Keeper's capped off-catalog improv lane as the
single exception) and the equipped-bonus aggregation (D3) live here once.
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

# ---------------------------------------------------------------------------
# Improv lane — off-catalog grants (D6's single exception)
# ---------------------------------------------------------------------------
# A Keeper may hand out items the catalog does not know: narrative trinkets
# (no bonus), light edges (small capped bonus) or consumables (quantity). The
# caps are engine constants, not pack data — the improv lane belongs to the
# Keeper, and a module author must not govern it. An improvised item can be a
# surprise, never a rival to a designed catalog item.


IMPROVISED_MAX_BONUS = 2
IMPROVISED_MAX_BONUS_TOTAL = 4


def parse_bonus_spec(text: str) -> dict[str, int]:
    """Parse a ``stat=value,stat=value`` bonus spec into {canonical: delta}.
    Raises ValueError on malformed input (shared by the CLI and the Keeper tool)."""
    out: dict[str, int] = {}
    for pair in text.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, sep, raw = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"malformed bonus spec: {pair!r}")
        try:
            delta = int(raw.strip())
        except (TypeError, ValueError):
            raise ValueError(f"bonus value must be an integer: {pair!r}") from None
        out[key.strip()] = delta
    return out


def validate_improvised_bonus(bonus: dict) -> str | None:
    """None when ``bonus`` fits the improv budget; else an error code
    (``invalid_value`` | ``stat_cap`` | ``total_cap``) the caller maps to i18n."""
    if not isinstance(bonus, dict):
        return "invalid_value"
    if not bonus:
        return None
    total = 0
    for canon, delta in bonus.items():
        if not isinstance(canon, str) or not canon.strip():
            return "invalid_value"
        if isinstance(delta, bool) or not isinstance(delta, int):
            return "invalid_value"
        if abs(delta) > IMPROVISED_MAX_BONUS:
            return "stat_cap"
        total += abs(delta)
    if total > IMPROVISED_MAX_BONUS_TOTAL:
        return "total_cap"
    return None


def improvised_template(
    name: str, *, description: str = "", bonus: dict | None = None, secret: bool = False
) -> dict:
    """A one-off template for an off-catalog grant. Improvised items are
    universal-scope (they travel with the holder, never die with a module) and
    carry at most a small capped bonus."""
    return {
        "name": name,
        "description": description,
        "bonus": dict(bonus) if isinstance(bonus, dict) and bonus else {},
        "scope": "universal",
        "secret": bool(secret),
        "improvised": True,
    }


async def consume_instance(
    documents: DocumentStore, chat_key: str, owner: str, name: str, qty: int = 1
) -> tuple[bool, int | None]:
    """Use up ``qty`` of an instance (consumable semantics): quantity decreases,
    and the instance disappears when it hits zero. Returns ``(found, remaining)``;
    ``remaining`` is None when the instance is gone."""
    doc = await find_instance(documents, chat_key, owner, name)
    if doc is None:
        return False, None
    new_qty = int(doc.data.get("quantity", 1)) - int(qty)
    if new_qty <= 0:
        await documents.delete(chat_key, "item", doc.id)
        return True, None
    await documents.put(chat_key, "item", doc.id, {**doc.data, "quantity": new_qty})
    return True, new_qty


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
        "scope": str(template.get("scope") or ""),
        "module_id": str(template.get("module_id") or ""),
        "description": str(template.get("description") or ""),
        "lore": str(template.get("lore") or ""),
        "effect": str(template.get("effect") or ""),
        "bonus": template.get("bonus") if isinstance(template.get("bonus"), dict) else {},
        "origin": str(template.get("origin") or ""),
        "original_holder": str(template.get("original_holder") or ""),
        "secret": bool(template.get("secret", False)),
        "improvised": bool(template.get("improvised", False)),
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
    "improvised",
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


def item_active(active_module: dict | None, data: dict) -> bool:
    """Whether an item instance contributes in the current room. Universal items (scope
    ``universal``, or legacy items with no module binding) always do; module-scoped items
    only while the room's active module matches the item's ``module_id`` (pack_id or
    source_id). With no active module, module-scoped items are inert — a plot artifact
    from another campaign must never leak bonuses into a sandbox room."""
    scope = str(data.get("scope") or "")
    if scope == "universal":
        return True
    module_id = str(data.get("module_id") or "").strip()
    if not module_id:
        return True  # unbound legacy item — treat as universal
    if not active_module:
        return False
    return module_id in {
        str(active_module.get("pack_id") or ""),
        str(active_module.get("source_id") or ""),
    }


def aggregate_equipped_bonuses(
    items: list[Document], active_module: dict | None = None
) -> dict[str, int]:
    """Sum the structured `bonus` maps ({canonical: delta}) of every EQUIPPED item
    (equipped_slot set) that is ACTIVE in this room (`item_active` — module-scoped
    items contribute nothing outside their own module). Used to refresh a sheet's
    `equipped_bonuses`."""
    total: dict[str, int] = {}
    for doc in items:
        data = doc.data
        if data.get("equipped_slot") is None:
            continue
        if not item_active(active_module, data):
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
