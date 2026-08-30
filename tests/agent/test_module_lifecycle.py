from __future__ import annotations

import json

import pytest

from agent.context import AgentCtx, LocalFs
from agent.kp_tools_knowledge import DocumentTools
from agent.module_lifecycle import (
    ModuleImportTransaction,
    active_module,
    identity_for_text,
    publish_active_module,
    purge_active_module,
)
from agent.services import build_services
from core.modvars import build_spec, define_modvar
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.llm import FakeLLM, assistant_text


def _services(tmp_path, *, script=None):
    settings = Settings(data_dir=str(tmp_path / "data"))
    return build_services(
        settings,
        llm=FakeLLM(script=list(script or [])),
        embeddings=FakeEmbeddings(8),
    )


async def test_failed_module_transaction_restores_documents_state_and_vectors(tmp_path):
    services = _services(tmp_path)
    room = "rollback-room"
    old = identity_for_text(tmp_path / "old.md", name="old.md")
    old["enabled_skills"] = ["old-module-skill"]
    await publish_active_module(services, room, old)
    await services.store.state_set(room, "skills_enabled", json.dumps(["manual-skill", "old-module-skill"]))
    await services.store.state_set(room, "room_hooks", '[{"id":"old#0","code":"old()"}]')
    await services.worldbook.import_entries(
        room,
        [{"title": "Old truth", "content": "old", "keys": ["old"]}],
        source=old["source_id"],
        is_keeper=True,
    )
    await services.vector_db.store_document("old-doc", "old", "old body", room, "module")

    with pytest.raises(RuntimeError, match="boom"):
        async with ModuleImportTransaction(services, room):
            await purge_active_module(services, room)
            await services.worldbook.import_entries(
                room,
                [{"title": "New truth", "content": "new", "keys": ["new"]}],
                source="new-source",
                is_keeper=True,
            )
            await services.vector_db.store_document("new-doc", "new", "new body", room, "module")
            raise RuntimeError("boom")

    assert (await active_module(services, room))["source_id"] == old["source_id"]
    assert [entry.title for entry in await services.worldbook.list(room)] == ["Old truth"]
    assert json.loads(await services.store.state_get(room, "skills_enabled")) == [
        "manual-skill",
        "old-module-skill",
    ]
    documents = await services.vector_db.list_documents(room, "module")
    assert [(item["document_id"], item["filename"]) for item in documents] == [("old-doc", "old")]


async def test_replacement_removes_only_module_owned_switches_and_content(tmp_path):
    services = _services(tmp_path)
    room = "ownership-room"
    old = identity_for_text(tmp_path / "old.md", name="old.md")
    old["enabled_skills"] = ["old-module-skill"]
    old["enabled_panel_packs"] = ["old-module-panels"]
    await publish_active_module(services, room, old)
    await services.store.state_set(room, "skills_enabled", '["manual-skill","old-module-skill"]')
    await services.store.state_set(room, "panels_enabled", '["manual-panels","old-module-panels"]')
    await services.store.state_set(room, "room_hooks", '[{"id":"old#0","code":"old()"}]')
    await define_modvar(services.documents, room, build_spec("old_alarm", "number", default=1))
    await services.documents.put(room, "module_brief", "old", {"name": "Old"}, source=old["source_id"])
    await services.documents.put(room, "npc", "old-npc", {"name": "Old NPC"}, source=old["source_id"])

    await purge_active_module(services, room)

    assert json.loads(await services.store.state_get(room, "skills_enabled")) == ["manual-skill"]
    assert json.loads(await services.store.state_get(room, "panels_enabled")) == ["manual-panels"]
    assert await services.store.state_get(room, "room_hooks") is None
    assert await services.documents.get_singleton(room, "modvars") is None
    assert await services.documents.list(room, "module_brief") == []
    assert await services.documents.list(room, "npc") == []


async def test_text_import_failure_keeps_the_previous_world_module(tmp_path, monkeypatch):
    services = _services(tmp_path)
    room = "world-to-text"
    old = {
        "schema": 1,
        "kind": "world_card",
        "source_id": "old-world",
        "name": "Old World",
        "source": "cards/old.json",
        "lore_sources": ["old-world"],
        "enabled_skills": [],
        "enabled_panel_packs": [],
    }
    await publish_active_module(services, room, old)
    await services.store.state_set(room, "world_import", "Old World")
    await services.worldbook.import_entries(
        room,
        [{"title": "Old truth", "content": "old", "keys": ["old"]}],
        source="old-world",
        is_keeper=True,
    )
    (tmp_path / "new.md").write_text("# New module", encoding="utf-8")

    async def fail_initialize(*args, **kwargs):
        raise RuntimeError("analysis failed")

    monkeypatch.setattr(services.module_init, "initialize", fail_initialize)
    ctx = AgentCtx(chat_key=room, user_id="keeper", locale="en", fs=LocalFs(tmp_path))
    result = await DocumentTools(services).upload_document(ctx, "new.md", "module")

    assert "analysis failed" in result
    assert (await active_module(services, room))["source_id"] == "old-world"
    assert await services.store.state_get(room, "world_import") == "Old World"
    assert [entry.title for entry in await services.worldbook.list(room)] == ["Old truth"]


async def test_same_text_source_replaces_its_vectors_instead_of_stacking(tmp_path):
    analysis = json.dumps({"scenes": [], "npcs": [], "clues": [], "summary": "ok"})
    services = _services(
        tmp_path,
        script=[assistant_text(analysis), assistant_text(analysis)],
    )
    path = tmp_path / "case.md"
    path.write_text("first body", encoding="utf-8")
    ctx = AgentCtx(chat_key="text-refresh", user_id="keeper", locale="en", fs=LocalFs(tmp_path))
    tools = DocumentTools(services)

    await tools.upload_document(ctx, "case.md", "module")
    first = await services.vector_db.list_documents(ctx.chat_key, "module")
    path.write_text("second body", encoding="utf-8")
    await tools.upload_document(ctx, "case.md", "module")
    second = await services.vector_db.list_documents(ctx.chat_key, "module")

    assert len(first) == len(second) == 1
    assert first[0]["document_id"] == second[0]["document_id"]
    chunks = await services.vector_db.list_all_chunks(ctx.chat_key)
    assert "second body" in "\n".join(str(chunk["text"]) for chunk in chunks)
    assert "first body" not in "\n".join(str(chunk["text"]) for chunk in chunks)


def test_text_identity_uses_source_path_not_display_name(tmp_path):
    left = identity_for_text(tmp_path / "left" / "same.md", name="Same Module")
    right = identity_for_text(tmp_path / "right" / "same.md", name="Same Module")
    assert left["source_id"] != right["source_id"]


async def test_purge_removes_old_module_media_blobs_but_keeps_uploads(tmp_path):
    services = _services(tmp_path)
    room = "media-switch-room"
    old = {
        "schema": 1,
        "kind": "world_card",
        "source_id": "pack:oldpack@0.1.0:cards/old.lorecard.json",
        "name": "Old",
        "source": "cards/old.lorecard.json",
        "pack_id": "oldpack",
        "lore_sources": ["pack:oldpack@0.1.0:cards/old.lorecard.json"],
    }
    await publish_active_module(services, room, old)

    import hashlib

    from infra.media_store import ALLOWED_IMAGE_MIMES, MediaStore, PendingUpload

    media = MediaStore(services.store, services.settings.data_dir, allowed_mimes=ALLOWED_IMAGE_MIMES)
    art = b"\x89PNG\r\n\x1a\n" + b"module-art"
    art_digest = hashlib.sha256(art).hexdigest()
    await media.commit_bytes(
        PendingUpload("u1", room, "image/png", len(art), "module-oldpack-npcs-1.png", "u", art_digest),
        art,
    )
    upload = b"\x89PNG\r\n\x1a\n" + b"upload"
    upload_digest = hashlib.sha256(upload).hexdigest()
    await media.commit_bytes(
        PendingUpload("u2", room, "image/png", len(upload), "portrait.png", "u", upload_digest),
        upload,
    )

    await purge_active_module(services, room)

    remaining = [record.name for record in await media.list_room_records(room)]
    assert remaining == ["portrait.png"]
    assert not media._path(room, art_digest).exists()
    assert media._path(room, upload_digest).exists()


async def test_purge_without_pack_id_leaves_media_untouched(tmp_path):
    services = _services(tmp_path)
    room = "text-module-room"
    old = identity_for_text(tmp_path / "old.md", name="old.md")
    await publish_active_module(services, room, old)

    import hashlib

    from infra.media_store import ALLOWED_IMAGE_MIMES, MediaStore, PendingUpload

    media = MediaStore(services.store, services.settings.data_dir, allowed_mimes=ALLOWED_IMAGE_MIMES)
    art = b"\x89PNG\r\n\x1a\n" + b"kept"
    art_digest = hashlib.sha256(art).hexdigest()
    await media.commit_bytes(
        PendingUpload("u1", room, "image/png", len(art), "module-any-npcs-1.png", "u", art_digest),
        art,
    )

    await purge_active_module(services, room)

    assert [record.name for record in await media.list_room_records(room)] == ["module-any-npcs-1.png"]
