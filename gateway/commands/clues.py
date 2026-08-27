"""The `.clue` command — the room's discovered-clue log (structural clue tracking).

Clues are worldbook entries the party has actually found. `.clue add` (keeper)
snapshots a matching worldbook clue entry into the room's `clue_log`; `.clue
list` shows the discovered clues to everyone; `.clue remove` (keeper) retracts
one. Players never see a secret clue — it is not in the log until the table
finds it, and the snapshot is taken at discovery time.

Subcommands:
    .clue [list]              — show the room's discovered clues (any member)
    .clue add <name|key>      — keeper: register a worldbook clue as discovered
    .clue remove <name>       — keeper: retract a discovered clue

Every user-facing string routes through `infra.i18n` + `locales/{en,zh}/commands.json`.
"""

from __future__ import annotations

from agent.clue_log import find_worldbook_clue, get_clue_log, remove_clue, reveal_clue
from agent.tool_trace import active_module_id
from gateway.commands.rooms import _is_keeper
from gateway.commands.types import CommandCtx


def _clue_line(ctx: CommandCtx, entry: dict) -> str:
    title = str(entry.get("title", ""))
    content = str(entry.get("content", ""))
    if len(content) > 140:
        content = content[:140] + "…"
    return ctx.i18n.t("commands.clue.entry", title=title, content=content)


class ClueCommands:
    """`.clue` — the room's discovered-clue log."""

    async def cmd_clue(self, ctx: CommandCtx) -> str:
        args = ctx.args.strip()
        parts = args.split(maxsplit=1)
        sub = parts[0].casefold() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if not sub or sub in ("list", "ls", "查看", "列表"):
            return await self._clue_list(ctx)
        if sub in ("add", "发现", "新增"):
            return await self._clue_add(ctx, rest)
        if sub in ("remove", "del", "移除", "删除"):
            return await self._clue_remove(ctx, rest)
        return ctx.i18n.t("commands.clue.usage")

    async def _clue_list(self, ctx: CommandCtx) -> str:
        clues = await get_clue_log(ctx.services.documents, ctx.chat_key)
        if not clues:
            return ctx.i18n.t("commands.clue.empty")
        lines = [ctx.i18n.t("commands.clue.header", count=len(clues))]
        lines.extend(_clue_line(ctx, entry) for entry in clues)
        return "\n".join(lines)

    async def _clue_add(self, ctx: CommandCtx, rest: str) -> str:
        if not _is_keeper(ctx.raw_ctx):
            return ctx.fail(ctx.i18n.t("commands.clue.denied"))
        name = rest.strip()
        if not name:
            return ctx.i18n.t("commands.clue.usage")
        entry = await find_worldbook_clue(ctx.services.worldbook, ctx.chat_key, name)
        if entry is None:
            return ctx.fail(ctx.i18n.t("commands.clue.not_found", name=name))
        added = await reveal_clue(
            ctx.services.documents,
            ctx.chat_key,
            title=entry["title"] or name,
            content=entry["content"],
            keys=entry["keys"],
            image=entry["image"],
            module=await active_module_id(ctx.services, ctx.chat_key),
        )
        if not added:
            return ctx.i18n.t("commands.clue.already_added", name=entry["title"] or name)
        return ctx.i18n.t("commands.clue.added", name=entry["title"] or name)

    async def _clue_remove(self, ctx: CommandCtx, rest: str) -> str:
        if not _is_keeper(ctx.raw_ctx):
            return ctx.fail(ctx.i18n.t("commands.clue.denied"))
        name = rest.strip()
        if not name:
            return ctx.i18n.t("commands.clue.usage")
        if not await remove_clue(ctx.services.documents, ctx.chat_key, name):
            return ctx.fail(ctx.i18n.t("commands.clue.not_found", name=name))
        return ctx.i18n.t("commands.clue.removed", name=name)
