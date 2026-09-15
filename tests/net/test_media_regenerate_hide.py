from __future__ import annotations

import json

from agent.services import build_services
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM
from net.keystore import Keystore
from net.session import SessionCore, render_frame


def _services(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    return build_services(settings, llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(8))


class RecordingMember:
    """A Member stub whose deliver/send_frame record every event/frame."""

    def __init__(self, session_key: str, role: str = "keeper", name: str = "Keeper") -> None:
        self.session_key = session_key
        self.role = role
        self.name = name
        self.id = f"tui:{session_key.split(':')[-1]}"
        self.locale = "en"
        self.user_key = f"user:{session_key.split(':')[-1]}"
        self.events = []

    async def deliver(self, event) -> None:
        self.events.append(event)

    async def send_frame(self, frame) -> None:
        self.events.append(frame)


async def test_media_hide_removes_history_and_broadcasts(tmp_path):
    services = _services(tmp_path)
    core = SessionCore(services, Keystore())
    chat_key = "tui:group:hide-room"
    member = RecordingMember(chat_key)
    await core.hub.subscribe(chat_key, member)
    await services.store.state_set(
        chat_key,
        "media_history",
        json.dumps(
            [{"type": "media", "id": "m1", "hash": "h1"}, {"type": "media", "id": "m2", "hash": "h2"}]
        ),
    )

    await core._handle_media_hide(member, {"type": "media_hide", "id": "m1"})

    remaining = json.loads(await services.store.state_get(chat_key, "media_history"))
    assert [item["id"] for item in remaining] == ["m2"]
    hidden = [e for e in member.events if getattr(e, "kind", "") == "media_hidden"]
    assert len(hidden) == 1
    assert hidden[0].data == {"type": "media_hidden", "id": "m1"}


async def test_media_hide_rejects_non_keeper_and_leaves_history(tmp_path):
    services = _services(tmp_path)
    core = SessionCore(services, Keystore())
    chat_key = "tui:group:hide-room"
    member = RecordingMember(chat_key, role="player")
    await services.store.state_set(
        chat_key, "media_history", json.dumps([{"type": "media", "id": "m1", "hash": "h1"}])
    )

    await core._handle_media_hide(member, {"type": "media_hide", "id": "m1"})

    frames = [e for e in member.events if isinstance(e, dict)]
    assert frames and frames[0].get("type") == "error" and frames[0].get("code") == "forbidden"
    remaining = json.loads(await services.store.state_get(chat_key, "media_history"))
    assert [item["id"] for item in remaining] == ["m1"]


async def test_media_hide_bad_frame_rejected(tmp_path):
    services = _services(tmp_path)
    core = SessionCore(services, Keystore())
    chat_key = "tui:group:hide-room"
    member = RecordingMember(chat_key)

    await core._handle_media_hide(member, {"type": "media_hide"})

    frames = [e for e in member.events if isinstance(e, dict)]
    assert frames and frames[0].get("code") == "bad_frame"


async def test_media_regenerate_rejects_unknown_target(tmp_path):
    services = _services(tmp_path)
    core = SessionCore(services, Keystore())
    chat_key = "tui:group:regen-room"
    member = RecordingMember(chat_key)
    # No media_history at all — nothing to re-render.
    await core._handle_media_regenerate(
        member, {"type": "media_regenerate", "id": "ghost", "kind": "scene", "prompt": "a misty cove"}
    )
    frames = [e for e in member.events if isinstance(e, dict)]
    assert frames and frames[0].get("code") == "media_not_found"


async def test_media_regenerate_rejects_non_keeper(tmp_path):
    services = _services(tmp_path)
    core = SessionCore(services, Keystore())
    chat_key = "tui:group:regen-room"
    member = RecordingMember(chat_key, role="player")
    await services.store.state_set(
        chat_key, "media_history", json.dumps([{"type": "media", "id": "m1", "hash": "h1"}])
    )

    await core._handle_media_regenerate(
        member, {"type": "media_regenerate", "id": "m1", "kind": "scene", "prompt": "a misty cove"}
    )

    frames = [e for e in member.events if isinstance(e, dict)]
    assert frames and frames[0].get("code") == "forbidden"


def test_render_media_hidden_frame():
    frame = render_frame(__import__("gateway.hub", fromlist=["Event"]).Event.media_hidden("m1"))
    assert frame == {"type": "media_hidden", "id": "m1"}
