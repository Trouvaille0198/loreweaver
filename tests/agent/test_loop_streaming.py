"""Reply streaming: in-progress text reaches `on_reply_delta` as epoch/seq slices,
machinery blocks can NEVER stream (fail-closed hold-back), tool-round drafts are
discarded, and the final reply stays authoritative."""

from __future__ import annotations

from agent.context import AgentCtx
from agent.kp_tools import build_kp_toolset
from agent.loop import _ReplyStreamGate, run_kp_turn
from agent.services import build_services
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import ChatResult, FakeLLM, assistant_text, assistant_tools, tool_call


async def _collecting_emitter(frames: list[dict]):
    async def emit(frame: dict) -> None:
        frames.append(frame)

    return emit


async def test_gate_streams_safe_text_and_never_releases_machinery():
    frames: list[dict] = []
    gate = _ReplyStreamGate(await _collecting_emitter(frames))

    gate.begin_round()
    gate.feed("雨声落在窗台上。老周抬起头。\n")
    gate.feed("「你来了。」<De")  # a suspicious opener split across deltas
    gate.feed("ep>\n<use><name>mcp__kp_note</name><args>{\"secret\": \"它不能说谎\"}</args></use>\n</Deep>")
    gate.feed("他把抹布放下。")
    gate.finish_round(discard=False)
    await gate.drain()

    streamed = "".join(frame["text"] for frame in frames)
    assert "雨声落在窗台上" in streamed and "他把抹布放下。" in streamed
    assert "<Deep" not in streamed and "mcp__" not in streamed and "不能说谎" not in streamed
    assert [frame["seq"] for frame in frames] == sorted(frame["seq"] for frame in frames)
    assert all(frame["epoch"] == 1 for frame in frames)


async def test_gate_holds_an_unclosed_suspicious_tail_at_round_end():
    frames: list[dict] = []
    gate = _ReplyStreamGate(await _collecting_emitter(frames))
    gate.begin_round()
    gate.feed("正文安全部分。<UpdateVariable>_.set('真凶'")  # never closes
    gate.finish_round(discard=False)
    await gate.drain()

    streamed = "".join(frame["text"] for frame in frames)
    assert streamed == "正文安全部分。"


async def test_gate_discard_archives_flushed_text_too():
    """A tool round's draft is archived for the keeper IN FULL — including the slices
    already flushed to the streaming client — not just the tail still held back."""
    frames: list[dict] = []
    gate = _ReplyStreamGate(await _collecting_emitter(frames))
    draft = (
        "雨声落在窗台上。老周抬起头，指节在桌沿敲了两下，压低了声音："
        "「那批货今晚走，码头上会有人接应。」\n\n"
        "窗外，港口的灯一盏接一盏地灭了下去。"
    )
    gate.begin_round()
    gate.feed(draft)
    gate.finish_round(discard=True)
    await gate.drain()

    # The long draft (>48 chars with a newline) was flushed to the client…
    assert frames and "".join(frame["text"] for frame in frames) == draft
    # …and the keeper's archived copy still holds every byte of it.
    assert gate.discarded_text() == draft


async def test_max_rounds_finalizer_streams_its_reply(tmp_path):
    """The finalizer produces the player-visible reply on every tool-heavy turn, so it
    must stream through the gate like an ordinary final round — those are exactly the
    turns a player otherwise watches arrive minutes later as one block."""
    final_text = "线索汇拢：刮痕来自船底，而钟声在涨潮时最响。今晚的港口不会安静。"
    script = [
        *[assistant_tools(tool_call("roll_dice", expression="1d100")) for _ in range(12)],
        assistant_text(final_text),  # consumed by the finalizer, not a loop round
    ]
    services = build_services(Settings(locale="zh"), llm=FakeLLM(script=script), embeddings=FakeEmbeddings(16))
    ctx = AgentCtx(chat_key="finalizer-stream-room", user_id="p1", locale="zh")
    frames: list[dict] = []

    async def emit(frame: dict) -> None:
        frames.append(frame)

    result = await run_kp_turn(ctx, services, build_kp_toolset(services), "我调查港口。", on_reply_delta=emit)

    assert result.reply == final_text
    assert frames, "the finalizer reply must stream deltas"
    final_epoch = max(frame["epoch"] for frame in frames)
    reconstructed = "".join(frame["text"] for frame in frames if frame["epoch"] == final_epoch)
    assert reconstructed == final_text


async def test_tool_round_draft_is_fed_back_to_the_model(tmp_path):
    """A tool round's discarded streamed narration is NOT lost from the fiction: it rides
    back into the model's context so the final reply preserves the action/sensory process
    detail (dice-first discards it from the live log, not from the story)."""
    draft = (
        "你伸手把铜镜从柜台上拿了起来。镜背贴上掌心的瞬间，那股凉意又爬上来了——"
        "镜面上没有你的倒影，映着的是一片黑。"
    )
    llm = FakeLLM(
        script=[
            # A tool round that STREAMED its narration before calling the dice tool:
            # `assistant_tools` alone carries no content, so spell the round out.
            ChatResult(content=draft, tool_calls=[tool_call("roll_dice", expression="1d100")]),
            assistant_text("你猛地回过神，镜面映出的是你自己的脸——额头上一层薄汗。"),
        ]
    )
    services = build_services(Settings(locale="zh"), llm=llm, embeddings=FakeEmbeddings(16))
    ctx = AgentCtx(chat_key="draft-feedback-room", user_id="p1", locale="zh")
    frames: list[dict] = []

    # The gate only exists when streaming is on — pass an emitter so the tool round's
    # narration is actually collected (and the draft can ride back to the model).
    async def emit(frame: dict) -> None:
        frames.append(frame)

    result = await run_kp_turn(ctx, services, build_kp_toolset(services), "把镜子拿走", on_reply_delta=emit)

    # The keeper-visible draft is still archived…
    assert result.discarded_draft == draft
    # …AND the next model call received the draft back with a resume instruction, so the
    # final reply can carry the process detail the players should have seen.
    assert len(llm.calls) >= 2
    second_messages = llm.calls[1][0]
    joined = "\n".join(str(m.get("content") or "") for m in second_messages)
    assert draft in joined
    assert "承接并保留" in joined  # loop.draft_resume (zh) — instructs to preserve the detail
    assert result.reply
