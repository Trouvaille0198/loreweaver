"""AI-KP tools for generated image handouts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from agent.context import AgentCtx
from agent.services import Services
from agent.tools import tool
from gateway.hub import Event
from gateway.imagegen import (
    acquire_imagegen_slot,
    allow_imagegen_request,
    gather_image_reference,
    image_name,
    release_imagegen_slot,
)
from gateway.media import media_frame, publish_media
from infra.i18n import I18n
from infra.imagegen import ImageGenError
from infra.media_store import ALLOWED_IMAGE_MIMES, MediaStore

if TYPE_CHECKING:
    from gateway.hub import RoomHub


class ImageTools:
    """Gated tools for generated player-visible image handouts."""

    def __init__(self, services: Services, *, hub: RoomHub | None = None) -> None:
        self._services = services
        self._hub = hub
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _i18n(self, ctx: AgentCtx) -> I18n:
        return self._services.i18n.with_locale(ctx.locale)

    @tool(read_only=True)
    async def list_reference_media(self, ctx: AgentCtx) -> str:
        """List the room's PUBLISHED illustrations (scenes, NPCs, items/clues the players have already seen).

        Every entry is a valid `reference_subject` for generate_image — reuse one to keep new art consistent with what the table knows. Check this before generating an image, and pick a matching subject instead of inventing a name the room has no art for."""
        i18n = self._i18n(ctx)
        try:
            import json

            raw = await self._services.store.state_get(ctx.chat_key, "module_media_index")
            entries: list[dict] = []
            if raw:
                try:
                    value = json.loads(raw)
                    if isinstance(value, list):
                        entries = [e for e in value if isinstance(e, dict)]
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            if not entries:
                return i18n.t("kp_tools.image.media_none")
            lines = [i18n.t("kp_tools.image.media_header", count=len(entries))]
            for entry in entries:
                kind = str(entry.get("kind") or "?")
                subject = str(entry.get("subject") or entry.get("name") or "?")
                lines.append(f" - [{kind}] {subject}")
            return "\n".join(lines)
        except Exception as exc:
            return i18n.t("kp_tools.image.media_failed", error=str(exc))

    @tool(gated=True)
    async def generate_image(
        self, ctx: AgentCtx, prompt: str, kind: str = "scene", caption: str = "", reference_subject: str = ""
    ) -> str:
        """Generate one player-visible image handout and send it to the room.

        The call returns immediately: generation runs in the background and the finished
        image is delivered to the room as a media message shortly after — never wait for
        it inside the narration, and do not call this tool again until the previous image
        has arrived (the room refuses a second request while one is in flight). Each call
        may spend real API money, so use at most one image per scene and never chain
        repeated generations. The prompt and resulting image must contain only information
        the players already know.

        Args:
            ctx: Framework-injected call context; never part of the model-facing schema.
            prompt: Player-safe image prompt sent to the external image provider.
            kind: scene, portrait, or item. Used only for the generated file name and reply text.
            caption: Optional player-visible caption for the Keeper to narrate after sending.
            reference_subject: Optional name of a subject the players have ALREADY SEEN an
                illustration of (a module scene, NPC, clue/item, or character name). When
                given, that published illustration anchors the new image as a style/subject
                reference, keeping the art consistent with what the table knows. Leave
                empty when no such image exists or none fits. Run list_reference_media
                first to see which subjects the room actually has art for — reuse one of
                those names instead of inventing a subject with no published image.

        Returns:
            A localized confirmation that generation started (the image itself arrives as
            a separate room media message), or a localized reason it was skipped.
        """
        i18n = self._i18n(ctx)
        imagegen = await self._services.imagegen_for_room(ctx.chat_key)
        if imagegen is None:
            return i18n.t("kp_tools.image.generate.not_configured")
        if not allow_imagegen_request(self._services, ctx.chat_key):
            return i18n.t("kp_tools.image.generate.rate_limited")
        if not acquire_imagegen_slot(self._services, ctx.chat_key):
            return i18n.t("kp_tools.image.generate.in_flight")

        # Generation is minutes-long (reference gathering + a provider I2I/T2I call).
        # Running it inline would hold the room's turn lock for the whole stretch, so
        # the slow half runs as a background task — this tool returns immediately and
        # the finished image lands in the room as a media frame + system message (the
        # spinner line doubles as the pending placeholder). The imagegen client and the
        # checks above are resolved up front so the task can never hit a misconfigured
        # room.
        if self._hub is not None:
            await self._hub.publish(
                ctx.chat_key,
                Event(
                    kind="system",
                    text=i18n.t("kp_tools.image.generate.generating"),
                    data={"level": "info", "spinner": True},
                ),
            )
        task = asyncio.create_task(
            self._generate_image_background(
                ctx, imagegen, prompt, kind=kind, caption=caption, reference_subject=reference_subject
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return i18n.t("kp_tools.image.generate.started")

    async def _generate_image_background(
        self,
        ctx: AgentCtx,
        imagegen: Any,
        prompt: str,
        *,
        kind: str,
        caption: str,
        reference_subject: str,
    ) -> None:
        """The slow half of `generate_image` — reference gathering, provider generation,
        storage and media publication — run OUTSIDE the room's turn lock so a
        minutes-long request never blocks the table. Success and failure both surface as
        room system messages (the spinner line doubles as the pending placeholder)."""
        i18n = self._i18n(ctx)
        try:
            # Reuse an illustration the PLAYERS HAVE ALREADY SEEN as the reference so
            # the new image stays consistent with what the table knows (iron rule #3:
            # `published_only` excludes keeper-only module art that was never shown).
            # Best-effort: a missing reference is a prompt-only generation, never an
            # error.
            ref_bytes, ref_mime = await gather_image_reference(
                self._services,
                ctx.chat_key,
                kind,
                imagegen,
                focus=reference_subject,
                extra=prompt,
                published_only=True,
            )
            # A scene/clue reference must only guide style and atmosphere — the image
            # provider may otherwise extract a person/face present in the reference as
            # the subject. Portrait references WANT that (character consistency), so the
            # hint is skipped there.
            display_prompt = prompt
            if ref_bytes and kind != "portrait":
                hint = i18n.t("commands.image.reference_hint")
                prompt = f"{prompt} {hint}".strip()
            data, mime = await imagegen.generate(
                prompt,
                size=self._services.settings.imagegen.size,
                reference=ref_bytes,
                reference_mime=ref_mime,
            )
            settings = self._services.settings.tui
            store = MediaStore(
                self._services.store,
                self._services.settings.data_dir,
                max_file_bytes=settings.media_max_file_bytes,
                room_quota_bytes=settings.media_room_quota_bytes,
                allowed_mimes=ALLOWED_IMAGE_MIMES,
            )
            record = await store.register_blob(
                room=ctx.chat_key,
                data=data,
                mime=mime,
                name=image_name(kind if kind in {"scene", "portrait", "item"} else "image", display_prompt),
                uploader=ctx.uid(),
            )
            frame = media_frame(record, from_name="KP", prompt=display_prompt)
            await publish_media(self._hub, self._services.store, ctx.chat_key, frame)
            await self._image_notify(
                ctx,
                i18n.t(
                    "kp_tools.image.generate.done",
                    name=record.name,
                    hash=record.hash[:12],
                    caption=caption.strip(),
                ),
            )
        except ImageGenError as exc:
            await self._image_notify(ctx, i18n.t(f"kp_tools.image.generate.error.{exc.code}"))
        except Exception as exc:
            await self._image_notify(ctx, i18n.t("kp_tools.image.generate.failed", error=str(exc)))
        finally:
            release_imagegen_slot(self._services, ctx.chat_key)

    async def _image_notify(self, ctx: AgentCtx, text: str) -> None:
        """Surface an image-generation outcome (success/failure) to the room and retire
        the pending spinner line. No-op when the room has no hub (CLI standalone)."""
        if self._hub is None:
            return
        i18n = self._i18n(ctx)
        # Retire the "Generating…" line: publish the SAME text WITHOUT the spinner
        # flag, so the client replaces the pending entry in place.
        await self._hub.publish(
            ctx.chat_key,
            Event(
                kind="system",
                text=i18n.t("kp_tools.image.generate.generating"),
                data={"level": "info", "spinner": False},
            ),
        )
        await self._hub.publish(
            ctx.chat_key,
            Event(kind="system", text=text, data={"level": "info"}),
        )
