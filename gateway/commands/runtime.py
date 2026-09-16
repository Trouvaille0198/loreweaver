"""`.runtime` — inspect and operate the imported native scenario state."""

from __future__ import annotations

from typing import Any

from core.module_runtime import ModuleRuntimeError, apply_runtime_action, project_runtime
from gateway.commands.rooms import _is_keeper
from gateway.commands.types import CommandCtx


class RuntimeCommands:
    """The command lane for the same deterministic module-runtime action bus used by AI."""

    async def cmd_runtime(self, ctx: CommandCtx) -> str:
        parts = ctx.args.split()
        sub = parts[0].casefold() if parts else "state"
        values = parts[1:]
        if sub in {"state", "list", "status", "查看", "状态"}:
            return await self._state(ctx)
        if not _is_keeper(ctx.raw_ctx):
            return ctx.fail(ctx.i18n.t("commands.runtime.denied"))
        try:
            if sub in {"scene", "enter", "进入"} and values:
                return await self._apply(ctx, {"action": "enter_scene", "scene_id": values[0]}, "commands.runtime.scene_done", {"id": values[0]})
            if sub in {"clue", "reveal", "线索", "揭示"} and values:
                return await self._apply(ctx, {"action": "reveal_clue", "clue_id": values[0]}, "commands.runtime.clue_done", {"id": values[0]})
            if sub in {"objective", "目标"} and len(values) >= 2:
                action: dict[str, Any] = {"action": "update_objective", "objective_id": values[0], "status": values[1]}
                if len(values) >= 3:
                    action["progress"] = int(values[2])
                return await self._apply(ctx, action, "commands.runtime.objective_done", {"id": values[0], "status": self._status(ctx, values[1])})
            if sub in {"actor", "角色"} and len(values) >= 2:
                return await self._apply(ctx, {"action": "update_actor_state", "actor_id": values[0], "status": values[1]}, "commands.runtime.actor_done", {"id": values[0], "status": self._status(ctx, values[1])})
            if sub in {"tracker", "追踪器"} and len(values) >= 2:
                return await self._apply(ctx, {"action": "set_tracker", "tracker_id": values[0], "value": values[1], "actor_id": values[2] if len(values) >= 3 else ""}, "commands.runtime.tracker_done", {"id": values[0]})
            if sub in {"adjust", "调整"} and len(values) >= 2:
                return await self._apply(ctx, {"action": "adjust_tracker", "tracker_id": values[0], "delta": int(values[1]), "actor_id": values[2] if len(values) >= 3 else ""}, "commands.runtime.tracker_done", {"id": values[0]})
            if sub in {"ending", "resolve", "结局", "结算"} and values:
                return await self._apply(ctx, {"action": "resolve_ending", "ending_id": values[0]}, "commands.runtime.ending_done", {"id": values[0]})
            if sub in {"reward", "grant", "奖励", "发放"} and values:
                return await self._apply(ctx, {"action": "grant_reward", "reward_id": values[0], "recipient": values[1] if len(values) >= 2 else "party", "quantity": int(values[2]) if len(values) >= 3 else 1}, "commands.runtime.reward_done", {"id": values[0]})
        except (ValueError, TypeError):
            return ctx.fail(ctx.i18n.t("commands.runtime.bad_args"))
        return ctx.fail(ctx.i18n.t("commands.runtime.usage"))

    @staticmethod
    def _status(ctx: CommandCtx, value: str) -> str:
        translated = ctx.i18n.t(f"commands.runtime.status.{value}")
        return value if translated == f"commands.runtime.status.{value}" else translated

    async def _state(self, ctx: CommandCtx) -> str:
        doc = await ctx.services.documents.get_singleton(ctx.chat_key, "module_runtime")
        if doc is None:
            return ctx.i18n.t("commands.runtime.none")
        # This command is private, but its normal summary is intentionally the
        # same player-safe projection the desk receives. Keeper-only audit and
        # blueprint material is available through the AI keeper tool, not here.
        view = project_runtime(doc.data, keeper=False) or {}
        scene = view.get("scene") or {}
        lines = [ctx.i18n.t("commands.runtime.header"), ctx.i18n.t("commands.runtime.scene", name=scene.get("name") or ctx.i18n.t("commands.runtime.none_value"))]
        objectives = view.get("objectives") or []
        if objectives:
            lines.append(ctx.i18n.t("commands.runtime.objectives"))
            lines.extend(ctx.i18n.t("commands.runtime.objective", name=item.get("name", item.get("id", "")), status=self._status(ctx, str(item.get("status", "pending"))), progress=item.get("progress", 0)) for item in objectives)
        actors = view.get("actors") or []
        if actors:
            lines.append(ctx.i18n.t("commands.runtime.actors"))
            lines.extend(ctx.i18n.t("commands.runtime.actor", name=item.get("name", item.get("id", "")), status=self._status(ctx, str(item.get("status", "active")))) for item in actors)
        trackers = view.get("trackers") or []
        if trackers:
            lines.append(ctx.i18n.t("commands.runtime.trackers"))
            lines.extend(ctx.i18n.t("commands.runtime.tracker", name=item.get("name", item.get("id", "")), value=item.get("value")) for item in trackers)
        clues = view.get("clues") or []
        if clues:
            lines.append(ctx.i18n.t("commands.runtime.clues", count=len(clues)))
        if view.get("ending"):
            lines.append(ctx.i18n.t("commands.runtime.ending", name=view["ending"].get("name", view["ending"].get("id", ""))))
        if view.get("rewards"):
            lines.append(ctx.i18n.t("commands.runtime.rewards", count=len(view["rewards"])))
        return "\n".join(lines)

    async def _apply(self, ctx: CommandCtx, action: dict[str, Any], key: str, fields: dict[str, Any]) -> str:
        try:
            _runtime, audit = await apply_runtime_action(
                ctx.services.documents,
                ctx.chat_key,
                action,
                actor=ctx.user_id or "keeper",
                reason=f"command:{action.get('action')}",
            )
        except ModuleRuntimeError as exc:
            return ctx.fail(ctx.i18n.t("commands.runtime.refused"))
        return ctx.i18n.t(key, **fields, sequence=audit["sequence"])
