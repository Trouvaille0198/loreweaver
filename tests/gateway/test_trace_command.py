"""Tests for the `.trace` command — the per-room tool-call probe toggle.

Keeper-only on/off, open status read, isolated per room (one table's toggle never
touches another), persisted via the server-level kv key (`runtime_config.tool_trace`,
a {room: path} map) so the choices survive restarts, and restored by `build_services`
at boot (`persisted_trace_paths_sync`).

Deterministic and offline: FakeLLM/FakeEmbeddings, fresh in-memory store per test.
"""

from __future__ import annotations

from pathlib import Path

from agent.services import build_services
from agent.tool_trace import (
    TOOL_TRACE_KV_KEY,
    disable_tool_trace,
    tool_trace_enabled,
)
from gateway.commands import CommandRouter
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM

ROOM_A = "cli:dm:trace-a"
ROOM_B = "cli:dm:trace-b"


def _services(tmp_path):
    return build_services(
        Settings(data_dir=str(tmp_path)),
        llm=FakeLLM(script=[]),
        embeddings=FakeEmbeddings(8),
        db_path=str(Path(tmp_path) / "loreweaver.db"),
    )


def _keeper_ctx(chat_key: str):
    from agent.context import AgentCtx

    return AgentCtx(chat_key=chat_key, user_id="k1", platform="cli", locale="en")


def _player_ctx(chat_key: str):
    from agent.context import AgentCtx

    return AgentCtx(chat_key=chat_key, user_id="p1", platform="tui", locale="en", extra={"role": "player"})


async def test_trace_bare_reports_off(tmp_path):
    services = _services(str(tmp_path))
    router = CommandRouter(services)

    reply = await router.dispatch(_keeper_ctx(ROOM_A), ".trace")

    assert "OFF" in reply
    assert not tool_trace_enabled(ROOM_A)


async def test_trace_on_persists_and_enables_for_this_room(tmp_path):
    services = _services(str(tmp_path))
    router = CommandRouter(services)

    reply = await router.dispatch(_keeper_ctx(ROOM_A), ".trace on")

    assert "enabled" in reply
    assert tool_trace_enabled(ROOM_A)
    assert not tool_trace_enabled(ROOM_B)  # other rooms untouched
    stored = await services.store.get(user_key="", store_key=TOOL_TRACE_KV_KEY)
    assert stored and ROOM_A in stored and "traces" in stored and "trace-a" in stored


async def test_trace_on_custom_path(tmp_path):
    services = _services(str(tmp_path))
    router = CommandRouter(services)
    target = str(Path(tmp_path) / "probe-a.jsonl")

    reply = await router.dispatch(_keeper_ctx(ROOM_A), f".trace on {target}")

    assert "enabled" in reply
    assert tool_trace_enabled(ROOM_A)
    stored = await services.store.get(user_key="", store_key=TOOL_TRACE_KV_KEY)
    assert stored and target in stored


async def test_trace_is_room_isolated(tmp_path):
    """Room A's toggle never turns Room B's probe on or off."""
    services = _services(str(tmp_path))
    router = CommandRouter(services)
    await router.dispatch(_keeper_ctx(ROOM_A), ".trace on")

    status_b = await router.dispatch(_keeper_ctx(ROOM_B), ".trace")

    assert "OFF" in status_b
    assert not tool_trace_enabled(ROOM_B)
    # 关 A 不影响 B（B 本来就没开），且 A 的键被移除
    await router.dispatch(_keeper_ctx(ROOM_A), ".trace off")
    assert not tool_trace_enabled(ROOM_A)
    stored = await services.store.get(user_key="", store_key=TOOL_TRACE_KV_KEY)
    assert not stored


async def test_trace_off_clears_this_room_only(tmp_path):
    services = _services(str(tmp_path))
    router = CommandRouter(services)
    await router.dispatch(_keeper_ctx(ROOM_A), ".trace on")
    await router.dispatch(_keeper_ctx(ROOM_B), ".trace on")

    reply = await router.dispatch(_keeper_ctx(ROOM_A), ".trace off")

    assert "disabled" in reply
    assert not tool_trace_enabled(ROOM_A)
    assert tool_trace_enabled(ROOM_B)  # B keeps its own toggle
    stored = await services.store.get(user_key="", store_key=TOOL_TRACE_KV_KEY)
    assert stored and ROOM_B in stored and ROOM_A not in stored


async def test_trace_toggle_requires_keeper(tmp_path):
    services = _services(str(tmp_path))
    router = CommandRouter(services)

    reply = await router.dispatch(_player_ctx(ROOM_A), ".trace on")

    assert "keeper" in reply
    assert not tool_trace_enabled(ROOM_A)


async def test_build_services_restores_each_rooms_toggle(tmp_path):
    """`.trace on` persists per room; a fresh build_services over the same DB
    restores exactly the toggles that were on (and only those)."""
    services = _services(str(tmp_path))
    router = CommandRouter(services)
    await router.dispatch(_keeper_ctx(ROOM_A), ".trace on")
    await router.dispatch(_keeper_ctx(ROOM_B), ".trace on")
    await router.dispatch(_keeper_ctx(ROOM_B), ".trace off")
    assert tool_trace_enabled(ROOM_A)
    assert not tool_trace_enabled(ROOM_B)
    # 清运行时状态，模拟冷启动
    disable_tool_trace(ROOM_A)
    assert not tool_trace_enabled(ROOM_A)

    build_services(
        Settings(data_dir=str(tmp_path)),
        llm=FakeLLM(script=[]),
        embeddings=FakeEmbeddings(8),
        db_path=str(Path(tmp_path) / "loreweaver.db"),
    )

    assert tool_trace_enabled(ROOM_A)
    assert not tool_trace_enabled(ROOM_B)
