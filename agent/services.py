"""Service bundle wiring — the single integration point that assembles the
deterministic core + infra services the AI-KP tools and loop depend on.

`Services` is a plain container; `build_services()` constructs the real graph
(injectable `llm`/`embeddings` so tests can pass FakeLLM/FakeEmbeddings and run
fully offline)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.context import AgentCtx
from agent.document_manager import VectorDatabaseManager
from agent.module_initializer import ModuleInitializer
from agent.tool_trace import enable_tool_trace, persisted_trace_paths_sync
from core.battle_report import BattleReportManager
from core.character_manager import CharacterManager, has_character
from core.dice_engine import DiceRoller
from core.dice_engine import config as dice_config
from core.documents import DocumentStore
from core.rulepacks import RulePack, load_rulepack
from core.worldbook import Worldbook
from infra.config import Settings, get_settings
from infra.embeddings import Embeddings, FakeEmbeddings, OpenAIEmbeddings
from infra.i18n import I18n, get_i18n
from infra.imagegen import ImageGen, apply_imagegen_overrides, build_imagegen
from infra.llm import LLMClient
from infra.providers import MutableLLM, build_llm
from infra.room_facets import STORAGE_ROOM_STATE, RoomStateFacet
from infra.runtime_config import (
    DIRECTOR_RUNTIME_KEY,
    LLM_PROFILES_KEY,
    SCRIBE_RUNTIME_KEY,
    CredentialBook,
    ImageGenCredentialBook,
    ImageGenRuntimeConfig,
    LaneRuntimeConfig,
    RuntimeConfig,
    apply_lane_overrides,
    apply_overrides,
    migrate_llm_credentials,
    model_profile_parts,
)
from infra.store import Store
from infra.vector import VectorStore

logger = logging.getLogger(__name__)
ROOM_LLM_SELECTION_KEY = "llm_selection"
ROOM_MODEL_DEFAULTS: dict[str, Any] = {
    "main": "",
    "scribe": "",
    "director": "",
    "imagegen": "",
    "scribe_enabled": True,
    "director_enabled": True,
}


@dataclass
class Services:
    """Everything a KP turn needs. Room/user scope comes from the AgentCtx, not here."""

    settings: Settings
    store: Store
    documents: DocumentStore
    i18n: I18n
    dice: DiceRoller
    characters: CharacterManager
    battles: BattleReportManager
    vector_db: VectorDatabaseManager
    module_init: ModuleInitializer
    worldbook: Worldbook
    llm: LLMClient
    imagegen: ImageGen | None
    embeddings: Embeddings
    runtime_config: RuntimeConfig
    llm_profiles: CredentialBook
    imagegen_runtime_config: ImageGenRuntimeConfig
    imagegen_credentials: ImageGenCredentialBook
    scribe_runtime_config: LaneRuntimeConfig
    director_runtime_config: LaneRuntimeConfig
    # Environment/settings baselines let the web admin clear a runtime lane
    # override without losing deployment-provided values.
    base_scribe_settings: Any = field(repr=False)
    base_director_settings: Any = field(repr=False)
    base_embedding_settings: Any = field(repr=False)
    base_embeddings: Embeddings = field(repr=False)
    embedding_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # One deployment-wide mutation lock shared by TUI admin frames, chat `.model`
    # commands, and subscription refresh publication. Room turn locks remain separate.
    config_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _room_llm_cache: dict[str, LLMClient] = field(default_factory=dict, init=False, repr=False)
    _room_imagegen_cache: dict[str, ImageGen] = field(default_factory=dict, init=False, repr=False)

    async def room_model_selection(self, chat_key: str | None) -> dict[str, Any]:
        """Return this room's validated-shape model assignment record."""
        selection = dict(ROOM_MODEL_DEFAULTS)
        # Knowledge-scoped actors may be exercised outside a live room (for
        # example authoring previews and unit tests).  With no room identity
        # there cannot be a room override, so inherit the deployment client.
        if not chat_key:
            return selection
        # Keeper admin frames are addressed by the human room name (for example
        # ``table``), while live TUI turns carry the canonical session key
        # (``tui:group:table``). Read either spelling so an assignment made in
        # the web model screen is also used by authoring, turns, and image lanes.
        # The canonical spelling MUST win when both rows exist: it is the only
        # spelling the current engine writes (`_set_room_model` persists via
        # `session_key_for_room`), so a bare-name row can only be a leftover
        # written by an older version. Preferring the bare row would shadow a
        # newer save and make the model screen read back a stale assignment —
        # the "pick another model, save, and it snaps back" bug.
        if chat_key.startswith("tui:group:"):
            raw = await self.store.state_get(chat_key, ROOM_LLM_SELECTION_KEY)
            if not raw:
                raw = await self.store.state_get(
                    chat_key.removeprefix("tui:group:"), ROOM_LLM_SELECTION_KEY
                )
        else:
            raw = await self.store.state_get(f"tui:group:{chat_key}", ROOM_LLM_SELECTION_KEY)
            if not raw:
                raw = await self.store.state_get(chat_key, ROOM_LLM_SELECTION_KEY)
        if not raw:
            return selection
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return selection
        if not isinstance(decoded, dict):
            return selection
        for key, default in ROOM_MODEL_DEFAULTS.items():
            if key not in decoded:
                continue
            selection[key] = bool(decoded[key]) if isinstance(default, bool) else str(decoded[key] or "")
        return selection

    async def clear_room_model_profile(self, profile_id: str) -> list[str]:
        """Clear every room's model-selection lane that references `profile_id`.

        Called when a profile is deleted so no room is left holding a dangling
        reference (which would otherwise silently fall back to the global
        default). Returns the chat keys of the rooms whose selection changed.
        """
        if not profile_id:
            return []
        affected: list[str] = []
        for room in await self.store.state_rooms():
            raw = await self.store.state_get(room, ROOM_LLM_SELECTION_KEY)
            if not raw:
                continue
            try:
                selection = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(selection, dict):
                continue
            changed = False
            for lane in ROOM_MODEL_DEFAULTS:
                if str(selection.get(lane) or "") == profile_id:
                    selection[lane] = ""
                    changed = True
            if changed:
                await self.store.state_set(room, ROOM_LLM_SELECTION_KEY, json.dumps(selection))
                affected.append(room)
        return affected

    async def room_lane_enabled(self, chat_key: str, lane: str) -> bool:
        """Whether a room may run its Scribe or Director lane."""
        if lane not in {"scribe", "director"}:
            raise ValueError("lane")
        globally_enabled = bool(getattr(self.settings, lane).enabled)
        selection = await self.room_model_selection(chat_key)
        return globally_enabled and bool(selection[f"{lane}_enabled"])

    async def room_llm(self, chat_key: str | None, lane: str = "main") -> LLMClient | None:
        """Build the chat profile assigned to one room lane, or ``None`` to inherit."""
        if lane not in {"main", "scribe", "director"}:
            raise ValueError("lane")
        selection = await self.room_model_selection(chat_key)
        profile_id = str(selection[lane] or "")
        if not profile_id:
            return None
        cached = self._room_llm_cache.get(profile_id)
        if cached is not None:
            return cached
        saved = await self.llm_profiles.get(profile_id)
        provider, kind, encoded_model = model_profile_parts(profile_id)
        if not saved or kind != "chat":
            logger.warning(
                "room %r lane %r references LLM profile %r which no longer exists; "
                "falling back to the global default",
                chat_key,
                lane,
                profile_id,
            )
            return None
        model = str(saved.get("chat_model") or encoded_model).strip()
        if not provider or not model:
            return None
        patched = self.settings.model_copy(deep=True)
        patched.llm.provider = provider
        patched.llm.chat_model = model
        patched.llm.api_key = saved.get("api_key") or ""
        patched.llm.base_url = saved.get("base_url") or ""
        client = build_llm(patched, credentials=self.llm_profiles)
        self._room_llm_cache[profile_id] = client
        return client

    async def main_llm(self, chat_key: str | None) -> LLMClient:
        """The room's primary model, falling back to the live global client."""
        return await self.room_llm(chat_key, "main") or self.llm

    async def room_llm_model(self, chat_key: str | None, lane: str = "main") -> str:
        """Return the selected room model name, or the deployment default."""
        if lane not in {"main", "scribe", "director"}:
            raise ValueError("lane")
        selection = await self.room_model_selection(chat_key)
        profile_id = str(selection[lane] or "")
        if profile_id:
            saved = await self.llm_profiles.get(profile_id)
            provider, kind, encoded_model = model_profile_parts(profile_id)
            if not saved or kind != "chat" or not provider:
                logger.warning(
                    "room %r lane %r references LLM profile %r which no longer exists; "
                    "reporting the global default",
                    chat_key,
                    lane,
                    profile_id,
                )
            elif saved and kind == "chat" and provider:
                model = str(saved.get("chat_model") or encoded_model).strip()
                if model:
                    return model
        if lane == "main":
            return self.settings.llm.analysis_model or self.settings.llm.chat_model
        lane_settings = getattr(self.settings, lane)
        return str(lane_settings.chat_model or self.settings.llm.chat_model)

    async def imagegen_for_room(self, chat_key: str) -> ImageGen | None:
        """The room's selected image profile, falling back to the global generator."""
        selection = await self.room_model_selection(chat_key)
        profile_id = str(selection["imagegen"] or "")
        if not profile_id:
            return self.imagegen
        cached = self._room_imagegen_cache.get(profile_id)
        if cached is not None:
            return cached
        saved = await self.llm_profiles.get(profile_id)
        provider, kind, encoded_model = model_profile_parts(profile_id)
        if not saved or kind != "image":
            logger.warning(
                "room %r image lane references imagegen profile %r which no longer "
                "exists; falling back to the global generator",
                chat_key,
                profile_id,
            )
            return self.imagegen
        model = str(saved.get("chat_model") or encoded_model).strip()
        if not provider or not model:
            return self.imagegen
        patched = self.settings.model_copy(deep=True)
        patched.imagegen.provider = provider
        patched.imagegen.model = model
        patched.imagegen.api_key = saved.get("api_key") or ""
        patched.imagegen.base_url = saved.get("base_url") or ""
        client = build_imagegen(patched, credentials=self.llm_profiles)
        if client is None:
            return self.imagegen
        self._room_imagegen_cache[profile_id] = client
        return client

    def invalidate_model_profile(self, profile_id: str | None = None) -> None:
        """Drop cached clients after an admin mutates one or all profiles."""
        if profile_id is None:
            self._room_llm_cache.clear()
            self._room_imagegen_cache.clear()
            return
        self._room_llm_cache.pop(profile_id, None)
        self._room_imagegen_cache.pop(profile_id, None)

    async def reconfigure_embeddings(
        self,
        candidate: Embeddings,
        *,
        profile_id: str,
        model: str,
    ) -> int:
        """Probe a new client, rebuild every recoverable vector, then swap atomically."""
        probe = await candidate.embed(["Loreweaver embedding compatibility probe"])
        if len(probe) != 1 or len(probe[0]) != candidate.dim:
            actual = len(probe[0]) if len(probe) == 1 else 0
            raise ValueError(f"embedding probe returned dimension {actual}, expected {candidate.dim}")

        async with self.embedding_lock:
            current_points = await self.vector_db.vector_store.dump()
            rebuildable: list[tuple[str, str, dict[str, Any]]] = []
            skipped = 0
            for point in current_points:
                payload = dict(point["payload"])
                text = str(payload.get("text") or "")
                if not text:
                    collection = str(payload.get("collection") or "")
                    room = str(payload.get("namespace") or payload.get("chat_key") or "")
                    entry_id = str(payload.get("entry_id") or "")
                    doc_type = "lore" if collection == "worldbook" else "chronicle" if collection == "chronicle" else ""
                    document = await self.documents.get(room, doc_type, entry_id) if room and doc_type and entry_id else None
                    text = str(document.data.get("content" if doc_type == "lore" else "text", "")) if document else ""
                if text:
                    payload["text"] = text
                    rebuildable.append((str(point["id"]), text, payload))
                else:
                    skipped += 1

            rebuilt: list[tuple[str, list[float], dict[str, Any]]] = []
            for offset in range(0, len(rebuildable), 64):
                batch = rebuildable[offset : offset + 64]
                vectors = await candidate.embed([text for _point_id, text, _payload in batch])
                if len(vectors) != len(batch):
                    raise ValueError("embedding provider returned an incomplete batch")  # i18n-exempt: internal invariant
                rebuilt.extend(
                    (point_id, vector, payload)
                    for (point_id, _text, payload), vector in zip(batch, vectors, strict=True)
                )

            await self.vector_db.vector_store.replace_all(candidate.dim, rebuilt)
            self.embeddings = candidate
            self.vector_db.embeddings = candidate
            self.worldbook.embeddings = candidate
            self.settings.llm.embedding_profile = profile_id
            self.settings.llm.embedding_model = model
            self.settings.llm.embedding_dim = candidate.dim
            if skipped:
                logger.warning("Dropped %d orphaned vectors while rebuilding the Embedding index", skipped)
            return len(rebuilt)


    async def room_rulepack(self, ctx: AgentCtx) -> RulePack:
        """The rule system THIS room plays: the active character's system, then the
        room's world-import system pin (`room_system`, written by `.import … world`
        when the module's pack ships exactly one rulepack), then the deployment's
        configured default pack. The ONE answer to "which pack is this room on" —
        every command and tool asks here (it used to be a function-level import in
        eight places)."""
        system = ""
        try:
            character = await self.characters.get_character(ctx.uid(), ctx.chat_key)
            if has_character(character):
                system = character.system
        except Exception:
            system = ""
        if not system:
            try:
                system = await self.store.state_get(ctx.chat_key, "room_system") or ""
            except Exception:
                system = ""
        try:
            return load_rulepack(system or self.settings.default_rulepack)
        except Exception:
            return load_rulepack(self.settings.default_rulepack)


def build_services(
    settings: Settings | None = None,
    *,
    llm: LLMClient | None = None,
    fallback_llm: LLMClient | None = None,
    embeddings: Embeddings | None = None,
    i18n: I18n | None = None,
    store: Store | None = None,
    db_path: str = ":memory:",
    vector_path: str | None = None,
) -> Services:
    """Wire the full service graph. Inject `llm`/`embeddings` (e.g. FakeLLM /
    FakeEmbeddings) to run offline; otherwise the configured client is built
    from `settings.llm`. ``fallback_llm`` remains inside ``MutableLLM`` so an
    initially offline app can hot-switch when credentials arrive."""
    settings = settings or get_settings()
    i18n = i18n or get_i18n(settings.locale)
    store = store or Store(db_path)
    # TRPG_DEBUG__TOOL_TRACE / `.trace` runtime toggles: every persisted per-room
    # choice (`.trace on` writes `runtime_config.tool_trace`) is restored so the
    # toggles survive restarts; the env var stays the legacy GLOBAL fallback (room
    # "") for rooms with no dedicated toggle. Relative paths land under data_dir,
    # which is already private-mode — the files hold keeper-grade content (tool
    # arguments and results), so they must never sit somewhere casually shared.
    for room, trace_path in persisted_trace_paths_sync(store).items():
        enable_tool_trace(trace_path, room=room)
    env_trace = (settings.debug.tool_trace or "").strip()
    if env_trace and "" not in persisted_trace_paths_sync(store):
        path = Path(env_trace)
        enable_tool_trace(path if path.is_absolute() else Path(settings.data_dir) / path, room="")
    runtime_config = RuntimeConfig(store)
    # One-shot: merge any legacy `runtime_config.credentials` data into the
    # unified `runtime_config.llm_profiles` book and drop the legacy key.
    migrate_llm_credentials(store)
    llm_profiles = CredentialBook(store, key=LLM_PROFILES_KEY)
    imagegen_runtime_config = ImageGenRuntimeConfig(store)
    imagegen_credentials = ImageGenCredentialBook(store)
    scribe_runtime_config = LaneRuntimeConfig(store, key=SCRIBE_RUNTIME_KEY)
    director_runtime_config = LaneRuntimeConfig(store, key=DIRECTOR_RUNTIME_KEY)
    base_scribe_settings = settings.scribe.model_copy(deep=True)
    base_director_settings = settings.director.model_copy(deep=True)
    base_embedding_settings = settings.llm.model_copy(deep=True)
    # The deployment baseline BEFORE any persisted runtime override is applied —
    # `MutableLLM.apply({})` must reset to this (e.g. deleting the live-default
    # profile), not to the startup-overridden snapshot.
    pristine_settings = settings.model_copy(deep=True)
    imagegen_overrides = imagegen_runtime_config.load_sync()
    runtime_overrides = runtime_config.load_sync()
    if runtime_overrides:
        settings = apply_overrides(settings, runtime_overrides)
    if imagegen_overrides:
        settings = apply_imagegen_overrides(settings, imagegen_overrides)
    scribe_overrides = scribe_runtime_config.load_sync()
    if scribe_overrides:
        settings = apply_lane_overrides(settings, "scribe", scribe_overrides)
    director_overrides = director_runtime_config.load_sync()
    if director_overrides:
        settings = apply_lane_overrides(settings, "director", director_overrides)
    # An injected `llm` (e.g. FakeLLM in tests) is used verbatim and left
    # UNWRAPPED so those paths stay byte-compatible. Otherwise wrap in a
    # `MutableLLM` whose provider/model the `.model` admin command can hot-swap,
    # and apply any persisted runtime overrides at startup. `build_llm` (inside
    # `MutableLLM`) honors settings.llm.provider + PRESETS (OpenAI/Anthropic/Gemini/
    # OpenAI-compatible).
    embedding_settings = settings.llm
    embedding_profile = settings.llm.embedding_profile.casefold()
    if embedding_profile:
        saved_profile = llm_profiles.load_sync().get(embedding_profile, {})
        profile_provider, profile_kind, encoded_model = model_profile_parts(embedding_profile)
        if saved_profile and profile_kind == "embedding":
            embedding_settings = settings.llm.model_copy(
                update={
                    "provider": profile_provider,
                    "api_key": saved_profile.get("api_key") or saved_profile.get("access_token") or "",
                    "base_url": saved_profile.get("base_url", ""),
                    "embedding_model": saved_profile.get("chat_model") or encoded_model,
                    "embedding_dim": int(saved_profile.get("embedding_dim") or settings.llm.embedding_dim),
                }
            )
    supplied_embeddings = embeddings
    if embeddings is None:
        embeddings = OpenAIEmbeddings(embedding_settings) if embedding_settings.api_key else FakeEmbeddings(64)
    if not embedding_profile or supplied_embeddings is not None:
        base_embeddings = embeddings
    elif base_embedding_settings.api_key:
        base_embeddings = OpenAIEmbeddings(base_embedding_settings)
    else:
        base_embeddings = FakeEmbeddings(64)
    if llm is None:
        # Warm the credential book cache so subscription providers can resolve
        # OAuth tokens at build_llm time (sync path).
        llm_profiles.load_sync()
        mutable_kwargs = {"credentials": llm_profiles, "base": pristine_settings}
        if fallback_llm is not None:
            mutable_kwargs["fallback_llm"] = fallback_llm
        mutable = MutableLLM(settings, **mutable_kwargs)
        persisted = runtime_config.load_sync()
        if persisted:
            # A persisted override that no longer builds (e.g. a native provider whose optional SDK
            # or key went missing) must NOT brick boot. Fall back to the env/`Settings` baseline the
            # `MutableLLM` was constructed with, and log it, instead of raising out of build_services.
            # The baseline itself is covered one layer down: when a `fallback_llm` is configured
            # (`app.py` always supplies one on this path), `MutableLLM.__init__` degrades to it
            # rather than raising, so neither path can take the server down and leave `.model set`
            # -- the repair interface -- unreachable. Without a fallback there is nothing to
            # degrade to and the build error still propagates.
            try:
                mutable.apply(persisted)
            except Exception:
                logger.warning(
                    "Ignoring unusable persisted LLM override for provider=%r model=%r; using base config",
                    persisted.get("provider"),
                    persisted.get("chat_model"),
                    exc_info=True,
                )
                try:
                    mutable.apply({})  # restore the pristine env/Settings baseline
                except Exception:
                    # The baseline is unbuildable too (MutableLLM already degraded to the
                    # offline fallback at construction and warned). Restoring it can only
                    # re-raise the same failure, and boot must survive that as well.
                    logger.warning("Base LLM config is unusable too; staying on the offline fallback")
        llm = mutable

    # keep the deterministic-core crit toggle in sync with config
    dice_config.ENABLE_CRITICAL_EFFECTS = settings.enable_critical_effects
    dice = DiceRoller()

    documents = DocumentStore(store)
    characters = CharacterManager(store)
    battles = BattleReportManager(store)
    embedding_lock = asyncio.Lock()
    vector_store = VectorStore(embeddings.dim, path=vector_path)
    vector_db = VectorDatabaseManager(
        embeddings,
        vector_store,
        i18n,
        llm=llm,
        operation_lock=embedding_lock,
    )
    module_init = ModuleInitializer(store, vector_db, llm, settings, i18n, battles=battles)
    # Worldbook talks the raw `infra.vector.VectorStore` upsert/search/delete
    # API (its own "worldbook" collection), so it takes `vector_store` directly --
    # not the higher-level `VectorDatabaseManager`, which exposes a different surface.
    worldbook = Worldbook(
        store,
        vector_db=vector_store,
        embeddings=embeddings,
        operation_lock=embedding_lock,
    )
    imagegen = build_imagegen(settings, credentials=llm_profiles)

    return Services(
        settings=settings,
        store=store,
        documents=documents,
        i18n=i18n,
        dice=dice,
        characters=characters,
        battles=battles,
        vector_db=vector_db,
        module_init=module_init,
        worldbook=worldbook,
        llm=llm,
        imagegen=imagegen,
        embeddings=embeddings,
        runtime_config=runtime_config,
        llm_profiles=llm_profiles,
        imagegen_runtime_config=imagegen_runtime_config,
        imagegen_credentials=imagegen_credentials,
        scribe_runtime_config=scribe_runtime_config,
        director_runtime_config=director_runtime_config,
        embedding_lock=embedding_lock,
        base_scribe_settings=base_scribe_settings,
        base_director_settings=base_director_settings,
        base_embedding_settings=base_embedding_settings,
        base_embeddings=base_embeddings,
    )


_RULE_VARIANT_KEY = "rule_variant"


async def room_rule_variant(store, chat_key: str) -> str | None:
    """The room's selected house-rule ladder (a rulepack `variants:` id), or
    ``None`` for the pack's default ladder. Set by the rule-variant command
    (`.rule`); read by every check path so grading and Luck re-grading
    agree. Stored per room in room_state under ``rule_variant``."""
    value = await store.state_get(chat_key, _RULE_VARIANT_KEY)
    value = (value or "").strip()
    return value or None


async def set_room_rule_variant(store, chat_key: str, variant: str | None) -> None:
    """Persist the room's house-rule ladder selection ("" clears to the default)."""
    await store.state_set(chat_key, _RULE_VARIANT_KEY, variant or "")


# --- Room lifecycle (M23 WS1) -----------------------------------------------
ROOM_FACETS = (
    RoomStateFacet(
        name="rule_variant",
        owner="agent.services",
        reset_scope=None,
        survives_because=(
            "`.setcoc` picks the table's house-rule ladder — a room setting, and one a "
            "fresh session at the same table wants to keep"
        ),
        state_keys=frozenset({_RULE_VARIANT_KEY}),
        storages=frozenset({STORAGE_ROOM_STATE}),
    ),
    RoomStateFacet(
        name="llm_selection",
        owner="agent.services",
        reset_scope=None,
        survives_because="The room chooses which global LLM profile each job uses.",
        state_keys=frozenset({ROOM_LLM_SELECTION_KEY}),
        storages=frozenset({STORAGE_ROOM_STATE}),
    ),
)
