"""AI-KP tools for importing SillyTavern character cards (`docs/specs/M12-charcard.md` §3).

`CharcardTools` bridges a persona-chat character into a real adventure: it parses a SillyTavern
card (`core.charcard`), asks the deterministic core to build a rule-LEGAL sheet biased toward the
persona (`agent.char_from_persona`), then drops the character in as EITHER the acting player's PC or
an AI player companion (M10). A card's embedded `character_book` is folded into the world lore
(M11), so the character brings its setting with it.

Every character import runs through `core.card_split` first (拆卡): the module machinery a
"heavy" ST card carries — hook scripts, `[InitVar]` variable declarations, executable EJS —
is STRIPPED from the character half and reported, because those payloads reprogram the whole
room and are the keeper's to bring in. The keeper does so with `.import <file> world`, which
calls `import_world_card` — deliberately a plain method, NOT an `@tool`: the world path exists
only behind the command surface's deterministic keeper gate, so no phrasing aimed at the model
can trigger it on a player's behalf.

Composes the already-built leaf modules with the shared services; every user-visible string is
looked up via `services.i18n` under `charcard.tools.*` (`locales/{en,zh}/charcard.json`). Card
fields (name/description/tags) are game DATA supplied at runtime, not string literals here.
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

from agent import npc as npc_records
from agent.char_from_persona import build_sheet_from_persona, infer_pronoun_note
from agent.context import AgentCtx
from agent.hook_runtime import install_room_hooks
from agent.items import ensure_catalog, normalize_item_links
from agent.kp_tools_npc import keeper_npc_refusal, player_name_refusal
from agent.module_lifecycle import (
    ModuleImportTransaction,
    active_module,
    identity_for_world_card,
    publish_active_module,
    purge_active_module,
)
from agent.services import Services
from agent.tools import tool
from core.card_split import WorldPayloads, card_hook_codes, detect_world_payloads, split_card
from core.character_manager import CharacterSheet
from core.character_rules import render_validation_notice, validate_sheet
from core.charcard import PNG_SIGNATURE, CharacterCard, parse_card_bytes
from core.documents import MODULE_POOL_ID, PLAYER_VIEWER
from core.lorecard import Lorecard, looks_like_lorecard, parse_lorecard_bytes
from core.module_brief import BRIEF_DOC_TYPE, brief_id, build_brief
from core.modvars import apply_define, load_modvars, save_modvars
from core.modvars import apply_set as apply_modvar_set
from core.modvars import empty_state as empty_modvar_state
from core.mvu_compat import MVU_DOC_ID, MVU_DOC_TYPE, flatten_leaves, load_mvu
from core.mvu_compat import apply_set as apply_mvu_set
from core.pregen_roster import pregen_add
from core.rulepacks import load_rulepack
from infra.i18n import I18n
from infra.media_store import MediaStore
from infra.room_facets import STORAGE_ROOM_STATE, RoomStateFacet

_PREVIEW_CHARS = 200
_KEY_STAT_COUNT = 6


def _load_skill_list(raw: str | None) -> list[str]:
    """Parse the room's `skills_enabled` state (a JSON list of skill ids) into a Python list,
    tolerating a missing/blank/malformed value."""
    if not raw:
        return []
    try:
        import json

        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001 — a malformed state degrades to an empty list
        return []
    return [str(item) for item in parsed if isinstance(item, str)]


def _parse_any_card_file(host_path: Path) -> tuple[CharacterCard, Lorecard | None]:
    """`core.charcard.parse_card_bytes`, extended with the native-bundle sniff (M14):
    a `*.lorecard.json` (the studio forge's lossless export) parses through
    `core.lorecard` and hands back its extras — typed variable specs — alongside the
    embedded card; anything else is a stock SillyTavern card with no extras."""
    data = host_path.read_bytes()
    if looks_like_lorecard(data):
        lorecard = parse_lorecard_bytes(data, host_path.name)
        return lorecard.card, lorecard
    return parse_card_bytes(data, host_path.name), None


def _pack_manifest_for_room_import(home: Path) -> object | None:
    """Read a built/dev manifest, tolerating minimal pre-schema test fixtures."""
    import core.pack as core_pack

    path = home / "pack.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for expect_trust in (True, False):
        try:
            return core_pack.parse_manifest_text(text, expect_trust=expect_trust)
        except Exception:
            continue
    try:
        from core.yaml_safety import safe_load_no_aliases

        raw = safe_load_no_aliases(text)
        contents = raw.get("contents") if isinstance(raw, dict) else None
        if not isinstance(contents, dict):
            return None
        normalized = {
            key: tuple(
                str(item.get("path") if isinstance(item, dict) else item)
                for item in (contents.get(key) or [])
            )
            for key in ("skills", "lorebooks", "panels", "presentation")
        }
        return SimpleNamespace(id=str(raw.get("id") or home.name.partition("@")[0]), contents=normalized)
    except Exception:
        return None


# The virtual per-player user_key a companion's CharacterSheet is stored under (M10) —
# the one definition lives with the cast writer, `agent.npc`.
_companion_uid = npc_records.companion_uid


def _persona_text(card: CharacterCard) -> str:
    """The roleplay persona carried onto the character, from the card's description + personality."""
    return "\n".join(part for part in (card.description, card.personality) if part).strip()


def _card_pronouns(card: CharacterCard) -> str:
    """Infer the card's gender/pronoun note from all of its prose fields (empty when unclear)."""
    blob = "\n".join(
        part for part in (card.description, card.personality, card.scenario, card.first_mes, card.mes_example) if part
    )
    return infer_pronoun_note(blob)


def _truncate(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _PREVIEW_CHARS else f"{text[:_PREVIEW_CHARS]}…"


def _key_stats(sheet: CharacterSheet) -> str:
    """A short, comma-joined recap of the sheet's headline attributes (data only)."""
    attrs = sheet.attributes or {}
    try:
        spec = load_rulepack(sheet.system).sheet_spec
    except Exception:
        spec = None
    keys = [attr for attr in spec.attributes if attr in attrs] if spec is not None else list(attrs)
    return ", ".join(f"{attr} {attrs[attr]}" for attr in keys[:_KEY_STAT_COUNT])


async def _register_png_avatar(services: Services, ctx: AgentCtx, host_path: Path, sheet: CharacterSheet) -> None:
    try:
        data = host_path.read_bytes()
    except OSError:
        return
    if not data.startswith(PNG_SIGNATURE):
        return
    try:
        store = MediaStore(
            services.store,
            services.settings.data_dir,
            max_file_bytes=services.settings.tui.media_max_file_bytes,
            room_quota_bytes=services.settings.tui.media_room_quota_bytes,
        )
        record = await store.register_blob(
            room=ctx.chat_key,
            data=data,
            mime="image/png",
            name=host_path.name,
            uploader=ctx.uid(),
        )
    except Exception:
        return
    sheet.avatar = record.ref()


async def _pregen_avatar_from_asset(
    services: Services, ctx: AgentCtx, host_path: Path, asset_name: str
) -> dict[str, Any] | None:
    """Read a pregen portrait asset from the pack and register it as the sheet's avatar ref, so
    claiming the pregen inherits the portrait as the player's character avatar. Best-effort: a
    missing asset or store rejection leaves the sheet without an avatar."""
    try:
        from core.pack import pack_home_of

        home = pack_home_of(Path(services.settings.data_dir), host_path)
        data = (home / "assets" / asset_name).read_bytes()
    except Exception:  # noqa: BLE001 — best-effort
        return None
    mime = mimetypes.guess_type(asset_name)[0] or "image/png"
    try:
        store = MediaStore(
            services.store,
            services.settings.data_dir,
            max_file_bytes=services.settings.tui.media_max_file_bytes,
            room_quota_bytes=services.settings.tui.media_room_quota_bytes,
        )
        record = await store.register_blob(
            room=ctx.chat_key,
            data=data,
            mime=mime,
            name=asset_name,
            uploader=ctx.uid(),
        )
    except Exception:  # noqa: BLE001 — best-effort
        return None
    return record.ref()


def _stripped_notice(i18n: I18n, world: WorldPayloads) -> str:
    """The itemized what-was-stripped line for a character import; "" for a plain card."""
    if not world.any:
        return ""
    return i18n.t(
        "charcard.tools.import.stripped",
        hooks=world.hooks,
        vars=world.initvar_entries,
        ejs=world.ejs_blocks,
        secret=world.secret_entries,
    )


async def _module_summary(services: Services, chat_key: str) -> str:
    """A brief, player-safe module summary to fit the character to the adventure;
    best-effort -- returns "" when no module has been initialized.

    The analyzed player pool (`module_pool`, M21+) is the primary source; older
    rooms never ran that lane and keep only the module BRIEF (the imported card's
    own description/scenario) — fall back to it so summary-driven lanes
    (`.pc gen`, card imports) still see the adventure there."""
    try:
        view = await services.documents.get_view(chat_key, "module_pool", MODULE_POOL_ID, PLAYER_VIEWER)
        summary = view.get("summary") if isinstance(view, dict) else ""
        if summary:
            return str(summary)[:400]
    except Exception:
        pass


async def _module_full_context(services: Services, chat_key: str) -> str:
    """The room's FULL module context for generative lanes — the same generosity
    forge's own pregen pass gives the model (the module fulltext, secrets
    included): the brief's prose (unclipped) plus the room's lore documents, so
    a character concept sees the whole adventure, not a 400-char abstract.

    Authoring-lane input (keeper-authorized generation): secret-flagged lore
    rides along like forge's module document — the model needs the plot to fit
    a character to it, and the concept it returns is a player-facing persona,
    not keeper notes. Cap the total so one generous room cannot blow the
    context."""
    parts: list[str] = []
    try:
        briefs = await services.documents.list(chat_key, "module_brief")
        if briefs:
            data = briefs[0].data
            for key in ("name", "description", "scenario"):
                text = str(data.get(key) or "").strip()
                if text:
                    parts.append(text)
    except Exception:
        pass
    try:
        view = await services.documents.get_view(chat_key, "module_pool", MODULE_POOL_ID, PLAYER_VIEWER)
        summary = view.get("summary") if isinstance(view, dict) else ""
        if str(summary or "").strip():
            parts.append(str(summary))
    except Exception:
        pass
    try:
        lore_docs = await services.documents.list(chat_key, "lore")
        for doc in lore_docs[:40]:
            data = doc.data
            title = str(data.get("title") or "").strip()
            content = str(data.get("content") or "").strip()
            if content:
                parts.append(f"{title}: {content[:500]}" if title else content[:500])
    except Exception:
        pass
    joined = "\n\n".join(part for part in parts if part.strip())
    return joined[:6000]



class CardImportRefused(RuntimeError):
    """A world import a caller asked to branch on could not proceed. Carries the same
    localized sentence the text-returning path would have printed."""

class CharcardTools:
    """AI-KP tools for importing SillyTavern cards as a player PC or an AI companion."""

    def __init__(self, services: Services) -> None:
        self._services = services

    def _i18n(self, ctx: AgentCtx) -> I18n:
        return self._services.i18n.with_locale(ctx.locale)

    @tool(prep_only=True)
    async def import_character(self, ctx: AgentCtx, file_path: str, system: str = "", as_: str = "pc", name: str = "") -> str:
        """Import a SillyTavern character card and drop it into the adventure with an auto-generated,
        rule-legal sheet -- as the acting player's PC, or as an AI player companion. Any lore in the
        card's character_book is imported into the world.

        Args:
            file_path: The sandbox/logical path to the card (PNG or JSON), resolved via ctx.fs.
            system: Target rules system for the generated sheet; when omitted, the character
                system of the installed pack the card ships in (if it has one), else the room's
                active rule system.
            as_: "pc" to make it the acting player's character, or "companion" for an AI party member.
            name: Optional name override (defaults to the card's name).

        Returns:
            A localized summary: name, system, key stats, and how many lore entries were imported.
        """
        i18n = self._i18n(ctx)
        # No system named: a card that lives in an installed pack with a character
        # system of its own is built on THAT (the author shipped the card for it — a
        # module's pregen imported before the keeper's world import must not land as
        # the room's default), else the room's active system.
        pack_system = ""
        if not system.strip():
            try:
                from core.pack import installed_pack_character_system

                pack_system = (
                    installed_pack_character_system(self._services.settings.data_dir, Path(ctx.fs.get_file(file_path)))
                    if ctx.fs is not None
                    else ""
                ) or ""
            except Exception:
                pack_system = ""
            system = pack_system or (await self._services.room_rulepack(ctx)).system
        if ctx.fs is None:
            return i18n.t("charcard.tools.import.no_fs")
        try:
            host_path = Path(ctx.fs.get_file(file_path))
            if not host_path.exists():
                return i18n.t("charcard.tools.import.no_file", path=file_path)

            # 拆卡: a character import takes ONLY the character half. Hook scripts, variable
            # declarations and EJS are module machinery — stripped here (structurally, before
            # anything touches room state) and reported so the keeper knows the card has a
            # world half waiting behind `.import <file> world`.
            full_card, _lorecard = _parse_any_card_file(host_path)
            card, world = split_card(full_card)
            module_context = await _module_summary(self._services, ctx.chat_key)
            sheet = await build_sheet_from_persona(
                self._services, card, system, module_context=module_context, chat_key=ctx.chat_key
            )
            final_name = name.strip() or card.name or sheet.name
            sheet.name = final_name
            sheet, violations = validate_sheet(sheet, system)
            await _register_png_avatar(self._services, ctx, host_path, sheet)
            notices = [render_validation_notice(i18n, violations), _stripped_notice(i18n, world)]

            if as_.strip().lower() == "companion":
                # A companion is a CLAIMED character: the card's character half first
                # lands on the roster as a claimable pregen (`.pc list` — players may
                # claim it too), then the AI claims it through the same path `.party
                # add` and the companion tools use. Record + sheet derive from that
                # roster entry; nothing is created outside the roster.
                from agent.kp_tools_companion import claim_pregen_as_companion
                from core.pregen_roster import pregen_add

                documents = self._services.documents
                entry = await pregen_add(
                    documents,
                    ctx.chat_key,
                    sheet,
                    source="card",
                    blurb=_persona_text(card),
                )
                if entry is None:
                    return i18n.t("charcard.tools.import.companion_roster_full", name=final_name)
                status, record = await claim_pregen_as_companion(
                    self._services,
                    ctx.chat_key,
                    entry["id"],
                    playstyle=", ".join(card.tags),
                    claimer_name="AI",
                    persona_extra=_persona_text(card),
                )
                if record is None:
                    return i18n.t(f"charcard.tools.import.companion_{status}", name=final_name)
                lore = await self._import_card_lore(ctx, card)
                result = i18n.t(
                    "charcard.tools.import.done_companion",
                    name=final_name,
                    id=record.id,
                    system=sheet.system,
                    stats=_key_stats(sheet),
                    lore=lore,
                )
                return "\n".join([result, *[notice for notice in notices if notice]])

            # Default: the acting player plays AS the card -- save + set active under their own uid.
            await self._services.characters.save_character(ctx.uid(), ctx.chat_key, sheet)
            lore = await self._import_card_lore(ctx, card)
            result = i18n.t(
                "charcard.tools.import.done_pc",
                name=final_name,
                system=sheet.system,
                stats=_key_stats(sheet),
                lore=lore,
            )
            return "\n".join([result, *[notice for notice in notices if notice]])
        except npc_records.PlayerNameReservedError as exc:
            # `as companion` with a PLAYER's name: refused by the cast writer, same text as
            # every other entry point (`agent.npc.PlayerNameReservedError`).
            return player_name_refusal(i18n, exc)
        except npc_records.KeeperNpcNameTakenError as exc:
            # `as companion` onto an existing KEEPER NPC: the writer refuses to convert the
            # module's own character into a party member, same text as every other door.
            return keeper_npc_refusal(i18n, exc)
        except Exception as exc:
            return i18n.t("charcard.tools.import.failed", error=str(exc))

    @tool(prep_only=True)
    async def preview_card(self, ctx: AgentCtx, file_path: str) -> str:
        """Preview a SillyTavern character card WITHOUT importing it: show its fields and how many
        lore entries it carries, so you can confirm before creating a sheet.

        Args:
            file_path: The sandbox/logical path to the card (PNG or JSON), resolved via ctx.fs.

        Returns:
            The card's name/description/personality/scenario/tags and its lore-entry count.
        """
        i18n = self._i18n(ctx)
        if ctx.fs is None:
            return i18n.t("charcard.tools.preview.no_fs")
        try:
            host_path = Path(ctx.fs.get_file(file_path))
            if not host_path.exists():
                return i18n.t("charcard.tools.preview.no_file", path=file_path)

            card, _lorecard = _parse_any_card_file(host_path)
            lines = [i18n.t("charcard.tools.preview.name_line", name=card.name or i18n.t("common.unknown"))]
            if card.description:
                lines.append(i18n.t("charcard.tools.preview.description_line", description=_truncate(card.description)))
            if card.personality:
                lines.append(i18n.t("charcard.tools.preview.personality_line", personality=_truncate(card.personality)))
            if card.scenario:
                lines.append(i18n.t("charcard.tools.preview.scenario_line", scenario=_truncate(card.scenario)))
            if card.tags:
                lines.append(i18n.t("charcard.tools.preview.tags_line", tags=", ".join(card.tags)))
            lines.append(i18n.t("charcard.tools.preview.lore_line", count=len(card.character_book)))
            world = detect_world_payloads(card)
            if world.any:
                lines.append(
                    i18n.t(
                        "charcard.tools.preview.world_line",
                        hooks=world.hooks,
                        vars=world.initvar_entries,
                        ejs=world.ejs_blocks,
                    )
                )
            return "\n".join(lines)
        except Exception as exc:
            return i18n.t("charcard.tools.preview.failed", error=str(exc))

    async def _import_card_lore(self, ctx: AgentCtx, card: CharacterCard) -> int:
        """Fold the card's embedded `character_book` into the world lore (M11); 0 when it has none.
        `card` is always the CHARACTER half of a split (`core.card_split`), so no hook scripts or
        variable declarations can reach this path."""
        if not card.character_book:
            return 0
        # A character card is untrusted input: its embedded lore lands in the room-local scope with
        # constant/secret stripped (is_keeper=False) so a crafted card cannot inject always-on or
        # keeper-only text. See core.worldbook.import_entries. `char_name` binds the card's own
        # {{char}} macro statically — that name never changes for imported entries.
        return await self._services.worldbook.import_entries(
            ctx.chat_key, card.character_book, source=card.name, is_keeper=False, char_name=card.name
        )

    async def import_world_card(
        self, ctx: AgentCtx, file_path: str, system: str = "", *, raise_on_failure: bool = False
    ) -> str:
        """Import a card as a MODULE, both halves at once (拆卡, keeper trust):

        - the WORLD half — full lorebook with secrecy flags honored, `[InitVar]`
          declarations seeded into the room's variable tree, and any
          `extensions.loreweaver_hooks` scripts installed room-wide;
        - the CHARACTER half — a rule-legal sheet built from the persona and placed on the
          room's pre-generated roster (`core.pregen_roster`) as a claimable PC: players
          pick it up with `.pc claim <name>`. (An AI-played version is still a separate
          `.import <file> companion`.)

        `system` targets the rules system for the generated pregen sheet; the room's active
        rule system is used when omitted.

        Deliberately NOT an `@tool`: reprogramming the room is the human keeper's decision,
        so this is reachable only through `.import <file> world`, whose keeper check is
        deterministic (`gateway.commands`).

        Every failure normally comes back as TEXT, because the keeper who typed `.import`
        is reading the reply. `raise_on_failure` is for a caller that must BRANCH on the
        outcome instead of printing it (`.pack install` decides what to claim in its
        summary): a refusal that reads as prose is indistinguishable from success to code,
        and the room state left behind is no substitute — the `world_import` marker is
        written partway through, so a room that already ran a module keeps a truthy marker
        no matter how this call ends.
        """
        i18n = self._i18n(ctx)
        transaction: ModuleImportTransaction | None = None

        def _refuse(key: str, **fields: object) -> str:
            message = i18n.t(key, **fields)
            if raise_on_failure:
                raise CardImportRefused(message)
            return message

        if ctx.fs is None:
            return _refuse("charcard.tools.import.no_fs")
        try:
            host_path = Path(ctx.fs.get_file(file_path))
            if not host_path.exists():
                return _refuse("charcard.tools.import.no_file", path=file_path)

            # System pin (owner verdict 2026-08-17, widened 2026-08-18): an explicit
            # `system` argument wins outright. Otherwise, a card imported FROM an
            # installed pack that has a CHARACTER system — its sole rulepack, or among
            # several the one that declares a make-character word of its own
            # (`core.pack.installed_pack_character_system`) — pins that system for the
            # room: the module's cast is built on the system its author shipped, and
            # later `.genchar`/make_char/click-imports follow it via `room_rulepack`.
            # Anything else keeps today's fallback. `pin_system` is only DECIDED here;
            # the room_state write happens at the END of the import, so a corrupt card
            # that fails to parse cannot leave the room retargeted onto a module that
            # never landed.
            pinned_line = ""
            pin_system = ""
            if not system.strip():
                from core.pack import installed_pack_character_system

                pack_system = installed_pack_character_system(self._services.settings.data_dir, host_path)
                if pack_system:
                    system = pack_system
                    pin_system = pack_system
                    pinned_line = i18n.t("charcard.tools.world.system_pinned", system=pack_system)
                else:
                    pack = await self._services.room_rulepack(ctx)
                    system = pack.system

            card, lorecard = _parse_any_card_file(host_path)
            module_identity = identity_for_world_card(
                Path(self._services.settings.data_dir), host_path, display_name=card.name or host_path.stem
            )
            source_id = str(module_identity["source_id"])
            # A native bundle may declare its own rule system in the card JSON without
            # shipping a rulepack. It wins over the room's fallback but NOT over an
            # explicit `system=` argument (handled above) or a shipped pack rulepack.
            if not pin_system and lorecard is not None and lorecard.system:
                system = lorecard.system
                pin_system = lorecard.system
                pinned_line = i18n.t("charcard.tools.world.system_pinned", system=pin_system)
            character, world = split_card(card)
            transaction = ModuleImportTransaction(self._services, ctx.chat_key)
            await transaction.__aenter__()
            await self._services.store.state_set(ctx.chat_key, "module_import_name", card.name or host_path.name)
            previous = await active_module(self._services, ctx.chat_key)
            same_source = previous is not None and previous.get("source_id") == source_id
            if previous is not None and previous.get("source_id") != source_id:
                await purge_active_module(self._services, ctx.chat_key)
            elif previous is None and (
                await self._services.store.state_get(ctx.chat_key, "world_import")
                or await self._services.store.state_get(ctx.chat_key, "module_fulltext")
            ):
                # Heal rooms created before the shared identity record existed.
                await purge_active_module(self._services, ctx.chat_key)
            old_modvars = await load_modvars(self._services.documents, ctx.chat_key)
            old_mvu = await load_mvu(self._services.documents, ctx.chat_key)
            old_mvu_doc = await self._services.documents.get(ctx.chat_key, MVU_DOC_TYPE, MVU_DOC_ID)
            old_exposed = list(old_mvu_doc.data.get("exposed") or []) if old_mvu_doc else []
            # Rebuild imported schemas from the card so removed variables cannot
            # survive a refresh; overlapping values are restored below.
            await self._services.documents.delete(ctx.chat_key, MVU_DOC_TYPE, MVU_DOC_ID)
            # Keeper trust: secrecy flags are honored and InitVar declarations are consumed
            # into the shared MVU tree (`core.worldbook.import_entries` gates that on
            # `is_keeper=True`). The ORIGINAL entries are imported, not the stripped half —
            # render-time EJS in world lore is exactly what this path exists to carry.
            skipped_titles: list[str] = []
            lore = await self._services.worldbook.import_entries(
                ctx.chat_key,
                card.character_book,
                source=source_id,
                is_keeper=True,
                char_name=card.name,
                skipped_titles=skipped_titles,
            )
            refreshed_mvu = await load_mvu(self._services.documents, ctx.chat_key)
            refreshed_paths = {leaf["path"] for leaf in flatten_leaves(refreshed_mvu)}
            for leaf in flatten_leaves(old_mvu):
                if leaf["path"] not in refreshed_paths:
                    continue
                try:
                    refreshed_mvu = apply_mvu_set(refreshed_mvu, leaf["path"], leaf["value"])
                except (TypeError, ValueError):
                    continue
            if refreshed_mvu:
                await self._services.documents.put(
                    ctx.chat_key,
                    MVU_DOC_TYPE,
                    MVU_DOC_ID,
                    {"tree": refreshed_mvu, "exposed": old_exposed},
                    source=source_id,
                )
            hooks = card_hook_codes(card)
            if hooks:
                await install_room_hooks(self._services, ctx.chat_key, source_id, hooks)
            else:
                # Same-source refresh also removes scripts no longer declared.
                await install_room_hooks(self._services, ctx.chat_key, source_id, [])
            # Durable "this room runs an imported module" marker: the prompt builder folds the
            # keeper_discipline/module_fidelity blocks into the lore section ONLY for rooms
            # that actually loaded a module this way — a free-sandbox room whose keeper merely
            # `.lore add`ed some setting notes must never receive run-the-module directives.
            new_world_name = card.name or "card"
            await self._services.store.state_set(ctx.chat_key, "world_import", new_world_name)
            # There is only one module, so stale module sources are removed physically.
            # An empty selector keeps standalone pack lorebooks and keeper-attached
            # supplemental lore visible beside that module.
            await self._services.worldbook.set_active_source(ctx.chat_key, "")

            # The card's PROSE gets a home (UPSTREAM item 10): a keeper-only brief
            # document, copied deterministically — before this, description/scenario
            # and the authored opening(s) seeded nothing and the Keeper could not even
            # quote the module's own opening. Same-card re-import replaces it.
            openings: tuple[str, ...] = ()
            if lorecard is not None and lorecard.alternate_greetings:
                openings = tuple(lorecard.alternate_greetings)
            else:
                raw_data = card.raw.get("data") if isinstance(card.raw, dict) else None
                alt = raw_data.get("alternate_greetings") if isinstance(raw_data, dict) else None
                if isinstance(alt, list):
                    openings = tuple(str(entry) for entry in alt if isinstance(entry, str))
            brief = build_brief(card, openings)
            brief_line = ""
            await self._services.documents.delete_type(ctx.chat_key, BRIEF_DOC_TYPE)
            if brief is not None:
                await self._services.documents.put(
                    ctx.chat_key,
                    BRIEF_DOC_TYPE,
                    brief_id(card.name),
                    brief,
                    source=source_id,
                )
                brief_line = i18n.t("charcard.tools.world.brief_line")

            # A native bundle (M14) additionally carries TYPED variable specs — the lossless
            # flavor of what an ST card can only ship as an [InitVar] tree. Keeper trust:
            # they land as real `core.modvars` trackers (validated/clamped from here on).
            specs_line = ""
            refreshed_modvars = empty_modvar_state()
            if lorecard is not None and lorecard.variable_specs:
                for spec in lorecard.variable_specs:
                    refreshed_modvars = apply_define(refreshed_modvars, dict(spec))
                for var_id, value in old_modvars.get("values", {}).items():
                    if var_id not in refreshed_modvars["specs"]:
                        continue
                    try:
                        refreshed_modvars, _old, _new = apply_modvar_set(
                            refreshed_modvars, var_id, value
                        )
                    except (TypeError, ValueError):
                        continue
                await save_modvars(
                    self._services.documents, ctx.chat_key, refreshed_modvars, source=source_id
                )
                specs_line = i18n.t("charcard.tools.world.specs_line", count=len(lorecard.variable_specs))
            else:
                await self._services.documents.delete_type(ctx.chat_key, "modvars")

            # Only a card with an actual PERSONA half self-registers as a claimable PC.
            # A pure world/module card (no personality; for native bundles `opening` is
            # module text, not a greeting) is machinery — putting IT on the roster gave
            # players ".pc claim <a bronze dial>". Multi-PC casts ride `pregens:` below.
            has_persona = bool(character.personality.strip()) or (
                lorecard is None and bool(character.first_mes.strip())
            )
            desired_pregen_ids: set[str] = set()
            pregen_line = ""
            if character.name.strip() and has_persona:
                sheet = await self._build_pregen_sheet(ctx, character, system, host_path)
                entry = await pregen_add(
                    self._services.documents, ctx.chat_key, sheet, source=source_id
                )
                if entry is not None:
                    desired_pregen_ids.add(str(entry["id"]))
                    pregen_line = i18n.t("charcard.tools.world.pregen_line", name=sheet.name)

            # Native bundles may ship a claimable CAST (`pregens:`): deterministic sheets
            # from the system's defaults + declared skill overrides — no LLM in the path.
            cast_line = ""
            if lorecard is not None and lorecard.pregens:
                from core.rulepacks import load_rulepack
                from core.sheets import set_sheet_value

                pack = load_rulepack(system)
                cast_names: list[str] = []
                for spec in lorecard.pregens:
                    sheet = self._services.characters.generate_character(system, spec["name"])
                    # A pack may declare a `name` sheet field whose default overwrites the
                    # pregen's own name during generation/refresh — the roster keys on the
                    # character's name, so pin it back after any pack defaults applied.
                    sheet.name = spec["name"]
                    # The module's persona paragraph (history/personality/voice/secret)
                    # lands on the sheet itself, so a player who claims the pregen can
                    # read and play it — and the keeper's roster panel can cite it.
                    persona = str(spec.get("notes") or "").strip()
                    if persona:
                        sheet.background = persona
                    from core.character_rules import normalize_pregen_skills
                    for skill_name, value in normalize_pregen_skills(spec.get("skills") or {}, pack).items():
                        try:
                            set_sheet_value(sheet, pack, skill_name, int(value))
                        except Exception:
                            sheet.skills[skill_name] = int(value)
                    sheet, _cast_violations = validate_sheet(
                        sheet, system, initialize_vitals=True, creation_method="rolled"
                    )
                    # A forge-generated portrait asset (bound to this pregen by name) becomes the
                    # sheet's avatar, so a player claiming the pregen inherits the portrait.
                    avatar_asset = str(spec.get("avatar") or "").strip()
                    if avatar_asset:
                        sheet.avatar = await _pregen_avatar_from_asset(
                            self._services, ctx, host_path, avatar_asset
                        )
                    entry = await pregen_add(
                        self._services.documents,
                        ctx.chat_key,
                        sheet,
                        source=source_id,
                        blurb=str(spec.get("blurb", "")),
                        appearance=str(spec.get("appearance", "")),
                        aliases=tuple(spec.get("aliases") or ()),
                    )
                    if entry is not None:
                        desired_pregen_ids.add(str(entry["id"]))
                        cast_names.append(sheet.name)
                if cast_names:
                    cast_line = i18n.t(
                        "charcard.tools.world.cast_line",
                        count=len(cast_names),
                        names=i18n.t("common.list_separator").join(cast_names),
                    )
            for document in await self._services.documents.list(ctx.chat_key, "pregen"):
                if document.source == source_id and document.id not in desired_pregen_ids:
                    await self._services.documents.delete(ctx.chat_key, "pregen", document.id)

            # The import made it through every step — only now does the pin land.
            if pin_system:
                await self._services.store.state_set(ctx.chat_key, "room_system", pin_system)

            # Auto-enable the KP skills the pack ships (mirroring `.pack install`'s
            # `_switch_everything_on`): a module imported into a room should be playable out of
            # the box — its pacing/rules skill enabled — not require the keeper to remember
            # `.skill enable <id>`. Only when the card comes from an installed pack.
            skill_line = ""
            # The skills/panels the PREVIOUS module auto-enabled leave with it: read them
            # from the module being replaced (whether or not it is the same source — a
            # module SWAP must also turn the old adventure's auto-enabled skills/panels
            # off; the `same_source`-only guard stranded them, the observed bug).
            prior_owned_skills = set(previous.get("enabled_skills") or []) if previous else set()
            prior_owned_panels = set(previous.get("enabled_panel_packs") or []) if previous else set()
            desired_skills: list[str] = []
            desired_panels: list[str] = []
            from core.pack import pack_home_of
            from gateway.ops import (
                get_enabled_panel_packs,
                get_enabled_skills,
                toggle_enabled_panel_pack,
                toggle_enabled_skill,
            )

            home = pack_home_of(Path(self._services.settings.data_dir), host_path)
            imported_pack_manifest = None
            if home is not None:
                manifest_path = home / "pack.yaml"
                if manifest_path.is_file():
                    manifest = _pack_manifest_for_room_import(home)
                    if manifest is None:
                        raise ValueError("unreadable pack manifest")
                    imported_pack_manifest = manifest
                    for skill_path in manifest.contents.get("skills", ()):
                        skill_id = PurePosixPath(str(skill_path)).name
                        if skill_id and skill_id not in desired_skills:
                            desired_skills.append(skill_id)
                    if desired_skills:
                        skill_line = i18n.t(
                            "charcard.tools.world.skills_enabled_line",
                            ids=", ".join(desired_skills),
                        )
                    if manifest.contents.get("panels") or manifest.contents.get("presentation"):
                        desired_panels.append(manifest.id)
                    # Lorebooks declared beside the selected world card are part of
                    # that module, under the same provenance and transaction.
                    for lorebook_path in manifest.contents.get("lorebooks", ()):
                        raw_book = json.loads((home / lorebook_path).read_text(encoding="utf-8-sig"))
                        lore += await self._services.worldbook.import_entries(
                            ctx.chat_key,
                            raw_book,
                            source=source_id,
                            is_keeper=True,
                            char_name=card.name,
                            skipped_titles=skipped_titles,
                            replace_source=False,
                        )

            before_skills = set(await get_enabled_skills(self._services.store, ctx.chat_key))
            for skill_id in prior_owned_skills - set(desired_skills):
                await toggle_enabled_skill(self._services.store, ctx.chat_key, skill_id, on=False)
            owned_skills: list[str] = []
            for skill_id in desired_skills:
                await toggle_enabled_skill(self._services.store, ctx.chat_key, skill_id, on=True)
                if skill_id in prior_owned_skills or skill_id not in before_skills:
                    owned_skills.append(skill_id)

            before_panels = set(await get_enabled_panel_packs(self._services.store, ctx.chat_key))
            for pack_id in prior_owned_panels - set(desired_panels):
                await toggle_enabled_panel_pack(self._services.store, ctx.chat_key, pack_id, on=False)
            owned_panels: list[str] = []
            for pack_id in desired_panels:
                await toggle_enabled_panel_pack(self._services.store, ctx.chat_key, pack_id, on=True)
                if pack_id in prior_owned_panels or pack_id not in before_panels:
                    owned_panels.append(pack_id)

            module_identity["enabled_skills"] = owned_skills
            module_identity["enabled_panel_packs"] = owned_panels
            await publish_active_module(self._services, ctx.chat_key, module_identity)

            # A native bundle may ship an item CATALOG (`items:`): templates with mechanical
            # effects (kind/slot/bonus) that `.item grant` can later hand to characters and
            # equip for derived bonuses — the same Layer 0 -> Layer 1 seeding the module
            # initializer performs for `.md` modules. Module-scoped items (scope !=
            # "universal") are stamped with THIS module's id (pack id when available), so a
            # plot artifact from another campaign contributes nothing in play.
            items_line = ""
            if lorecard is not None and lorecard.items:
                module_tag = str(module_identity.get("pack_id") or "") or source_id
                clue_ref_map: dict[str, str] = {}
                for raw_entry in card.character_book:
                    if not isinstance(raw_entry, dict) or str(raw_entry.get("category") or "").casefold() != "clue":
                        continue
                    stable_id = str(raw_entry.get("id") or "").strip()
                    title = str(raw_entry.get("comment") or raw_entry.get("title") or "").strip()
                    keys = raw_entry.get("keys")
                    first_key = next(
                        (str(key).strip() for key in keys if str(key).strip()),
                        "",
                    ) if isinstance(keys, list) else ""
                    target = title or first_key
                    if stable_id and target:
                        clue_ref_map[stable_id] = target
                tagged: list[dict[str, Any]] = []
                for tpl in lorecard.items:
                    entry = normalize_item_links(dict(tpl), clue_ref_map=clue_ref_map)
                    if str(entry.get("scope") or "") != "universal":
                        entry["module_id"] = module_tag
                    tagged.append(entry)
                await ensure_catalog(self._services.documents, ctx.chat_key, tagged)
                items_line = i18n.t("charcard.tools.world.items_line", count=len(lorecard.items))

            # The receipt's variable count is the TOTAL actually injected into the
            # room: the ST-compat [InitVar] tree plus the native typed specs. Reporting
            # only the ST channel read "0" for a native card that shipped variables —
            # the variable was live in the room while the receipt said none were.
            native_vars = len(lorecard.variable_specs) if lorecard is not None and lorecard.variable_specs else 0
            result = i18n.t(
                "charcard.tools.world.done",
                name=card.name or i18n.t("common.unknown"),
                lore=lore,
                vars=world.initvar_entries + native_vars,
                hooks=len(hooks),
            )
            skipped_line = ""
            if skipped_titles:
                skipped_line = i18n.t(
                    "charcard.tools.world.skipped_line",
                    count=len(skipped_titles),
                    titles=i18n.t("common.list_separator").join(skipped_titles[:5]),
                )
            extra_lines = [
                line for line in (pinned_line, specs_line, brief_line, pregen_line, cast_line, items_line, skill_line, skipped_line) if line
            ]
            await transaction.__aexit__(None, None, None)
            transaction = None
            if home is not None and imported_pack_manifest is not None:
                from gateway.pack_media import sync_pack_media_to_room

                await sync_pack_media_to_room(
                    self._services, ctx.chat_key, home, imported_pack_manifest
                )
            return "\n".join([result, *extra_lines])
        except CardImportRefused as exc:
            if transaction is not None:
                await transaction.__aexit__(CardImportRefused, exc, exc.__traceback__)
            raise
        except Exception as exc:
            if transaction is not None:
                await transaction.__aexit__(type(exc), exc, exc.__traceback__)
            if raise_on_failure:
                raise
            return i18n.t("charcard.tools.world.failed", error=str(exc))

    @tool(keeper_only=True, read_only=True)
    async def module_brief(self, ctx: AgentCtx, name: str = "") -> str:
        """Read the imported module's brief -- the world card's own prose (pitch, scenario,
        authored opening and its alternates), kept verbatim from `.import ... world`. Open play by
        quoting or adapting the module's own opening; foreshadow from its scenario. Keeper eyes
        only: never paste it to players.

        Args:
            name: Which card's brief when several are imported; omit to get the only one (or a
                list of names when there are more).

        Returns:
            The brief's prose sections, or the list of available briefs.
        """
        i18n = self._i18n(ctx)
        from core.documents import KEEPER_VIEWER

        pairs = await self._services.documents.list_views(ctx.chat_key, BRIEF_DOC_TYPE, KEEPER_VIEWER)
        briefs = [view for _doc, view in pairs if view]
        if not briefs:
            return i18n.t("charcard.tools.brief.none")
        chosen = None
        if name.strip():
            wanted = brief_id(name)
            chosen = next(
                (view for view in briefs if brief_id(str(view.get("name", ""))) == wanted),
                None,
            )
            if chosen is None:
                return i18n.t(
                    "charcard.tools.brief.list",
                    names=i18n.t("common.list_separator").join(str(view.get("name", "")) for view in briefs),
                )
        elif len(briefs) == 1:
            chosen = briefs[0]
        else:
            return i18n.t(
                "charcard.tools.brief.list",
                names=i18n.t("common.list_separator").join(str(view.get("name", "")) for view in briefs),
            )
        lines = [i18n.t("charcard.tools.brief.header", name=str(chosen.get("name", "")))]
        for field in ("description", "personality", "scenario", "examples", "notes"):
            value = str(chosen.get(field, "")).strip()
            if value:
                lines.append(f"{i18n.t('charcard.tools.brief.label.' + field)}:\n{value}")
        opening = str(chosen.get("opening", "")).strip()
        if opening:
            lines.append(f"{i18n.t('charcard.tools.brief.label.opening')}:\n{opening}")
        for index, alt in enumerate(chosen.get("openings", []) or [], start=1):
            text = str(alt).strip()
            if text:
                lines.append(f"{i18n.t('charcard.tools.brief.label.alt_opening', index=index)}:\n{text}")
        return "\n\n".join(lines)

    async def _purge_old_module(self, ctx: AgentCtx, old_name: str) -> None:
        """Remove every trace of a previously imported module when a DIFFERENT one replaces it:
        its lorebook entries, its pregen cast, and the KP skills it enabled. Runs keeper-side
        only (import_world_card is keeper-gated); old content never bleeds into the current
        campaign's Keeper context or roster. Best-effort — a purge failure must never fail the
        new import."""
        try:
            # Lore: drop every entry this old module wrote (source == its card name).
            await self._services.worldbook.remove_by_source(ctx.chat_key, old_name)
            # Pregen cast: drop roster entries that shipped with this old module.
            from core.pregen_roster import pregen_entries

            old_source = f"card:{old_name}"
            for entry in await pregen_entries(self._services.documents, ctx.chat_key):
                if entry["source"] == old_source and entry["id"]:
                    await self._services.documents.delete(ctx.chat_key, "pregen", entry["id"])
            # KP skills: disable everything currently enabled. The new module's own skills are
            # re-enabled right after this (import_world_card's skill pass), so the room ends up
            # with exactly the current module's skills and none of the old one's.
            from gateway.ops import toggle_enabled_skill

            raw = await self._services.store.state_get(ctx.chat_key, "skills_enabled")
            for skill_id in _load_skill_list(raw):
                await toggle_enabled_skill(self._services.store, ctx.chat_key, skill_id, on=False)
        except Exception:  # noqa: BLE001 — a failed purge must not break the new import
            pass

    async def _build_pregen_sheet(
        self, ctx: AgentCtx, character: CharacterCard, system: str, host_path: Path
    ) -> CharacterSheet:
        """A rule-legal, validated sheet from the split CHARACTER half (same pipeline as a
        player import: persona-biased build + rulepack validation + PNG avatar)."""
        module_context = await _module_summary(self._services, ctx.chat_key)
        sheet = await build_sheet_from_persona(
            self._services, character, system, module_context=module_context, chat_key=ctx.chat_key
        )
        sheet.name = character.name or sheet.name
        sheet, _violations = validate_sheet(sheet, system)
        await _register_png_avatar(self._services, ctx, host_path, sheet)
        return sheet


# --- Room lifecycle (M23 WS1) -----------------------------------------------
ROOM_FACETS = (
    RoomStateFacet(
        name="world_import",
        owner="agent.kp_tools_charcard",
        reset_scope="all",
        # The marker recording which world card a keeper imported (拆卡): module
        # provenance, kept exactly as long as the module it describes. `room_system`
        # is the world-import system pin — module-derived, so it lives and dies with
        # the same provenance.
        state_keys=frozenset({"world_import", "room_system"}),
        storages=frozenset({STORAGE_ROOM_STATE}),
    ),
)
