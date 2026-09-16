"""AI Keeper tools for the native scenario runtime.

These tools submit intent to ``core.module_runtime``.  They do not edit a card,
interpret a dice result, or turn narration into a fact: every mutation is one
validated, serialized action with an audit record.
"""

from __future__ import annotations

import json
from typing import Any

from agent.context import AgentCtx
from agent.services import Services
from agent.tools import tool
from core.module_runtime import ModuleRuntimeError, apply_runtime_action, project_runtime


class ModuleRuntimeTools:
    """Keeper-facing deterministic scene, objective, actor, ending and reward lane."""

    def __init__(self, services: Services) -> None:
        self._services = services

    async def _action(
        self,
        ctx: AgentCtx,
        action: dict[str, Any],
        *,
        reason: str = "",
        evidence: str = "",
        result_key: str,
        fields: dict[str, Any] | None = None,
    ) -> str:
        i18n = self._services.i18n.with_locale(ctx.locale)
        try:
            _runtime, audit = await apply_runtime_action(
                self._services.documents,
                ctx.chat_key,
                action,
                actor=ctx.uid() or "AI keeper",
                reason=reason,
                evidence=evidence,
            )
            return i18n.t(result_key, **(fields or {}), sequence=audit["sequence"])
        except ModuleRuntimeError as exc:
            return i18n.t("kp_tools.runtime.refused", error=str(exc))
        except Exception as exc:  # a tool must return a bounded model-readable refusal
            return i18n.t("kp_tools.runtime.failed", error=str(exc))

    @tool(keeper_only=True, read_only=True)
    async def get_module_runtime(self, ctx: AgentCtx) -> str:
        """Read the complete native scenario runtime and its available actions.

        Use this before changing a scene, objective, actor, tracker, ending or
        reward. The returned JSON is state, not permission to invent a result.
        """
        doc = await self._services.documents.get_singleton(ctx.chat_key, "module_runtime")
        if doc is None:
            return self._services.i18n.with_locale(ctx.locale).t("kp_tools.runtime.none")
        return json.dumps(project_runtime(doc.data, keeper=True), ensure_ascii=False, indent=2)

    @tool(keeper_only=True)
    async def enter_module_scene(self, ctx: AgentCtx, scene_id: str, reason: str = "", evidence: str = "") -> str:
        """Enter an authored scene after its deterministic condition is satisfied.

        Never call this merely because narration mentioned a place. The scene
        must be the place the table actually entered.
        """
        return await self._action(ctx, {"action": "enter_scene", "scene_id": scene_id}, reason=reason, evidence=evidence, result_key="kp_tools.runtime.scene_entered", fields={"scene": scene_id})

    @tool(keeper_only=True)
    async def reveal_module_clue(self, ctx: AgentCtx, clue_id: str, reason: str = "", evidence: str = "") -> str:
        """Reveal one authored clue after the party genuinely discovers it."""
        return await self._action(ctx, {"action": "reveal_clue", "clue_id": clue_id}, reason=reason, evidence=evidence, result_key="kp_tools.runtime.clue_revealed", fields={"clue": clue_id})

    @tool(keeper_only=True)
    async def update_module_objective(self, ctx: AgentCtx, objective_id: str, status: str, progress: int = -1, reason: str = "", evidence: str = "") -> str:
        """Set an authored objective to pending, active, complete, failed or blocked."""
        action: dict[str, Any] = {"action": "update_objective", "objective_id": objective_id, "status": status}
        if progress >= 0:
            action["progress"] = progress
        return await self._action(ctx, action, reason=reason, evidence=evidence, result_key="kp_tools.runtime.objective_updated", fields={"objective": objective_id, "status": status})

    @tool(keeper_only=True)
    async def update_module_actor(self, ctx: AgentCtx, actor_id: str, status: str, reason: str = "", evidence: str = "") -> str:
        """Set an authored actor/NPC to one of its declared statuses."""
        return await self._action(ctx, {"action": "update_actor_state", "actor_id": actor_id, "status": status}, reason=reason, evidence=evidence, result_key="kp_tools.runtime.actor_updated", fields={"actor": actor_id, "status": status})

    @tool(keeper_only=True)
    async def set_module_tracker(self, ctx: AgentCtx, tracker_id: str, value: str, actor_id: str = "", reason: str = "", evidence: str = "") -> str:
        """Set a declared module tracker; bounds and allowed values are enforced by the engine."""
        parsed: Any = value
        if str(value).strip().casefold() in {"true", "false"}:
            parsed = str(value).strip().casefold() == "true"
        else:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                pass
        return await self._action(ctx, {"action": "set_tracker", "tracker_id": tracker_id, "value": parsed, "actor_id": actor_id}, reason=reason, evidence=evidence, result_key="kp_tools.runtime.tracker_set", fields={"tracker": tracker_id})

    @tool(keeper_only=True)
    async def adjust_module_tracker(self, ctx: AgentCtx, tracker_id: str, delta: int, actor_id: str = "", reason: str = "", evidence: str = "") -> str:
        """Adjust a declared numeric module tracker with its authored bounds."""
        return await self._action(ctx, {"action": "adjust_tracker", "tracker_id": tracker_id, "delta": delta, "actor_id": actor_id}, reason=reason, evidence=evidence, result_key="kp_tools.runtime.tracker_adjusted", fields={"tracker": tracker_id, "delta": delta})

    @tool(keeper_only=True)
    async def resolve_module_ending(self, ctx: AgentCtx, ending_id: str, reason: str = "", evidence: str = "") -> str:
        """Resolve one authored ending only when its condition matrix is satisfied."""
        return await self._action(ctx, {"action": "resolve_ending", "ending_id": ending_id}, reason=reason, evidence=evidence, result_key="kp_tools.runtime.ending_resolved", fields={"ending": ending_id})

    @tool(keeper_only=True)
    async def grant_module_reward(self, ctx: AgentCtx, reward_id: str, recipient: str = "party", quantity: int = 1, reason: str = "", evidence: str = "") -> str:
        """Record an authored reward after the ending or its own availability condition permits it.

        This records a deterministic reward ledger entry. Physical catalog items
        still use the ordinary item-grant tool so inventory validation remains in
        one place.
        """
        return await self._action(ctx, {"action": "grant_reward", "reward_id": reward_id, "recipient": recipient, "quantity": quantity}, reason=reason, evidence=evidence, result_key="kp_tools.runtime.reward_granted", fields={"reward": reward_id, "recipient": recipient, "quantity": quantity})
