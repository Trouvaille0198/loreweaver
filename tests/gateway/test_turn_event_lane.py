"""The join-replay event lane (`turn_event_history`) — what goes in, what stays, and what
`.undo` does to it.

Every rule here exists because a reconnecting member used to see a different scene than
the one everyone else watched: typed rolls were never recorded, `.undo` deleted the
restored turn's own rolls, a dice-heavy stretch evicted older turns mid-sequence, and a
hook's refusal could be spoken as an NPC's line.
"""

from __future__ import annotations

import json

from agent.context import AgentCtx
from agent.kp_tools import build_kp_toolset
from agent.services import build_services
from agent.undo import capture, restore
from core.dice_engine import seed_dice
from gateway.commands import CommandRouter
from gateway.hub import Event, RoomHub
from agent.history import DEFAULT_HISTORY_KEY, append_message, load_chain
from gateway.turn import (
    TURN_EVENT_HISTORY_CAP,
    TURN_EVENT_HISTORY_KEY,
    TURN_EVENT_HISTORY_TURNS,
    _npc_events,
    _public_tool_events,
    prune_turn_events,
    record_turn_events,
    run_turn,
)
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.i18n import get_i18n
from infra.llm import ChatResult, FakeLLM, assistant_text, tool_call


class RecordingMember:
    transport = "tui"
    locale = "en"

    def __init__(self, member_id: str, name: str, role: str = "player") -> None:
        self.id = member_id
        self.user_key = f"user:{member_id}"
        self.name = name
        self.role = role
        self.events: list[Event] = []

    async def deliver(self, event: Event) -> None:
        self.events.append(event)


def _services():
    return build_services(Settings(locale="en"), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(8))


async def _lane(services, room: str) -> list[dict]:
    raw = await services.store.state_get(room, TURN_EVENT_HISTORY_KEY)
    return json.loads(raw) if raw else []


# --- the command branch records its PUBLIC rolls, anchored after the last turn --------


async def test_a_typed_roll_joins_the_replay_lane_after_the_last_turn() -> None:
    services = _services()
    room = "tui:group:typed-roll-lane"
    ctx = AgentCtx(chat_key=room, user_id="u1", platform="tui", locale="en")
    router = CommandRouter(services)
    toolset = build_kp_toolset(services)
    await router.dispatch(ctx, ".coc Investigator")
    hub = RoomHub()
    origin = RecordingMember("u1", "Nora")
    await hub.subscribe(room, origin)
    seed_dice(7)

    await run_turn(hub, services, ctx, ".roll 2d6+1", command_router=router, toolset=toolset, origin=origin)

    lane = await _lane(services, room)
    assert len(lane) == 1
    record = lane[0]
    # No AI-Keeper turn has run: the roll is anchored to the ROOT — the top of a replay.
    assert record["turn"] == 0 and record["after_id"] == ""
    assert record["event"]["kind"] == "dice"
    assert record["event"]["data"]["actor"] == "Investigator (Nora)"
    published = next(event for event in origin.events if event.kind == "dice")
    assert record["event"]["data"]["total"] == published.data["total"]


async def test_a_hidden_roll_is_private_and_stays_out_of_the_lane() -> None:
    """A private event is a unicast to one connection; a lane re-broadcast to whoever
    joins next has no place for it."""
    services = _services()
    room = "tui:group:hidden-roll-lane"
    ctx = AgentCtx(chat_key=room, user_id="u1", platform="tui", locale="en")
    router = CommandRouter(services)
    toolset = build_kp_toolset(services)
    await router.dispatch(ctx, ".coc Investigator")
    hub = RoomHub()
    origin = RecordingMember("u1", "Nora")
    await hub.subscribe(room, origin)

    await run_turn(hub, services, ctx, ".rh 1d100", command_router=router, toolset=toolset, origin=origin)

    assert [event.kind for event in origin.events if event.kind == "dice"] == ["dice"]
    assert await _lane(services, room) == []


# --- the window is counted in TURNS ---------------------------------------------------


def test_the_lane_keeps_whole_turns_never_half_of_one() -> None:
    """A flat event cap cut at an arbitrary offset: a run of dice-heavy combat turns
    evicted older turns' rolls and left the oldest surviving turn showing only its last
    few rolls — indistinguishable from "nobody rolled the rest"."""
    records = [
        {"turn": turn, "event": {"kind": "dice", "data": {"n": index}}}
        for turn in range(1, TURN_EVENT_HISTORY_TURNS + 11)
        for index in range(12)
    ]
    kept = prune_turn_events(records)
    turns = sorted({record["turn"] for record in kept})
    # The newest forty turns, every one of them complete.
    assert turns[0] == 11 and turns[-1] == TURN_EVENT_HISTORY_TURNS + 10
    assert all(sum(1 for record in kept if record["turn"] == turn) == 12 for turn in turns)
    # Malformed entries do not survive to be replayed.
    assert prune_turn_events([{"turn": "abc"}, "nope", {"turn": 3, "event": {}}]) == [{"turn": 3, "event": {}}]
    # The flat ceiling is a safety net, not the working bound.
    huge = [{"turn": 1, "event": {}}] * (TURN_EVENT_HISTORY_CAP + 5)
    assert len(prune_turn_events(huge)) == TURN_EVENT_HISTORY_CAP
    # "Newest" is by append order, not by the largest turn number: one imported record
    # with an absurd `turn` is one distinct turn among forty — it must not evict every
    # legitimate record and then swallow every future write.
    poisoned = [{"turn": 10**9, "event": {}}, *({"turn": turn, "event": {}} for turn in range(1, 6))]
    kept = prune_turn_events(poisoned)
    assert [record["turn"] for record in kept] == [10**9, 1, 2, 3, 4, 5]
    assert [record["turn"] for record in prune_turn_events([*kept, {"turn": 6, "event": {}}])][-1] == 6


# --- `.undo` keeps the restored turn's own rolls, drops the abandoned future ----------


async def test_undo_keeps_the_restored_turn_s_rolls_and_drops_what_came_after() -> None:
    """Every roll is recorded the moment it happens, so the turn-boundary snapshot of turn
    N (taken as the turn closes) already holds turn N's own rolls and none of the
    abandoned future's: restoring the snapshot's copy of the lane is exactly right —
    which is why the lane needs no special handling on rewind."""
    services = _services()
    room = "tui:group:undo-lane"
    for turn in (11, 12, 13):
        await append_message(services, room, DEFAULT_HISTORY_KEY, role="user", content=f"do {turn}", turn=turn)
        await record_turn_events(services, room, [Event.dice(actor="A", kind="check", expr="d", total=turn)])
        await append_message(services, room, DEFAULT_HISTORY_KEY, role="assistant", content=f"ok {turn}", turn=turn)
        await capture(services, room, turn)  # as `run_kp_turn` does, at the turn's close
        # A typed roll after the turn closed — the next snapshot's, not this one's.
        await record_turn_events(services, room, [Event.dice(actor="A", kind="roll", expr="d", total=turn * 100)])

    assert await restore(services, room, 12)

    totals = [record["event"]["data"]["total"] for record in await _lane(services, room)]
    assert totals == [11, 1100, 12]  # turn 12's OWN roll survives; the roll typed after it, and turn 13, do not


# --- NPC lines come from the structural channel, never from a tool's return string ---


def test_npc_lines_are_built_from_what_the_tool_emitted_not_from_its_result() -> None:
    i18n = get_i18n("en")
    spoken = {
        "name": "speak_as_npc",
        "arguments": {"npc": "Martha"},
        "result": "Martha (uneasy): I heard the gate.",
        "npc_lines": [{"name": "Martha", "text": "Martha (uneasy): I heard the gate."}],
    }
    assert [event.name for event in _npc_events(spoken, i18n)] == ["Martha"]

    # A hook vetoed the call: the trace carries the refusal as RESULT and `suppressed`.
    vetoed = {
        "name": "speak_as_npc",
        "arguments": {"npc": "Martha"},
        "result": "Tool speak_as_npc was refused by a room hook: not now",
        "suppressed": True,
    }
    assert _npc_events(vetoed, i18n) == []
    # An unknown NPC, a gated tool, a prep-only tool: a result string, no emitted line.
    errored = {"name": "speak_as_npc", "arguments": {"npc": "Nobody"}, "result": "❌ No NPC found matching Nobody"}
    assert _npc_events(errored, i18n) == []
    assert _public_tool_events(errored, "Nora", i18n) == []
    # And a keeper-only call has no public consequences at all.
    keeper = {**spoken, "keeper_only": True}
    assert _public_tool_events(keeper, "Nora", i18n) == []


async def test_speak_as_npc_emits_its_line_on_success_only() -> None:
    """The tool's own contract, end to end: a voiced line lands on the ctx channel; a
    missing NPC returns its message and emits nothing."""
    from agent.kp_tools_npc import NpcTools

    services = _services()
    ctx = AgentCtx(chat_key="tui:group:npc-emit", user_id="u1", platform="tui", locale="en")
    tools = NpcTools(services)
    reply = await tools.speak_as_npc(ctx, npc="Nobody", situation="…")
    assert "Nobody" in reply
    assert ctx.consume_npc_lines() == []


async def test_tool_round_draft_reaches_keeper_only_as_narrative_draft() -> None:
    """A tool round's streamed narration is dropped from the live log (dice-first) but
    kept as a KEEPER-ONLY `narrative_draft` attached to the reply — players never
    receive it, and the draft is persisted with the reply record."""
    draft_text = "美咲的刀锋抵上岩本的喉咙，血珠顺着刀刃滑落。"
    final_text = "骰子落定：突袭失败。岩本反手扣住美咲的手腕。"
    script = [
        ChatResult(content=draft_text, tool_calls=[tool_call("roll_dice", expression="1d100")]),
        assistant_text(final_text),
    ]
    services = build_services(Settings(locale="en"), llm=FakeLLM(script=script), embeddings=FakeEmbeddings(8))
    room = "tui:group:draft-lane"
    ctx = AgentCtx(chat_key=room, user_id="k1", platform="tui", locale="en", extra={"role": "keeper"})
    hub = RoomHub()
    keeper = RecordingMember("k1", "Keeper", role="keeper")
    player = RecordingMember("p1", "Nora", role="player")
    await hub.subscribe(room, keeper)
    await hub.subscribe(room, player)
    router = CommandRouter(services)
    toolset = build_kp_toolset(services)
    seed_dice(7)

    await run_turn(hub, services, ctx, "我突袭他。", command_router=router, toolset=toolset, origin=keeper)

    # The keeper receives the discarded draft keyed to the reply's message id.
    drafts = [event for event in keeper.events if event.kind == "narrative_draft"]
    assert drafts
    assert drafts[-1].data["text"] == draft_text
    reply_event = next(event for event in keeper.events if event.kind == "narrative" and event.text == final_text)
    assert drafts[-1].data["id"] == reply_event.origin_id
    # The player connection never sees it — the hub filters keeper_only events.
    assert not any(event.kind == "narrative_draft" for event in player.events)
    # The draft rides the persisted reply record for rejoin replay.
    chain = await load_chain(services, room, DEFAULT_HISTORY_KEY)
    assert chain[-1]["_lw_draft"] == draft_text
    assert chain[-1]["content"] == final_text


# --- a hidden AI roll (tool hidden=True) is a KEEPER-ONLY frame, live and in the lane ----


async def test_a_hidden_ai_roll_publishes_a_keeper_only_frame_and_stays_in_the_lane() -> None:
    """`roll_dice(hidden=True)` emits a dice payload flagged hidden: the frame
    reaches the keeper ONLY — a player never sees the number or that a roll
    happened — and the replay lane keeps it, flagged, for a keeper's rejoin."""
    services = _services()
    room = "tui:group:hidden-ai-roll"
    i18n = get_i18n()
    keeper = RecordingMember("kp", "Keeper", role="keeper")
    player = RecordingMember("pl", "Nora", role="player")
    hub = RoomHub()
    await hub.subscribe(room, keeper)
    await hub.subscribe(room, player)

    entry = {
        "name": "roll_dice",
        "arguments": {},
        "dice_payloads": [
            {
                "kind": "roll",
                "expr": "1d100",
                "rolls": [42],
                "total": 42,
                "hidden": True,
            }
        ],
    }
    (event,) = _public_tool_events(entry, "Keeper", i18n)
    assert event.kind == "dice"
    assert event.keeper_only is True
    assert event.data["hidden"] is True

    await hub.publish(room, event)
    await record_turn_events(services, room, [event])

    kinds = lambda member: [e.kind for e in member.events if e.kind != "presence"]
    assert kinds(keeper) == ["dice"]
    assert kinds(player) == []
    lane = await _lane(services, room)
    assert len(lane) == 1
    # The flag survives the lane so a keeper's rejoin replays it, a player's never does.
    assert lane[0]["event"]["keeper_only"] is True
    assert lane[0]["event"]["data"]["hidden"] is True


async def test_a_public_ai_roll_reaches_everyone_and_is_not_flagged() -> None:
    """The same payload without the hidden flag stays a plain public roll: no
    keeper_only marker live, no hidden flag in the lane."""
    services = _services()
    room = "tui:group:public-ai-roll"
    i18n = get_i18n()
    keeper = RecordingMember("kp", "Keeper", role="keeper")
    player = RecordingMember("pl", "Nora", role="player")
    hub = RoomHub()
    await hub.subscribe(room, keeper)
    await hub.subscribe(room, player)

    entry = {
        "name": "roll_dice",
        "arguments": {},
        "dice_payloads": [{"kind": "roll", "expr": "1d100", "rolls": [42], "total": 42}],
    }
    (event,) = _public_tool_events(entry, "Keeper", i18n)
    assert event.keeper_only is False
    assert "hidden" not in event.data

    await hub.publish(room, event)
    await record_turn_events(services, room, [event])

    kinds = lambda member: [e.kind for e in member.events if e.kind != "presence"]
    assert kinds(keeper) == ["dice"]
    assert kinds(player) == ["dice"]
    lane = await _lane(services, room)
    assert lane[0]["event"]["keeper_only"] is False
    assert "hidden" not in lane[0]["event"]["data"]
