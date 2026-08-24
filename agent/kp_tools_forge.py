"""AI-KP tools for Layer B.3: the self-extension engines (`agent.forge`).

`ForgeTools` exposes the three `generate_*` tools -- `generate_skill` (B.3a), `generate_rulepack`
and `generate_module` (B.3b) -- and all three are `gated=True` (Layer B.2 -- see `agent.tools.tool`
and `docs/plugins.md` "Layer B"): hidden from the model's toolset and refused on dispatch unless
the room has enabled the matching forge skill (`skill-forge`/`rule-forge`/`module-forge`, whose
`allowed-tools:` is what unlocks each one). A fresh install can never have the AI Keeper author and
install new skills/rule systems/modules on its own initiative -- the keeper must opt in first.
"""

from __future__ import annotations

from agent.context import AgentCtx
from agent.forge import (
    generate_and_install_module,
    generate_and_install_pack_module,
    generate_and_install_rulepack,
    generate_and_install_skill,
)
from agent.services import Services
from agent.tools import tool
from infra.i18n import I18n


class ForgeTools:
    """The forge tool provider: authors + installs a new KP skill / rule system / module from a
    natural-language ask, reusing `agent.forge`'s three generators."""

    def __init__(self, services: Services) -> None:
        self._services = services

    def _i18n(self, ctx: AgentCtx) -> I18n:
        return self._services.i18n.with_locale(ctx.locale)

    @tool(gated=True, prep_only=True)
    async def generate_skill(self, ctx: AgentCtx, description: str) -> str:
        """Author and install a brand-new KP skill (a SKILL.md play-style bundle) from a
        natural-language description of the desired play-style. Only available once the
        `skill-forge` skill is enabled for this room.

        Args:
            description: A clear, self-contained description of the play-style to package: what
                it's for, what tone or mechanics it should bring, and (if anything) what tools it
                should unlock.

        Returns:
            Confirmation naming the new skill and its id, or an explanation of why generation
            failed (nothing is installed on failure).
        """
        i18n = self._i18n(ctx)
        result = await generate_and_install_skill(self._services, description, chat_key=ctx.chat_key)
        if result.ok:
            return i18n.t("agent.forge.installed", name=result.name, skill_id=result.skill_id, path=result.path)
        if result.error == "no_data_dir":
            return i18n.t("agent.forge.no_data_dir")
        if result.error.startswith("bad_id"):
            return i18n.t("agent.forge.bad_id", error=result.error.removeprefix("bad_id: "))
        return i18n.t("agent.forge.invalid", error=result.error)

    @tool(gated=True, prep_only=True)
    async def generate_rulepack(self, ctx: AgentCtx, description: str) -> str:
        """Author and install a brand-new TTRPG rule system (a rulepacks/<id>.yaml data pack) from a
        natural-language description of the system's sheet and how its checks resolve. Only
        available once the `rule-forge` skill is enabled for this room.

        Args:
            description: A clear, self-contained description of the rule system to package: its
                core attributes/skills and their starting values, how a check succeeds or fails,
                and any derived stats (health, damage bonus, modifiers, ...) it needs.

        Returns:
            Confirmation naming the new rule system and its id, or an explanation of why generation
            failed (nothing is installed on failure).
        """
        i18n = self._i18n(ctx)
        result = await generate_and_install_rulepack(self._services, description, chat_key=ctx.chat_key)
        if result.ok:
            return i18n.t("agent.forge.rulepack_installed", name=result.name, rulepack_id=result.skill_id, path=result.path)
        if result.error == "no_data_dir":
            return i18n.t("agent.forge.rulepack_no_data_dir")
        if result.error.startswith("bad_id"):
            return i18n.t("agent.forge.rulepack_bad_id", error=result.error.removeprefix("bad_id: "))
        return i18n.t("agent.forge.rulepack_invalid", error=result.error)

    @tool(gated=True, prep_only=True)
    async def generate_module(
        self,
        ctx: AgentCtx,
        description: str,
        media: list[str] | None = None,
        companion: list[str] | None = None,
    ) -> str:
        """Author and install a brand-new module/scenario document from a natural-language
        description (or a keeper-provided premise), landing it directly in THIS room's module
        knowledge pool through the same analysis the `.module` command uses. Only available once
        the `module-forge` skill is enabled for this room.

        Args:
            description: A clear, self-contained description of the scenario to author: setting,
                premise, the key NPCs/threats involved, and the shape of the mystery/adventure.
            media: Optional extra illustrations to generate alongside the module, chosen from:
                "cover" (one opening image), "scenes" (one per key scene), "npcs" (one portrait
                per key NPC), "items" (one per key item/clue). Generated images are stored in the
                room's media deck for the keeper to show later, never auto-broadcast.
            companion: Optional companion content to generate alongside the module, chosen from:
                "skills" (a KP skill for this scenario), "rulepacks" (a rule system for it),
                "cards" (claimable pre-generated character cards).

        Returns:
            Confirmation naming the new module and summarizing this room's resulting knowledge-pool
            state (plus any generated media/companion content), or an explanation of why generation
            failed (nothing is installed on failure).
        """
        i18n = self._i18n(ctx)
        result = await generate_and_install_module(self._services, ctx, description, media=media, companion=companion)
        if result.ok:
            if result.reused:
                return i18n.t(
                    "agent.forge.module_reused",
                    name=result.name,
                    path=result.path,
                )
            return i18n.t("agent.forge.module_installed", name=result.name, path=result.path, detail=result.detail)
        if result.error == "no_data_dir":
            return i18n.t("agent.forge.module_no_data_dir")
        if result.error.startswith("bad_id"):
            return i18n.t("agent.forge.module_bad_id", error=result.error.removeprefix("bad_id: "))
        return i18n.t("agent.forge.module_invalid", error=result.error)


    @tool(gated=True, prep_only=True)
    async def generate_pack_module(
        self,
        ctx: AgentCtx,
        description: str,
        media: list[str] | None = None,
        companion: list[str] | None = None,
        extends_base: str = "",
        system: str = "",
    ) -> str:
        """Author and install a COMPLETE module as a native world card wrapped in a `.lwpack`
        content pack — the engine's canonical full-module shape (lorebook + typed trackers +
        claimable cast + optional assets + bundled companion skill/rulepack), unlike
        `generate_module`'s flat Markdown scenario. Only available once the `module-forge` skill
        is enabled for this room.

        Args:
            description: A clear, self-contained description of the scenario to author: setting,
                premise, the key NPCs/threats involved, and the shape of the mystery/adventure.
            media: Optional illustrations to generate and bundle INTO the pack's assets (travel
                with the module), chosen from: "cover", "scenes", "npcs", "items".
            companion: Optional companion content to bundle INTO the pack, chosen from: "skills"
                (a KP skill for this scenario), "rulepacks" (a rule system for it). Pregen cards
                are already carried by the world card's own `pregens:` cast.
            extends_base: When set (e.g. "coc7"), the generated rulepack is a PATCH on that base
                system (``extends: <base>``) instead of a standalone replacement — so the module
                reuses a known system's attributes/skills and just adds its own mechanics.
            system: When set (e.g. "dnd5e"), the module DIRECTLY uses that built-in rule system —
                the world card declares it and the room pins it on import, with NO rulepack
                generated or shipped (mutually exclusive with ``extends_base``).

        Returns:
            Confirmation naming the built `.lwpack` and what it installed into this room, or an
            explanation of why generation failed (nothing is installed on failure).
        """
        i18n = self._i18n(ctx)
        result = await generate_and_install_pack_module(
            self._services,
            ctx,
            description,
            media=media,
            companion=companion,
            extends_base=extends_base,
            system=system,
        )
        if result.ok:
            return result.detail or i18n.t("agent.forge.pack_module_installed", name=result.name, path=result.path)
        if result.error == "no_data_dir":
            return i18n.t("agent.forge.module_no_data_dir")
        if result.error.startswith("invalid_pack_module"):
            return i18n.t("agent.forge.module_invalid", error=result.error)
        return i18n.t("agent.forge.module_invalid", error=result.error)
