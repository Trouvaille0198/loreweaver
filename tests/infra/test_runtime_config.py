"""Tests for infra.runtime_config (RuntimeConfig persistence + apply_overrides)
and the infra.providers.MutableLLM runtime-swappable wrapper. All offline:
RuntimeConfig round-trips through an in-memory/temp-file Store and MutableLLM is
driven with a stub builder so no provider client is ever constructed."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.services import build_services
from infra.config import ImageGenSettings, LLMSettings, Settings
from infra.imagegen import apply_imagegen_overrides
from infra.llm import FakeLLM
from infra.providers import MutableLLM
from infra.runtime_config import (
    CREDENTIALS_KEY,
    LLM_PROFILES_KEY,
    CredentialBook,
    ImageGenRuntimeConfig,
    RuntimeConfig,
    apply_overrides,
    migrate_llm_credentials,
)
from infra.store import Store


def _settings(**llm) -> Settings:
    return Settings(llm=LLMSettings(**llm))


# ---------------------------------------------------------------------------
# apply_overrides — pure overlay
# ---------------------------------------------------------------------------


def test_apply_overrides_overlays_only_known_nonempty_fields_and_is_pure():
    base = _settings(provider="openai", chat_model="gpt-4o", api_key="env-key")

    out = apply_overrides(
        base,
        {"provider": "deepseek", "chat_model": "deepseek-chat", "bogus": "x", "base_url": ""},
    )

    assert out.llm.provider == "deepseek"
    assert out.llm.chat_model == "deepseek-chat"
    assert out.llm.api_key == "env-key"  # untouched (no override supplied)
    assert not hasattr(out.llm, "bogus")  # unknown key ignored
    # base is unchanged (pure)
    assert base.llm.provider == "openai"
    assert base.llm.chat_model == "gpt-4o"


def test_apply_overrides_empty_returns_independent_copy():
    base = _settings(provider="openai")

    out = apply_overrides(base, {})

    assert out is not base
    assert out.llm is not base.llm
    assert out.llm.provider == "openai"


def test_apply_overrides_explicit_empty_clears_base_credentials():
    base = _settings(api_key="env-key", base_url="https://env.example/v1")

    out = apply_overrides(base, {"api_key": "", "base_url": ""})

    assert out.llm.api_key == ""
    assert out.llm.base_url == ""
    assert base.llm.api_key == "env-key"


# ---------------------------------------------------------------------------
# RuntimeConfig — Store round-trips
# ---------------------------------------------------------------------------


async def test_runtime_config_set_get_clear_roundtrip():
    store = Store(":memory:")
    rc = RuntimeConfig(store)
    assert await rc.get() == {}

    merged = await rc.set(provider="anthropic", chat_model="claude-x")
    assert merged == {"provider": "anthropic", "chat_model": "claude-x"}

    # a fresh instance over the same store observes the persisted value
    assert await RuntimeConfig(store).get() == {"provider": "anthropic", "chat_model": "claude-x"}

    # set merges rather than replaces
    await rc.set(api_key="sk-1")
    assert await rc.get() == {"provider": "anthropic", "chat_model": "claude-x", "api_key": "sk-1"}

    await rc.clear()
    assert await rc.get() == {}
    assert await RuntimeConfig(store).get() == {}


async def test_runtime_config_set_skips_empty_and_rejects_unknown_field():
    rc = RuntimeConfig(Store(":memory:"))

    assert await rc.set(provider="openai", chat_model="") == {"provider": "openai"}

    with pytest.raises(ValueError):
        await rc.set(temperature="0.5")  # not an OVERRIDE_FIELDS key


async def test_runtime_config_replace_persists_complete_snapshot_with_empty_fields():
    store = Store(":memory:")
    rc = RuntimeConfig(store)
    await rc.set(provider="openai", api_key="old", base_url="https://old.example/v1")

    replaced = await rc.replace(provider="chatgpt", api_key="", base_url="")

    assert replaced == {"provider": "chatgpt", "api_key": "", "base_url": ""}
    assert await RuntimeConfig(store).get() == replaced
    # Legacy merge semantics remain: empty values are ignored and old fields stay.
    assert await rc.set(chat_model="", npc_model="npc-x") == {
        **replaced,
        "npc_model": "npc-x",
    }
    with pytest.raises(ValueError):
        await rc.replace(temperature="0.5")


async def test_imagegen_runtime_replace_and_overlay_preserve_explicit_empty_fields():
    store = Store(":memory:")
    rc = ImageGenRuntimeConfig(store)
    await rc.set(provider="openai", api_key="old", base_url="https://old.example/v1")

    replaced = await rc.replace(provider="supergrok", api_key="", base_url="")

    assert replaced == {"provider": "supergrok", "api_key": "", "base_url": ""}
    assert await ImageGenRuntimeConfig(store).get() == replaced
    base = Settings(
        imagegen=ImageGenSettings(
            provider="openai",
            api_key="env-key",
            base_url="https://env.example/v1",
        )
    )
    out = apply_imagegen_overrides(base, replaced)
    assert out.imagegen.provider == "supergrok"
    assert out.imagegen.api_key == ""
    assert out.imagegen.base_url == ""


def test_runtime_config_load_sync_reads_persisted_file(tmp_path):
    db = tmp_path / "rc.db"
    store = Store(str(db))
    asyncio.run(RuntimeConfig(store).set(provider="deepseek", chat_model="deepseek-chat"))
    store.close()

    fresh = RuntimeConfig(Store(str(db)))
    assert fresh.load_sync() == {"provider": "deepseek", "chat_model": "deepseek-chat"}


def test_runtime_config_load_sync_is_empty_for_memory_and_missing_file(tmp_path):
    assert RuntimeConfig(Store(":memory:")).load_sync() == {}
    assert RuntimeConfig(Store(str(tmp_path / "absent.db"))).load_sync() == {}


def test_build_services_resolves_selected_embedding_profile_on_restart(tmp_path):
    db = tmp_path / "embedding-profile.db"
    store = Store(str(db))
    profile_id = "openai::embedding::text-embedding-3-large"
    asyncio.run(
        CredentialBook(store, key=LLM_PROFILES_KEY).replace_static(
            profile_id,
            api_key="sk-embedding",
            base_url="https://embedding.example/v1",
            chat_model="text-embedding-3-large",
            kind="embedding",
            embedding_dim="3072",
        )
    )
    asyncio.run(
        RuntimeConfig(store).replace(
            embedding_profile=profile_id,
            embedding_model="text-embedding-3-large",
            embedding_dim="3072",
        )
    )
    store.close()

    services = build_services(
        Settings(data_dir=str(tmp_path)),
        llm=FakeLLM(script=[]),
        db_path=str(db),
    )
    embedding_settings = services.embeddings._settings
    assert embedding_settings.api_key == "sk-embedding"
    assert embedding_settings.base_url == "https://embedding.example/v1"
    assert embedding_settings.embedding_model == "text-embedding-3-large"
    assert embedding_settings.embedding_dim == 3072
    services.store.close()

# ---------------------------------------------------------------------------
# MutableLLM — runtime swap seen by all consumers (shared Settings mutated)
# ---------------------------------------------------------------------------


class _StubLLM:
    """A no-network LLMClient stand-in that records the settings it was built from."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def chat(self, *args, **kwargs):  # pragma: no cover - never invoked here
        raise AssertionError("chat should not be called in these tests")


def test_mutable_llm_reconfigure_swaps_inner_and_mutates_shared_settings():
    settings = _settings(provider="openai", chat_model="gpt-4o", api_key="sk-secretkey-123456")
    built: list[_StubLLM] = []

    def builder(s: Settings) -> _StubLLM:
        stub = _StubLLM(s)
        built.append(stub)
        return stub

    llm = MutableLLM(settings, builder=builder)
    first = llm.inner
    assert first is built[-1]

    llm.apply({"provider": "deepseek", "chat_model": "deepseek-chat"})

    assert llm.inner is not first  # inner client rebuilt
    # the SHARED settings object was mutated in place (module_init / actors see it)
    assert settings.llm.provider == "deepseek"
    assert settings.llm.chat_model == "deepseek-chat"

    info = llm.describe()
    assert info["provider"] == "deepseek"
    assert info["chat_model"] == "deepseek-chat"
    assert info["api_key"].startswith("sk-s") and info["api_key"].endswith("3456")
    assert "secretkey" not in info["api_key"]  # masked


def test_mutable_llm_apply_empty_reverts_to_pristine_baseline():
    settings = _settings(provider="openai", chat_model="gpt-4o")
    llm = MutableLLM(settings, builder=_StubLLM)

    llm.apply({"provider": "anthropic", "chat_model": "claude-x"})
    assert settings.llm.provider == "anthropic"

    llm.apply({})  # reset -> back to the env baseline captured at construction
    assert settings.llm.provider == "openai"
    assert settings.llm.chat_model == "gpt-4o"


def test_mutable_llm_failed_reconfigure_keeps_live_settings_and_client():
    settings = _settings(provider="openai", chat_model="gpt-4o")

    def builder(candidate: Settings) -> _StubLLM:
        if candidate.llm.provider == "anthropic":
            raise RuntimeError("provider unavailable")
        return _StubLLM(candidate)

    llm = MutableLLM(settings, builder=builder)
    original_inner = llm.inner

    with pytest.raises(RuntimeError, match="provider unavailable"):
        llm.apply({"provider": "anthropic", "chat_model": "claude-x"})

    assert llm.inner is original_inner
    assert settings.llm.provider == "openai"
    assert settings.llm.chat_model == "gpt-4o"


def test_migrate_llm_credentials_merges_legacy_into_unified_book(tmp_path):
    """The one-shot boot migration folds a legacy `runtime_config.credentials`
    book into the unified `runtime_config.llm_profiles` book and drops the legacy
    key — a chat_model-ed entry lands under `provider::model`, an OAuth bare
    entry stays keyed by provider, existing typed profiles are untouched."""
    store = Store(tmp_path / "cfg.db")
    asyncio.run(
        store.set(
            user_key="",
            store_key=CREDENTIALS_KEY,
            value=json.dumps(
                {
                    "deepseek": {
                        "api_key": "sk-deep",
                        "chat_model": "deepseek-chat",
                        "base_url": "https://api.deepseek.com/v1",
                    },
                    "supergrok": {
                        "access_token": "at",
                        "refresh_token": "rt",
                        "expires_at": "123",
                        "account_id": "acct",
                    },
                }
            ),
        )
    )
    asyncio.run(
        store.set(
            user_key="",
            store_key=LLM_PROFILES_KEY,
            value=json.dumps({"openai::gpt-4o": {"api_key": "sk-open", "chat_model": "gpt-4o", "kind": "chat"}}),
        )
    )

    assert migrate_llm_credentials(store) == 2

    merged = json.loads(asyncio.run(store.get(user_key="", store_key=LLM_PROFILES_KEY)))
    assert merged["deepseek::deepseek-chat"]["api_key"] == "sk-deep"
    assert merged["deepseek::deepseek-chat"]["chat_model"] == "deepseek-chat"
    assert merged["supergrok"]["access_token"] == "at"  # bare provider key preserved
    assert merged["openai::gpt-4o"]["api_key"] == "sk-open"  # untouched typed profile
    # The legacy key is gone and the migration is idempotent.
    assert asyncio.run(store.get(user_key="", store_key=CREDENTIALS_KEY)) is None
    assert migrate_llm_credentials(store) == 0
