"""Audio and avatars: `.audio` / `.bgm` / `.ambience` / `.sfx` and `.avatar`, with their parsers."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any

from agent import npc as npc_records
from agent.history import DEFAULT_HISTORY_KEY, load_chain
from core.documents import MODULE_POOL_ID, PLAYER_VIEWER, DocumentStore
from core.prompt_sections import inject_game_state_prompt
from gateway.audio import add_audio_item, build_audio_control, list_audio_items, resolve_audio_item, update_audio_item
from gateway.avatar import AvatarError, set_target_avatar, set_user_avatar
from gateway.commands.rooms import _is_keeper
from gateway.commands.types import CommandCtx
from gateway.hub import Event
from gateway.imagegen import allow_imagegen_request, gather_image_reference, image_name, imagegen_failure_text
from gateway.media import media_frame, publish_media
from gateway.ops import (
    is_media_enabled,
)
from gateway.turn import publish_state
from infra.imagegen import ImageGenError
from infra.media_store import (
    ALLOWED_AUDIO_MIMES,
    ALLOWED_IMAGE_MIMES,
    MediaError,
    MediaStore,
)
from infra.model_call_trace import lane_scope

# `.audio` / `.bgm` / `.ambience` / `.sfx` subcommand vocabularies.
_AUDIO_LIST_WORDS = {"", "list", "ls", "show", "列表", "查看"}
_AUDIO_SET_WORDS = {"set", "meta", "metadata", "设置", "設置", "元数据", "元資料"}
_AUDIO_IMPORT_WORDS = {"import", "load", "导入", "導入"}
_AUDIO_PLAY_WORDS = {"play", "start", "播放", "开始", "開始"}
_AUDIO_STOP_WORDS = {"stop", "停止"}
_AUDIO_PAUSE_WORDS = {"pause", "暂停", "暫停"}
_AUDIO_RESUME_WORDS = {"resume", "继续", "繼續"}
_AUDIO_VOLUME_WORDS = {"volume", "vol", "音量"}
_AVATAR_GEN_WORDS = {"gen", "generate", "生成"}
_AVATAR_CLEAR_WORDS = {"clear", "remove", "rm", "清除", "删除", "刪除"}


def _shell_words(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


# Intents that ask for a depiction of the table's LIVE state, expanded via the LLM
# against the player-visible game-state panel. Anything else is a plain prompt.
_LIVE_STATE_INTENTS = frozenset(
    {
        "current scene",
        "scene",
        "场景",
        "当前场景",
        "現在場景",
        "current character",
        "character",
        "角色",
        "当前角色",
        "當前角色",
        "我",
        "self",
        "portrait of me",
        "combat",
        "battle",
        "战斗",
        "戰鬥",
        "当前战斗",
        "當前戰鬥",
        "current combat",
    }
)


def _is_live_state_intent(prompt: str) -> bool:
    """True when the trimmed description reads as a "depict the current X" intent.

    Uses a whole-token match on a bounded intent vocabulary (bilingual) so a normal
    descriptive sentence is never mistaken for an intent and routed to the LLM."""
    return prompt.casefold().strip() in _LIVE_STATE_INTENTS


# A bare `.image <kind>` word maps to the live-state intent the LLM expands against
# the room's player-visible state. `combat` is a scene-type image (same kind).
# `clue` depicts the room's unlocked clues/items — there is no separate "item"
# concept in the knowledge pool, so the noun list is the clues pool.
_KIND_INTENTS = {
    "scene": "当前场景",
    "portrait": "当前角色",
    "clue": "当前线索",
    "combat": "当前战斗",
}


async def _resolve_avatar_target(ctx: CommandCtx, target: str) -> Any | None:
    try:
        return await npc_records.get_npc(ctx.services.documents, ctx.chat_key, target)
    except Exception:
        return None


def _split_audio_metadata(tokens: list[str]) -> tuple[str, dict[str, Any]]:
    query_tokens: list[str] = []
    metadata: dict[str, Any] = {}
    seen_metadata = False
    for token in tokens:
        key, sep, value = token.partition("=")
        normalized = key.casefold().replace("-", "_")
        if sep and normalized in {"title", "license", "source", "tags"}:
            seen_metadata = True
            if normalized == "tags":
                metadata[normalized] = [item.strip() for item in value.split(",")]
            else:
                metadata[normalized] = value.strip()
        elif seen_metadata:
            continue
        else:
            query_tokens.append(token)
    return " ".join(query_tokens).strip(), metadata


def _split_audio_play(tokens: list[str], *, default_loop: bool) -> tuple[str, dict[str, Any]]:
    query_tokens: list[str] = []
    options: dict[str, Any] = {"loop": default_loop}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.casefold()
        if lowered in {"--loop", "loop"}:
            options["loop"] = True
            index += 1
            continue
        if lowered in {"--no-loop", "noloop", "once"}:
            options["loop"] = False
            index += 1
            continue
        if lowered in {"--volume", "volume", "vol"}:
            if index + 1 < len(tokens):
                volume = _parse_audio_volume([tokens[index + 1]])
                if volume is not None:
                    options["volume"] = volume
            index += 2
            continue
        if lowered.startswith("--volume=") or lowered.startswith("volume=") or lowered.startswith("vol="):
            volume = _parse_audio_volume([token.split("=", 1)[1]])
            if volume is not None:
                options["volume"] = volume
            index += 1
            continue
        if lowered in {"--fade", "fade", "fade_ms"}:
            if index + 1 < len(tokens):
                options["fade_ms"] = _parse_int(tokens[index + 1], default=0)
            index += 2
            continue
        if lowered.startswith("--fade=") or lowered.startswith("fade=") or lowered.startswith("fade_ms="):
            options["fade_ms"] = _parse_int(token.split("=", 1)[1], default=0)
            index += 1
            continue
        query_tokens.append(token)
        index += 1
    return " ".join(query_tokens).strip(), options


def _parse_audio_volume(tokens: list[str]) -> float | None:
    if not tokens:
        return None
    token = str(tokens[0]).strip().rstrip("%")
    try:
        value = float(token)
    except ValueError:
        return None
    if value > 1:
        value = value / 100.0
    return max(0.0, min(1.0, value))


def _parse_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _audio_item_label(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").strip()
    name = str(item.get("name") or item.get("hash") or "").strip()
    return title or name or str(item.get("hash", ""))[:12]


def _audio_item_line(item: dict[str, Any]) -> str:
    label = _audio_item_label(item)
    short_hash = str(item.get("hash") or "")[:12]
    details = [short_hash]
    if item.get("license"):
        details.append(str(item["license"]))
    if item.get("tags"):
        details.append(",".join(str(tag) for tag in item["tags"]))
    return f"{label} ({' · '.join(details)})"


def _audio_matches(matches: tuple[dict[str, Any], ...]) -> str:
    return ", ".join(_audio_item_label(item) for item in matches[:5])


class MediaCommands:
    """`CommandRouter` mixin — see the module docstring."""

    async def cmd_audio(self, ctx: CommandCtx) -> str:
        tokens = _shell_words(ctx.args)
        sub = tokens[0].casefold() if tokens else ""
        rest = tokens[1:] if tokens else []
        if sub in _AUDIO_LIST_WORDS:
            return await self._audio_list(ctx)
        if sub in _AUDIO_SET_WORDS:
            return await self._audio_set(ctx, rest)
        if sub in _AUDIO_IMPORT_WORDS:
            return await self._audio_import(ctx, rest)
        return ctx.i18n.t("commands.audio.usage")

    async def _audio_import(self, ctx: CommandCtx, rest: list[str]) -> str:
        """`.audio import <packId>` — register an installed pack's audio assets into THIS
        room's library. Packs ship soundtracks, but the room library only ever filled from
        uploads; this is the deliberate keeper lever that bridges the two (a pack install
        is host-wide, a room's soundscape is the keeper's call)."""
        if not _is_keeper(ctx.raw_ctx):
            return ctx.fail(ctx.i18n.t("rooms.denied"))
        pack_id = rest[0].strip() if rest else ""
        if not pack_id:
            return ctx.i18n.t("commands.audio.import_usage")
        import mimetypes

        from core.pack import MANIFEST_NAME, installed_pack_dir, parse_manifest_text

        pack_dir = installed_pack_dir(ctx.services.settings.data_dir, pack_id)
        if pack_dir is None:
            return ctx.i18n.t("commands.audio.import_missing", pack=pack_id)
        # Manifest titles/tags become the library metadata when present (the built
        # manifest ships with the install); a missing/unreadable manifest just
        # degrades to filename stems.
        titles: dict[str, tuple[str, tuple[str, ...]]] = {}
        try:
            manifest = parse_manifest_text((pack_dir / MANIFEST_NAME).read_text("utf-8"), expect_trust=True)
            for asset in manifest.assets:
                titles[asset.path] = (asset.title, asset.tags)
        except Exception:  # noqa: BLE001 — metadata is best-effort, import proceeds
            titles = {}
        tui_settings = ctx.services.settings.tui
        store = MediaStore(
            ctx.services.store,
            ctx.services.settings.data_dir,
            max_file_bytes=tui_settings.audio_max_file_bytes,
            room_quota_bytes=tui_settings.audio_room_quota_bytes,
            allowed_mimes=ALLOWED_AUDIO_MIMES,
        )
        imported: list[str] = []
        for path in sorted(pack_dir.rglob("*")):
            if len(imported) >= 24:
                break
            if not path.is_file():
                continue
            mime = mimetypes.guess_type(path.name)[0] or ""
            if mime not in ALLOWED_AUDIO_MIMES:
                continue
            rel = path.relative_to(pack_dir).as_posix()
            title, tags = titles.get(rel, ("", ()))
            display = title or path.stem
            try:
                record = await store.register_blob(
                    room=ctx.chat_key, data=path.read_bytes(), mime=mime, name=display, uploader=ctx.user_id
                )
            except (MediaError, OSError):
                continue
            await add_audio_item(ctx.services.store, ctx.chat_key, record, ctx.user_id)
            if title or tags:
                await update_audio_item(
                    ctx.services.store, ctx.chat_key, record.hash, {"title": display, "tags": list(tags)}
                )
            imported.append(display)
        if not imported:
            return ctx.i18n.t("commands.audio.import_none", pack=pack_id)
        return ctx.i18n.t(
            "commands.audio.import_done",
            pack=pack_id,
            count=len(imported),
            names=ctx.i18n.t("common.list_separator").join(imported),
        )

    async def cmd_bgm(self, ctx: CommandCtx) -> str:
        return await self._audio_layer(ctx, "bgm", default_loop=True)

    async def cmd_ambience(self, ctx: CommandCtx) -> str:
        return await self._audio_layer(ctx, "ambience", default_loop=True)

    async def cmd_sfx(self, ctx: CommandCtx) -> str:
        return await self._audio_layer(ctx, "sfx", default_loop=False)

    async def cmd_avatar(self, ctx: CommandCtx) -> str:
        tokens = _shell_words(ctx.args)
        sub = tokens[0].casefold() if tokens else ""
        rest = tokens[1:] if tokens else []
        if sub in _AVATAR_CLEAR_WORDS:
            return await self._avatar_clear(ctx)
        if sub in _AVATAR_GEN_WORDS:
            return await self._avatar_generate(ctx, rest)
        return ctx.i18n.t("commands.avatar.usage")

    async def cmd_image(self, ctx: CommandCtx) -> str:
        """`.image [scene|portrait|clue|combat] [description]` — Keeper-only: generate a
        player-visible image handout and publish it to the room media stream, WITHOUT
        attaching it to any character's avatar slot. Unlike `.avatar`, the image is
        pure room content (a scene, a clue/item, a portrait) with no target sheet.

        A bare kind word (`.image scene` / `.image portrait` / `.image clue` /
        `.image combat`) or a live-state phrase (`当前场景`, `当前角色`, `战斗`, `self`)
        is expanded by the LLM against the room's player-visible game state (scene,
        roster, clues, combat, world changes — a keeper-secret-free projection) into a
        concrete image prompt; an optional extra description is folded into that intent
        (`.image scene 迷雾中的灯塔`). Any other description is sent to the image
        provider verbatim."""
        tokens = _shell_words(ctx.args)
        if not tokens:
            return ctx.i18n.t("commands.image.usage")
        # `.image last` — use the Keeper's most recent narration text as the subject.
        if tokens[0].casefold() in {"last", "recent", "from", "上一条", "最近"}:
            text = await self._last_keeper_text(ctx)
            if not text:
                return ctx.i18n.t("commands.image.no_recent_text")
            # Keep the optional kind word after `.image last <kind>` for the category.
            kind = "scene"
            if len(tokens) >= 2 and tokens[1].casefold() in {"scene", "portrait", "clue", "combat"}:
                kind = tokens[1].casefold()
            return await self._generate_image(ctx, kind, text, intent=text, story_mode=True)
        kind = "scene"
        kind_word = False
        if tokens[0].casefold() in {"scene", "portrait", "clue", "combat"}:
            kind = tokens[0].casefold()
            kind_word = True
            tokens = tokens[1:]
        extra = " ".join(tokens).strip()
        # LLM-assisted expansion happens when the request names the table's live
        # state: a bare kind word (`.image scene`), a live-state phrase
        # (`当前场景` / `战斗` / …), or a kind word with extra detail
        # (`.image scene 迷雾中的灯塔`). Anything else is a plain prompt.
        if kind_word:
            intent = _KIND_INTENTS.get(kind, "")
            prompt = f"{intent} {extra}".strip() if extra else intent
        elif _is_live_state_intent(extra):
            intent = extra
            prompt = extra
        else:
            intent = ""
            prompt = extra
        if not prompt:
            return ctx.i18n.t("commands.image.usage")
        # For `.image clue <name>` the extra detail is the clue FOCUS — the material
        # gatherer keeps only scenes/clues mentioning it.
        focus = extra if kind == "clue" and extra else ""
        return await self._generate_image(ctx, kind, prompt, intent=intent, focus=focus)

    async def _generate_image(
        self, ctx: CommandCtx, kind: str, prompt: str, *, intent: str, focus: str = "", story_mode: bool = False
    ) -> str:
        """Shared image-handout generation: expand a live-state intent via the LLM
        (when the prompt signals one), generate, and publish to the room media stream."""
        if not await is_media_enabled(ctx.services.store, ctx.chat_key):
            return ctx.i18n.t("commands.avatar.media_disabled")
        imagegen = await ctx.services.imagegen_for_room(ctx.chat_key)
        if imagegen is None:
            return ctx.i18n.t("commands.avatar.not_configured")
        if not allow_imagegen_request(ctx.services, ctx.chat_key):
            return ctx.i18n.t("commands.avatar.rate_limited")

        if ctx.router.hub is not None:
            await ctx.router.hub.publish(
                ctx.chat_key,
                Event(kind="system", text=ctx.i18n.t("commands.image.generating"), data={"level": "info", "spinner": True}),
            )
        # Image generation is minutes-long (LLM prompt expansion + a provider I2I/T2I
        # call). Running it inline holds the room's turn lock for the whole stretch and
        # queues every other input behind it; hand the slow half to a background task
        # instead — the command returns immediately and the finished image lands in the
        # room as a media frame + system message. The imagegen client and the checks
        # above are resolved up front so the task can never hit a misconfigured room.
        tasks = getattr(self, "_image_background_tasks", None)
        if tasks is None:
            tasks = set()
            self._image_background_tasks = tasks
        task = asyncio.create_task(
            self._generate_image_background(
                ctx, kind, prompt, imagegen, intent=intent, focus=focus, story_mode=story_mode
            )
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return ctx.i18n.t("commands.image.started")

    async def _generate_image_background(
        self,
        ctx: CommandCtx,
        kind: str,
        prompt: str,
        imagegen: Any,
        *,
        intent: str,
        focus: str = "",
        story_mode: bool = False,
    ) -> None:
        """The slow half of `.image` — prompt expansion, reference gathering, provider
        generation, storage and media publication — run OUTSIDE the room's turn lock so
        a minutes-long request never blocks the table. Success and failure both surface
        as room system messages (the spinner line doubles as the pending placeholder)."""
        try:
            prompt = await self._expand_image_prompt(ctx, kind, prompt, force=bool(intent), focus=focus, story_mode=story_mode)
            # Reuse the module's illustration of this subject as a reference so the new
            # image stays consistent with what the players already saw. Best-effort:
            # a missing reference is a prompt-only generation, never an error.
            ref_bytes, ref_mime = await gather_image_reference(ctx.services, ctx.chat_key, kind, imagegen, focus=focus, extra=prompt)
            # A scene/clue reference must only guide style and atmosphere — the image
            # provider may otherwise extract a person/face present in the reference as
            # the subject. Portrait references WANT that (character consistency), so the
            # hint is skipped there.
            display_prompt = prompt
            if ref_bytes and kind != "portrait":
                hint = ctx.services.i18n.with_locale(ctx.locale).t("commands.image.reference_hint")
                prompt = f"{prompt} {hint}".strip()
            data, mime = await imagegen.generate(
                prompt,
                size=ctx.services.settings.imagegen.size,
                reference=ref_bytes,
                reference_mime=ref_mime,
            )
            record = await self._store_image_blob(ctx, data, mime, image_name(kind, display_prompt))
            await publish_media(
                ctx.router.hub,
                ctx.services.store,
                ctx.chat_key,
                media_frame(record, from_name="KP", prompt=display_prompt),
            )
            # Retire the "Generating…" line: publish the SAME text WITHOUT the
            # spinner flag, so the client replaces the pending entry in place.
            if ctx.router.hub is not None:
                await ctx.router.hub.publish(
                    ctx.chat_key,
                    Event(
                        kind="system",
                        text=ctx.i18n.t("commands.image.generating"),
                        data={"level": "info", "spinner": False},
                    ),
                )
                await ctx.router.hub.publish(
                    ctx.chat_key,
                    Event(
                        kind="system",
                        text=ctx.i18n.t(
                            "commands.image.generated", kind=kind, file=record.name, hash=record.hash[:12]
                        ),
                        data={"level": "info"},
                    ),
                )
        except ImageGenError as exc:
            await self._image_notify(ctx, imagegen_failure_text(ctx.i18n, exc, key_prefix="commands.avatar.error", chat_key=ctx.chat_key))
        except Exception as exc:
            await self._image_notify(ctx, ctx.i18n.t("commands.image.failed", error=str(exc)))

    async def _image_notify(self, ctx: CommandCtx, text: str) -> None:
        """Surface an image-generation outcome (success/failure) to the room and retire
        the pending spinner line. No-op when the room has no hub (CLI standalone)."""
        if ctx.router.hub is None:
            return
        await ctx.router.hub.publish(
            ctx.chat_key,
            Event(kind="system", text=ctx.i18n.t("commands.image.generating"), data={"level": "info", "spinner": False}),
        )
        await ctx.router.hub.publish(
            ctx.chat_key,
            Event(kind="system", text=text, data={"level": "info"}),
        )

    async def _expand_image_prompt(self, ctx: CommandCtx, kind: str, prompt: str, *, force: bool = False, focus: str = "", story_mode: bool = False) -> str:
        """Expand a "generate the current X" request into a concrete image prompt.

        With `force` (a bare kind word or a live-state phrase routed by `cmd_image`), an
        AUTHORING-lane LLM call turns the room's PLAYER-VISIBLE game state into a single
        detailed image prompt. The state is assembled from every keeper-secret-free
        source the room has (the battle-status panel, the module knowledge pool's
        player views of scenes/NPCs/items, non-secret worldbook entries, and the tail
        of the conversation) so the picture reflects what is actually happening.
        Otherwise the prompt is returned unchanged so the command stays a plain
        prompt-to-image pass-through."""
        if not force and not _is_live_state_intent(prompt):
            return prompt
        # The image prompt must match the room's material language (Chinese for a
        # Chinese module), NOT the command session's locale — a Chinese table on an
        # English UI still wants Chinese prompts, and the material is Chinese.
        prompt_i18n = ctx.services.i18n.with_locale("zh")
        state = await inject_game_state_prompt(ctx.raw_ctx, ctx.services.characters, ctx.services.store, prompt_i18n)
        material = await self._gather_scene_material(ctx, kind, focus=focus, story_mode=story_mode)
        if not state.strip() and not material.strip():
            return prompt
        messages = [
            {
                "role": "system",
                "content": prompt_i18n.t("commands.image.expand_system"),
            },
            {
                "role": "user",
                "content": prompt_i18n.t(
                    "commands.image.expand_user",
                    kind=kind,
                    intent=prompt,
                    state=state,
                    material=material,
                ),
            },
        ]
        try:
            with lane_scope("authoring", chat_key=ctx.chat_key):
                llm = await ctx.services.main_llm(ctx.chat_key)
                result = await llm.chat(messages)
            expanded = (getattr(result, "content", "") or "").strip()
            if expanded:
                _log_image_prompt(ctx, kind, prompt, expanded, ok=True)
            return expanded if expanded else prompt
        except Exception as exc:  # noqa: BLE001 — image generation must never break a turn
            _log_image_prompt(ctx, kind, prompt, "", ok=False, error=str(exc))
            return prompt

    async def _last_keeper_text(self, ctx: CommandCtx) -> str:
        """The Keeper's most recent narration text from the room's history.

        The AI Keeper's replies are persisted as `role="assistant"` records; the
        last non-empty one is the "current narration" `.image last` should depict.
        Best-effort: returns "" when there is none."""
        try:
            chain = await load_chain(ctx.services, ctx.chat_key, DEFAULT_HISTORY_KEY)
            for message in reversed(chain):
                if str(message.get("role", "")) == "assistant":
                    text = str(message.get("content") or "").strip()
                    if text:
                        return text
        except Exception:
            pass
        return ""

    async def _gather_scene_material(self, ctx: CommandCtx, kind: str, focus: str = "", story_mode: bool = False) -> str:
        """Assemble the room's player-visible visual material for the current moment.

        Aggregates, keeper-secret-free (iron rule #3), everything a prompt authoring
        model can draw on to depict the scene/character/combat: the module knowledge
        pool's player views of scenes (matched to the current scene when possible),
        NPCs and items, non-secret worldbook entries, and the tail of the table's
        conversation so the picture tracks where the story actually is. Each source is
        independently guarded and skipped on failure — generation must never depend on
        one lookup."""
        chat_key = ctx.chat_key
        docs = DocumentStore(ctx.services.store)
        lines: list[str] = []
        seen = set()

        # Current scene name (for matching pool scenes).
        current_scene = ""
        try:
            scene_doc = await docs.get_view(chat_key, "scene", "scene", PLAYER_VIEWER)
            if scene_doc:
                current_scene = str(scene_doc.get("name") or "")
        except Exception:
            pass

        # Module knowledge pool — player views only.
        try:
            pool = await docs.get_view(chat_key, "module_pool", MODULE_POOL_ID, PLAYER_VIEWER)
            if pool:
                if kind == "portrait":
                    npcs = pool.get("npcs") or []
                    if npcs:
                        lines.append("## Characters present (player-visible)")
                        for n in npcs[:5]:
                            name = n.get("name", "")
                            desc = str(n.get("description") or "").strip()
                            if name and desc and name not in seen:
                                seen.add(name)
                                lines.append(f"- {name}：{desc}")
                elif kind == "clue":
                    # A clue picture depicts an actual unlocked clue from the clues
                    # pool — NOT ordinary objects in scene descriptions. With a
                    # `focus` (`.image clue 玉蟾`) only matching clues are kept.
                    clues = pool.get("clues") or []
                    if clues:
                        focused = [
                            c for c in clues
                            if not focus or focus in str(c.get("name") or "") or focus in str(c.get("description") or "")
                        ]
                        if focused:
                            lines.append("## Clues in play (player-visible)")
                            for c in focused[:6]:
                                cname = str(c.get("name") or "").strip()
                                cdesc = str(c.get("description") or "").strip()
                                if cname and cdesc and cname not in seen:
                                    seen.add(cname)
                                    lines.append(f"- {cname}：{cdesc}")
                else:
                    scenes = pool.get("scenes") or []

                    def _scene_line(s: dict) -> str | None:
                        name = str(s.get("name") or "").strip()
                        desc = str(s.get("description") or "").strip()
                        if not name or not desc or name in seen:
                            return None
                        seen.add(name)
                        return f"- {name}：{desc}"

                    selected = [s for s in scenes if current_scene and str(s.get("name") or "") == current_scene] or scenes
                    scene_lines = [ln for ln in (_scene_line(s) for s in selected) if ln]
                    if scene_lines:
                        lines.append("## Scenes (player-visible)")
                        lines.extend(scene_lines)
                    npcs = pool.get("npcs") or []
                    if npcs:
                        lines.append("## Character appearances (player-visible)")
                        for n in npcs[:5]:
                            name = n.get("name", "")
                            desc = str(n.get("description") or "").strip()
                            if name and desc and name not in seen:
                                seen.add(name)
                                lines.append(f"- {name}：{desc}")
                    bg = str(pool.get("background") or "").strip()
                    if bg:
                        lines.append(f"## Background\n{bg[:400]}")
        except Exception:
            pass

        # Worldbook — non-secret entries only (player-visible).
        try:
            entries = await ctx.services.worldbook.list(chat_key)
            visible = [
                e for e in entries
                if not getattr(e, "secret", False) and str(getattr(e, "content", "") or "").strip()
            ]
            if visible:
                lines.append("## World setting (player-visible)")
                for e in visible[:8]:
                    content = str(getattr(e, "content", "") or "").strip()[:200]
                    if content:
                        lines.append(f"- {content}")
        except Exception:
            pass

        # Tail of the conversation — where the story currently is. In story mode
        # (`.image last`) this is the PRIMARY material: take more of it and put it
        # first, so the picture reflects the latest narration over static scene data.
        try:
            chain = await load_chain(ctx.services, chat_key, DEFAULT_HISTORY_KEY)
            tail = [str(m.get("content") or "").strip() for m in chain[-10:] if str(m.get("content") or "").strip()]
            story = ""
            if tail:
                cap = 2000 if story_mode else 800
                story = "\n".join(tail)[-cap:]
            if story:
                if story_mode:
                    # Keep only the most relevant scene context in story mode.
                    lines = [ln for ln in lines if ln.startswith("## Recent story") or ln.startswith("- ")]
                lines.append("## Recent story")
                lines.append(story)
        except Exception:
            pass

        if story_mode:
            # Story first: move the recent-narration block ahead of static material.
            story_blocks = [ln for ln in lines if ln.startswith("## Recent story")]
            if story_blocks:
                rest = [ln for ln in lines if not ln.startswith("## Recent story")]
                lines = story_blocks + rest

        return "\n".join(lines)


    async def _store_image_blob(
        self, ctx: CommandCtx, data: bytes, mime: str, name: str
    ) -> Any:
        """Persist a generated image blob into the room's media store."""
        settings = ctx.services.settings.tui
        store = MediaStore(
            ctx.services.store,
            ctx.services.settings.data_dir,
            max_file_bytes=settings.media_max_file_bytes,
            room_quota_bytes=settings.media_room_quota_bytes,
            allowed_mimes=ALLOWED_IMAGE_MIMES,
        )
        return await store.register_blob(
            room=ctx.chat_key,
            data=data,
            mime=mime,
            name=name,
            uploader=ctx.user_id,
        )

    async def _avatar_clear(self, ctx: CommandCtx) -> str:
        try:
            sheet = await set_user_avatar(ctx.services, user_id=ctx.user_id, chat_key=ctx.chat_key, avatar=None)
        except AvatarError as exc:
            return ctx.i18n.t(f"commands.avatar.error.{exc.code}")
        if ctx.router.hub is not None:
            await publish_state(ctx.router.hub, ctx.services, ctx.raw_ctx)
        return ctx.i18n.t("commands.avatar.cleared", name=sheet.name)

    async def _avatar_generate(self, ctx: CommandCtx, tokens: list[str]) -> str:
        if not tokens:
            return ctx.i18n.t("commands.avatar.usage")
        if not await is_media_enabled(ctx.services.store, ctx.chat_key):
            return ctx.i18n.t("commands.avatar.media_disabled")
        imagegen = await ctx.services.imagegen_for_room(ctx.chat_key)
        if imagegen is None:
            return ctx.i18n.t("commands.avatar.not_configured")

        # Resolve the target and enforce the keeper gate BEFORE consuming the shared
        # rate-limit token: a target-avatar request from a non-keeper must be denied
        # without burning the room's imagegen quota (otherwise a player could exhaust
        # it with requests that are rejected anyway).
        target_name = ""
        prompt_tokens = tokens
        if len(tokens) >= 2:
            maybe_target = tokens[0]
            target_record = await _resolve_avatar_target(ctx, maybe_target)
            if target_record is not None:
                if not _is_keeper(ctx.raw_ctx):
                    return ctx.fail(ctx.i18n.t("commands.avatar.denied"))
                target_name = maybe_target
                prompt_tokens = tokens[1:]
        prompt = " ".join(prompt_tokens).strip()
        if not prompt:
            return ctx.i18n.t("commands.avatar.usage")

        if not allow_imagegen_request(ctx.services, ctx.chat_key):
            return ctx.i18n.t("commands.avatar.rate_limited")

        if ctx.router.hub is not None:
            await ctx.router.hub.publish(
                ctx.chat_key,
                Event(kind="system", text=ctx.i18n.t("commands.avatar.generating"), data={"level": "info", "spinner": True}),
            )
        try:
            data, mime = await imagegen.generate(prompt, size=ctx.services.settings.imagegen.size)
            settings = ctx.services.settings.tui
            store = MediaStore(
                ctx.services.store,
                ctx.services.settings.data_dir,
                max_file_bytes=settings.media_max_file_bytes,
                room_quota_bytes=settings.media_room_quota_bytes,
                allowed_mimes=ALLOWED_IMAGE_MIMES,
            )
            record = await store.register_blob(
                room=ctx.chat_key,
                data=data,
                mime=mime,
                name=image_name("avatar", prompt),
                uploader=ctx.user_id,
            )
            if target_name:
                sheet = await set_target_avatar(ctx.services, chat_key=ctx.chat_key, target=target_name, avatar=record.ref())
            else:
                sheet = await set_user_avatar(ctx.services, user_id=ctx.user_id, chat_key=ctx.chat_key, avatar=record.ref())
            await publish_media(ctx.router.hub, ctx.services.store, ctx.chat_key, media_frame(record, from_name=sheet.name))
            if ctx.router.hub is not None:
                await publish_state(ctx.router.hub, ctx.services, ctx.raw_ctx)
            return ctx.i18n.t("commands.avatar.generated", name=sheet.name, file=record.name, hash=record.hash[:12])
        except AvatarError as exc:
            return ctx.i18n.t(f"commands.avatar.error.{exc.code}")
        except ImageGenError as exc:
            return imagegen_failure_text(ctx.i18n, exc, key_prefix="commands.avatar.error", chat_key=ctx.chat_key)
        except Exception as exc:
            return ctx.i18n.t("commands.avatar.failed", error=str(exc))

    async def _audio_list(self, ctx: CommandCtx) -> str:
        items = await list_audio_items(ctx.services.store, ctx.chat_key)
        if not items:
            return ctx.i18n.t("commands.audio.empty")
        lines = [_audio_item_line(item) for item in items[-25:]]
        return ctx.i18n.t("commands.audio.list", items="\n".join(lines))

    async def _audio_set(self, ctx: CommandCtx, tokens: list[str]) -> str:
        query, metadata = _split_audio_metadata(tokens)
        if not query or not metadata:
            return ctx.i18n.t("commands.audio.set_usage")
        resolved = await update_audio_item(ctx.services.store, ctx.chat_key, query, metadata)
        if resolved.status == "not_found":
            return ctx.i18n.t("commands.audio.not_found", query=query)
        if resolved.status == "ambiguous":
            return ctx.i18n.t("commands.audio.ambiguous", matches=_audio_matches(resolved.matches))
        assert resolved.item is not None
        await self._publish_audio(ctx, resolved.item)
        return ctx.i18n.t("commands.audio.updated", item=_audio_item_label(resolved.item))

    async def _audio_layer(self, ctx: CommandCtx, layer: str, *, default_loop: bool) -> str:
        tokens = _shell_words(ctx.args)
        if not tokens:
            return ctx.i18n.t(f"commands.audio.{layer}.usage")

        sub = tokens[0].casefold()
        if sub in _AUDIO_STOP_WORDS:
            return await self._audio_control(ctx, layer, "stop")
        if sub in _AUDIO_PAUSE_WORDS:
            return await self._audio_control(ctx, layer, "pause")
        if sub in _AUDIO_RESUME_WORDS:
            return await self._audio_control(ctx, layer, "resume")
        if sub in _AUDIO_VOLUME_WORDS:
            volume = _parse_audio_volume(tokens[1:])
            if volume is None:
                return ctx.i18n.t("commands.audio.volume_usage")
            return await self._audio_control(ctx, layer, "volume", volume=volume)

        play_tokens = tokens[1:] if sub in _AUDIO_PLAY_WORDS else tokens
        query, options = _split_audio_play(play_tokens, default_loop=default_loop)
        if not query:
            return ctx.i18n.t(f"commands.audio.{layer}.usage")
        resolved = await resolve_audio_item(ctx.services.store, ctx.chat_key, query)
        if resolved.status == "not_found":
            return ctx.i18n.t("commands.audio.not_found", query=query)
        if resolved.status == "ambiguous":
            return ctx.i18n.t("commands.audio.ambiguous", matches=_audio_matches(resolved.matches))
        assert resolved.item is not None
        return await self._audio_control(
            ctx,
            layer,
            "play",
            item=resolved.item,
            volume=options.get("volume"),
            loop=bool(options.get("loop")),
            fade_ms=options.get("fade_ms"),
        )

    async def _audio_control(
        self,
        ctx: CommandCtx,
        layer: str,
        action: str,
        *,
        item: dict[str, Any] | None = None,
        volume: float | None = None,
        loop: bool | None = None,
        fade_ms: int | None = None,
    ) -> str:
        control, state = await build_audio_control(
            ctx.services.store,
            ctx.chat_key,
            layer=layer,
            action=action,
            item=item,
            volume=volume,
            loop=loop,
            fade_ms=fade_ms,
        )
        await self._publish_audio(ctx, control)
        if state is not None:
            await self._publish_audio(ctx, state)
        if action == "play" and item is not None:
            return ctx.i18n.t("commands.audio.played", layer=ctx.i18n.t(f"commands.audio.layer.{layer}"), item=_audio_item_label(item))
        if action == "volume":
            return ctx.i18n.t("commands.audio.volume_done", layer=ctx.i18n.t(f"commands.audio.layer.{layer}"), volume=f"{(volume or 0) * 100:.0f}%")
        return ctx.i18n.t("commands.audio.control_done", layer=ctx.i18n.t(f"commands.audio.layer.{layer}"), action=ctx.i18n.t(f"commands.audio.action.{action}"))

    async def _publish_audio(self, ctx: CommandCtx, frame: dict[str, Any]) -> None:
        if ctx.router.hub is not None:
            await ctx.router.hub.publish(ctx.chat_key, Event.audio(frame))


def _log_image_prompt(
    ctx: CommandCtx, kind: str, intent: str, expanded: str, *, ok: bool, error: str = ""
) -> None:
    """Append one image-prompt audit line to `<data_dir>/image_prompts.log`.

    Records what was actually sent to the image provider (the LLM-expanded prompt,
    or the raw fallback), so a keeper can inspect why a generated picture looks the
    way it does. Best-effort: a write failure must never break generation."""
    try:
        from datetime import datetime

        path = Path(ctx.services.settings.data_dir) / "image_prompts.log"
        lines = [
            f"[{datetime.now().isoformat(timespec='seconds')}] kind={kind} ok={ok}",
            f"  intent: {intent}",
        ]
        if ok:
            lines.append(f"  prompt: {expanded}")
        else:
            lines.append(f"  error: {error}")
            lines.append(f"  fallback_prompt: {intent}")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001 — audit logging never breaks generation
        pass
