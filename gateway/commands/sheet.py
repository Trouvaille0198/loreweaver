"""Character sheets: the `.st` assignment DSL, creation (`.coc` / `.dnd` / `.genchar`),
`.growth`, `.rename`, the player roster (`.characters`), and pregen claiming (`.pc`)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from agent.char_from_persona import build_sheet_from_description
from agent.services import Services
from core.character_manager import (
    CharacterNameTakenError,
    CharacterSheet,
    has_character,
)
from core.character_rules import render_validation_notice, validate_sheet
from core.rulepacks import RulePack, load_rulepack
from core.sheets import canonical_values as sheet_canonical_values
from core.sheets import set_sheet_value, sheet_value
from gateway.commands.checks import _pack_for_character
from gateway.commands.rooms import _is_keeper
from gateway.commands.types import CommandCtx
from gateway.hub import Event
from gateway.turn import publish_state

# Matches only the VALUE half of a `.st` assignment (a signed number or dice
# expression). `_parse_sheet_assignments` scans for these and takes the text before
# each value as the attribute name. Anchoring on the value (which has no unbounded
# lazy prefix) makes parsing strictly linear, so no argument can trigger the
# quadratic backtracking the old `(.+?)(value)` pattern suffered.
_SHEET_VALUE_RE = re.compile(r"[+-]?(?:\d+d\d+(?:[+\-*/]\d+)?|\d+)", re.I)
# The EXPLICIT `.st NAME=VALUE` / `NAME+=VALUE` / `NAME-=VALUE` form. The operator is
# glued to the value half, so this scan is anchored the same way `_SHEET_VALUE_RE` is
# (no unbounded lazy name prefix) and stays strictly linear. The attribute NAME is
# whatever precedes the operator, so it may hold digits, spaces or CJK.
_SHEET_ASSIGN_RE = re.compile(rf"(\+=|-=|=)\s*({_SHEET_VALUE_RE.pattern})", re.I)
# Assignment operators -> how `_apply_value_expr` folds the value into the current one.
_SHEET_OPS = {"=": "set", "+=": "add", "-=": "sub"}
_SHEET_SIGN_OPS = {"+": "add", "-": "sub"}
# The reverse map, for spelling a correction back to whoever mis-typed one.
_SHEET_OP_SYMBOLS = {"set": "=", "add": "+=", "sub": "-="}

# `.st`/`.sheet` finalize word: re-derive current HP/MP/SAN to their maxima for
# the sheet's CURRENT characteristics (CREATION semantics -- see `cmd_sheet`).
# Deliberately locale-agnostic (checked regardless of `ctx.locale`), matching the
# other reserved `.st` subcommand words above (`clr`/`del`/...).
_SHEET_FINALIZE_WORDS = {"finalize", "定稿", "初始化"}

_CHARACTER_LIST_WORDS = {"", "list", "ls", "show", "查看", "列表"}
_CHARACTER_SWITCH_WORDS = {"switch", "use", "activate", "切换", "切換", "使用"}



@dataclass(frozen=True)
class GenCharRequest:
    system: str
    name: str
    description: str


def _resolve_system_token(token: str) -> str | None:
    """Resolve a command token to a canonical rule-system id via the pack
    registry's declared names (None when it names no installed system)."""
    word = token.strip()
    if not word:
        return None
    try:
        return load_rulepack(word).system
    except Exception:
        return None


def _parse_genchar_args(args: str) -> GenCharRequest | None:
    """Parse `.genchar [system] [name] | <description>`. A leading token naming
    an installed rule system selects it; ``system=""`` means the caller should
    fall back to the room's active system."""
    raw = args.strip()
    if not raw:
        return None

    head, sep, body = raw.partition("|")
    if sep:
        tokens = head.split()
        system = ""
        name = head.strip()
        if tokens:
            resolved = _resolve_system_token(tokens[0])
            if resolved:
                system = resolved
                name = " ".join(tokens[1:]).strip()
        description = body.strip()
    else:
        tokens = raw.split(maxsplit=1)
        system = ""
        description = raw
        name = ""
        if tokens:
            resolved = _resolve_system_token(tokens[0])
            if resolved:
                system = resolved
                description = tokens[1].strip() if len(tokens) > 1 else ""

    if not description:
        return None
    return GenCharRequest(system=system, name=name, description=description)


def _migrate_legacy_luck(character: CharacterSheet, pack: RulePack) -> None:
    """Move values written by the old `.st LUC` bug (skill slot instead of the
    declared attribute slot) into the real slot, per the pack's key bridge."""
    spec = pack.sheet_spec
    if spec is None:
        return
    canonical = pack.resolve_skill("luc")
    attr_key = spec.attr_keys.get(canonical or "")
    if not attr_key:
        return
    legacy_keys = [key for key in character.skills if str(key).casefold() == "luc"]
    for key in legacy_keys:
        try:
            value = int(character.skills[key])
        except (TypeError, ValueError):
            continue
        character.attributes[attr_key] = value
        character.skills.pop(key, None)


def _clean_assignment_name(raw: str) -> str:
    """Trim the whitespace and `,`/`，` separators around a parsed attribute name."""
    return raw.strip().strip(",，").strip()


def _parse_sheet_assignments(text: str) -> list[tuple[str, str, str]]:
    """Parse ``.st`` assignments into ``(name, op, value)`` triples, ``op`` in set/add/sub.

    Two forms; one command uses one form, never a mix:

    * EXPLICIT — ``STR=16``, ``HP+=1d6``, ``HP-=4``. ``=`` assigns ABSOLUTELY, so the
      sign belongs to the number (``mod=-3`` stores minus three) and ``+=``/``-=`` are
      the spelled-out relative forms. The name is everything left of the operator, so
      it may hold digits, spaces or CJK (``skill2=30``, ``spot hidden=70``).
    * LEGACY — ``STR16 DEX14``, ``力量50，敏捷60``, ``HP-4``. A bare leading ``+``/``-``
      on the value reads as relative, which leaves no spelling for an absolute negative
      and mis-splits a digit-bearing name (``skill2 30`` -> ``skill``/``2``). That is
      why the explicit form exists: clients build ``.st <wire-key> <n>`` out of whatever
      storage keys a pack declares.

    The explicit form wins whenever the argument holds one valid ``NAME<op>VALUE``;
    otherwise the legacy scan runs, unchanged.

    Both scans anchor on the VALUE half (`_SHEET_VALUE_RE`, with the operator glued in
    front for the explicit form) and take the text that precedes it as the name, which
    is strictly LINEAR: the original ``(.+?)(value)`` pattern backtracked quadratically,
    so a ~20k-char argument stalled the event loop for seconds. The name is everything
    since the prior value, so glued (``STR16``), spaced (``STR 16``) and multi-word
    (``spot hidden=70``) forms all parse without splitting a name.
    """
    explicit = _SHEET_ASSIGN_RE.search(text) is not None
    assignments: list[tuple[str, str, str]] = []
    last = 0
    for match in (_SHEET_ASSIGN_RE if explicit else _SHEET_VALUE_RE).finditer(text):
        name = _clean_assignment_name(text[last : match.start()])
        last = match.end()
        if explicit:
            op = _SHEET_OPS[match.group(1)]
            value = match.group(2).strip()
            # `x==5` / `a=b=5`: a name holding an operator is a garbled command, not a
            # skill called `x=`. Refuse the whole thing (the caller answers bad_args).
            if "=" in name:
                return []
        else:
            raw = match.group(0).strip()
            sign = raw[0] if raw[:1] in _SHEET_SIGN_OPS else ""
            op = _SHEET_SIGN_OPS.get(sign, "set")
            value = raw[len(sign) :]
        if name and value:
            assignments.append((name, op, value))
    # A mix of the two forms (`STR=16 DEX14`) used to apply the explicit half and drop
    # the glued one without a word. Anything left after the last explicit assignment is
    # exactly that dropped half — refuse the whole command instead of half-doing it.
    if explicit and _clean_assignment_name(text[last:]):
        return []
    return assignments


async def _table_character_names(ctx: CommandCtx) -> set[str]:
    """Every character NAME this room knows, case-folded: the live party roster plus the
    module's pregen cast. Only ever read to EXPLAIN a refusal, never to authorize one."""
    from core.pregen_roster import pregen_entries

    names: set[str] = set()
    for member in await ctx.services.characters.get_party_roster(ctx.chat_key):
        name = str(member.get("name") or "").strip()
        if name:
            names.add(name.casefold())
    try:
        for entry in await pregen_entries(ctx.services.documents, ctx.chat_key):
            name = str(entry.get("name") or "").strip()
            if name:
                names.add(name.casefold())
    except Exception:
        # A room with no pregen documents (or an unreadable one) still gets the
        # generic refusal — the roster half is what usually answers.
        pass
    return names


async def _refuse_spaced_key(
    ctx: CommandCtx, pack: RulePack, assignments: list[tuple[str, str, str]]
) -> str | None:
    """Refuse a `.st` whose attribute name holds whitespace; ``None`` when all are clean.

    BOTH scans take "everything before the value" as the name, so `.st <teammate> <attr>
    <n>` — a real habit from dice-bot dialects — used to mint a ghost attribute
    "<teammate> <attr>" on the CALLER's own sheet and echo it back as updated, while the
    named character's real attribute never moved (run-3 play-test). A name nobody declared
    that holds a space is a mis-parse, never a house skill: single-token names the pack has
    never heard of (`学识星象=45`) still write, because inventing a skill mid-session is
    what `.st` is for. A pack-DECLARED multi-word name (`spot hidden=70`) still writes too —
    it resolves, so it is not a mis-parse.
    """
    for raw_name, op, raw_value in assignments:
        name = raw_name.strip()
        if not any(char.isspace() for char in name):
            continue
        if pack.resolve_skill(name):
            continue
        tokens = name.split(maxsplit=1)
        head = tokens[0]
        rest = tokens[1].strip() if len(tokens) > 1 else ""
        known = head.casefold() in await _table_character_names(ctx)
        lines = [
            ctx.i18n.t(
                "commands.sheet.key_is_name" if known else "commands.sheet.key_has_space",
                name=head,
                key=name,
            )
        ]
        # Only when what is left of the name reads as one plausible attribute: a
        # suggestion built out of a second mis-parse would just be wrong twice.
        if rest and not any(char.isspace() for char in rest):
            lines.append(
                ctx.i18n.t(
                    "commands.sheet.key_suggestion",
                    command=ctx.command,
                    suggestion=f"{rest}{_SHEET_OP_SYMBOLS.get(op, '=')}{raw_value}",
                )
            )
        return "\n".join(lines)
    return None


def _apply_value_expr(services: Services, current: int, op: str, raw_value: str) -> int:
    """Fold a parsed assignment value into the current one: set / add / sub."""
    expression = raw_value.strip()
    sign = expression[0] if expression[:1] in {"+", "-"} else ""
    expression = expression[len(sign) :]
    if "d" in expression.casefold():
        rolled = services.dice.roll_expression(expression).total
    else:
        rolled = int(expression)
    if sign == "-":
        rolled = -rolled
    if op == "add":
        return current + rolled
    if op == "sub":
        return current - rolled
    return rolled


def _render_sheet(ctx: CommandCtx, character: CharacterSheet, pack: RulePack) -> str:
    values = dict(pack.defaults)
    values.update(sheet_canonical_values(character, pack))
    values.update(pack.compute_derived(values))
    top = pack.st_show.get("top") or list(values.keys())[:12]
    items = []
    for name in top:
        value = values.get(name)
        if value is None:
            value = sheet_value(character, pack, str(name))
        items.append(ctx.i18n.t("commands.sheet.item", name=name, value=value))
    items_per_line = int(pack.st_show.get("itemsPerLine") or 0)
    if items_per_line > 0 and len(items) > items_per_line:
        rendered = "\n".join(
            ", ".join(items[index : index + items_per_line]) for index in range(0, len(items), items_per_line)
        )
    else:
        rendered = ", ".join(items)
    result = ctx.i18n.t("commands.sheet.show", name=character.name, items=rendered)
    # The character's backstory is part of who they are — show it on the card,
    # not only in the keeper's roster line.
    background = str(getattr(character, "background", "") or "").strip()
    if background:
        result = f"{result}\n{ctx.i18n.t('commands.sheet.background', background=background)}"
    return result


class SheetCommands:
    """`CommandRouter` mixin — see the module docstring."""

    async def cmd_sheet(self, ctx: CommandCtx) -> str:
        character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
        pack = await _pack_for_character(ctx, character)
        args = ctx.args.strip()
        if not args or args.casefold() == "show":
            return _render_sheet(ctx, character, pack)
        # Bare `.st delete` deletes the ACTIVE sheet (the TUI's original form);
        # `.st delete <name>` deletes a NAMED owned sheet — the character library's
        # delete button for a sheet that is not in use needs exactly that, and
        # `.st retire <name>` already set the named-subcommand precedent.
        delete_words = {"clr", "clear", "del", "delete", "删除", "刪除"}
        delete_first = args.casefold().split(None, 1)
        if delete_first[0].casefold() in delete_words:
            name = delete_first[1].strip() if len(delete_first) > 1 else character.name
            if name and name.casefold() != character.name.casefold():
                sheets = await ctx.services.characters.list_character_sheets(ctx.user_id, ctx.chat_key)
                if not any(sheet.name.casefold() == name.casefold() for sheet in sheets):
                    return ctx.fail(ctx.i18n.t("commands.characters.not_found", name=name))
            if await ctx.services.characters.delete_character(ctx.user_id, ctx.chat_key, name):
                return ctx.i18n.t("commands.sheet.deleted", name=name)
            return ctx.fail(ctx.i18n.t("commands.sheet.delete_failed", name=name))
        retire_word = args.casefold().split(None, 1)[0] if args else ""
        if retire_word in {"retire", "ret", "退队", "退役"}:
            # Step a character out of this scenario's party — the sheet survives,
            # so the owner can re-join from the character library. A bare `.st
            # retire` retires the ACTIVE character; `.st retire <name>` retires a
            # named owned sheet (the party-row context menu uses the named form).
            name = args.split(None, 1)[1].strip() if len(args.split(None, 1)) > 1 else character.name
            if await ctx.services.characters.retire_character(ctx.user_id, ctx.chat_key, name):
                return ctx.i18n.t("commands.sheet.retired", name=name)
            return ctx.fail(ctx.i18n.t("commands.sheet.retire_failed", name=name))
        join_word = args.casefold().split(None, 1)[0] if args else ""
        if join_word in {"join", "入队", "回队"}:
            # Bring a named (retired) sheet back into the party and make it active.
            name = args.split(None, 1)[1].strip() if len(args.split(None, 1)) > 1 else ""
            if not name:
                return ctx.i18n.t("commands.sheet.join_usage")
            if await ctx.services.characters.join_character(ctx.user_id, ctx.chat_key, name):
                return ctx.i18n.t("commands.sheet.joined", name=name)
            return ctx.fail(ctx.i18n.t("commands.sheet.join_failed", name=name))
        _migrate_legacy_luck(character, pack)
        if args.casefold() in _SHEET_FINALIZE_WORDS:
            # A manual build (a make-char word with DEFAULT characteristics, then one or
            # more `.st` edits to the chosen ones) never re-derives current HP/MP/SAN:
            # `.st` validates with `initialize_vitals=False` (in-play EDIT semantics —
            # preserve, never heal) by design (see `core.character_rules.validate_sheet`).
            # This finalize word is the CREATION-side re-derive: it forces the current
            # vitals back to their maxima for the sheet's final characteristics, same as
            # the make-char/`.genchar` commands do at birth. Safe to reuse mid-play too (a player
            # who wants to top off HP/MP/SAN to the current max after e.g. levelling can
            # invoke it deliberately) since it is the same explicit, opt-in verb.
            character, violations = validate_sheet(character, character.system, initialize_vitals=True)
            await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
            result = ctx.i18n.t("commands.sheet.finalized", name=character.name)
            notice = render_validation_notice(ctx.i18n, violations)
            return f"{result}\n{notice}" if notice else result

        assignments = _parse_sheet_assignments(args)
        if not assignments:
            return ctx.i18n.t("commands.error.bad_args")
        # A mis-parsed name must never reach a write: half of `.st`'s value is that its
        # receipt can be trusted, and a ghost attribute is echoed back as "updated".
        refusal = await _refuse_spaced_key(ctx, pack, assignments)
        if refusal is not None:
            return ctx.fail(refusal)

        changed = []
        changed_names = []
        explicit_values: list[tuple[str, int]] = []
        for raw_name, op, raw_value in assignments:
            canonical = pack.resolve_skill(raw_name) or raw_name.strip()
            current = sheet_value(character, pack, canonical)
            # A malformed value expression (bad int, or an over-large dice term like
            # `力量+9999d6` that trips d20's roll cap) must not crash the turn.
            try:
                value = _apply_value_expr(ctx.services, current, op, raw_value)
            except ValueError:
                return ctx.i18n.t("commands.roll.invalid", expr=raw_value)
            set_sheet_value(character, pack, canonical, value)
            changed_names.append(canonical)
            explicit_values.append((canonical, value))
        # Derived slots refresh inside validate_sheet/save; an explicit override
        # written in the SAME command (``.st AC18 DEX14``) survives through the
        # trained-value semantics of the derived pipeline (a stored value that
        # differs from its derivation is a manual override and is preserved).
        character, violations = validate_sheet(character, pack.system)
        for canonical in changed_names:
            changed.append(
                ctx.i18n.t("commands.sheet.changed_item", name=canonical, value=sheet_value(character, pack, canonical))
            )
        await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
        result = ctx.i18n.t("commands.sheet.changed", items=", ".join(changed))
        notice = render_validation_notice(ctx.i18n, violations)
        return f"{result}\n{notice}" if notice else result

    async def cmd_characters(self, ctx: CommandCtx) -> str:
        """`.characters [list | switch <name>]` — list this player's sheets
        and choose which owned sheet is active."""
        tokens = ctx.args.strip().split(maxsplit=1)
        sub = tokens[0].casefold() if tokens else ""
        rest = tokens[1].strip() if len(tokens) > 1 else ""

        if sub in _CHARACTER_LIST_WORDS:
            characters = await ctx.services.characters.list_characters(ctx.user_id, ctx.chat_key)
            if not characters:
                return ctx.i18n.t("commands.characters.empty")
            lines = [ctx.i18n.t("commands.characters.header", count=len(characters))]
            lines.extend(
                ctx.i18n.t(
                    "commands.characters.item",
                    name=str(character.get("name") or ""),
                    system=str(character.get("system") or ""),
                )
                for character in characters
            )
            return "\n".join(lines)

        if sub in _CHARACTER_SWITCH_WORDS:
            if not rest:
                return ctx.i18n.t("commands.characters.switch_usage")
            sheets = await ctx.services.characters.list_character_sheets(ctx.user_id, ctx.chat_key)
            character = next((sheet for sheet in sheets if sheet.name.casefold() == rest.casefold()), None)
            if character is None:
                return ctx.fail(ctx.i18n.t("commands.characters.not_found", name=rest))
            await ctx.services.characters.set_active_character(ctx.user_id, ctx.chat_key, character.name)
            if ctx.router.hub is not None:
                await publish_state(ctx.router.hub, ctx.services, ctx.raw_ctx)
            return ctx.i18n.t(
                "commands.characters.switched",
                name=character.name,
                system=character.system,
            )

        return ctx.i18n.t("commands.characters.usage")


    async def cmd_growth(self, ctx: CommandCtx) -> str:
        """The pack-declared improvement check (`improvement_check` template):
        roll above the current value (or above the auto-success line) to grow
        the stat by the declared improvement roll."""
        character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
        pack = await _pack_for_character(ctx, character)
        spec = next((entry for entry in pack.subsystems.values() if entry.template == "improvement_check"), None)
        if spec is None:
            return ctx.i18n.t("commands.pack_word.not_in_system", word=ctx.spec.canonical)
        default_skill = pack.resolver.check.default_skill if pack.resolver else ""
        name = ctx.args or default_skill
        canonical = pack.resolve_skill(name) or name
        current = sheet_value(character, pack, canonical)
        roll = ctx.services.dice.roll_expression(spec.roll).total
        grows = roll > current or (spec.auto_success_above is not None and roll > spec.auto_success_above)
        gain = ctx.services.dice.roll_expression(spec.improve).total if grows else 0
        new_value = min(spec.cap, current + gain)
        if gain:
            set_sheet_value(character, pack, canonical, new_value)
            await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
        return ctx.i18n.t("commands.growth.result", name=canonical, roll=roll, gain=gain, value=new_value)

    async def cmd_mem(self, ctx: CommandCtx) -> str:
        """`.mem [character]` — a character's durable memory: the per-turn
        experience log the Scribe kept plus the playthrough memories a settle
        produced. Player-facing (any member): a character's memory records
        events the table shared, so it is readable like a sheet. Defaults to
        the caller's active character."""
        from core.character_memory import CHARACTER_MEMORY_DOC_TYPE
        from core.documents import KEEPER_VIEWER, PLAYER_VIEWER

        name = ctx.args.strip()
        if not name:
            character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
            if not has_character(character):
                return ctx.fail(ctx.i18n.t("commands.mem.usage"))
            name = character.name
        if not name:
            return ctx.fail(ctx.i18n.t("commands.mem.usage"))
        viewer = KEEPER_VIEWER if _is_keeper(ctx.raw_ctx) else PLAYER_VIEWER
        view = await ctx.services.documents.get_view(ctx.chat_key, CHARACTER_MEMORY_DOC_TYPE, name, viewer)
        if view is None:
            return ctx.fail(ctx.i18n.t("commands.mem.empty", name=name))
        entries = [entry for entry in view.get("entries") or [] if isinstance(entry, dict) and entry.get("text")]
        if not entries:
            return ctx.fail(ctx.i18n.t("commands.mem.empty", name=name))
        lines = [ctx.i18n.t("commands.mem.header", name=name)]
        shown = entries[-10:]
        lines.extend(f"- {str(entry.get('text')).strip()}" for entry in shown)
        if len(entries) > len(shown):
            lines.append(ctx.i18n.t("commands.mem.count", total=len(entries), shown=len(shown)))
        return "\n".join(lines)

    async def cmd_make_char(self, ctx: CommandCtx, pack: RulePack | None = None) -> str:
        """Create a sheet for `pack`'s system (the pack whose `make_char`
        command word routed here — the word set itself is pack data), falling
        back to the room's active system."""
        if pack is None:

            pack = await ctx.services.room_rulepack(ctx.raw_ctx)
        name = ctx.args.strip() or ctx.i18n.t("commands.character.default_name")
        character = ctx.services.characters.generate_character(pack.system, name)
        character, violations = validate_sheet(
            character,
            pack.system,
            initialize_vitals=True,
            creation_method="rolled",
        )
        try:
            await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
        except CharacterNameTakenError:
            # The bare make-char word falls back to the pack's localized default
            # name, so two players typing it collide with no attacker involved —
            # and sheets are keyed by NAME room-wide. Refusing (rather than
            # silently de-duplicating to "Adventurer 2") is the live-table
            # behaviour: it costs one re-typed command, names the conflict out
            # loud, and never saddles a 20-session character with a machine name.
            return ctx.fail(
                ctx.i18n.t("commands.character.name_taken", name=character.name, command=ctx.command)
            )
        result = ctx.i18n.t("commands.character.created", name=character.name, system=character.system)
        notice = render_validation_notice(ctx.i18n, violations)
        return f"{result}\n{notice}" if notice else result

    async def cmd_genchar(self, ctx: CommandCtx) -> str:
        request = _parse_genchar_args(ctx.args)
        if request is None:
            return ctx.i18n.t("charcard.commands.genchar.usage")
        system = request.system
        if not system:

            system = (await ctx.services.room_rulepack(ctx.raw_ctx)).system

        character = await build_sheet_from_description(
            ctx.services,
            request.description,
            system,
            chat_key=ctx.chat_key,
            name=request.name,
        )
        character, violations = validate_sheet(character, system, initialize_vitals=True)
        await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
        result = ctx.i18n.t("charcard.commands.genchar.done", name=character.name, system=character.system)
        notice = render_validation_notice(ctx.i18n, violations)
        return f"{result}\n{notice}" if notice else result

    async def cmd_rename(self, ctx: CommandCtx) -> str:
        new_name = ctx.args.strip()
        if not new_name:
            return ctx.i18n.t("commands.error.bad_args")
        character = await ctx.services.characters.get_character(ctx.user_id, ctx.chat_key)
        old_name = character.name
        character.name = new_name
        try:
            await ctx.services.characters.save_character(ctx.user_id, ctx.chat_key, character)
        except CharacterNameTakenError:
            # Sheets are keyed by NAME room-wide: renaming onto someone else's
            # character would overwrite their sheet and steal it. Refuse instead.
            return ctx.fail(ctx.i18n.t("commands.rename.name_taken", name=new_name))
        if old_name and old_name != new_name:
            await ctx.services.characters.delete_character(ctx.user_id, ctx.chat_key, old_name)
        return ctx.i18n.t("commands.rename.changed", old=old_name, new=new_name)

    async def cmd_pc(self, ctx: CommandCtx) -> str:
        """.pc [list]|gen [reference]|claim <name>|release [name]|delete <name>|info <name>`
        — the room's pre-generated character roster (`core.pregen_roster`): module
        imports AND the keeper's `.pc gen` (a room-born character, fitted to the module's
        summary) fill it. Listing and claiming are PLAYER actions (claiming is the whole
        point) — the AI claims the same characters through its companion tools; releasing
        someone else's claim is keeper-only; `delete` (keeper-only) removes a ROOM-BORN,
        UNCLAIMED character entirely; `info` shows any character's dossier."""
        from core.pregen_roster import pregen_claim, pregen_entries, pregen_find, pregen_release

        tokens = ctx.args.split()
        sub = tokens[0].casefold() if tokens else "list"
        rest = " ".join(tokens[1:]).strip()
        documents = ctx.services.documents
        chat_key = ctx.chat_key
        if sub in {"gen", "generate", "生成"}:
            return await self._pc_gen(ctx, rest)
        if sub in {"delete", "del", "remove", "删除", "刪除"}:
            return await self._pc_delete(ctx, rest)
        if sub in {"claim", "认领", "認領"}:
            if not rest:
                return ctx.i18n.t("pregen.commands.claim_usage")
            status, sheet = await pregen_claim(
                documents,
                chat_key,
                rest,
                ctx.user_id,
                ctx.services.characters,
                claimer_name=str(getattr(ctx.raw_ctx, "user_name", "") or ""),
            )
            if status in {"ok", "yours"} and sheet is not None:
                if ctx.router.hub is not None:
                    await publish_state(ctx.router.hub, ctx.services, ctx.raw_ctx)
                key = "pregen.commands.claimed" if status == "ok" else "pregen.commands.reclaimed"
                return ctx.i18n.t(key, name=sheet.name, system=sheet.system)
            return ctx.i18n.t(f"pregen.commands.claim_{status}", name=rest)
        if sub in {"release", "放弃", "放棄", "释放", "釋放"}:
            if not rest:
                return ctx.i18n.t("pregen.commands.release_usage")
            entry = await pregen_find(documents, chat_key, rest)
            if entry is not None and entry.get("claimed_by_kind") == "ai":
                # An AI claim is the companion's whole (record + sheet + marker), and
                # only the keeper has a hand on it — the AI has no CLI. Releasing goes
                # through the agent layer's whole-or-nothing path.
                if not _is_keeper(ctx.raw_ctx):
                    return ctx.fail(ctx.i18n.t("pregen.commands.release_not_yours", name=rest))
                from agent.kp_tools_companion import release_pregen_companion

                status = await release_pregen_companion(ctx.services, chat_key, rest, force=True)
            else:
                status = await pregen_release(
                    documents, chat_key, rest, ctx.user_id, ctx.services.characters, force=_is_keeper(ctx.raw_ctx)
                )
            if status == "ok":
                if ctx.router.hub is not None:
                    await publish_state(ctx.router.hub, ctx.services, ctx.raw_ctx)
                return ctx.i18n.t("pregen.commands.released", name=rest)
            return ctx.i18n.t(f"pregen.commands.release_{status}", name=rest)
        if sub not in {"list", "列表"}:
            return ctx.i18n.t("pregen.commands.usage")
        entries = await pregen_entries(documents, chat_key)
        if not entries:
            return ctx.i18n.t("pregen.commands.empty")
        lines = [ctx.i18n.t("pregen.commands.list_header", count=len(entries))]
        for entry in entries:
            key = "pregen.commands.line_claimed" if entry.get("claimed_by") else "pregen.commands.line_free"
            line = ctx.i18n.t(key, name=entry.get("name", ""), system=entry.get("system", ""))
            claimer = self._pregen_claimer_name(ctx, str(entry.get("claimed_by") or ""))
            if claimer:
                line += ctx.i18n.t("pregen.commands.claimed_by_suffix", claimer=claimer)
            lines.append(line)
    async def _pc_gen(self, ctx: CommandCtx, rest: str) -> str:
        """.pc gen [reference] — author a CLAIMABLE roster character (.pc list).
        Keeper-only: it spends model calls and grows the room's claimable cast.

        The AI writes the character's NAME and DESCRIPTION from the module's summary
        (always) and your optional `reference` text (a name, a concept, or both —
        e.g. `.pc gen 阿岚 | 瘴雾镇的调查员`) as a hint; the sheet is built on the
        ROOM's active rule system, never a picked one. The character lives in the
        ROOM, not in any module pack: it rides no `.lwpack`, survives a module swap
        (`source="room"` is exempt from the purge), and is claimable by players
        (`.pc claim`) and by the AI (`.party add` / the companion tools) exactly
        like a module-imported pregen. The description doubles as the character's
        blurb — the persona an AI claim builds its companion record from.

        In a live room the generation runs ASYNCHRONOUSLY: a pending spinner is
        broadcast first, the two model calls happen in a background task (never
        holding the turn lock), and the outcome lands as a system message when it
        finishes. The CLI path (no hub) stays synchronous."""
        if not _is_keeper(ctx.raw_ctx):
            return ctx.fail(ctx.i18n.t("rooms.denied"))
        try:
            from agent.kp_tools_charcard import _module_full_context

            system = (await ctx.services.room_rulepack(ctx.raw_ctx)).system
            module_context = await _module_full_context(ctx.services, ctx.chat_key)
            if not module_context.strip():
                # No adventure to fit the character to: the model would only invent a
                # placeholder (a "待定" nobody asked for) — refuse loudly instead.
                return ctx.fail(ctx.i18n.t("pregen.commands.gen_no_module"))
        except Exception as exc:
            return ctx.fail(ctx.i18n.t("pregen.commands.gen_error", error=str(exc)))
        if ctx.router.hub is not None:
            # Two model calls can take tens of seconds — never under the room's turn
            # lock. Broadcast a pending spinner, generate in the background, publish
            # the outcome when it lands (spinner retired in place, like `.image`).
            await ctx.router.hub.publish(
                ctx.chat_key,
                Event(
                    kind="system",
                    text=ctx.i18n.t("pregen.commands.gen_pending"),
                    data={"level": "info", "spinner": True},
                ),
            )
            asyncio.get_running_loop().create_task(
                self._pc_gen_worker(ctx, system, module_context, rest.strip())
            )
            return ""
        return await self._pc_gen_sync(ctx, system, module_context, rest.strip())

    async def _pc_gen_sync(self, ctx: CommandCtx, system: str, module_context: str, reference: str) -> str:
        """The generation proper — concept call, sheet build, roster write. Shared by
        the CLI path (returns the outcome text) and the hub worker (whose caller
        publishes that text)."""
        try:
            from agent.char_from_persona import roster_character_concept
            from core.pregen_roster import pregen_add

            concept = await roster_character_concept(
                ctx.services, system, module_context, reference=reference
            )
            name = concept.get("name") or ""
            description = concept.get("description") or ""
            if not name:
                return ctx.fail(ctx.i18n.t("pregen.commands.gen_no_name"))
            sheet = await build_sheet_from_description(
                ctx.services,
                description or name,
                system,
                chat_key=ctx.chat_key,
                name=name,
                module_context=module_context,
                creation="pregen",
            )
            sheet.name = name
            sheet, violations = validate_sheet(sheet, system, initialize_vitals=True)
            entry = await pregen_add(
                ctx.services.documents,
                ctx.chat_key,
                sheet,
                source="room",
                blurb=description or name,
                appearance=concept.get("appearance") or "",
            )
            if entry is None:
                return ctx.fail(ctx.i18n.t("pregen.commands.gen_failed", name=name))
            if ctx.router.hub is not None:
                await publish_state(ctx.router.hub, ctx.services, ctx.raw_ctx)
            result = ctx.i18n.t(
                "pregen.commands.gen_done", name=sheet.name, system=sheet.system
            )
            notice = render_validation_notice(ctx.i18n, violations)
            return f"{result}\n{notice}" if notice else result
        except Exception as exc:
            return ctx.fail(ctx.i18n.t("pregen.commands.gen_error", error=str(exc)))

    async def _pc_gen_worker(self, ctx: CommandCtx, system: str, module_context: str, reference: str) -> None:
        """Hub-mode background generation: run the sync core, then retire the pending
        spinner in place and broadcast the outcome."""
        try:
            result = await self._pc_gen_sync(ctx, system, module_context, reference)
        except Exception as exc:  # noqa: BLE001 — the worker must never die silently
            result = ctx.fail(ctx.i18n.t("pregen.commands.gen_error", error=str(exc)))
        await ctx.router.hub.publish(
            ctx.chat_key,
            Event(
                kind="system",
                text=ctx.i18n.t("pregen.commands.gen_pending"),
                data={"level": "info", "spinner": False},
            ),
        )
        await ctx.router.hub.publish(
            ctx.chat_key,
            Event(kind="system", text=result, data={"level": "info"}),
        )


    async def _pc_delete(self, ctx: CommandCtx, rest: str) -> str:
        """.pc delete <name> — remove a ROOM-BORN, UNCLAIMED roster character entirely.
        Keeper-only. Only `.pc gen` characters (`source="room"`) that nobody has claimed
        can be deleted: a module-imported cast member is the module's own asset, and a
        claimed character is someone's active seat — neither is a delete."""
        if not _is_keeper(ctx.raw_ctx):
            return ctx.fail(ctx.i18n.t("rooms.denied"))
        if not rest:
            return ctx.i18n.t("pregen.commands.delete_usage")
        from core.pregen_roster import pregen_find, slug_for

        entry = await pregen_find(ctx.services.documents, ctx.chat_key, rest)
        if entry is None:
            return ctx.fail(ctx.i18n.t("pregen.commands.delete_unknown", name=rest))
        name = str(entry.get("name") or rest)
        if str(entry.get("source") or "") != "room":
            return ctx.fail(ctx.i18n.t("pregen.commands.delete_module_denied", name=name))
        if entry.get("claimed_by"):
            return ctx.fail(ctx.i18n.t("pregen.commands.delete_claimed", name=name))
        deleted = await ctx.services.documents.delete(ctx.chat_key, "pregen", slug_for(name))
        if not deleted:
            return ctx.fail(ctx.i18n.t("pregen.commands.delete_unknown", name=name))
        # The character's portrait jobs die with it — no stray job record in the
        # room's async lane for a character that no longer exists.
        try:
            from gateway.module_media import drop_pregen_job

            await drop_pregen_job(ctx.services, ctx.chat_key, name)
        except Exception:  # noqa: BLE001 — job cleanup must never fail the delete
            pass
        if ctx.router.hub is not None:
            await publish_state(ctx.router.hub, ctx.services, ctx.raw_ctx)
        return ctx.i18n.t("pregen.commands.delete_done", name=name)


    async def _pc_info(self, ctx: CommandCtx, name: str) -> str:
        """`.pc info <name>` — a character's dossier: module source, memory
        (life summary + recent lines) and relationship tracks. Reads the same
        player projections the browser character page uses, so the terminal and
        the web agree on what a character is."""
        from core.character_memory import CHARACTER_MEMORY_DOC_TYPE, project_character_memory
        from core.documents import PLAYER_VIEWER
        from core.relationships import TRACKS, RelationshipManager

        documents = ctx.services.documents
        chat_key = ctx.chat_key
        lines: list[str] = []

        # Module source — only pregen characters carry one.
        try:
            from core.pregen_roster import slug_for

            source_doc = await documents.get(chat_key, "pregen", slug_for(name))
            if source_doc is not None and source_doc.data.get("source"):
                lines.append(ctx.i18n.t("pregen.commands.info_source", source=str(source_doc.data["source"])))
        except Exception:  # noqa: BLE001 — a dossier is best-effort reading
            pass

        # Memory (player projection): the most recent lines (playthrough
        # memories + journal). The retired folded life-summary is not shown.
        try:
            memory_doc = await documents.get(chat_key, CHARACTER_MEMORY_DOC_TYPE, name)
            if memory_doc is not None:
                memory = project_character_memory(memory_doc, PLAYER_VIEWER) or {}
                entries = []
                for entry in (memory.get("entries") or []):
                    text = str(entry.get("text") if isinstance(entry, dict) else entry or "").strip()
                    if text:
                        entries.append(text)
                entries = entries[-5:]
                entries.reverse()
                if entries:
                    lines.append(ctx.i18n.t("pregen.commands.info_memory_entries", count=len(entries)))
                    lines.extend(f"  • {entry}" for entry in entries)
        except Exception:  # noqa: BLE001
            pass

        # Relationship tracks this character holds toward each entity.
        try:
            relationship_state = await RelationshipManager(ctx.services.store).load(chat_key)
            for target, tracks in (relationship_state.get(name) or {}).items():
                pairs: list[str] = []
                for track_id, value in tracks.items():
                    spec = TRACKS.get(track_id)
                    if spec is None or value == spec.default:
                        continue
                    pairs.append(f"{ctx.i18n.t(spec.label_key)} {value:+d}")
                if pairs:
                    lines.append(
                        ctx.i18n.t("pregen.commands.info_relationship", target=target, tracks=", ".join(pairs))
                    )
        except Exception:  # noqa: BLE001
            pass

        if not lines:
            return ctx.i18n.t("pregen.commands.info_empty", name=name)
        return "\n".join(lines)

    def _pregen_claimer_name(self, ctx: CommandCtx, user_id: str) -> str:
        """Map a pregen claim's user_id back to the human name on the key that
        made it, so `.pc list` shows who holds what (claims are table talk —
        the roster entry is player-visible either way, only the holder was
        anonymous). No keystore (standalone CLI) or unknown id → nothing."""
        if not user_id:
            return ""
        keystore = getattr(self, "keystore", None)
        if keystore is None:
            return ""
        try:
            from net.keystore import member_id_for_key

            for entry in keystore.entries(purpose=None):
                if entry.name and member_id_for_key(entry.key) == user_id:
                    return str(entry.name)
        except Exception:
            return ""
        return ""
