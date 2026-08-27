"""AI-KP tools for AI *player companions* (`docs/specs/M10-companions.md` §5).

`CompanionTools` is the function-calling surface for steering AI party members. A companion is a
CLAIMED CHARACTER: the roster already holds the character (module-imported or `.pc gen`-created),
and `add_companion` claims it FOR the AI — record and real
`core.character_manager.CharacterSheet` under the virtual user_key `companion:{id}` derive from
that roster entry, so the KP's normal `skill_check`/character tools resolve REAL dice on the
companion's own sheet when it takes a turn. A companion never precedes its character: claiming
creates no new character, it only binds an existing one to the AI.

The heavy lifting -- generating a companion's action under strict information isolation, then running
it through the normal turn pipeline so the KP resolves real dice -- lives in
`agent.companion_actor` + `gateway.director`. These tools are the thin CRUD/steering layer over
`agent.npc` (document-backed records); every user-visible string is looked up via `services.i18n` under
`companion.tools.*` (`locales/{en,zh}/companion.json`). Companion persona/knowledge/names are game
DATA supplied at runtime, not literals here, so they need no i18n of their own (same convention as
`agent.kp_tools_npc`).

`companion_act` is the one tool that can drive a live turn: when the KP toolset was built WITH a hub
(the shared-room path), it delegates to `gateway.director.request_companion`; otherwise it degrades
to declaring the companion's action for the KP to weave and adjudicate. A companion turn never
re-enters this tool (the director builds companion turns a hub-less toolset), and the tool itself
refuses to run while already inside a companion turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent import npc as npc_records
from agent.companion_actor import companion_action
from agent.context import AgentCtx
from agent.services import Services
from agent.tools import tool
from core.pregen_roster import pregen_claim, pregen_find, pregen_release
from infra.i18n import I18n
from infra.room_facets import (
    STORAGE_DOCUMENTS,
    STORAGE_ROOM_STATE,
    FacetContext,
    RoomStateFacet,
)

if TYPE_CHECKING:
    from gateway.commands import CommandRouter
    from gateway.hub import RoomHub

_TRUTHY = {"on", "1", "true", "yes", "y", "开", "开启", "啟用", "開"}
_FALSY = {"off", "0", "false", "no", "n", "关", "关闭", "關閉"}


# The virtual per-player user_key a companion's CharacterSheet is stored under — ONE
# definition (`agent.npc.companion_uid`), because a sheet's owner prefix is also how the
# cast writer tells a companion apart from a player.
_companion_uid = npc_records.companion_uid


class CompanionSheetNotRemovedError(RuntimeError):
    """A companion's sheet could not be removed, so its record stays too.

    The one way this happens: the record's `stat_char` points at a sheet the companion
    does not own — a PLAYER's, which `update_npc` can retarget it to. `delete_character`
    refuses that write (its owner check is the thing standing between one member and
    every other member's sheet), and retiring the record anyway would strand the
    companion half-removed with no record left to fix the pointer on. So nothing is
    deleted and the keeper is told which sheet is in the way."""

    def __init__(self, companion_name: str, sheet_name: str) -> None:
        self.companion_name = companion_name
        self.sheet_name = sheet_name
        # Developer-facing; the doors localize (`companion_sheet_refusal`).
        super().__init__(f"companion_sheet_not_removed: {sheet_name!r}")


def companion_sheet_refusal(i18n: I18n, exc: CompanionSheetNotRemovedError) -> str:
    """The localized answer to `CompanionSheetNotRemovedError` — one text for both doors
    (the `remove_companion` tool and the keeper's `.companion` / `.npc delete`)."""
    return i18n.t(
        "companion.tools.remove.sheet_not_owned", name=exc.companion_name, sheet=exc.sheet_name
    )


def companion_sheet_name(companion: npc_records.NpcRecord) -> str:
    """The CharacterSheet name a companion's sheet mechanics reference addresses."""
    return npc_records.sheet_reference(companion) or companion.name


async def retire_companion(services: Services, chat_key: str, companion: npc_records.NpcRecord) -> None:
    """Remove an AI companion WHOLE: its sheet (and roster row) and then its record.

    A companion is record + sheet; deleting only the record left the sheet on the table
    under `companion:<old id>` — a party member the HUD, `list_party_sheets` and the
    roster kept showing, that no command could reach, and that a same-name
    `add_companion` then tripped over (`CharacterNameTakenError` under a fresh id) — the
    exact ghost `.companion delete` was added to remove (2026-08-18 《安土》 npc-4). The
    sheet delete keeps the owner check: only a sheet THIS companion owns goes, never a
    same-named sheet a player holds. A refused sheet delete raises
    `CompanionSheetNotRemovedError` and keeps the record too — whole or nothing, in both
    directions. Both doors — the `remove_companion` tool and the keeper's `.companion` /
    `.npc delete` — come through here."""
    sheet_name = companion_sheet_name(companion)
    if not await services.characters.delete_character(_companion_uid(companion.id), chat_key, sheet_name):
        raise CompanionSheetNotRemovedError(companion.name, sheet_name)
    await npc_records.delete_npc(services.documents, chat_key, companion.id)


async def claim_pregen_as_companion(
    services: Services,
    chat_key: str,
    ref: str,
    *,
    playstyle: str = "",
    claimer_name: str = "",
    persona_extra: str = "",
) -> tuple[str, npc_records.NpcRecord | None]:
    """Claim a roster character FOR the AI. Returns ``(status, record)``: ``ok`` /
    ``yours`` with the companion record, or ``unknown`` / ``taken`` / ``corrupt`` /
    ``name_conflict`` with ``None``.

    A companion is a CLAIMED CHARACTER: this never invents a new character (that is
    the module-import / `.pc gen` lane) — it only binds an existing roster entry to
    the AI, deriving the companion record from the entry's own data and materializing
    the sheet copy under the companion's virtual uid (the same place an AI-created
    companion's sheet always lived). Re-claiming a name the AI already holds is
    idempotent and may refresh its playstyle. A failed materialization rolls the
    freshly-minted record back — whole or nothing."""
    documents = services.documents
    entry = await pregen_find(documents, chat_key, ref)
    if entry is None:
        return "unknown", None
    claimer = str(entry.get("claimed_by") or "")
    if claimer:
        existing = await npc_records.get_npc(documents, chat_key, claimer)
        if existing is None or existing.role != npc_records.COMPANION_ROLE:
            return "taken", None
        if playstyle and playstyle != existing.playstyle:
            existing = await npc_records.update_npc(documents, chat_key, existing.id, playstyle=playstyle) or existing
        # Idempotent re-activation: the claim is already ours — re-point the active
        # pointer (best-effort) and report "yours", never overwriting the sheet.
        await pregen_claim(
            documents,
            chat_key,
            entry["id"],
            existing.id,
            services.characters,
            claimer_name=claimer_name,
            kind="ai",
            owner_uid=_companion_uid(existing.id),
        )
        return "yours", existing
    try:
        record = await npc_records.companion_from_pregen(
            documents, chat_key, entry, playstyle=playstyle, persona_extra=persona_extra
        )
    except npc_records.KeeperNpcNameTakenError:
        return "taken", None
    try:
        status, _sheet = await pregen_claim(
            documents,
            chat_key,
            entry["id"],
            record.id,
            services.characters,
            claimer_name=claimer_name,
            kind="ai",
            owner_uid=_companion_uid(record.id),
        )
    except Exception:
        # The materialization itself failed (a store error, not a status): the
        # freshly-minted record must not strand — whole or nothing, then re-raise
        # so the caller surfaces the real error.
        await npc_records.delete_npc(documents, chat_key, record.id)
        raise
    if status not in {"ok", "yours"}:
        # The sheet copy never materialized — roll the freshly-minted record back.
        await npc_records.delete_npc(documents, chat_key, record.id)
        return status, None
    return ("ok" if status == "ok" else "yours"), record


async def release_pregen_companion(
    services: Services, chat_key: str, ref: str, *, force: bool = False
) -> str:
    """Release an AI claim WHOLE: the companion record, its sheet, and the roster
    marker. Mirrors `retire_companion`'s whole-or-nothing discipline — the record
    goes only when its sheet does, and the claim marker clears only after both.
    ``not_yours`` for a player-held claim (those release through `.pc release`)."""
    documents = services.documents
    entry = await pregen_find(documents, chat_key, ref)
    if entry is None:
        return "unknown"
    claimer = str(entry.get("claimed_by") or "")
    if not claimer:
        return "free"
    if entry.get("claimed_by_kind") != "ai":
        return "not_yours"
    record = await npc_records.get_npc(documents, chat_key, claimer)
    if record is None or record.role != npc_records.COMPANION_ROLE:
        return "unknown"
    await retire_companion(services, chat_key, record)
    await pregen_release(
        documents,
        chat_key,
        entry["id"],
        claimer,
        services.characters,
        force=force,
        owner_uid=_companion_uid(record.id),
    )
    return "ok"


class CompanionTools:
    """AI-KP tools for adding/steering AI player companions (party members the AI fills seats with)."""

    def __init__(
        self,
        services: Services,
        *,
        hub: RoomHub | None = None,
        command_router: CommandRouter | None = None,
    ) -> None:
        self._services = services
        # Present only on the shared-room (hub) path; when set, `companion_act` can drive a live
        # companion turn via the director. Absent everywhere else, where it degrades gracefully.
        self._hub = hub
        self._command_router = command_router

    def _i18n(self, ctx: AgentCtx) -> I18n:
        return self._services.i18n.with_locale(ctx.locale)

    @tool(prep_only=True)
    async def add_companion(self, ctx: AgentCtx, name: str, playstyle: str = "") -> str:
        """Claim an existing roster character FOR the AI to play. A companion is a CLAIMED
        character — this never creates a new one: the character must already be on the room's
        roster (`.pc list`; module imports and `.pc gen` fill it). The claim materializes the
        character's real sheet under the companion's own identity, so it takes real,
        KP-resolved dice turns.

        Args:
            name: The roster character's name or id to claim.
            playstyle: The companion's tactical/roleplay leaning, e.g. "cautious support".

        Returns:
            Confirmation naming the claimed character and its resolved id, or a refusal
            telling you why (no such character, already claimed, unreadable sheet).
        """
        i18n = self._i18n(ctx)
        try:
            status, record = await claim_pregen_as_companion(
                self._services, ctx.chat_key, name, playstyle=playstyle, claimer_name="AI"
            )
            if record is None:
                return i18n.t(f"companion.tools.add.{status}", name=name)
            if status == "yours":
                return i18n.t("companion.tools.add.reclaimed", name=record.name, id=record.id)
            return i18n.t("companion.tools.add.done", name=record.name, id=record.id)
        except Exception as exc:
            return i18n.t("companion.tools.add.failed", error=str(exc))

    @tool
    async def companion_act(self, ctx: AgentCtx, name: str, situation: str = "") -> str:
        """Have a companion take a turn NOW: it declares an in-character action and the KP resolves
        it with real dice on the companion's own sheet. Use in exploration to spotlight a companion.

        Args:
            name: The companion's name or id.
            situation: What is happening right now, for the companion to react to.

        Returns:
            Confirmation that the companion acted, or the companion's declared action for you to
            adjudicate, or a not-found message.
        """
        i18n = self._i18n(ctx)
        # Anti-runaway: a companion turn must never spawn another companion turn.
        if ctx.platform == "companion":
            return i18n.t("companion.tools.act.nested")
        try:
            companion = await npc_records.get_npc(self._services.documents, ctx.chat_key, name)
            if companion is None or companion.role != npc_records.COMPANION_ROLE:
                return i18n.t("companion.tools.not_found", name=name)

            if self._hub is not None and self._command_router is not None:
                from gateway.director import companion_turn_toolset, request_companion

                await request_companion(
                    self._hub,
                    self._services,
                    companion.id,
                    chat_key=ctx.chat_key,
                    command_router=self._command_router,
                    toolset=companion_turn_toolset(self._services),
                    hint=situation,
                    locale=ctx.locale,
                )
                return i18n.t("companion.tools.act.done", name=companion.name)

            # No hub wired in (standalone/tool-only path): declare the action for you to weave and
            # adjudicate -- still fully info-isolated, still never rolls its own dice.
            sheet = await self._services.characters.get_character(_companion_uid(companion.id), ctx.chat_key)
            # M12 card compatibility: chat_key/user_uid let card-derived persona templates render
            # at consumption time (player-view variables only). See agent.card_text.
            out = await companion_action(
                self._services, companion, sheet, situation, locale=ctx.locale, chat_key=ctx.chat_key, user_uid=ctx.uid()
            )
            action = out.get("action", "")
            dialogue = out.get("dialogue", "")
            line = i18n.t(
                "companion.tools.act.line",
                name=companion.name,
                dialogue=dialogue or i18n.t("companion.tools.act.no_dialogue"),
                action=action or i18n.t("companion.tools.act.no_action"),
            )
            return line
        except Exception as exc:
            return i18n.t("companion.tools.act.failed", error=str(exc))

    @tool(prep_only=True)
    async def party_auto(self, ctx: AgentCtx, action: str = "") -> str:
        """Turn on/off automatic companion turns during combat (each companion acts on its initiative).

        Args:
            action: "on" to enable auto companion combat turns, "off" to disable, empty to report.

        Returns:
            The new (or current) auto-turn state.
        """
        i18n = self._i18n(ctx)
        value = action.strip().casefold()
        try:
            if value in _TRUTHY:
                await self._services.store.state_set(ctx.chat_key, "party_auto", "1")
                return i18n.t("companion.tools.auto.on")
            if value in _FALSY:
                await self._services.store.state_set(ctx.chat_key, "party_auto", "0")
                return i18n.t("companion.tools.auto.off")
            current = await self._services.store.state_get(ctx.chat_key, "party_auto")
            return i18n.t("companion.tools.auto.on" if current == "1" else "companion.tools.auto.off")
        except Exception as exc:
            return i18n.t("companion.tools.auto.failed", error=str(exc))

    @tool(read_only=True)
    async def list_companions(self, ctx: AgentCtx) -> str:
        """List this room's AI player companions (name, id, playstyle).

        Returns:
            A roster of the party's AI companions, or an empty-roster notice.
        """
        i18n = self._i18n(ctx)
        try:
            companions = await npc_records.list_companions(self._services.documents, ctx.chat_key)
            if not companions:
                return i18n.t("companion.tools.list.empty")
            lines = [i18n.t("companion.tools.list.header", count=len(companions))]
            for companion in companions:
                # Surface the companion's pronoun hint right after its name (e.g. "沈墨 (he/him)") so the
                # Keeper narrates its gender from the imported card instead of guessing off the name.
                display_name = f"{companion.name} ({companion.pronouns})" if companion.pronouns else companion.name
                lines.append(
                    i18n.t(
                        "companion.tools.list.item",
                        name=display_name,
                        id=companion.id,
                        playstyle=companion.playstyle or i18n.t("common.none"),
                    )
                )
            return "\n".join(lines)
        except Exception as exc:
            return i18n.t("companion.tools.list.failed", error=str(exc))

    @tool(prep_only=True)
    async def remove_companion(self, ctx: AgentCtx, name: str) -> str:
        """Remove an AI companion from the party: its record AND its character sheet.

        Args:
            name: The companion's name or id.

        Returns:
            Confirmation, or a not-found message.
        """
        i18n = self._i18n(ctx)
        try:
            companion = await npc_records.get_npc(self._services.documents, ctx.chat_key, name)
            if companion is None or companion.role != npc_records.COMPANION_ROLE:
                return i18n.t("companion.tools.not_found", name=name)
            # A claimed companion's roster marker leaves with it: retire deletes
            # record + sheet (whole or nothing), then the claim marker clears —
            # the character is claimable again. Legacy companions without a
            # pregen_id have no marker to clear.
            pregen_id = companion.pregen_id
            await retire_companion(self._services, ctx.chat_key, companion)
            if pregen_id:
                await pregen_release(
                    self._services.documents,
                    ctx.chat_key,
                    pregen_id,
                    companion.id,
                    self._services.characters,
                    owner_uid=_companion_uid(companion.id),
                )
            return i18n.t("companion.tools.remove.done", name=companion.name)
        except CompanionSheetNotRemovedError as exc:
            return companion_sheet_refusal(i18n, exc)
        except Exception as exc:
            return i18n.t("companion.tools.remove.failed", error=str(exc))

    @tool(prep_only=True)
    async def set_companion_playstyle(self, ctx: AgentCtx, name: str, playstyle: str) -> str:
        """Set a companion's tactical/roleplay leaning (how it approaches encounters).

        Args:
            name: The companion's name or id.
            playstyle: The new playstyle, e.g. "cautious support" or "reckless front-liner".

        Returns:
            Confirmation, or a not-found message.
        """
        i18n = self._i18n(ctx)
        try:
            companion = await npc_records.get_npc(self._services.documents, ctx.chat_key, name)
            if companion is None or companion.role != npc_records.COMPANION_ROLE:
                return i18n.t("companion.tools.not_found", name=name)
            record = await npc_records.update_npc(self._services.documents, ctx.chat_key, companion.id, playstyle=playstyle)
            return i18n.t("companion.tools.playstyle.done", name=record.name, playstyle=record.playstyle)
        except Exception as exc:
            return i18n.t("companion.tools.playstyle.failed", error=str(exc))

    @tool(prep_only=True)  # low-frequency knowledge injection, prep-phase work
    async def companion_learns(self, ctx: AgentCtx, name: str, fact: str) -> str:
        """Have a companion learn one new fact (its player-scoped knowledge grows as the party
        discovers things, so it stays current but never gets ahead of what the party knows).

        Args:
            name: The companion's name or id.
            fact: The single fact the companion just learned.

        Returns:
            Confirmation, or a not-found message.
        """
        i18n = self._i18n(ctx)
        try:
            companion = await npc_records.get_npc(self._services.documents, ctx.chat_key, name)
            if companion is None or companion.role != npc_records.COMPANION_ROLE:
                return i18n.t("companion.tools.not_found", name=name)
            record = await npc_records.npc_learns(self._services.documents, ctx.chat_key, companion.id, fact)
            return i18n.t("companion.tools.learns.done", name=record.name, fact=fact)
        except Exception as exc:
            return i18n.t("companion.tools.learns.failed", error=str(exc))


async def witness(services: Services, chat_key: str, fact: str) -> None:
    """Append ``fact`` to EVERY companion's player-scoped knowledge (best-effort).

    The party-discovery hook (`docs/specs/M10-companions.md` §5): when the group learns something,
    each companion learns it too, so companions stay current with -- but never ahead of -- the
    party. Silently no-ops on any error and never raises into the caller.
    """
    try:
        for companion in await npc_records.list_companions(services.documents, chat_key):
            await npc_records.npc_learns(services.documents, chat_key, companion.id, fact)
    except Exception:
        pass


async def _dispose_companion_sheets(facet_ctx: FacetContext) -> None:
    """The sheet half of every companion, disposed with the records at `.reset story`.

    A companion is record + sheet, and the two halves sit in facets with DIFFERENT reset
    scopes: the records are session state (`npc_records`, story), the sheets are the
    `characters` facet's `sheet` documents, kept until `chars` so the same investigators
    can replay. So `.reset story` used to leave the companion's sheet behind, recordless:
    the HUD kept its party row with `ai` flipped to False (it impersonated a real player),
    `.companion delete` could no longer reach it, and `list_party_sheets` counted it as a
    member — the same ghost 968bd1b closed on the delete door, arriving through the reset
    door instead.

    Only a slice of the `sheet` family goes, which is why this is a hook and not a target
    list: the rows a `companion:` uid owns. `delete_character` is the door, so the roster
    row and the active-character pointer leave with the document, and its owner check
    still stands — a `stat_char` retargeted at a PLAYER's sheet deletes nothing here, and
    the reset moves on rather than failing the room's whole cleanup for one bad pointer.

    The slice is selected by OWNER, not by the record's `stat_char` pointer: the pointer
    is model-writable (`update_npc`), and following a retargeted one would spare the
    companion's own sheet — the very ghost this hook exists to dispose of. What the
    companion uid owns is what leaves with it.
    """
    services, chat_key = facet_ctx.services, facet_ctx.chat_key
    for companion in await npc_records.list_companions(services.documents, chat_key):
        uid = _companion_uid(companion.id)
        for sheet in await services.characters.list_characters(uid, chat_key):
            await services.characters.delete_character(uid, chat_key, str(sheet.get("name", "")))
        # The claim marker follows the record+sheet out: `.reset story` clears the
        # companion records (npc_records, story) one scope earlier than the pregens
        # facet (all), so without this the roster would keep showing an AI-claimed
        # character whose companion is gone — a claim nothing can release.
        if companion.pregen_id:
            await pregen_release(
                services.documents,
                chat_key,
                companion.pregen_id,
                companion.id,
                services.characters,
                owner_uid=_companion_uid(companion.id),
            )


# --- Room lifecycle (M23 WS1) -----------------------------------------------
ROOM_FACETS = (
    RoomStateFacet(
        name="party_autopilot",
        owner="agent.kp_tools_companion",
        reset_scope="chars",
        # `.party auto` is a property of THIS party: it leaves when the party does.
        state_keys=frozenset({"party_auto"}),
        storages=frozenset({STORAGE_ROOM_STATE}),
    ),
    RoomStateFacet(
        name="companion_sheets",
        owner="agent.kp_tools_companion",
        reset_scope="story",
        # The companion half of the `sheet` documents and their party-roster rows: a slice
        # of two families the `characters` facet owns wholesale at `chars`, which dies one
        # scope earlier because it belongs to the companion RECORDS (`npc_records`, story)
        # rather than to the table's players.
        storages=frozenset({STORAGE_DOCUMENTS, STORAGE_ROOM_STATE}),
        on_reset=_dispose_companion_sheets,
    ),
)
