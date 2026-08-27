"""The `.trace` command — the per-room tool-call probe toggle.

`.trace on [path]` turns the probe on for THE CALLING ROOM only (its own JSONL
file, `tool-trace-<room>.jsonl` under the data dir by default) and persists the
choice (server-level kv, `runtime_config.tool_trace`, keyed by room), so it
survives restarts; `.trace off` turns this room off; bare `.trace` reports.
The probe rows carry the room's chat_key and the active module id, so a file
stays attributable to one table and filterable by scenario. Keeper-only toggling
— the files hold keeper-grade content (tool arguments and results).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.tool_trace import (
    TOOL_TRACE_KV_KEY,
    default_trace_path,
    disable_tool_trace,
    enable_tool_trace,
    tool_trace_enabled,
)
from gateway.commands.rooms import _is_keeper
from gateway.commands.types import CommandCtx

_OFF_WORDS = {"off", "stop", "close", "关", "關", "关闭", "關閉", "停"}
_ON_WORDS = {"on", "start", "open", "开", "開", "开启", "開啟", "打开", "打開"}


class TraceCommands:
    """`CommandRouter` mixin — the per-room tool-call probe toggle."""

    async def cmd_trace(self, ctx: CommandCtx) -> str:
        """`.trace [on [path] | off]` — this room's tool-call probe: which tool the AI called, with what arguments, and how long each model call took.

        Bare `.trace` reports this room's state; `.trace on [path]` enables the JSONL
        probe for THIS ROOM only (default `tool-trace-<room>.jsonl` under the server
        data dir) and survives restarts; `.trace off` disables it here. Other rooms
        are untouched. The probe file holds keeper-grade content (tool
        arguments/results), so toggling is keeper-only."""
        wanted = ctx.args.strip()
        room = ctx.chat_key
        if not wanted:
            state = ctx.i18n.t("commands.trace.state_on" if tool_trace_enabled(room) else "commands.trace.state_off")
            path = await self._room_path(ctx)
            return ctx.i18n.t(
                "commands.trace.status", state=state, path=path or ctx.i18n.t("common.none")
            )
        if not _is_keeper(ctx.raw_ctx):
            return ctx.fail(ctx.i18n.t("commands.trace.denied"))
        parts = wanted.split(maxsplit=1)
        verb = parts[0].casefold()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if verb in _OFF_WORDS:
            disable_tool_trace(room)
            await self._persist_room(ctx, room, "")
            return ctx.i18n.t("commands.trace.off_done")
        if verb in _ON_WORDS:
            resolved = self._resolve(ctx, room, arg)
            enable_tool_trace(resolved, room=room)
            await self._persist_room(ctx, room, str(resolved))
            return ctx.i18n.t("commands.trace.on_done", path=str(resolved))
        return ctx.i18n.t("commands.trace.usage")

    def _resolve(self, ctx: CommandCtx, room: str, path: str) -> Path:
        """The absolute probe path for `room`: `path` as given (absolute or under
        data_dir), else the per-room default `tool-trace-<room>.jsonl` under data_dir."""
        data_dir = str(getattr(getattr(ctx.services, "settings", None), "data_dir", "") or "")
        if path:
            candidate = Path(path)
            if candidate.is_absolute():
                return candidate
            return Path(data_dir) / path if data_dir else candidate
        return default_trace_path(data_dir, room=room) if data_dir else default_trace_path("", room=room)

    async def _room_path(self, ctx: CommandCtx) -> str:
        """This room's persisted trace path ("" when off)."""
        mapping = await self._persisted_map(ctx)
        if ctx.chat_key in mapping:
            return mapping[ctx.chat_key]
        return mapping.get("", "")

    async def _persist_room(self, ctx: CommandCtx, room: str, path: str) -> None:
        """Merge `room` -> `path` ("" removes it) into the persisted map."""
        mapping = await self._persisted_map(ctx)
        if path:
            mapping[room] = path
        else:
            mapping.pop(room, None)
        await ctx.services.store.set(
            user_key="",
            store_key=TOOL_TRACE_KV_KEY,
            value=json.dumps(mapping, ensure_ascii=False) if mapping else None,
        )

    async def _persisted_map(self, ctx: CommandCtx) -> dict[str, str]:
        """The persisted {room: path} map (legacy bare string -> global room "")."""
        try:
            raw = await ctx.services.store.get(user_key="", store_key=TOOL_TRACE_KV_KEY)
        except Exception:
            return {}
        if not raw:
            return {}
        text = str(raw).strip()
        try:
            value = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"": text} if text else {}
        if not isinstance(value, dict):
            return {"": text} if text else {}
        return {str(room): str(p).strip() for room, p in value.items() if str(p).strip()}
