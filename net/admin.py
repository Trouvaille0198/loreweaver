"""Keeper-gated admin surface for the networked TUI (see `docs/protocol.md`).

The `net.tui_server.TuiServer` routes the v1.1 `admin_*` frames here. A keeper
holds an admin gate BY CONSTRUCTION: the keystore role stamped on the connection
at `join` decides it — a `keeper`-role connection may read/mutate the live LLM
config and mint/list keys for its own room; anyone else gets
`admin_error {code:"forbidden"}`.
There is no separate auth system.

Config/model handling REUSES the same primitives the `.model` chat command uses
(`infra.providers`: `is_known_provider`, `describe_settings`, `mask_secret`,
provider catalogs) and the shared `services.runtime_config`, so a switch made
here persists and hot-reconfigures the live `MutableLLM` exactly like
`.model set` -- every LLM consumer observes it without a restart.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from agent.context import AgentCtx, FsAdapter
from agent.forge import (
    ForgeResult,
    generate_and_install_module,
    generate_and_install_pack_module,
    generate_and_install_rulepack,
    generate_and_install_skill,
    generate_module_prompt,
)
from agent.services import ROOM_LLM_SELECTION_KEY, Services
from core.rulepacks import available_systems, built_in_rulepack_ids
from core.skills import available_skills
from gateway.ops import get_enabled_skills, toggle_enabled_skill
from gateway.rooms import (
    clear_bindings_for_session,
    clear_keeper_binding,
    clear_keeper_bindings_for_room,
    list_keeper_bindings_for_room,
    session_key_for_room,
)
from gateway.turn import publish_state
from infra.embeddings import OpenAIEmbeddings
from infra.i18n import I18n
from infra.imagegen import (
    IMAGEGEN_PRESETS,
    apply_imagegen_overrides,
    build_imagegen,
    describe_imagegen_settings,
)
from infra.oauth_flows import (
    SUBSCRIPTION_DEFAULT_MODELS,
    canonical_subscription_provider,
    is_subscription_provider,
)
from infra.providers import (
    CHATGPT_SUBSCRIPTION_PROXY_PROVIDER_NAMES,
    PRESETS,
    describe_settings,
    is_known_provider,
    list_models,
    mask_secret,
    provider_auth_type,
    provider_catalog,
    provider_supports_kind,
)
from infra.runtime_config import MODEL_KINDS, model_profile_id, model_profile_parts
from net.keystore import _DEFAULT_PURPOSE, Keystore
from net.room_backup import (
    RESET_SCOPES,
    chat_key_for_room,
    delete_room_data,
    export_room,
    import_room,
    reset_room_state,
)

logger = logging.getLogger(__name__)

# The client -> server admin request frames this module answers.
_ADMIN_REQUESTS: frozenset[str] = frozenset(
    {
        "admin_get_config",
        "admin_set_model",
        "admin_set_llm",
        "admin_delete_llm",
        "admin_set_embedding",
        "admin_get_room_config",
        "admin_set_room_model",
        "admin_set_llm_lane",
        "admin_set_imagegen",
        "admin_list_models",
        "admin_list_keys",
        "admin_mint_key",
        "admin_update_key",
        "admin_delete_key",
        "admin_delete_room",
        "admin_export_room",
        "admin_import_room",
        "admin_export_llm",
        "admin_import_llm",
        "admin_delete_room_data",
        "admin_reset_room",
        "admin_list_skills",
        "admin_enable_skill",
        "admin_list_rules",
        "admin_generate",
        "admin_update_server",
    }
)

_KEEPER_ROLE = "keeper"


def is_admin_frame(kind: Any) -> bool:
    """True if `kind` names one of the admin request frames handled here."""
    return isinstance(kind, str) and kind in _ADMIN_REQUESTS


class AdminService:
    """Transport-independent facade over the existing admin frame handlers."""

    def __init__(
        self,
        services: Services,
        keystore: Keystore,
        *,
        fs: FsAdapter | None = None,
        hub: Any = None,
    ) -> None:
        self.services = services
        self.keystore = keystore
        self.fs = fs
        self.hub = hub

    async def dispatch(
        self,
        role: str,
        caller_room: str,
        frame: dict[str, Any],
        i18n: I18n,
        *,
        reauthorize: Any = None,
        emit_frame: Any = None,
    ) -> dict[str, Any]:
        if role == _KEEPER_ROLE and frame.get("type") == "admin_delete_key":
            binding = await self._chat_binding_for_id(caller_room, str(frame.get("id") or ""))
            if binding is not None:
                await clear_keeper_binding(
                    self.services.store,
                    *binding,
                    expected_room=caller_room,
                )
                await self._evict_chat_members(caller_room, binding)
                reply = _keys_frame(self.keystore, caller_room)
                return await self._with_chat_bindings(reply, caller_room)
        reply = await _dispatch_admin_frame(
            self.services,
            self.keystore,
            role,
            caller_room,
            frame,
            i18n,
            fs=self.fs,
            reauthorize=reauthorize,
            hub=self.hub,
            emit_frame=emit_frame,
        )
        if reply.get("type") != "admin_error" and frame.get("type") in {
            "admin_delete_room",
            "admin_delete_room_data",
        }:
            await self._evict_chat_members(caller_room)
        if reply.get("type") != "admin_error" and frame.get("type") == "admin_reset_room" and self.hub is not None:
            # The reset keeps everyone connected (no eviction), so proactively push a
            # fresh reset-flagged state frame: connected clients refresh their info panel
            # and clear their stale chat scrollback without needing to reconnect or send.
            await publish_state(
                self.hub,
                self.services,
                AgentCtx(chat_key=chat_key_for_room(caller_room)),
                reset=True,
            )
        return await self._with_chat_bindings(reply, caller_room)

    async def _evict_chat_members(
        self,
        room: str,
        identity: tuple[str, str] | None = None,
    ) -> None:
        if self.hub is None:
            return
        for member in self.hub.members(session_key_for_room(room)):
            source = getattr(member, "source", None)
            if source is None:
                continue
            if (
                identity is not None
                and (
                    getattr(source, "platform", ""),
                    getattr(source, "user_id", ""),
                )
                != identity
            ):
                continue
            await self.hub.unsubscribe(member)

    async def _with_chat_bindings(
        self,
        reply: dict[str, Any],
        room: str,
    ) -> dict[str, Any]:
        if reply.get("type") != "admin_keys":
            return reply
        keys = list(reply.get("keys") or [])
        for platform, user_id in await list_keeper_bindings_for_room(self.services.store, room):
            identity = f"{platform}:{user_id}"
            keys.append(
                {
                    "id": _chat_binding_id(identity),
                    "key_masked": identity,
                    "room": room,
                    "name": identity,
                    "role": _KEEPER_ROLE,
                    "purpose": "chat_bind",
                    "expires_at": None,
                }
            )
        return {**reply, "keys": keys}

    async def _chat_binding_for_id(
        self,
        room: str,
        binding_id: str,
    ) -> tuple[str, str] | None:
        for platform, user_id in await list_keeper_bindings_for_room(self.services.store, room):
            if _chat_binding_id(f"{platform}:{user_id}") == binding_id:
                return platform, user_id
        return None


async def _dispatch_admin_frame(
    services: Services,
    keystore: Keystore,
    role: str,
    caller_room: str,
    frame: dict[str, Any],
    i18n: I18n,
    *,
    fs: FsAdapter | None = None,
    reauthorize: Any = None,
    hub: Any = None,
    emit_frame: Any = None,
) -> dict[str, Any]:
    """Handle one admin request `frame`, returning the reply frame to send.

    Gated two ways: (1) every admin request requires a `keeper`-role connection;
    (2) the destructive / room-content ops (export/import/delete_room/
    delete_room_data) and every key operation are scoped to the
    caller's OWN room (`caller_room`, the room the connecting keeper key is bound
    to) — a keeper cannot reach into another room's data or keys. Either gate
    failing yields `admin_error {code:"forbidden"}` and nothing is read or mutated.
    The KP-skills list/enable and the forge (`admin_generate`) requests are ALSO
    scoped to `caller_room` (a room's enabled-skill set, and — for `kind:"module"`
    — the room a generated module is installed into). `fs` is the `FsAdapter` a
    generated module's install needs (see `_generate`); transports without one
    (e.g. no filesystem bridge configured) still answer, but a module generation
    then fails cleanly via `agent.kp_tools_knowledge.DocumentTools.upload_document`'s
    own `ctx.fs is None` guard rather than raising.
    """
    if role != _KEEPER_ROLE:
        return _error("forbidden", i18n)

    kind = frame.get("type")
    if kind == "admin_get_room_config":
        return await _room_config_frame(services, caller_room)
    if kind == "admin_set_room_model":
        async with services.config_lock:
            if reauthorize is not None and not reauthorize():
                return _error("forbidden", i18n)
            return await _set_room_model(services, caller_room, frame, i18n)
    if kind == "admin_get_config":
        return await _config_frame(services)
    if kind == "admin_set_model":
        async with services.config_lock:
            if reauthorize is not None and not reauthorize():
                return _error("forbidden", i18n)
            return await _set_model(services, frame, i18n)
    if kind == "admin_set_embedding":
        async with services.config_lock:
            if reauthorize is not None and not reauthorize():
                return _error("forbidden", i18n)
            return await _set_embedding(services, frame, i18n)
    if kind == "admin_set_llm":
        async with services.config_lock:
            if reauthorize is not None and not reauthorize():
                return _error("forbidden", i18n)
            return await _set_llm_profile(services, frame, i18n)
    if kind == "admin_delete_llm":
        async with services.config_lock:
            if reauthorize is not None and not reauthorize():
                return _error("forbidden", i18n)
            return await _delete_llm_profile(services, frame, i18n)
    if kind == "admin_set_llm_lane":
        async with services.config_lock:
            if reauthorize is not None and not reauthorize():
                return _error("forbidden", i18n)
            return await _set_llm_lane(services, frame, i18n)
    if kind == "admin_set_imagegen":
        async with services.config_lock:
            if reauthorize is not None and not reauthorize():
                return _error("forbidden", i18n)
            return await _set_imagegen(services, frame, i18n)
    if kind == "admin_list_models":
        return await _list_models(services, frame, i18n)
    if kind == "admin_list_keys":
        return _keys_frame(keystore, caller_room)
    if kind == "admin_mint_key":
        return _mint_key(keystore, caller_room, frame, i18n)
    if kind == "admin_update_key":
        return _update_key(keystore, caller_room, frame, i18n)
    if kind == "admin_delete_key":
        return _delete_key(keystore, caller_room, frame, i18n)
    if kind == "admin_delete_room":
        return await _delete_room(services, keystore, caller_room, frame, i18n)
    if kind == "admin_export_room":
        return await _export_room(services, keystore, caller_room, frame, i18n)
    if kind == "admin_import_room":
        return await _import_room(services, keystore, caller_room, frame, i18n)
    if kind == "admin_export_llm":
        return await _export_llm_config(services, i18n)
    if kind == "admin_import_llm":
        async with services.config_lock:
            if reauthorize is not None and not reauthorize():
                return _error("forbidden", i18n)
            return await _import_llm_config(services, frame, i18n)
    if kind == "admin_delete_room_data":
        return await _delete_room_data(services, keystore, caller_room, frame, i18n, hub=hub)
    if kind == "admin_reset_room":
        return await _reset_room(services, keystore, caller_room, frame, i18n)
    if kind == "admin_update_server":
        return await _update_server(services, i18n)
    if kind == "admin_list_skills":
        return await _skills_frame(services, caller_room, i18n, frame)
    if kind == "admin_enable_skill":
        return await _enable_skill(services, caller_room, frame, i18n)
    if kind == "admin_list_rules":
        return _rules_frame()
    if kind == "admin_generate":
        return await _generate(services, caller_room, fs, frame, i18n, emit_frame=emit_frame)
    return _error("bad_request", i18n)


async def _set_embedding(services: Services, frame: dict[str, Any], i18n: I18n) -> dict[str, Any]:
    profile_id = str(frame.get("profile_id") or "").strip().casefold()
    profiles = await services.llm_profiles.all()
    saved = profiles.get(profile_id)
    if not saved:
        return _error("not_found", i18n)
    provider, kind, encoded_model = model_profile_parts(profile_id)
    if kind != "embedding":
        return _error("bad_request", i18n)
    model = str(saved.get("chat_model") or encoded_model).strip()
    raw_dim = frame.get("embedding_dim", saved.get("embedding_dim"))
    try:
        dim = int(raw_dim)
    except (TypeError, ValueError):
        return _error("bad_request", i18n)
    if not provider or not model or dim <= 0:
        return _error("bad_request", i18n)
    current = await services.runtime_config.get()
    updated = dict(current)
    updated.update(
        {
            "embedding_profile": profile_id,
            "embedding_model": model,
            "embedding_dim": str(dim),
        }
    )
    candidate_settings = services.settings.llm.model_copy(
        update={
            "provider": provider,
            "api_key": saved.get("api_key") or saved.get("access_token") or "",
            "base_url": saved.get("base_url", ""),
            "embedding_model": model,
            "embedding_dim": dim,
        }
    )
    candidate = OpenAIEmbeddings(candidate_settings)
    try:
        await services.runtime_config.replace(**updated)
        rebuilt = await services.reconfigure_embeddings(
            candidate,
            profile_id=profile_id,
            model=model,
        )
    except Exception:
        logger.exception("admin_set_embedding failed for profile=%s", profile_id)
        try:
            await services.runtime_config.replace(**current)
        except Exception:
            logger.exception("failed to restore Embedding runtime configuration")
        return _error("set_failed", i18n)
    reply = await _config_frame(services)
    reply["embedding_rebuilt"] = rebuilt
    return reply


# -- LLM config -------------------------------------------------------------


async def _config_frame(services: Services) -> dict[str, Any]:
    info = _describe_llm(services)
    overrides = await services.runtime_config.get()
    saved_providers = await services.llm_profiles.providers()
    providers = provider_catalog()
    # Subscription status for the model screen (no new protocol frames).
    provider = (info["provider"] or "").casefold()
    base_url = info.get("base_url") or ""
    api_key_masked = info["api_key"]
    # Pure OAuth path only: supergrok, or chatgpt/gpt-subscription without a proxy base_url.
    # chatgpt + base_url still means a user-operated proxy (classic key masking).
    oauth_path = provider == "supergrok" or (
        is_subscription_provider(provider) and provider != "supergrok" and not base_url
    )
    subscription_status = ""
    if oauth_path:
        sub = await services.llm_profiles.load_subscription(provider)
        if sub is not None:
            subscription_status = "logged_in"
            from datetime import UTC, datetime

            try:
                api_key_masked = datetime.fromtimestamp(float(sub.expires_at), tz=UTC).strftime("sub:%Y-%m-%dT%H:%MZ")
            except (TypeError, ValueError, OSError):
                api_key_masked = "sub:logged_in"
        else:
            subscription_status = "logged_out"
    return {
        "type": "admin_config",
        "provider": info["provider"],
        "chat_model": info["chat_model"],
        "base_url": info["base_url"],
        "api_key_masked": api_key_masked,
        "embedding_model": overrides.get("embedding_model", services.settings.llm.embedding_model),
        "embedding_dim": int(overrides.get("embedding_dim", services.settings.llm.embedding_dim)),
        "embedding_profile": str(overrides.get("embedding_profile") or ""),
        # `providers` is the ID-only projection; metadata-aware clients use the catalog.
        "providers": [provider["id"] for provider in providers],
        "provider_catalog": providers,
        # Providers that already have a saved key — the model screen marks these 'ready' and
        # switching to one never re-asks for its key (see `_set_model`).
        "saved_providers": saved_providers,
        "llms": await _llm_profiles(services),
        "override_active": bool(overrides),
        "scribe": await _lane_status(services, "scribe"),
        "director": await _lane_status(services, "director"),
        "imagegen": await _imagegen_status(services),
        # Lets connected clients remove a stale guided-demo affordance immediately.
        "using_demo": bool(getattr(services.llm, "using_fallback", False)),
        # Optional hint (clients that ignore unknown fields stay compatible).
        "subscription_status": subscription_status,
    }


async def _llm_profiles(services: Services) -> list[dict[str, Any]]:
    """Return typed model profiles, treating untyped persisted entries as chat."""
    book = await services.llm_profiles.all()
    # A bare `provider` entry is the live-model credential written by the
    # `admin_set_model` path. When a typed `provider::model` profile already
    # exists for the same provider+model, the bare entry is a redundant mirror —
    # skip it so it never renders as a duplicate of the typed profile.
    typed_ids = {str(pid) for pid in book if "::" in str(pid)}
    profiles: dict[str, dict[str, Any]] = {}
    for profile_id, saved in book.items():
        profile_id = str(profile_id)
        provider, encoded_kind, encoded_model = model_profile_parts(profile_id)
        model = saved.get("chat_model", "") or encoded_model
        kind = str(saved.get("kind") or encoded_kind)
        if not provider or kind not in MODEL_KINDS:
            continue
        if "::" not in profile_id and model:
            # Bare chat entry duplicating an existing typed chat profile.
            if model_profile_id(provider, str(model)) in typed_ids:
                continue
        secret = saved.get("api_key") or saved.get("access_token") or ""
        profiles[profile_id] = {
            "id": profile_id,
            "provider": provider,
            "chat_model": model,
            "kind": kind,
            "embedding_dim": int(saved.get("embedding_dim") or 0),
            "base_url": saved.get("base_url", ""),
            "api_key_masked": mask_secret(secret),
            "has_key": bool(secret),
        }
    return sorted(profiles.values(), key=lambda item: (str(item["provider"]), str(item["kind"]), str(item["id"])))


def _default_room_llm_selection() -> dict[str, Any]:
    return {
        "main": "",
        "scribe": "",
        "director": "",
        "imagegen": "",
        "scribe_enabled": True,
        "director_enabled": True,
    }


async def _room_llm_selection(services: Services, room: str) -> dict[str, Any]:
    return await services.room_model_selection(room)


async def _room_config_frame(services: Services, room: str) -> dict[str, Any]:
    profiles = await _llm_profiles(services)
    selection = await _room_llm_selection(services, room)
    profile_ids = [str(profile["id"]) for profile in profiles]
    return {
        "type": "admin_room_config",
        "room": room,
        "active": any(selection[key] for key in ("main", "scribe", "director", "imagegen")),
        "providers": profile_ids,
        "saved_providers": profile_ids,
        "stored": selection,
    }


async def _set_room_model(
    services: Services,
    room: str,
    frame: dict[str, Any],
    i18n: I18n,
) -> dict[str, Any]:
    profiles = {str(profile["id"]): str(profile["kind"]) for profile in await _llm_profiles(services)}
    # Admin frames are addressed by the human room name, but the room's runtime state is
    # keyed by its canonical session key. Writing under the bare room name would orphan the
    # selection from the turn / reset machinery that reads and clears it (see the dual-
    # prefix read in `room_model_selection`). Persist under the session key so a single
    # spelling owns the row.
    chat_key = session_key_for_room(room)
    if frame.get("clear") is True:
        await services.store.state_delete(chat_key, ROOM_LLM_SELECTION_KEY)
        # Retire a legacy bare-name row written by an older engine so a cleared
        # selection can never resurface through the fallback read.
        if room != chat_key:
            await services.store.state_delete(room, ROOM_LLM_SELECTION_KEY)
        return await _room_config_frame(services, room)
    selection = await _room_llm_selection(services, room)
    expected_kind = {"main": "chat", "scribe": "chat", "director": "chat", "imagegen": "image"}
    for key, kind in expected_kind.items():
        if key in frame:
            value = str(frame.get(key) or "").strip().casefold()
            if value and value not in profiles:
                return _error("not_found", i18n)
            if value and profiles[value] != kind:
                return _error("bad_request", i18n)
            selection[key] = value
    for key in ("scribe_enabled", "director_enabled"):
        if key in frame:
            selection[key] = bool(frame[key])
    await services.store.state_set(chat_key, ROOM_LLM_SELECTION_KEY, json.dumps(selection))
    # Self-heal: drop the legacy bare-name row (older engines wrote that
    # spelling). With both rows present, the canonical-preference read in
    # `room_model_selection` already ignores it, but removing it keeps the
    # store single-spelling for new deployments and heals old ones on first save.
    if room != chat_key:
        await services.store.state_delete(room, ROOM_LLM_SELECTION_KEY)
    return await _room_config_frame(services, room)


async def _lane_status(services: Services, lane: str) -> dict[str, Any]:
    settings = getattr(services.settings, lane)
    runtime = getattr(services, f"{lane}_runtime_config")
    overrides = await runtime.get()
    return {
        "enabled": bool(settings.enabled),
        "provider": settings.provider,
        "chat_model": settings.chat_model,
        "base_url": settings.base_url,
        "api_key_masked": mask_secret(settings.api_key),
        "override_active": bool(overrides),
    }


def _clear_lane_client_cache(services: Services, lane: str) -> None:
    attr = f"_{lane}_llm_cache"
    if hasattr(services, attr):
        delattr(services, attr)


async def _set_llm_lane(services: Services, frame: dict[str, Any], i18n: I18n) -> dict[str, Any]:
    lane = str(frame.get("lane") or "").strip().casefold()
    if lane not in {"scribe", "director"}:
        return _error("bad_request", i18n)
    runtime = getattr(services, f"{lane}_runtime_config")
    if frame.get("clear") is True:
        await runtime.clear()
        setattr(services.settings, lane, getattr(services, f"base_{lane}_settings").model_copy(deep=True))
        _clear_lane_client_cache(services, lane)
        return await _config_frame(services)

    allowed = {"enabled", "provider", "chat_model", "base_url", "reasoning_effort"}
    current = await runtime.get()
    for key in allowed:
        if key in frame:
            value = frame[key]
            current[key] = bool(value) if key == "enabled" else str(value or "").strip()
    if "api_key" in frame:
        current["api_key"] = str(frame.get("api_key") or "").strip()
    if frame.get("clear_api_key") is True:
        current["api_key"] = ""
    provider = str(current.get("provider") or "").strip().casefold()
    if provider and not is_known_provider(provider):
        return _error("unknown_provider", i18n)
    baseline = getattr(services, f"base_{lane}_settings")
    previous = getattr(services.settings, lane).model_copy(deep=True)
    candidate = baseline.model_copy(update=current)
    try:
        setattr(services.settings, lane, candidate)
        _clear_lane_client_cache(services, lane)
        await runtime.replace(**current)
    except Exception:
        logger.exception("admin_set_llm_lane failed (lane=%s)", lane)
        setattr(services.settings, lane, previous)
        _clear_lane_client_cache(services, lane)
        return _error("set_failed", i18n)
    return await _config_frame(services)


async def _set_model(services: Services, frame: dict[str, Any], i18n: I18n) -> dict[str, Any]:
    provider = str(frame.get("provider") or "").strip().casefold()
    if not provider or not is_known_provider(provider):
        return _error("unknown_provider", i18n)

    current = await services.runtime_config.get()
    live = _live_llm_settings(services)
    same_provider = _provider_identity(provider) == _provider_identity(live.provider)
    saved = await _saved_llm_credentials(services, provider)
    api_key_supplied = "api_key" in frame
    base_url_supplied = "base_url" in frame
    supplied_api_key = str(frame.get("api_key") or "").strip()
    supplied_base_url = str(frame.get("base_url") or "").strip()
    if provider == "supergrok":
        api_key = ""
        base_url = ""
    else:
        current_api_key = (live.api_key or "") if same_provider else ""
        current_base_url = (live.base_url or "") if same_provider else ""
        fallback_api_key, fallback_base_url = _static_credential_pair(
            same_provider, current_api_key, current_base_url, saved
        )
        base_url = supplied_base_url if base_url_supplied else fallback_base_url
        endpoint_changed = base_url_supplied and not _same_endpoint(
            _effective_llm_endpoint(provider, base_url),
            _effective_llm_endpoint(provider, fallback_base_url),
        )
        api_key = supplied_api_key if api_key_supplied else "" if endpoint_changed else fallback_api_key

    oauth_path = provider == "supergrok" or (provider in CHATGPT_SUBSCRIPTION_PROXY_PROVIDER_NAMES and not base_url)
    if oauth_path and await services.llm_profiles.load_subscription(provider) is None:
        return _error("set_failed", i18n)

    supplied_model = str(frame.get("chat_model") or "").strip()
    if supplied_model:
        chat_model = supplied_model
    elif same_provider:
        chat_model = live.chat_model
    else:
        chat_model = saved.get("chat_model") or SUBSCRIPTION_DEFAULT_MODELS.get(provider, live.chat_model)
    overrides = {
        key: value for key, value in current.items() if key not in {"provider", "chat_model", "api_key", "base_url"}
    }
    overrides.update(
        {
            "provider": provider,
            "chat_model": chat_model,
            "api_key": api_key,
            "base_url": base_url,
        }
    )
    try:
        _reconfigure_llm(services, overrides)
        await services.runtime_config.replace(**overrides)
        if not oauth_path and (api_key_supplied or base_url_supplied or api_key or base_url):
            await _replace_llm_static_credentials(
                services,
                provider,
                api_key=api_key,
                base_url=base_url,
                chat_model=chat_model,
            )
    except Exception:
        logger.exception("admin_set_model failed (provider=%s)", provider)
        return _error("set_failed", i18n)
    return await _config_frame(services)


async def _set_llm_profile(services: Services, frame: dict[str, Any], i18n: I18n) -> dict[str, Any]:
    """Save one typed global provider/model profile without changing the live model."""
    provider = str(frame.get("provider") or "").strip().casefold()
    model = str(frame.get("chat_model") or "").strip()
    kind = str(frame.get("kind") or "chat").strip().casefold()
    if not provider or not is_known_provider(provider):
        return _error("unknown_provider", i18n)
    if not model or kind not in MODEL_KINDS:
        return _error("bad_request", i18n)
    if not provider_supports_kind(provider, kind):
        return _error("bad_request", i18n)
    raw_dim = frame.get("embedding_dim", 0)
    try:
        embedding_dim = int(raw_dim or 0)
    except (TypeError, ValueError):
        return _error("bad_request", i18n)
    if kind == "embedding" and embedding_dim <= 0:
        return _error("bad_request", i18n)
    profile_id = model_profile_id(provider, model, kind)
    all_profiles = await services.llm_profiles.all()
    saved = all_profiles.get(profile_id, {})
    legacy_saved = await services.llm_profiles.get(provider)
    base_url_supplied = "base_url" in frame
    base_url = str(frame.get("base_url") or "").strip() if base_url_supplied else str(saved.get("base_url") or "")
    endpoint = _profile_endpoint(provider, kind, base_url)
    sibling_saved = next(
        (
            dict(value)
            for key, value in all_profiles.items()
            if str(key).partition("::")[0].casefold() == provider
            and value.get("api_key")
            and _same_endpoint(
                endpoint,
                _profile_endpoint(provider, str(value.get("kind") or "chat"), str(value.get("base_url") or "")),
            )
        ),
        {},
    )
    legacy_matches = bool(legacy_saved.get("api_key")) and _same_endpoint(
        endpoint,
        _profile_endpoint(provider, kind, str(legacy_saved.get("base_url") or "")),
    )
    same_saved_endpoint = bool(saved) and _same_endpoint(
        endpoint,
        _profile_endpoint(provider, kind, str(saved.get("base_url") or "")),
    )
    supplied_api_key = str(frame.get("api_key") or "").strip()
    clear_api_key = frame.get("clear_api_key") is True
    if supplied_api_key:
        api_key = supplied_api_key
    elif clear_api_key:
        api_key = ""
    elif same_saved_endpoint:
        api_key = str(saved.get("api_key") or "")
    elif legacy_matches:
        api_key = str(legacy_saved.get("api_key") or "")
    else:
        api_key = str(sibling_saved.get("api_key") or "")

    auth_type = provider_auth_type(provider)
    subscription = (
        await services.llm_profiles.load_subscription(provider)
        if auth_type in {"oauth", "api_key_or_oauth"}
        else None
    )
    oauth_path = auth_type == "oauth" or (
        auth_type == "api_key_or_oauth" and not base_url and subscription is not None
    )
    if auth_type == "api_key" and not api_key:
        return _error("set_failed", i18n)
    if auth_type == "api_key_or_oauth" and not api_key and not oauth_path:
        return _error("set_failed", i18n)
    try:
        await services.llm_profiles.replace_static(
            profile_id,
            api_key=api_key,
            base_url=base_url,
            chat_model=model,
            kind=kind,
            embedding_dim=str(embedding_dim) if kind == "embedding" else "",
        )
        services.invalidate_model_profile(profile_id.casefold())
    except Exception:
        logger.exception("admin_set_llm failed (profile=%s)", profile_id)
        return _error("set_failed", i18n)
    return await _config_frame(services)


async def _delete_llm_profile(services: Services, frame: dict[str, Any], i18n: I18n) -> dict[str, Any]:
    """Delete one typed provider/model profile without deleting siblings."""
    raw_id = str(frame.get("id") or "").strip().casefold()
    provider = str(frame.get("provider") or "").strip().casefold()
    model = str(frame.get("chat_model") or "").strip()
    profile_id = raw_id or (model_profile_id(provider, model) if model else provider)
    if not profile_id:
        return _error("bad_request", i18n)
    profile_provider, profile_kind, profile_model = model_profile_parts(profile_id)
    if not profile_provider:
        profile_provider = provider
    canonical = canonical_subscription_provider(profile_provider)
    embedding_rebuilt: int | None = None
    try:
        current = await services.runtime_config.get()
        if current.get("embedding_profile", "").casefold() == profile_id:
            embedding_rebuilt = await services.reconfigure_embeddings(
                services.base_embeddings,
                profile_id="",
                model=services.base_embedding_settings.embedding_model,
            )
            for key in ("embedding_profile", "embedding_model", "embedding_dim"):
                current.pop(key, None)
        profiles = await services.llm_profiles.all()
        if profile_id in profiles:
            await services.llm_profiles.forget(profile_id)
            services.invalidate_model_profile(profile_id)
        else:
            # A bare (chat_model-less) entry — e.g. an OAuth subscription or a
            # legacy provider-scoped key — is keyed by provider name.
            legacy_provider = profile_provider or profile_id
            await services.llm_profiles.forget(legacy_provider)
            if canonical != legacy_provider:
                await services.llm_profiles.forget(canonical)
        # A room whose model selection referenced the deleted profile must not
        # silently fall back to the global default — clear the dangling lanes.
        cleared = await services.clear_room_model_profile(profile_id)
        if cleared:
            logger.info(
                "admin_delete_llm cleared deleted profile %r from rooms: %s",
                profile_id,
                ", ".join(cleared),
            )
        live = _live_llm_settings(services)
        if (
            profile_kind == "chat"
            and _provider_identity(live.provider) == canonical
            and (not profile_model or live.chat_model == profile_model)
        ):
            _reconfigure_llm(services, {})
            for key in ("provider", "chat_model", "api_key", "base_url"):
                current.pop(key, None)
        if profile_kind == "image":
            # Mirror the live-LLM reset: if the deleted image profile is the one
            # the GLOBAL imagegen runtime selection currently points at, reset the
            # runtime selection so the operator's deletion is honored, not kept
            # alive by a stale runtime-config copy.
            ig_runtime = await services.imagegen_runtime_config.get()
            ig_provider = str(ig_runtime.get("provider") or "").casefold()
            ig_model = str(ig_runtime.get("model") or "").strip()
            if ig_provider == profile_provider and (not profile_model or ig_model == profile_model):
                await services.imagegen_runtime_config.replace()
                _reconfigure_imagegen(services, {})
        await services.runtime_config.replace(**current)
    except Exception:
        logger.exception("admin_delete_llm failed (profile=%s)", profile_id)
        return _error("set_failed", i18n)
    reply = await _config_frame(services)
    if embedding_rebuilt is not None:
        reply["embedding_rebuilt"] = embedding_rebuilt
    return reply


_LLM_EXPORT_FORMAT = "loreweaver-llm-config"
# v1 carried two books (`llm_profiles` + legacy `llm_credentials`) and its import
# WIPED the typed profiles by replace_all'ing credentials over them. v2 unifies to
# one `llm_profiles` book (typed + bare provider entries) and imports accept v1 by
# merging its legacy entries into the unified book.
_LLM_EXPORT_VERSION = 2


async def _export_llm_config(services: Services, i18n: I18n) -> dict[str, Any]:
    """Export every saved LLM/embedding/imagegen profile plus the live runtime
    selection as one portable JSON document (keeper-gated; contains plaintext keys,
    so the reply is only ever sent to the requesting keeper connection)."""
    profiles = await services.llm_profiles.all()
    runtime = await services.runtime_config.get()
    imagegen_credentials = await services.imagegen_credentials.all()
    # The live image-generation runtime selection (which provider/model/endpoint
    # actually produces images), so an import restores "which imagegen" — not just
    # the saved credential boxes.
    imagegen_runtime = await services.imagegen_runtime_config.get()
    # Persisted runtime overrides may be empty (the Model screen saves profiles,
    # not overrides). Merge the LIVE effective selection so the export round-trips
    # the actually-running provider/model/endpoint even without an override.
    live = _live_llm_settings(services)
    runtime = {
        **runtime,
        "provider": live.provider or runtime.get("provider", ""),
        "chat_model": live.chat_model or runtime.get("chat_model", ""),
        "base_url": live.base_url or runtime.get("base_url", ""),
    }
    payload = {
        "format": _LLM_EXPORT_FORMAT,
        "version": _LLM_EXPORT_VERSION,
        "llm_profiles": profiles,
        "runtime": runtime,
        "imagegen_credentials": imagegen_credentials,
        "imagegen_runtime": imagegen_runtime,
    }
    return {
        "type": "admin_llm_export",
        "ok": True,
        "config": payload,
    }


async def _import_llm_config(services: Services, frame: dict[str, Any], i18n: I18n) -> dict[str, Any]:
    """Replace the saved LLM/embedding/imagegen profiles with a previously exported
    document. The live runtime selection is restored too; the MutableLLM hot-swaps.

    Validate shape and known providers before writing anything, so a malformed
    import can never leave a half-applied credential book. An empty document
    (no profiles at all) is a valid wipe — importing it clears every saved key.
    """
    raw = frame.get("config")
    if not isinstance(raw, dict):
        return _error("bad_request", i18n)
    if str(raw.get("format") or "") != _LLM_EXPORT_FORMAT:
        return _error("bad_request", i18n)
    try:
        version = int(raw.get("version") or 0)
    except (TypeError, ValueError):
        return _error("bad_request", i18n)
    if version not in (1, _LLM_EXPORT_VERSION):
        return _error("bad_request", i18n)

    profiles_raw = raw.get("llm_profiles")
    credentials_raw = raw.get("llm_credentials") if version == 1 else None
    runtime_raw = raw.get("runtime")
    imagegen_raw = raw.get("imagegen_credentials")
    imagegen_runtime_raw = raw.get("imagegen_runtime")
    if not isinstance(profiles_raw, dict):
        return _error("bad_request", i18n)
    if version == 1 and not isinstance(credentials_raw, dict):
        return _error("bad_request", i18n)
    if not isinstance(runtime_raw, dict) or not isinstance(imagegen_raw, dict):
        return _error("bad_request", i18n)
    if not isinstance(imagegen_runtime_raw, dict):
        return _error("bad_request", i18n)

    profiles: dict[str, dict[str, str]] = {}
    for profile_id, saved in profiles_raw.items():
        provider, kind, model = model_profile_parts(str(profile_id))
        if not provider or not is_known_provider(provider):
            return _error("bad_request", i18n)
        if not isinstance(saved, dict):
            return _error("bad_request", i18n)
        profiles[str(profile_id)] = {str(k): str(v) for k, v in saved.items()}
    if credentials_raw:
        # v1 legacy entries are keyed by provider; fold them into the unified book
        # (a chat_model-ed entry lands under `provider::model`).
        for provider, saved in credentials_raw.items():
            provider = str(provider).casefold()
            if not is_known_provider(provider):
                return _error("bad_request", i18n)
            if not isinstance(saved, dict):
                return _error("bad_request", i18n)
            entry = {str(k): str(v) for k, v in saved.items()}
            model = str(entry.get("chat_model") or "").strip()
            profile_id = model_profile_id(provider, model) if model else provider
            profiles[profile_id] = {**profiles.get(profile_id, {}), **entry}

    try:
        await services.llm_profiles.replace_all(profiles)
        await services.imagegen_credentials.replace_all(
            {
                str(provider): {str(k): str(v) for k, v in saved.items()}
                for provider, saved in imagegen_raw.items()
                if isinstance(saved, dict)
            }
        )
        imagegen_runtime = {str(k): str(v) for k, v in imagegen_runtime_raw.items() if v is not None}
        # Always replace (an empty selection wipes imagegen too, mirroring how an
        # empty `runtime` wipes the LLM selection), then rebuild the live client.
        await services.imagegen_runtime_config.replace(**imagegen_runtime)
        _reconfigure_imagegen(services, imagegen_runtime)
        await services.runtime_config.replace(
            **{key: str(value) for key, value in runtime_raw.items() if value is not None}
        )
        services.invalidate_model_profile(None)
        _reconfigure_llm(services, runtime_raw)
    except Exception:
        logger.exception("admin_import_llm failed")
        return _error("set_failed", i18n)
    return await _config_frame(services)


async def _list_models(services: Services, frame: dict[str, Any], i18n: I18n) -> dict[str, Any]:
    """Answer `admin_list_models` with the provider's LIVE model catalog (OpenAI `/models`).

    Resolves the credential to try in priority order: an api_key/base_url supplied on the
    frame (previewing before Save), else this provider's saved credential, else the current
    live config (only when it's the same provider). Unsupported/unreachable → `models: []`,
    which the client renders as a free-text model field."""
    live = getattr(services.llm, "settings", None)
    base_llm = live.llm if live is not None else services.settings.llm
    current_provider = (base_llm.provider or "openai").lower()
    provider = str(frame.get("provider") or "").strip().casefold() or current_provider
    if not is_known_provider(provider):
        return _error("unknown_provider", i18n)
    model_kind = str(frame.get("kind") or "").strip().casefold()
    if model_kind and model_kind not in MODEL_KINDS:
        return _error("bad_request", i18n)

    api_key_supplied = "api_key" in frame
    base_url_supplied = "base_url" in frame
    supplied_api_key = str(frame.get("api_key") or "").strip()
    supplied_base_url = str(frame.get("base_url") or "").strip()
    saved = await services.llm_profiles.get(provider)
    same_provider = _provider_identity(provider) == _provider_identity(current_provider)
    fallback_api_key, fallback_base_url = _static_credential_pair(
        same_provider,
        base_llm.api_key or "",
        base_llm.base_url or "",
        saved,
    )
    if not fallback_api_key:
        profiles = await services.llm_profiles.all()
        requested_base_url = supplied_base_url if base_url_supplied else fallback_base_url
        target_endpoint = _profile_endpoint(provider, model_kind or "chat", requested_base_url)
        sibling = next(
            (
                value
                for profile_id, value in profiles.items()
                if model_profile_parts(profile_id)[0] == provider
                and value.get("api_key")
                and _same_endpoint(
                    target_endpoint,
                    _profile_endpoint(
                        provider,
                        str(value.get("kind") or "chat"),
                        str(value.get("base_url") or ""),
                    ),
                )
            ),
            {},
        )
        fallback_api_key = str(sibling.get("api_key") or "")
        fallback_base_url = str(sibling.get("base_url") or fallback_base_url)
    base_url = supplied_base_url if base_url_supplied else fallback_base_url
    endpoint_changed = base_url_supplied and not _same_endpoint(
        _effective_llm_endpoint(provider, base_url),
        _effective_llm_endpoint(provider, fallback_base_url),
    )
    api_key = supplied_api_key if api_key_supplied else "" if endpoint_changed else fallback_api_key

    candidate = base_llm.model_copy(update={"provider": provider, "api_key": api_key, "base_url": base_url})
    models = await list_models(candidate, model_kind) if model_kind else await list_models(candidate)
    reply = {
        "type": "admin_models",
        "provider": provider,
        "models": models,
        "imagegen": await _imagegen_status(services),
    }
    if model_kind:
        reply["kind"] = model_kind
    return reply


async def _set_imagegen(services: Services, frame: dict[str, Any], i18n: I18n) -> dict[str, Any]:
    provider = str(frame.get("provider") or "").strip().casefold()
    model = str(frame.get("model") or "").strip()
    if not provider or not model:
        return _error("bad_request", i18n)

    size = str(frame.get("size") or services.settings.imagegen.size or "1024x1024").strip()
    if not _valid_image_size(size):
        return _error("bad_request", i18n)

    api_key_supplied = "api_key" in frame
    base_url_supplied = "base_url" in frame
    supplied_api_key = str(frame.get("api_key") or "").strip()
    supplied_base_url = str(frame.get("base_url") or "").strip()
    live = services.settings.imagegen
    same_provider = provider == (live.provider or "").casefold()
    saved = await services.imagegen_credentials.get(provider) or {}
    # "Set as the default image model" reuses the image profile's stored key, which
    # lives in the LLM credential book (`llm_profiles`), not the imagegen book — so
    # clicking a saved minimax/… profile as default never makes the operator re-enter
    # its key. Only fills gaps; never overrides a key the operator supplied or saved.
    if not saved.get("api_key") and not saved.get("access_token"):
        for profile_id, profile in (await services.llm_profiles.all()).items():
            profile_provider, encoded_kind, _ = model_profile_parts(str(profile_id))
            if profile_provider != provider or str(profile.get("kind") or encoded_kind) != "image":
                continue
            saved = {
                **saved,
                "api_key": profile.get("api_key") or profile.get("access_token") or saved.get("api_key", ""),
                "base_url": profile.get("base_url") or saved.get("base_url", ""),
            }
            break
    # A provider's imagegen often reuses its CHAT key (one api_key for qwen chat +
    # qwen image). The generic chat credential book was never consulted, so such a
    # key was missed and imagegen got configured with api_key="" — build_imagegen
    # then returned None and generation was silently skipped. Only fills a gap,
    # never overrides a key the operator supplied or saved.
    if not saved.get("api_key") and not saved.get("access_token"):
        chat_cred = await services.llm_profiles.get(provider) or {}
        if chat_cred.get("api_key") or chat_cred.get("access_token"):
            saved = {
                **saved,
                "api_key": chat_cred.get("api_key") or chat_cred.get("access_token") or saved.get("api_key", ""),
                "base_url": chat_cred.get("base_url") or saved.get("base_url", ""),
            }
    if provider == "supergrok":
        api_key = ""
        base_url = ""
    else:
        current_api_key = (live.api_key or "") if same_provider else ""
        current_base_url = (live.base_url or "") if same_provider else ""
        fallback_api_key, fallback_base_url = _static_credential_pair(
            same_provider, current_api_key, current_base_url, saved
        )
        base_url = supplied_base_url if base_url_supplied else fallback_base_url
        endpoint_changed = base_url_supplied and not _same_endpoint(
            _effective_imagegen_endpoint(provider, base_url),
            _effective_imagegen_endpoint(provider, fallback_base_url),
        )
        api_key = supplied_api_key if api_key_supplied else "" if endpoint_changed else fallback_api_key

    overrides: dict[str, str] = {
        "provider": provider,
        "model": model,
        "size": size,
        "api_key": api_key,
        "base_url": base_url,
    }

    try:
        _reconfigure_imagegen(services, overrides)
        await services.imagegen_runtime_config.replace(**overrides)
        if provider != "supergrok" and (api_key_supplied or base_url_supplied or api_key or base_url):
            await services.imagegen_credentials.replace_static(provider, api_key=api_key, base_url=base_url)
    except Exception:
        # As in _set_model: keep a traceback so a genuine bug is not masked by the
        # generic client error. Never log the key/base_url themselves.
        logger.exception("admin_set_imagegen failed (provider=%s)", provider)
        return _error("set_failed", i18n)
    return await _config_frame(services)


async def _imagegen_status(services: Services) -> dict[str, Any]:
    saved = await services.imagegen_credentials.providers()
    status = describe_imagegen_settings(services.settings.imagegen, configured=services.imagegen is not None)
    status["saved_providers"] = saved
    return status


def _live_llm_settings(services: Services) -> Any:
    """Return the effective mutable LLM settings, including unmasked credentials."""
    live = getattr(services.llm, "settings", None)
    return live.llm if live is not None else services.settings.llm


def _provider_identity(provider: str) -> str:
    return canonical_subscription_provider((provider or "").casefold())


def _same_endpoint(left: str, right: str) -> bool:
    """Compare endpoint spellings without treating a trailing slash as a move."""
    return (left or "").strip().rstrip("/") == (right or "").strip().rstrip("/")


def _effective_llm_endpoint(provider: str, base_url: str) -> str:
    """Resolve the endpoint a preset-backed LLM actually uses.

    Admin config returns this effective URL.  When a client sends that value
    back unchanged, compare it with the same effective fallback instead of the
    raw empty setting; otherwise a harmless round-trip looks like an endpoint
    move and drops the provider's API key.
    """
    provider = (provider or "").casefold()
    default = "https://api.openai.com/v1" if provider == "openai" else PRESETS.get(provider, "")
    return (base_url or default).strip()


def _effective_imagegen_endpoint(provider: str, base_url: str) -> str:
    """Resolve the endpoint an image-generation preset actually uses."""
    preset = IMAGEGEN_PRESETS.get((provider or "").casefold(), {})
    return (base_url or preset.get("base_url", "")).strip()


def _profile_endpoint(provider: str, kind: str, base_url: str) -> str:
    """Resolve the effective credential boundary for a typed model profile."""
    if kind == "image":
        return _effective_imagegen_endpoint(provider, base_url)
    return _effective_llm_endpoint(provider, base_url)


def _static_credential_pair(
    same_provider: bool,
    current_api_key: str,
    current_base_url: str,
    saved: dict[str, str],
) -> tuple[str, str]:
    """Keep a key and its endpoint paired instead of mixing two sources."""
    if same_provider and (current_api_key or current_base_url):
        return current_api_key, current_base_url
    return saved.get("api_key", ""), saved.get("base_url", "")


async def _replace_llm_static_credentials(
    services: Services,
    provider: str,
    *,
    api_key: str,
    base_url: str,
    chat_model: str = "",
) -> None:
    """Replace exact + canonical alias profile fields."""
    canonical = canonical_subscription_provider(provider)
    await services.llm_profiles.replace_static(
        canonical,
        api_key=api_key,
        base_url=base_url,
        chat_model=chat_model,
    )
    if canonical != provider:
        await services.llm_profiles.replace_static(
            provider,
            api_key=api_key,
            base_url=base_url,
            chat_model=chat_model,
        )


async def _saved_llm_credentials(services: Services, provider: str) -> dict[str, str]:
    """Load target-scoped static credentials, with canonical alias fallback."""
    provider = (provider or "").casefold()
    canonical = canonical_subscription_provider(provider)
    canonical_saved = await services.llm_profiles.get(canonical) if canonical != provider else {}
    exact_saved = await services.llm_profiles.get(provider)
    return {**canonical_saved, **exact_saved}


def _describe_llm(services: Services) -> dict[str, str]:
    """The live LLM's display snapshot — from the `MutableLLM` if present, else
    from the (possibly injected) settings. Mirrors `gateway.commands.llm._describe_llm`."""
    describe = getattr(services.llm, "describe", None)
    if callable(describe):
        return describe()
    return describe_settings(services.settings.llm)


def _reconfigure_llm(services: Services, overrides: dict[str, str]) -> bool:
    """Hot-reconfigure the `MutableLLM` if present (else the override is still
    persisted and applies on restart). Mirrors `gateway.commands.llm._reconfigure_llm`."""
    apply = getattr(services.llm, "apply", None)
    if callable(apply):
        apply(overrides)
        return True
    return False


def _reconfigure_imagegen(services: Services, overrides: dict[str, str]) -> None:
    effective = apply_imagegen_overrides(services.settings, overrides)
    candidate = build_imagegen(effective, credentials=services.llm_profiles)
    # Publish the new settings/client as one synchronous step only after the
    # candidate was constructed successfully.  In particular, a raising builder
    # must leave the old live settings and client untouched.
    services.settings.imagegen = effective.imagegen
    services.imagegen = candidate


def _valid_image_size(value: str) -> bool:
    parts = value.lower().split("x", 1)
    if len(parts) != 2:
        return False
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 128 <= width <= 4096 and 128 <= height <= 4096


# -- room keys --------------------------------------------------------------


def _keys_frame(keystore: Keystore, caller_room: str, *, minted: dict[str, Any] | None = None) -> dict[str, Any]:
    keys: list[dict[str, Any]] = []
    for entry in keystore.entries(purpose=None):
        if entry.room != caller_room:
            continue
        row: dict[str, Any] = {
            "id": _key_id(entry.key),
            "key_masked": mask_secret(entry.key),
            "room": entry.room,
            "name": entry.name,
            "role": entry.role,
            "purpose": entry.purpose,
            "expires_at": entry.expires_at,
        }
        if entry.purpose == _DEFAULT_PURPOSE:
            # Cleartext invite (join) key — carried ONLY on this keeper-gated,
            # caller-room-scoped admin channel, so the keeper can copy an invite
            # to share. Chat-binding rows deliberately carry no full key: their
            # token is a different credential, and the binding identity is
            # already visible in `key_masked`.
            row["key"] = entry.key
        keys.append(row)
    frame: dict[str, Any] = {"type": "admin_keys", "keys": keys}
    if minted is not None:
        frame["minted"] = minted
    return frame


def _key_id(key: str) -> str:
    """Stable, non-secret handle for admin mutations over the wire."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _chat_binding_id(identity: str) -> str:
    return f"chat:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


def _resolve_key(keystore: Keystore, key_id: str) -> str | None:
    for entry in keystore.entries(purpose=None):
        if _key_id(entry.key) == key_id:
            return entry.key
    return None


def _mint_key(keystore: Keystore, caller_room: str, frame: dict[str, Any], i18n: I18n) -> dict[str, Any]:
    requested_room = str(frame.get("room") or caller_room).strip()
    if not caller_room or requested_room != caller_room:
        return _error("forbidden", i18n)
    room = caller_room
    name = str(frame.get("name") or "").strip()
    purpose = str(frame.get("purpose") or "join").strip()
    if purpose not in {"join", "chat_bind"}:
        return _error("bad_request", i18n)
    role = str(frame.get("role") or ("keeper" if purpose == "chat_bind" else "player")).strip()
    if purpose == "chat_bind" and role != _KEEPER_ROLE:
        return _error("bad_request", i18n)
    expires_at: float | None = None
    if purpose == "chat_bind":
        try:
            expires_at = time.time() + int(frame.get("expires_in") or 600)
        except (TypeError, ValueError):
            return _error("bad_request", i18n)

    with keystore.persisted_mutation():
        key = keystore.add(
            room=room,
            name=name,
            role=role,
            purpose=purpose,
            expires_at=expires_at,
        )
    entry = keystore.get(key, purpose=None)
    assert entry is not None  # just added
    # The full key travels once, here, so the keeper can copy it; list views mask.
    minted = {
        "key": key,
        "room": entry.room,
        "name": entry.name,
        "role": entry.role,
        "purpose": entry.purpose,
        "expires_at": entry.expires_at,
    }
    return _keys_frame(keystore, caller_room, minted=minted)


def _last_keeper_error(i18n: I18n) -> dict[str, Any]:
    """Anti-lockout: a room's last keeper key can never be demoted or deleted.

    The TUI's KeeperKeys form doubles as mint + edit surface; a stale role in the
    form has already demoted the bootstrap keeper key in the wild (the room then
    permanently loses every keeper surface). Refusing the transition server-side
    makes lockout impossible even if a client misbehaves.
    """
    return _error("last_keeper", i18n)


def _keeper_count(keystore: Keystore, room: str) -> int:
    # Join keys only (the entries() default): a pending keeper chat_bind token cannot
    # authenticate a connection, so it must not count toward "the room still has a keeper".
    return sum(1 for e in keystore.entries() if e.room == room and e.role == "keeper")


def _update_key(keystore: Keystore, caller_room: str, frame: dict[str, Any], i18n: I18n) -> dict[str, Any]:
    key_id = str(frame.get("id") or "").strip()
    updates: dict[str, str] = {}
    if "room" in frame:
        room = str(frame.get("room") or "").strip()
        if not room:
            return _error("bad_request", i18n)
        if room != caller_room:  # and never move a key OUT of the caller's room
            return _error("forbidden", i18n)
        updates["room"] = room
    if "name" in frame:
        updates["name"] = str(frame.get("name") or "").strip()
    if "role" in frame:
        role = str(frame.get("role") or "").strip()
        if role not in {"player", "keeper"}:
            return _error("bad_request", i18n)
        updates["role"] = role
    if not updates:
        return _error("bad_request", i18n)

    with keystore.persisted_mutation():
        # Resolve and authorize only after persisted_mutation has reloaded the authoritative
        # on-disk snapshot while holding its cross-process lock. Checking before the lock would
        # allow another process to move this key between rooms in the intervening window.
        key = _resolve_key(keystore, key_id)
        if key is None:
            return _error("not_found", i18n)
        entry = keystore.get(key, purpose=None)
        if entry is None or entry.room != caller_room:
            return _error("forbidden", i18n)
        # Never demote the room's last keeper key: the keeper surface is the only
        # way to mint new keeper keys, so losing it is a permanent lockout.
        if entry.role == "keeper" and updates.get("role", "keeper") != "keeper":
            if _keeper_count(keystore, caller_room) <= 1:
                return _last_keeper_error(i18n)
        keystore.update(key, **updates)
    return _keys_frame(keystore, caller_room)


def _delete_key(keystore: Keystore, caller_room: str, frame: dict[str, Any], i18n: I18n) -> dict[str, Any]:
    key_id = str(frame.get("id") or "").strip()
    with keystore.persisted_mutation():
        key = _resolve_key(keystore, key_id)
        if key is None:
            return _error("not_found", i18n)
        entry = keystore.get(key, purpose=None)
        if entry is None or entry.room != caller_room:
            return _error("forbidden", i18n)
        # Same anti-lockout rule as update: deleting the last keeper key would
        # strand the room without any way to recover keeper access.
        if entry.role == "keeper" and _keeper_count(keystore, caller_room) <= 1:
            return _last_keeper_error(i18n)
        keystore.remove(key)
    return _keys_frame(keystore, caller_room)


async def _delete_room(
    services: Services,
    keystore: Keystore,
    caller_room: str,
    frame: dict[str, Any],
    i18n: I18n,
) -> dict[str, Any]:
    room = str(frame.get("room") or "").strip()
    if not room:
        return _error("bad_request", i18n)
    if room != caller_room:  # a keeper can only delete its OWN room
        return _error("forbidden", i18n)
    with keystore.persisted_mutation():
        removed = keystore.remove_room(room)
        if removed <= 0:
            return _error("not_found", i18n)
    await clear_keeper_bindings_for_room(services.store, room)
    await clear_bindings_for_session(services.store, session_key_for_room(room))
    return _keys_frame(keystore, caller_room)


async def _export_room(
    services: Services,
    keystore: Keystore,
    caller_room: str,
    frame: dict[str, Any],
    i18n: I18n,
) -> dict[str, Any]:
    room = str(frame.get("room") or "").strip()
    if not room:
        return _error("bad_request", i18n)
    if room != caller_room:  # a keeper can only export its OWN room
        return _error("forbidden", i18n)
    path = str(frame.get("path") or "").strip()
    try:
        return _room_op_frame("export", await export_room(services, keystore, room, path))
    except Exception:
        return _error("op_failed", i18n)


async def _import_room(
    services: Services,
    keystore: Keystore,
    caller_room: str,
    frame: dict[str, Any],
    i18n: I18n,
) -> dict[str, Any]:
    path = str(frame.get("path") or "").strip()
    if not path:
        return _error("bad_request", i18n)
    # A named target room must be the caller's own; the snapshot is always imported INTO the
    # caller's room, and `import_room` additionally requires the file to be a backup OF it.
    room = str(frame.get("room") or "").strip()
    if room and room != caller_room:
        return _error("forbidden", i18n)
    try:
        return _room_op_frame("import", await import_room(services, keystore, path, expected_room=caller_room))
    except Exception:
        return _error("op_failed", i18n)


async def _delete_room_data(
    services: Services,
    keystore: Keystore,
    caller_room: str,
    frame: dict[str, Any],
    i18n: I18n,
    *,
    hub: Any = None,
) -> dict[str, Any]:
    room = str(frame.get("room") or "").strip()
    if not room:
        return _error("bad_request", i18n)
    if room != caller_room:  # a keeper can only wipe its OWN room
        return _error("forbidden", i18n)

    backup = frame.get("backup", True) is not False
    path = str(frame.get("path") or "").strip()
    backup_path = ""
    try:
        if backup:
            backup_result = await export_room(services, keystore, room, path)
            backup_path = str(backup_result.get("path") or "")
        # The hub rides along so a DIRECT caller drops the room's in-process turn lock
        # with the room (M23 WS1). This path is not one: the session layer serializes
        # destructive admin frames under that very lock, the in-op disposal declines on
        # a held lock, and net/session disposes right after the lock releases.
        result = await delete_room_data(services, keystore, room, hub=hub)
        await clear_keeper_bindings_for_room(services.store, room)
        await clear_bindings_for_session(services.store, session_key_for_room(room))
    except Exception:
        return _error("op_failed", i18n)
    if backup_path:
        result["path"] = backup_path
    return _room_op_frame("delete", result)


async def _reset_room(
    services: Services,
    keystore: Keystore,
    caller_room: str,
    frame: dict[str, Any],
    i18n: I18n,
) -> dict[str, Any]:
    """Wipe one room's campaign state in place — the button behind an in-place
    campaign restart. Unlike ``_delete_room_data`` it takes NO backup and removes
    NO keys/bindings, so the room's members stay connected and re-provisioning is
    unnecessary (this is why ``admin_reset_room`` is deliberately absent from the
    member-eviction set above)."""
    room = str(frame.get("room") or "").strip()
    if not room:
        return _error("bad_request", i18n)
    if room != caller_room:  # a keeper can only reset its OWN room
        return _error("forbidden", i18n)
    scope = str(frame.get("scope") or "story").strip().casefold()
    if scope not in RESET_SCOPES:
        return _error("bad_request", i18n)
    try:
        result = await reset_room_state(services, chat_key_for_room(room), scope=scope, keystore=keystore)
    except Exception:
        return _error("op_failed", i18n)
    result["room"] = room
    return _room_op_frame("reset", result)


async def _update_server(services: Services, i18n: I18n) -> dict[str, Any]:
    """Run the operator-configured self-update command, then re-exec into the new code.

    Keeper-gated (like every admin frame). The command is `services.settings.tui.update_command`
    — the operator's own, never client input — and is a no-op unless configured. On success the
    server schedules a re-exec so the client should expect a brief disconnect + reconnect."""
    command = (services.settings.tui.update_command or "").strip()
    if not command:
        return _error("not_configured", i18n)
    from net.updater import run_update_command, schedule_reexec

    try:
        result = await run_update_command(command)
    except Exception:
        logger.exception("server self-update failed to run")
        return _error("op_failed", i18n)
    if not result.ok:
        return {"type": "admin_update", "status": "failed", "output": result.output}
    schedule_reexec()
    return {"type": "admin_update", "status": "restarting", "output": result.output}


def _room_op_frame(action: str, result: dict[str, Any]) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "admin_room_op",
        "action": action,
        "room": str(result.get("room") or ""),
        "keys": int(result.get("keys") or 0),
        "documents": int(result.get("documents") or 0),
        "room_state_rows": int(result.get("room_state_rows") or 0),
        "store_rows": int(result.get("store_rows") or 0),
        "vector_points": int(result.get("vector_points") or 0),
        "media_files": int(result.get("media_files") or 0),
    }
    path = str(result.get("path") or "")
    if path:
        frame["path"] = path
    scope = result.get("scope")
    if scope:
        frame["scope"] = str(scope)
    return frame


# -- KP skills (Layer B.1/B.2) ----------------------------------------------


async def _skills_frame(
    services: Services, caller_room: str, i18n: I18n, frame: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Answer `admin_list_skills`/a fresh post-`admin_enable_skill` reply: every discoverable
    skill (`core.skills.available_skills`), each marked `enabled` per the CALLER'S room.

    `name`/`description` follow the caller's locale when a skill ships `name-zh` /
    `description-zh` frontmatter (falling back to the English fields). The locale comes from
    the request frame when present — a client can pin its own UI language independently of the
    server's — otherwise the server locale applies.
    """
    requested = str((frame or {}).get("locale") or "").strip()
    locale = requested or i18n.locale
    chat_key = chat_key_for_room(caller_room)
    enabled_ids = set(await get_enabled_skills(services.store, chat_key))
    use_zh = locale == "zh"
    skills = [
        {
            "id": skill.id,
            "name": (skill.name_zh if use_zh and skill.name_zh else skill.name),
            "description": (skill.description_zh if use_zh and skill.description_zh else skill.description),
            "content_rating": skill.content_rating,
            "enabled": skill.id in enabled_ids,
        }
        for skill in available_skills()
    ]
    return {"type": "admin_skills", "skills": skills}


async def _enable_skill(services: Services, caller_room: str, frame: dict[str, Any], i18n: I18n) -> dict[str, Any]:
    skill_id = str(frame.get("id") or "").strip()
    known_ids = {skill.id for skill in available_skills()}
    if not skill_id or skill_id not in known_ids:
        return _error("bad_request", i18n)

    chat_key = chat_key_for_room(caller_room)
    await toggle_enabled_skill(services.store, chat_key, skill_id, on=bool(frame.get("on")))
    return await _skills_frame(services, caller_room, i18n, frame)


# -- rule systems (Layer A) ---------------------------------------------------


def _rules_frame() -> dict[str, Any]:
    """Answer `admin_list_rules`: every discoverable rule system
    (`core.rulepacks.available_systems`), each marked `built_in` per
    `core.rulepacks.built_in_rulepack_ids` (a generated/user-installed pack is `False`)."""
    built_in = built_in_rulepack_ids()
    systems = [{"id": system_id, "built_in": system_id in built_in} for system_id in available_systems()]
    return {"type": "admin_rules", "systems": systems}


# -- self-extension forge (Layer B.3) ----------------------------------------

_FORGE_KINDS: frozenset[str] = frozenset({"skill", "rule", "module", "pack", "module_prompt"})


async def _generate(
    services: Services,
    caller_room: str,
    fs: FsAdapter | None,
    frame: dict[str, Any],
    i18n: I18n,
    *,
    emit_frame: Any = None,
) -> dict[str, Any]:
    """Answer `admin_generate`: run the matching `agent.forge` engine and reply
    `admin_generated`. Never `eval`/`exec`s anything — see `agent.forge`'s module docstring;
    this is only the wire-level dispatch to it, mirroring the gated `generate_*` KP tools
    (`agent.kp_tools_forge.ForgeTools`) but without requiring a forge skill to be enabled (the
    admin surface is already keeper-gated by construction)."""
    kind = str(frame.get("kind") or "").strip()
    if kind not in _FORGE_KINDS:
        return _error("bad_request", i18n)
    description = str(frame.get("description") or "").strip()
    requested_locale = str(frame.get("locale") or "").strip()
    generation_i18n = i18n.with_locale(requested_locale) if requested_locale in {"en", "zh"} else i18n
    room_chat_key = chat_key_for_room(caller_room)
    request_id = str(frame.get("request_id") or "").strip()
    if kind == "module_prompt":
        try:
            prompt_request = json.loads(description)
        except (json.JSONDecodeError, TypeError):
            return _error("bad_request", i18n)
        if not isinstance(prompt_request, dict):
            return _error("bad_request", i18n)
        idea = prompt_request.get("idea")
        mode = prompt_request.get("mode")
        rule_strategy = prompt_request.get("rule_strategy", "")
        room_system = prompt_request.get("room_system", "")
        if not isinstance(idea, str) or mode not in {"suggest", "rewrite"}:
            return _error("bad_request", i18n)
        if not isinstance(rule_strategy, str) or not isinstance(room_system, str):
            return _error("bad_request", i18n)
        if mode == "rewrite" and not idea.strip():
            return _error("bad_request", i18n)
        result = await generate_module_prompt(
            services,
            idea.strip(),
            mode=mode,
            rule_strategy=rule_strategy,
            room_system=room_system,
            locale=generation_i18n.locale,
            chat_key=room_chat_key,
        )
        return _generated_frame(kind, result, request_id=request_id)
    if not description:
        return _error("bad_request", i18n)
    if kind == "skill":
        result = await generate_and_install_skill(services, description, chat_key=room_chat_key)
        return _generated_frame(kind, result)
    if kind == "rule":
        result = await generate_and_install_rulepack(services, description, chat_key=room_chat_key)
        return _generated_frame(kind, result)

    # module / pack: a long generation (world card + optional media + companion skill/rulepack can
    # take minutes). Run it in the BACKGROUND, stream stage progress to the calling keeper, and
    # push the final result when done — so the keeper's UI is not blocked on a spinner for the
    # whole pipeline. `emit_frame` is the connection's `send_frame` (provided by the session);
    # without it (a test caller), we fall back to a synchronous await so the reply still lands.
    ctx = AgentCtx(
        chat_key=room_chat_key,
        user_id="keeper",
        platform="tui",
        locale=generation_i18n.locale,
        fs=fs,
        extra={"role": _KEEPER_ROLE},
    )

    async def _progress(stage: str, detail: str = "") -> None:
        if emit_frame is not None:
            frame = {"type": "admin_generate_progress", "kind": kind, "stage": stage, "detail": detail}
            try:
                await emit_frame(frame)
            except Exception:  # noqa: BLE001 — a dead connection must not fail the generation
                pass
        # Persist the in-flight stage so a refreshed/reconnected keeper still sees the running
        # generation in the module library (`module_admin._list` merges this row) — progress that
        # only lived on the wire vanished on every refresh.
        try:
            await services.store.state_set(
                chat_key_for_room(caller_room),
                "generation_progress",
                json.dumps({"kind": kind, "stage": stage, "detail": detail}, ensure_ascii=False),
            )
        except Exception:  # noqa: BLE001 — progress persistence must never fail a generation
            pass

    async def _run() -> ForgeResult:
        if kind == "pack":
            return await generate_and_install_pack_module(
                services,
                ctx,
                description,
                media=_option_list(frame.get("options"), "media"),
                companion=_option_list(frame.get("options"), "companion"),
                progress=_progress,
                auto_import=False,
                extends_base=str(_option_value(frame.get("options"), "extends") or ""),
                system=str(_option_value(frame.get("options"), "system") or ""),
            )
        return await generate_and_install_module(
            services,
            ctx,
            description,
            media=_option_list(frame.get("options"), "media"),
            companion=_option_list(frame.get("options"), "companion"),
            progress=_progress,
            auto_import=False,
        )

    import asyncio

    if emit_frame is not None:
        asyncio.get_running_loop().create_task(_finish_generation(_run(), kind, emit_frame, services, caller_room))
        return {"type": "admin_generate_started", "kind": kind}
    result = await _run()
    return _generated_frame(kind, result)


async def _finish_generation(
    coro: Any,
    kind: str,
    emit_frame: Any,
    services: Services | None = None,
    caller_room: str = "",
) -> None:
    """Await a background forge generation and push its `admin_generated` result to the caller.
    Best-effort: a dead connection just loses the result frame; the persisted generation-progress
    row is cleared either way."""
    try:
        result = await coro
        if emit_frame is not None:
            await emit_frame(_generated_frame(kind, result))
    except Exception:  # noqa: BLE001 — a background generation must never crash the loop
        pass
    finally:
        if services is not None and caller_room:
            try:
                await services.store.state_delete(chat_key_for_room(caller_room), "generation_progress")
            except Exception:  # noqa: BLE001 — clearing progress is best-effort
                pass


def _option_list(options: Any, key: str) -> list[str] | None:
    """Pull one string list out of the additive `options` field on `admin_generate` (protocol
    2.5). Anything that is not a dict holding a list of strings degrades to `None` -- unknown or
    malformed ids are ignored downstream (`agent.forge._normalize_option_ids`), never an error."""
    if not isinstance(options, dict):
        return None
    value = options.get(key)
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, str)]


def _option_value(options: Any, key: str) -> str | None:
    """Pull one scalar string out of `options` (e.g. ``extends``) — `None` when absent or not a
    non-empty string."""
    if not isinstance(options, dict):
        return None
    value = options.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _generated_frame(
    kind: str,
    result: ForgeResult,
    *,
    request_id: str = "",
) -> dict[str, Any]:
    frame = {
        "type": "admin_generated",
        "kind": kind,
        "ok": result.ok,
        "id": result.skill_id,
        "name": result.name,
        "error": result.error,
        # `detail` carries the per-room install outcome — for kind="module" it is the ONLY signal
        # of whether the generated module actually landed in the room's knowledge pool (`ok` merely
        # means a valid module was authored + written). Empty for skill/rule (no per-room step).
        "detail": result.detail,
    }
    if request_id:
        frame["request_id"] = request_id
    return frame


def _error(code: str, i18n: I18n) -> dict[str, Any]:
    return {"type": "admin_error", "code": code, "message": i18n.t(f"tui.admin.error.{code}")}
