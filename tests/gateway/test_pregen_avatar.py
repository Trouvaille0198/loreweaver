"""The chat-side "生成头像" lane: a roster character's portrait goes through the
same async illustration discipline as the module detail page — one room-scoped job
whose prompt folds the character's APPEARANCE in first, rendered by a background
worker, then bound to the pregen document and any claimed party member.
"""

from __future__ import annotations

import pytest

from agent.services import build_services
from core.character_manager import CharacterSheet
from core.pregen_roster import pregen_add, pregen_pristine_sheet, slug_for
from gateway.module_media import build_pregen_portrait_prompt, queue_pregen_avatar
from infra.config import ImageGenSettings, Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM

pytestmark = pytest.mark.asyncio


class _FakeImageGen:
    """A minimal imagegen provider: returns the prompt echoed as PNG bytes."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(self, prompt: str, *, size: str | None = None, reference=None, reference_mime=None) -> tuple[bytes, str]:
        self.calls.append(prompt)
        return b"\x89PNG\r\n\x1a\n" + prompt.encode("utf-8"), "image/png"


def _services():
    return build_services(
        Settings(imagegen=ImageGenSettings()), llm=FakeLLM(script=[]), embeddings=FakeEmbeddings(8)
    )


async def test_build_pregen_portrait_prompt_leads_with_appearance():
    services = _services()
    renderer = services.i18n.with_locale("zh")
    prompt = build_pregen_portrait_prompt("阿岚", "花白短发，灰布长衫", "瘴雾镇的调查员", renderer=renderer)
    assert "阿岚" in prompt
    assert prompt.index("外貌") < prompt.index("人物")  # appearance first
    assert "花白短发" in prompt
    # No appearance: the persona carries the portrait.
    fallback = build_pregen_portrait_prompt("沈墨", "", "客栈的老板娘", renderer=renderer)
    assert "客栈的老板娘" in fallback


async def test_queue_pregen_avatar_renders_and_binds(monkeypatch):
    services = _services()
    chat_key = "cli:avatar:room"
    await pregen_add(
        services.documents,
        chat_key,
        CharacterSheet("阿岚", "coc7"),
        source="room",
        blurb="瘴雾镇的调查员，沉默寡言",
        appearance="花白短发，灰布长衫，右颊一道旧疤",
    )
    imagegen = _FakeImageGen()

    async def _fake_for_room(_room: str):
        return imagegen

    monkeypatch.setattr(services, "imagegen_for_room", _fake_for_room)

    ok, detail = await queue_pregen_avatar(services, chat_key, "阿岚")
    assert ok is True and detail.startswith("pregen-")

    # The worker is a background task: let it run, then assert.
    import asyncio

    for _ in range(100):
        await asyncio.sleep(0.01)
        if imagegen.calls:
            break
    assert imagegen.calls, "the worker must have rendered the job"
    assert "花白短发" in imagegen.calls[0]  # appearance folded into the prompt
    entry_doc = await services.documents.get(chat_key, "pregen", slug_for("阿岚"))
    assert entry_doc is not None
    assert entry_doc.data.get("avatar"), "the pregen document must carry the new portrait"
    # The pristine sheet's avatar is stamped too — the state projection reads sheet.avatar
    # for the roster card, and a fresh claim copies it to the claiming player.
    pristine = await pregen_pristine_sheet(services.documents, chat_key, slug_for("阿岚"))
    assert pristine is not None and pristine.name == "阿岚"
    assert pristine.avatar, "the pristine sheet must carry the new portrait"


async def test_queue_pregen_avatar_unknown_character_and_dedupe(monkeypatch):
    services = _services()
    chat_key = "cli:avatar:missing"
    ok, detail = await queue_pregen_avatar(services, chat_key, "不存在的角色")
    assert ok is False and detail == "unknown"

    # A pending job for the same subject is not queued twice.
    await pregen_add(services.documents, chat_key, CharacterSheet("沈墨", "coc7"), source="room")
    imagegen = _FakeImageGen()

    async def _fake_for_room(_room: str):
        return imagegen

    monkeypatch.setattr(services, "imagegen_for_room", _fake_for_room)
    import asyncio

    ok1, id1 = await queue_pregen_avatar(services, chat_key, "沈墨")
    ok2, id2 = await queue_pregen_avatar(services, chat_key, "沈墨")
    assert ok1 and ok2 and id1 == id2
    for _ in range(100):
        await asyncio.sleep(0.01)
        if imagegen.calls:
            break
    assert len(imagegen.calls) == 1
