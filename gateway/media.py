"""Shared media-frame helpers for room transports and KP tools."""

from __future__ import annotations

import json
import uuid
from typing import Any

from gateway.hub import Event, RoomHub
from infra.media_store import MediaRecord
from infra.room_facets import STORAGE_DOCUMENTS, STORAGE_MEDIA, STORAGE_ROOM_STATE, RoomStateFacet
from infra.store import Store

MEDIA_HISTORY_REPLAY_CAP = 30


def media_frame(
    record: MediaRecord, *, from_name: str, frame_id: str | None = None, prompt: str | None = None
) -> dict[str, Any]:
    frame = {
        "type": "media",
        "id": frame_id or uuid.uuid4().hex,
        "hash": record.hash,
        "mime": record.mime,
        "size": record.size,
        "name": record.name,
        "from": from_name,
        "ts": record.created_at,
    }
    # The image-generation prompt that produced this picture rides along so a
    # client can show it (hover tooltip) and a keeper can audit why it looks
    # the way it does. Only present for generated handouts.
    if prompt:
        frame["prompt"] = prompt
    return frame


async def record_media_history(store: Store, chat_key: str, frame: dict[str, Any]) -> None:
    store_key = "media_history"
    try:
        raw = await store.state_get(chat_key, store_key)
        history = json.loads(raw) if raw else []
    except Exception:
        history = []
    if not isinstance(history, list):
        history = []
    # Stamp the campaign turn so a join replay can interleave the picture at the
    # same point in the story it was generated (see `_replay_history_body`). A
    # frame that already carries one (a re-recorded upload) keeps it.
    if "turn" not in frame:
        try:
            from agent.chronicle import chronicle_turn

            frame = dict(frame, turn=await chronicle_turn(store, chat_key))
        except Exception:
            frame = dict(frame, turn=0)
    history.append(frame)
    await store.state_set(chat_key, store_key, json.dumps(history[-MEDIA_HISTORY_REPLAY_CAP:], ensure_ascii=False),
    )


async def publish_media(
    hub: RoomHub | None,
    store: Store,
    chat_key: str,
    frame: dict[str, Any],
) -> None:
    await record_media_history(store, chat_key, frame)
    if hub is not None:
        await hub.publish(chat_key, Event.media(frame))


# --- Room lifecycle (M23 WS1) -----------------------------------------------
ROOM_FACETS = (
    RoomStateFacet(
        name="room_media",
        owner="gateway.media",
        reset_scope="all",
        # The `media` document type is registered for the frame contract and no document
        # of it is written today; it is claimed here so the type cannot become an orphan
        # the day something does write one.
        doc_types=frozenset({"media"}),
        state_keys=frozenset({"media_history"}),
        storages=frozenset({STORAGE_DOCUMENTS, STORAGE_ROOM_STATE, STORAGE_MEDIA}),
    ),
)
