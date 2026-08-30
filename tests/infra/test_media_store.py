import asyncio
import hashlib
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from infra.media_store import (
    ALLOWED_AUDIO_MIMES,
    ALLOWED_IMAGE_MIMES,
    MediaError,
    MediaStore,
    PendingUpload,
)
from infra.store import Store


def _png_bytes(seed: bytes = b"img") -> bytes:
    return b"\x89PNG\r\n\x1a\n" + seed


async def test_media_store_rejects_bad_mime_size_and_quota(tmp_path):
    store = MediaStore(Store(), tmp_path, max_file_bytes=16, room_quota_bytes=12)
    data = _png_bytes(b"a")
    digest = hashlib.sha256(data).hexdigest()

    with pytest.raises(MediaError, match="media_bad_mime"):
        await store.validate_offer(room="room-a", mime="text/plain", size=len(data), sha256=digest)

    with pytest.raises(MediaError, match="media_too_large"):
        await store.validate_offer(room="room-a", mime="image/png", size=99, sha256=digest)

    await store.commit_bytes(
        PendingUpload("u1", "room-a", "image/png", len(data), "a.png", "u", digest),
        data,
    )
    other = _png_bytes(b"bb")
    other_digest = hashlib.sha256(other).hexdigest()
    with pytest.raises(MediaError, match="media_quota_exceeded"):
        await store.validate_offer(room="room-a", mime="image/png", size=len(other), sha256=other_digest)


async def test_media_store_rejects_sha_mismatch_and_dedupes(tmp_path):
    store = MediaStore(Store(), tmp_path)
    data = _png_bytes()
    digest = hashlib.sha256(data).hexdigest()
    pending = PendingUpload("u1", "room-a", "image/png", len(data), "a.png", "u", digest)

    with pytest.raises(MediaError, match="media_hash_mismatch"):
        await store.commit_bytes(PendingUpload("bad", "room-a", "image/png", len(data), "a.png", "u", "0" * 64), data)

    first = await store.commit_bytes(pending, data)
    duplicate = await store.validate_offer(room="room-a", mime="image/png", size=len(data), sha256=digest)

    assert duplicate == first
    assert await store.room_total_size("room-a") == len(data)


async def test_media_store_is_room_isolated(tmp_path):
    store = MediaStore(Store(), tmp_path)
    data = _png_bytes()
    digest = hashlib.sha256(data).hexdigest()
    await store.commit_bytes(PendingUpload("u1", "room-a", "image/png", len(data), "a.png", "u", digest), data)

    record, loaded = await store.read_bytes("room-a", digest)
    assert record.hash == digest
    assert loaded == data

    if os.name == "posix":
        media_dir = tmp_path / "media"
        room_dir = media_dir / "room-a"
        blob = room_dir / digest
        assert stat.S_IMODE(media_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(room_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(blob.stat().st_mode) == 0o600

    with pytest.raises(MediaError, match="media_not_found"):
        await store.read_bytes("room-b", digest)


async def test_media_store_offer_policy_can_be_overridden_for_audio(tmp_path):
    store = MediaStore(Store(), tmp_path, max_file_bytes=4, room_quota_bytes=4, allowed_mimes=ALLOWED_IMAGE_MIMES | ALLOWED_AUDIO_MIMES)
    data = b"ID3audio"
    digest = hashlib.sha256(data).hexdigest()

    with pytest.raises(MediaError, match="media_too_large"):
        await store.validate_offer(room="room-a", mime="audio/mpeg", size=len(data), sha256=digest)

    existing = await store.validate_offer(
        room="room-a",
        mime="audio/mpeg",
        size=len(data),
        sha256=digest,
        max_file_bytes=32,
        room_quota_bytes=64,
        allowed_mimes=ALLOWED_AUDIO_MIMES,
    )
    assert existing is None


async def test_image_and_audio_quotas_are_counted_separately(tmp_path):
    backing = Store()
    image = _png_bytes()
    audio = b"ID3audio"
    image_store = MediaStore(
        backing,
        tmp_path,
        max_file_bytes=len(image),
        room_quota_bytes=len(image),
        allowed_mimes=ALLOWED_IMAGE_MIMES,
    )
    audio_store = MediaStore(
        backing,
        tmp_path,
        max_file_bytes=len(audio),
        room_quota_bytes=len(audio),
        allowed_mimes=ALLOWED_AUDIO_MIMES,
    )

    await audio_store.register_blob(
        room="room-a",
        data=audio,
        mime="audio/mpeg",
        name="track.mp3",
        uploader="u",
    )
    await image_store.register_blob(
        room="room-a",
        data=image,
        mime="image/png",
        name="handout.png",
        uploader="u",
    )

    assert await image_store.room_total_size("room-a") == len(image)
    assert await audio_store.room_total_size("room-a") == len(audio)


async def test_media_store_rejects_unsafe_svg_on_commit(tmp_path):
    store = MediaStore(Store(), tmp_path)
    data = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    digest = hashlib.sha256(data).hexdigest()

    with pytest.raises(MediaError, match="media_bad_svg"):
        await store.commit_bytes(PendingUpload("u1", "room-a", "image/svg+xml", len(data), "bad.svg", "u", digest), data)


async def test_commit_rechecks_pending_offer_quota_atomically(tmp_path):
    backing = Store()
    store = MediaStore(backing, tmp_path, max_file_bytes=64, room_quota_bytes=64)
    first = _png_bytes(b"a")
    second = _png_bytes(b"b")
    quota = len(first)
    pending = []
    for upload_id, data in (("u1", first), ("u2", second)):
        digest = hashlib.sha256(data).hexdigest()
        assert (
            await store.validate_offer(
                room="room-a",
                mime="image/png",
                size=len(data),
                sha256=digest,
                room_quota_bytes=quota,
            )
            is None
        )
        pending.append(
            PendingUpload(
                upload_id,
                "room-a",
                "image/png",
                len(data),
                f"{upload_id}.png",
                "u",
                digest,
                room_quota_bytes=quota,
            )
        )

    results = await asyncio.gather(
        store.commit_bytes(pending[0], first),
        store.commit_bytes(pending[1], second),
        return_exceptions=True,
    )

    errors = [result for result in results if isinstance(result, MediaError)]
    assert len(errors) == 1
    assert errors[0].code == "media_quota_exceeded"
    assert await store.room_total_size("room-a") == quota
    assert len(await store.list_room_records("room-a")) == 1


async def test_pending_offer_policy_does_not_leak_to_later_uploads(tmp_path):
    store = MediaStore(
        Store(),
        tmp_path,
        max_file_bytes=64,
        room_quota_bytes=64,
        allowed_mimes=ALLOWED_IMAGE_MIMES | ALLOWED_AUDIO_MIMES,
    )
    first = b"ID3first"
    first_digest = hashlib.sha256(first).hexdigest()
    await store.commit_bytes(
        PendingUpload(
            "u1",
            "room-a",
            "audio/mpeg",
            len(first),
            "first.mp3",
            "u",
            first_digest,
            max_file_bytes=16,
            room_quota_bytes=16,
            allowed_mimes=ALLOWED_AUDIO_MIMES,
        ),
        first,
    )

    second = b"ID3second-upload"
    second_digest = hashlib.sha256(second).hexdigest()
    record = await store.commit_bytes(
        PendingUpload(
            "u2",
            "room-a",
            "audio/mpeg",
            len(second),
            "second.mp3",
            "u",
            second_digest,
            max_file_bytes=64,
            room_quota_bytes=64,
            allowed_mimes=ALLOWED_AUDIO_MIMES,
        ),
        second,
    )

    assert record.size == len(second)


async def test_commit_removes_new_blob_when_metadata_commit_fails(tmp_path):
    backing = Store()
    store = MediaStore(backing, tmp_path)
    data = _png_bytes(b"new")
    digest = hashlib.sha256(data).hexdigest()
    pending = PendingUpload("u1", "room-a", "image/png", len(data), "new.png", "u", digest)

    with patch.object(store, "_insert_record", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            await store.commit_bytes(pending, data)

    assert not store._path("room-a", digest).exists()
    assert await store.get_record("room-a", digest) is None


async def test_commit_failure_never_removes_a_preexisting_shared_blob(tmp_path):
    backing = Store()
    store = MediaStore(backing, tmp_path)
    data = _png_bytes(b"shared")
    digest = hashlib.sha256(data).hexdigest()
    first_room = "room/a"
    colliding_room = "room a"
    await store.commit_bytes(
        PendingUpload("u1", first_room, "image/png", len(data), "shared.png", "u", digest),
        data,
    )
    real_commit = backing._commit
    commit_calls = 0

    def fail_metadata_commit(conn):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            return real_commit(conn)
        raise OSError("disk full")

    with patch.object(backing, "_commit", side_effect=fail_metadata_commit):
        with pytest.raises(OSError, match="disk full"):
            await store.commit_bytes(
                PendingUpload(
                    "u2",
                    colliding_room,
                    "image/png",
                    len(data),
                    "shared.png",
                    "u",
                    digest,
                ),
                data,
            )

    _, loaded = await store.read_bytes(first_room, digest)
    assert loaded == data
    assert await store.get_record(colliding_room, digest) is None


async def test_commit_fsyncs_blob_and_directory_before_publishing_metadata(tmp_path):
    store = MediaStore(Store(), tmp_path)
    data = _png_bytes(b"durable")
    digest = hashlib.sha256(data).hexdigest()
    real_fsync = os.fsync

    with patch("infra.file_permissions.os.fsync", side_effect=real_fsync) as fsync:
        await store.commit_bytes(
            PendingUpload("u1", "room-a", "image/png", len(data), "durable.png", "u", digest),
            data,
        )

    # ``atomic_write_private`` fsyncs the blob and then its containing directory.
    assert fsync.call_count >= 2


async def test_delete_room_restores_staged_blobs_when_db_commit_fails(tmp_path):
    backing = Store()
    store = MediaStore(backing, tmp_path)
    data = _png_bytes(b"keep")
    digest = hashlib.sha256(data).hexdigest()
    await store.commit_bytes(
        PendingUpload("u1", "room-a", "image/png", len(data), "keep.png", "u", digest),
        data,
    )
    real_commit = backing._commit
    commit_calls = 0

    def fail_delete_commit(conn):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:  # schema setup inside delete_room
            return real_commit(conn)
        raise OSError("disk full")

    with patch.object(backing, "_commit", side_effect=fail_delete_commit):
        with pytest.raises(OSError, match="disk full"):
            await store.delete_room("room-a")

    _, loaded = await store.read_bytes("room-a", digest)
    assert loaded == data


async def test_delete_room_rolls_back_blobs_when_staging_fails(tmp_path):
    backing = Store()
    store = MediaStore(backing, tmp_path)
    blobs = [_png_bytes(b"one"), _png_bytes(b"two")]
    digests = []
    for index, data in enumerate(blobs):
        digest = hashlib.sha256(data).hexdigest()
        digests.append(digest)
        await store.commit_bytes(
            PendingUpload(
                f"u{index}",
                "room-a",
                "image/png",
                len(data),
                f"{index}.png",
                "u",
                digest,
            ),
            data,
        )

    real_replace = os.replace
    staged_moves = 0

    def fail_second_staged_move(source, destination):
        nonlocal staged_moves
        if ".delete-staging" in Path(destination).parts:
            staged_moves += 1
            if staged_moves == 2:
                raise OSError("staging failed")
        return real_replace(source, destination)

    with patch("infra.media_store.os.replace", side_effect=fail_second_staged_move):
        with pytest.raises(OSError, match="staging failed"):
            await store.delete_room("room-a")

    assert len(await store.list_room_records("room-a")) == 2
    for digest, data in zip(digests, blobs, strict=True):
        _, loaded = await store.read_bytes("room-a", digest)
        assert loaded == data


async def test_delete_room_removes_index_and_unshared_blobs(tmp_path):
    store = MediaStore(Store(), tmp_path)
    data = _png_bytes(b"gone")
    digest = hashlib.sha256(data).hexdigest()
    await store.commit_bytes(
        PendingUpload("u1", "room-a", "image/png", len(data), "gone.png", "u", digest),
        data,
    )

    assert await store.delete_room("room-a") == 1
    assert await store.list_room_records("room-a") == []
    assert not store._path("room-a", digest).exists()


async def test_delete_by_name_prefix_removes_only_matching_entries(tmp_path):
    store = MediaStore(Store(), tmp_path)
    module_a = _png_bytes(b"mod-a")
    module_b = _png_bytes(b"mod-b")
    upload = _png_bytes(b"upload")
    for name, data in (
        ("module-sword-coast-keepsakes-npcs-1.png", module_a),
        ("module-sword-coast-keepsakes-scenes-2.png", module_b),
        ("portrait-generated.png", upload),
    ):
        digest = hashlib.sha256(data).hexdigest()
        await store.commit_bytes(
            PendingUpload("u1", "room-a", "image/png", len(data), name, "u", digest),
            data,
        )

    deleted = await store.delete_by_name_prefix("room-a", "module-sword-coast-keepsakes-")

    assert deleted == 2
    remaining = await store.list_room_records("room-a")
    assert [r.name for r in remaining] == ["portrait-generated.png"]
    assert not store._path("room-a", hashlib.sha256(module_a).hexdigest()).exists()
    assert not store._path("room-a", hashlib.sha256(module_b).hexdigest()).exists()
    assert store._path("room-a", hashlib.sha256(upload).hexdigest()).exists()


async def test_delete_by_name_prefix_preserves_shared_blob_of_other_room(tmp_path):
    store = MediaStore(Store(), tmp_path)
    data = _png_bytes(b"shared-art")
    digest = hashlib.sha256(data).hexdigest()
    first_room = "room/a"
    colliding_room = "room a"
    await store.commit_bytes(
        PendingUpload("u1", first_room, "image/png", len(data), "module-x-npcs-1.png", "u", digest),
        data,
    )
    await store.commit_bytes(
        PendingUpload("u2", colliding_room, "image/png", len(data), "module-x-npcs-1.png", "u", digest),
        data,
    )

    deleted = await store.delete_by_name_prefix(first_room, "module-x-")

    assert deleted == 1
    # The colliding room's sanitized directory still indexes the same hash, so
    # the shared blob file must survive.
    assert store._path(first_room, digest).exists()
    assert await store.get_record(colliding_room, digest) is not None


async def test_delete_by_name_prefix_restores_staged_blobs_when_commit_fails(tmp_path):
    backing = Store()
    store = MediaStore(backing, tmp_path)
    data = _png_bytes(b"keep-prefix")
    digest = hashlib.sha256(data).hexdigest()
    await store.commit_bytes(
        PendingUpload("u1", "room-a", "image/png", len(data), "module-x-npcs-1.png", "u", digest),
        data,
    )

    with patch.object(backing, "_commit", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            await store.delete_by_name_prefix("room-a", "module-x-")

    assert await store.get_record("room-a", digest) is not None
    _, loaded = await store.read_bytes("room-a", digest)
    assert loaded == data


async def test_delete_by_name_prefix_no_match_returns_zero(tmp_path):
    store = MediaStore(Store(), tmp_path)
    data = _png_bytes(b"plain")
    digest = hashlib.sha256(data).hexdigest()
    await store.commit_bytes(
        PendingUpload("u1", "room-a", "image/png", len(data), "scene.png", "u", digest),
        data,
    )

    assert await store.delete_by_name_prefix("room-a", "module-nothing-") == 0
    assert len(await store.list_room_records("room-a")) == 1


async def test_delete_generated_images_removes_image_kinds_only(tmp_path):
    store = MediaStore(Store(), tmp_path)
    blobs = {
        "scene-mist-at-dawn.png": b"\x89PNG\r\n\x1a\n" + b"s1",
        "portrait-generated.png": b"\x89PNG\r\n\x1a\n" + b"s2",
        "clue-gold-brooch.png": b"\x89PNG\r\n\x1a\n" + b"s3",
        "combat-round-3.png": b"\x89PNG\r\n\x1a\n" + b"s4",
        "avatar-marina.png": b"\x89PNG\r\n\x1a\n" + b"av",
        "pregen-old-tinker.png": b"\x89PNG\r\n\x1a\n" + b"pg",
        "module-sword-coast-keepsakes-npcs-1.png": b"\x89PNG\r\n\x1a\n" + b"mo",
        "portrait.jpg": b"\x89PNG\r\n\x1a\n" + b"up",
    }
    for name, data in blobs.items():
        digest = hashlib.sha256(data).hexdigest()
        await store.commit_bytes(
            PendingUpload("u1", "room-a", "image/png", len(data), name, "u", digest), data
        )

    deleted = await store.delete_generated_images("room-a")

    assert deleted == 4
    remaining = sorted(r.name for r in await store.list_room_records("room-a"))
    assert remaining == [
        "avatar-marina.png",
        "module-sword-coast-keepsakes-npcs-1.png",
        "portrait.jpg",
        "pregen-old-tinker.png",
    ]
    for name, data in blobs.items():
        digest = hashlib.sha256(data).hexdigest()
        exists = store._path("room-a", digest).exists()
        assert exists == (name not in {"scene-mist-at-dawn.png", "portrait-generated.png", "clue-gold-brooch.png", "combat-round-3.png"})


async def test_delete_generated_images_preserves_shared_blob_of_other_room(tmp_path):
    store = MediaStore(Store(), tmp_path)
    data = b"\x89PNG\r\n\x1a\n" + b"shared-scene"
    digest = hashlib.sha256(data).hexdigest()
    first_room = "room/a"
    colliding_room = "room a"
    await store.commit_bytes(
        PendingUpload("u1", first_room, "image/png", len(data), "scene-shared.png", "u", digest), data
    )
    await store.commit_bytes(
        PendingUpload("u2", colliding_room, "image/png", len(data), "scene-shared.png", "u", digest), data
    )

    await store.delete_generated_images(first_room)

    assert store._path(first_room, digest).exists()
    assert await store.get_record(colliding_room, digest) is not None


async def test_dedupe_by_name_prefix_keeps_newest_per_name(tmp_path):
    store = MediaStore(Store(), tmp_path)
    versions = {
        "oldest": b"\x89PNG\r\n\x1a\n" + b"oldest",
        "middle": b"\x89PNG\r\n\x1a\n" + b"middle",
        "newest": b"\x89PNG\r\n\x1a\n" + b"newest",
    }
    digests = {}
    for key, data in versions.items():
        digest = hashlib.sha256(data).hexdigest()
        digests[key] = digest
        # SAME name — the re-import / re-generation stacking case.
        await store.commit_bytes(
            PendingUpload("u1", "room-a", "image/png", len(data), "module-pack-npcs-1.png", "u", digest), data
        )
    # Distinct timestamps: bump created_at directly so the newest is deterministic.
    backing = store._store
    async with backing._lock:
        conn = backing._ensure_conn()
        conn.execute("UPDATE media_index SET created_at = 100 WHERE hash = ?", (digests["oldest"],))
        conn.execute("UPDATE media_index SET created_at = 200 WHERE hash = ?", (digests["middle"],))
        conn.execute("UPDATE media_index SET created_at = 300 WHERE hash = ?", (digests["newest"],))
        backing._commit(conn)
    # An unrelated same-prefix name must survive untouched.
    other = b"\x89PNG\r\n\x1a\n" + b"other"
    other_digest = hashlib.sha256(other).hexdigest()
    await store.commit_bytes(
        PendingUpload("u2", "room-a", "image/png", len(other), "module-pack-scene-2.png", "u", other_digest), other
    )

    deleted = await store.dedupe_by_name_prefix("room-a", "module-pack-")

    assert deleted == 2
    remaining = sorted(r.name for r in await store.list_room_records("room-a"))
    assert remaining == ["module-pack-npcs-1.png", "module-pack-scene-2.png"]
    assert not store._path("room-a", digests["oldest"]).exists()
    assert not store._path("room-a", digests["middle"]).exists()
    assert store._path("room-a", digests["newest"]).exists()


async def test_dedupe_by_name_prefix_no_duplicates_returns_zero(tmp_path):
    store = MediaStore(Store(), tmp_path)
    data = b"\x89PNG\r\n\x1a\n" + b"single"
    digest = hashlib.sha256(data).hexdigest()
    await store.commit_bytes(
        PendingUpload("u1", "room-a", "image/png", len(data), "module-pack-scene-1.png", "u", digest), data
    )

    assert await store.dedupe_by_name_prefix("room-a", "module-pack-") == 0
    assert len(await store.list_room_records("room-a")) == 1
