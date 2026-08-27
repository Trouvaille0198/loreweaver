"""The asynchronous illustration lane for generated .lwpack modules.

Module generation no longer waits for its illustrations: the forge plans the shot list,
records the jobs in the installed pack home's ``media-jobs.json`` sidecar, and returns
immediately. This module runs the jobs in the background -- one worker per pack id --
rendering each shot through the pack's room imagegen lane, then wiring every result into
the pack (the asset file + manifest entry + world-card image bindings) and into each room
that imported the pack (room media store + ``module_media_index`` provenance, so `.image
<kind> <subject>` can reuse the illustration as a generation reference).

A job persists: kind, subject, prompt, caption, status (``pending``/``generating``/
``done``/``failed``), and -- when done -- the asset name + content hash; when failed --
the error. The prompt is persisted verbatim (iron-clad requirement: a failed job keeps
the prompt it was attempted with), so the keeper can re-queue it without re-planning, or
re-plan fresh shots from the module detail page.

Rooms: the sidecar records which rooms imported the pack while jobs were still pending
(the creating room at generation time, plus any room that imports it mid-generation).
Completion registers the finished image in each of those rooms, so a room imported before
the images finished still picks them up -- scene art additionally resolves live from the
pack manifest, so an enabled room sees the new plate on the next state frame regardless.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from agent.forge import (
    _IMAGE_MIME_EXTS,
    _append_module_media_index,
    _bind_pregen_portraits,
    _bind_worldbook_images,
    _build_module_media_messages,
    _imagegen_generate_retry,
    _llm_authored,
    _option_reason,
    _parse_shot_list,
    _worldbook_subject_names,
)
from agent.services import Services
from core.pack import DEV_PACK_HOMES, MANIFEST_NAME, parse_manifest_text
from core.yaml_safety import safe_load_no_aliases
from gateway.imagegen import allow_imagegen_request
from gateway.panels import installed_pack_homes
from infra.file_permissions import atomic_write_private
from infra.media_store import ALLOWED_IMAGE_MIMES, MediaStore

logger = logging.getLogger(__name__)

_JOBS_FILENAME = "media-jobs.json"
_STEM_RE = re.compile(r"-(\d+)$")
# One illustration's total render budget inside the worker (provider client timeouts can be
# minutes and the retry lane multiplies them): a hung plate fails and the queue continues.
# 150s covers a normal qwen render (15-90s) plus a couple of retry attempts; a genuinely hung
# plate gives way in ~2.5 minutes instead of stalling every later job for the client's 300s.
_JOB_TIMEOUT_SECONDS = 150.0

# Pack ids with a live worker. `schedule_pack_media` dedupes on it so a retry button can
# never start a second render loop for the same pack; the worker discards its id when done.
_inflight: set[str] = set()


def jobs_path(home: Path) -> Path:
    return home / _JOBS_FILENAME


def load_jobs(home: Path) -> dict[str, Any]:
    """The sidecar contents, or an empty jobs document when absent/unreadable."""
    try:
        data = json.loads(jobs_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        data["jobs"] = []
    rooms = data.get("rooms")
    if not isinstance(rooms, list):
        data["rooms"] = []
    return data


def save_jobs(home: Path, data: dict[str, Any]) -> None:
    data["updated_at"] = int(time.time())
    try:
        atomic_write_private(jobs_path(home), json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        logger.warning("[module-media] jobs write failed under %s: %s", home, exc)


def _card_module_text(card: dict[str, Any]) -> str:
    """A compact module document for the shot-list prompt, assembled from the world card --
    the regeneration path's analogue of the forge's original `description`."""
    parts: list[str] = []
    for key in ("name", "scenario", "opening"):
        value = str(card.get(key) or "").strip()
        if value:
            parts.append(value)
    entries: list[str] = []
    for entry in card.get("worldbook") or []:
        if not isinstance(entry, dict):
            continue
        keys = entry.get("keys") or []
        title = str(entry.get("title") or entry.get("comment") or (keys[0] if keys else "") or "").strip()
        content = str(entry.get("content") or "").strip()
        if title or content:
            entries.append(f"- {title}: {content}" if title else f"- {content}")
    if entries:
        parts.append("\n".join(entries))
    return "\n\n".join(parts)


def _next_kind_indexes(jobs: list[dict[str, Any]]) -> dict[str, int]:
    """Highest per-kind asset index among existing jobs (from their ``asset_stem``), so a
    re-planned shot list never collides with an earlier generation's asset names."""
    counts: dict[str, int] = {}
    for job in jobs:
        kind = str(job.get("kind") or "")
        stem = str(job.get("asset_stem") or "")
        match = _STEM_RE.search(stem)
        if kind and match:
            counts[kind] = max(counts.get(kind, 0), int(match.group(1)))
    return counts


def _manifest_kind_indexes(home: Path, pack_id: str) -> dict[str, int]:
    """Highest per-kind asset index already present in the installed pack's manifest — from a
    hand-authored build or an earlier generation that predates the jobs sidecar — so a re-plan
    can never collide with an existing plate's filename (`module-<pack_id>-<kind>-<n>.*`)."""
    counts: dict[str, int] = {}
    try:
        raw = safe_load_no_aliases((home / MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable manifest contributes nothing
        return counts
    assets = raw.get("assets") if isinstance(raw, dict) else None
    if not isinstance(assets, list):
        return counts
    pattern = re.compile(rf"module-{re.escape(pack_id)}-([a-z]+)-(\d+)\.")
    for entry in assets:
        if not isinstance(entry, dict):
            continue
        match = pattern.search(str(entry.get("path") or ""))
        if match:
            counts[match.group(1)] = max(counts.get(match.group(1), 0), int(match.group(2)))
    return counts


def _dedupe_existing(jobs: list[dict[str, Any]], shots: list[Any]) -> list[Any]:
    """Drop shots whose (kind, subject) already exists in any status -- a subject is
    illustrated at most once; the retry button handles a failed one's re-attempt."""
    seen = {(str(j.get("kind") or "").casefold(), str(j.get("subject") or "").casefold()) for j in jobs}
    kept: list[Any] = []
    for shot in shots:
        key = (shot.kind.casefold(), (shot.subject or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        kept.append(shot)
    return kept


async def plan_media_jobs(
    services: Services,
    pack_id: str,
    home: Path,
    card: dict[str, Any],
    kinds: list[str],
    chat_key: str,
    i18n,
    *,
    content: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Plan new illustration jobs for an installed pack: one shot-list LLM call, then one
    pending job per accepted shot. Returns ``(new_jobs, note)`` where ``note`` is empty on
    success and a localized reason when nothing could be planned. ``home`` supplies the
    existing jobs (dedupe + per-kind index continuity); ``pack_id`` names the asset stems."""
    pregen_names = [
        str(p.get("name"))
        for p in (card.get("pregens") or [])
        if isinstance(p, dict) and p.get("name")
    ]
    subject_names = _worldbook_subject_names(card)
    raw, failure = await _llm_authored(
        services,
        _build_module_media_messages(
            services,
            content or _card_module_text(card),
            kinds,
            i18n,
            pregen_names=pregen_names or None,
            subject_names=subject_names or None,
        ),
        chat_key=chat_key,
    )
    if failure is not None:
        return [], _option_reason(i18n, "shot_list_failed")
    assert raw is not None
    shots = _parse_shot_list(raw, kinds)
    if not shots:
        return [], _option_reason(i18n, "no_shots")
    data = load_jobs(home)
    existing = [j for j in data.get("jobs", []) if isinstance(j, dict)]
    shots = _dedupe_existing(existing, shots)
    if not shots:
        return [], _option_reason(i18n, "no_shots")
    indexes = _next_kind_indexes(existing)
    # Pre-existing plates in the installed manifest (hand-authored builds, or generations that
    # predate the jobs sidecar) claim their per-kind indices too — otherwise a re-plan would
    # stamp `module-<id>-<kind>-<n>` over an existing file and leave a stale manifest digest.
    manifest_indexes = _manifest_kind_indexes(home, pack_id)
    for kind, index in manifest_indexes.items():
        indexes[kind] = max(indexes.get(kind, 0), index)
    now = int(time.time())
    new_jobs: list[dict[str, Any]] = []
    for shot in shots:
        indexes[shot.kind] = indexes.get(shot.kind, 0) + 1
        index = indexes[shot.kind]
        new_jobs.append(
            {
                "id": f"{shot.kind}-{index}",
                "kind": shot.kind,
                "subject": shot.subject,
                "prompt": shot.prompt,
                "caption": shot.caption,
                # The asset's stem is fixed at plan time so later re-plans cannot collide;
                # the extension is stamped on completion once the provider's mime is known.
                "asset_stem": f"module-{pack_id}-{shot.kind}-{index}",
                "status": "pending",
                "asset": "",
                "hash": "",
                "mime": "",
                "error": "",
                "planned_at": now,
            }
        )
    return new_jobs, ""


def append_jobs(home: Path, jobs: list[dict[str, Any]], room: str | None = None) -> int:
    """Persist newly planned jobs (deduped by id) into the sidecar; appends ``room`` to the
    importing-rooms list when given. Returns how many jobs were actually added."""
    data = load_jobs(home)
    existing_ids = {str(j.get("id") or "") for j in data.get("jobs", []) if isinstance(j, dict)}
    added = [j for j in jobs if str(j.get("id") or "") not in existing_ids]
    if not added:
        return 0
    data["jobs"] = [*data.get("jobs", []), *added]
    if room and room not in data.get("rooms", []):
        data["rooms"] = [*data.get("rooms", []), room]
    save_jobs(home, data)
    return len(added)


def _update_job(home: Path, job_id: str, **fields: Any) -> None:
    """Patch one job in the LATEST sidecar: re-load first so a requeue/append that landed
    while a render was in flight is never clobbered by the worker's stale in-memory view."""
    data = load_jobs(home)
    for job in data.get("jobs", []):
        if isinstance(job, dict) and job.get("id") == job_id:
            job.update(fields)
            break
    save_jobs(home, data)


def _fail_all_pending(home: Path, error: str) -> None:
    """Fail every unfinished job in the LATEST sidecar (used when the queue must stop: no
    image provider, or the room's hourly cap is exhausted)."""
    data = load_jobs(home)
    for job in data.get("jobs", []):
        if isinstance(job, dict) and job.get("status") in ("pending", "generating"):
            job["status"] = "failed"
            job["error"] = error
    save_jobs(home, data)


def requeue_jobs(home: Path, job_ids: list[str]) -> int:
    """Return failed — or already-done — jobs to ``pending`` (prompt preserved verbatim) so
    the worker re-renders them. Done jobs re-render with their SAME prompt (a fresh provider
    call yields a new plate, overwriting the old file), which is how the detail page's
    "re-generate" button on a finished illustration swaps it for a new one. Returns how many
    jobs were re-queued."""
    wanted = set(job_ids)
    data = load_jobs(home)
    requeued = 0
    for job in data.get("jobs", []):
        if not isinstance(job, dict) or str(job.get("id") or "") not in wanted:
            continue
        if job.get("status") not in ("failed", "done"):
            continue
        job["status"] = "pending"
        job["error"] = ""
        # A re-render produces fresh bytes: drop the previous result so the detail page never
        # shows a stale plate under a job that is actively regenerating.
        job["asset"] = ""
        job["hash"] = ""
        job["mime"] = ""
        requeued += 1
    if requeued:
        save_jobs(home, data)
    return requeued


def pending_count(home: Path) -> int:
    """How many jobs are not yet finished -- drives the import hook's decision to attach the
    importing room and make sure a worker is scheduled."""
    data = load_jobs(home)
    return sum(1 for j in data.get("jobs", []) if isinstance(j, dict) and j.get("status") in ("pending", "generating"))


def attach_importing_room(services: Services, home: Path, room: str) -> bool:
    """A room just imported this pack: remember it for completion registration and resume the
    worker when jobs are still pending. Returns True when jobs are still running."""
    data = load_jobs(home)
    jobs = data.get("jobs", [])
    if not jobs:
        return False
    if room not in data.get("rooms", []):
        data["rooms"] = [*data.get("rooms", []), room]
        save_jobs(home, data)
    pack_id = home.name.partition("@")[0]
    if pending_count(home) > 0:
        schedule_pack_media(services, pack_id)
        return True
    return False


def schedule_pack_media(services: Services, pack_id: str) -> bool:
    """Start the background worker for ``pack_id`` unless one is already running. Returns
    True when a worker was started (or already active), False when there is no running loop."""
    if pack_id in _inflight:
        return True
    _inflight.add(pack_id)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _inflight.discard(pack_id)
        return False
    loop.create_task(_run_wrapped(services, pack_id))
    return True


async def resume_pending_queues(services: Services) -> None:
    """Re-schedule the worker for every installed pack with unfinished illustration jobs.

    The worker is an in-process task: a server restart kills it while the jobs sidecar keeps
    its pending/generating status, freezing the queue mid-render. Called once at startup so
    interrupted plates resume (and the bounded per-job budget eventually fails a truly hung
    one) instead of silently waiting forever."""
    homes = installed_pack_homes(Path(services.settings.data_dir))
    resumed = 0
    for pack_id, home in homes.items():
        try:
            if pending_count(home) > 0:
                schedule_pack_media(services, pack_id)
                resumed += 1
        except Exception:  # noqa: BLE001 — one unreadable sidecar must not stop the sweep
            continue
    if resumed:
        logger.info("[module-media] resumed %d illustration queue(s)", resumed)


async def _run_wrapped(services: Services, pack_id: str) -> None:
    try:
        await run_pack_media_jobs(services, pack_id)
    except Exception:  # noqa: BLE001 — a crashed worker must never take the process down
        logger.exception("[module-media] worker crashed for %s", pack_id)
    finally:
        _inflight.discard(pack_id)


def reset_inflight() -> None:
    """Test hook: clear the in-process worker registry."""
    _inflight.clear()


def world_card_paths(home: Path) -> list[Path]:
    """The pack's world-card file paths (manifest-declared `kind: world`), for the worker's
    image-binding rewrite and the detail page's re-plan source."""
    try:
        is_dev = home.resolve() in {Path(p).resolve() for p in DEV_PACK_HOMES.values()}
        manifest = parse_manifest_text(
            (home / MANIFEST_NAME).read_text(encoding="utf-8"), expect_trust=not is_dev
        )
    except Exception:  # noqa: BLE001 — an unreadable manifest yields no cards
        return []
    base = home.resolve()
    cards: list[Path] = []
    for card in manifest.card_entries:
        if card.kind != "world":
            continue
        try:
            path = (home / card.path).resolve(strict=True)
            path.relative_to(base)
        except (OSError, ValueError):
            continue
        if path.is_file():
            cards.append(path)
    return cards


async def _bind_card_images(home: Path, done: list[dict[str, Any]]) -> None:
    """Stamp completed illustrations onto the pack's world card (pregen avatars + worldbook
    entry images) so the bindings ride the card, survive re-import, and the detail page
    shows portraits beside their pregens. Idempotent: a re-render of the same subject binds
    the same value."""
    for card_path in world_card_paths(home):
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(card, dict):
            continue
        rewritten = False
        if _bind_pregen_portraits(card, done):
            rewritten = True
        if _bind_worldbook_images(card, done):
            rewritten = True
        if rewritten:
            try:
                atomic_write_private(card_path, json.dumps(card, ensure_ascii=False, indent=2) + "\n")
            except OSError as exc:
                logger.warning("[module-media] card rewrite failed %s: %s", card_path, exc)


def _append_manifest_asset(home: Path, asset_path: str, sha256: str, mime: str, size: int, title: str) -> bool:
    """Append one completed asset to the installed pack's manifest `assets:` list. The
    manifest is the runtime's content-addressed index (module detail, scene art, asset
    fetch); the generated `trust`/`files` blocks stay untouched -- nothing re-verifies them
    at runtime. Returns False when the manifest is unreadable."""
    try:
        raw = safe_load_no_aliases((home / MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("[module-media] manifest unreadable under %s", home)
        return False
    if not isinstance(raw, dict):
        return False
    assets = raw.get("assets")
    if not isinstance(assets, list):
        assets = []
    entry: dict[str, Any] = {"path": asset_path, "sha256": sha256, "mime": mime, "size": size}
    if title:
        entry["title"] = title
    # A re-render of the same plate (same provenance name) REPLACES the manifest entry:
    # keeping a stale digest would break the content-addressed fetch (bytes re-hashed against
    # the manifest before serving) exactly like the cover collision this guard prevents.
    for index, existing in enumerate(assets):
        if isinstance(existing, dict) and existing.get("path") == asset_path:
            assets[index] = entry
            break
    else:
        assets.append(entry)
    raw["assets"] = assets
    try:
        atomic_write_private(
            home / MANIFEST_NAME, yaml_dump(raw) + "\n"
        )
    except OSError as exc:
        logger.warning("[module-media] manifest write failed %s: %s", home, exc)
        return False
    return True


def yaml_dump(data: dict[str, Any]) -> str:
    """The manifest rewrite's serializer (isolated for tests); sort_keys mirrors the build."""
    import yaml

    return yaml.safe_dump(data, sort_keys=True, allow_unicode=True, default_flow_style=False)


async def _update_claimed_avatars(
    services: Services, rooms: list[str], subject: str, data: bytes, mime: str, name: str
) -> None:
    """Sync a freshly regenerated pregen portrait onto every room that has CLAIMED that
    pregen: register the new bytes in the room's media store and repoint the roster avatar,
    so the table sees the new portrait the moment the render lands -- not after a manual
    re-claim. Best-effort per room."""
    if not subject:
        return
    settings = services.settings.tui
    store = MediaStore(
        services.store,
        services.settings.data_dir,
        max_file_bytes=settings.media_max_file_bytes,
        room_quota_bytes=settings.media_room_quota_bytes,
        allowed_mimes=ALLOWED_IMAGE_MIMES,
    )
    for room in rooms:
        try:
            raw = await services.store.state_get(room, "party_roster")
        except Exception:  # noqa: BLE001
            continue
        if not raw:
            continue
        try:
            roster = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(roster, dict) or subject not in roster:
            continue
        try:
            record = await store.register_blob(
                room=room, data=data, mime=mime, name=name, uploader="keeper"
            )
        except Exception as exc:  # noqa: BLE001 -- quota rejection costs only this room
            logger.warning("[module-media] avatar register failed for %s in %s: %s", subject, room, exc)
            continue
        entry = roster[subject]
        if not isinstance(entry, dict):
            continue
        entry["avatar"] = record.ref()
        try:
            await services.store.state_set(room, "party_roster", json.dumps(roster, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[module-media] roster update failed for %s in %s: %s", subject, room, exc)


async def _register_in_rooms(
    services: Services, rooms: list[str], data: bytes, mime: str, name: str, kind: str, subject: str
) -> None:
    """Make a finished illustration usable inside each importing room: register the blob in
    the room's media store and append shot→image provenance to `module_media_index`, so
    `.image <kind> <subject>` reuses it as a reference. Best-effort per room."""
    settings = services.settings.tui
    store = MediaStore(
        services.store,
        services.settings.data_dir,
        max_file_bytes=settings.media_max_file_bytes,
        room_quota_bytes=settings.media_room_quota_bytes,
        allowed_mimes=ALLOWED_IMAGE_MIMES,
    )
    for room in rooms:
        try:
            record = await store.register_blob(room=room, data=data, mime=mime, name=name, uploader="keeper")
        except Exception as exc:  # noqa: BLE001 — quota/mime rejection costs only this room
            logger.warning("[module-media] room %s register failed %s: %s", room, name, exc)
            continue
        # Provenance parity with the old synchronous pass: every finished illustration maps
        # to its subject, so `.image <kind> <subject>` can reuse it as a generation reference.
        if subject:
            await _append_module_media_index(
                services, room, [{"kind": kind, "subject": subject, "hash": record.hash, "name": record.name}]
            )


async def run_pack_media_jobs(services: Services, pack_id: str) -> dict[str, Any]:
    """Render every pending/generating job for ``pack_id`` (the worker body, also awaited
    directly by tests). Returns the final jobs document."""
    homes = installed_pack_homes(Path(services.settings.data_dir))
    home = homes.get(pack_id)
    if home is None:
        return {}
    data = load_jobs(home)
    jobs = data.get("jobs", [])
    rooms = [r for r in data.get("rooms", []) if isinstance(r, str) and r]
    pending = [j for j in jobs if isinstance(j, dict) and j.get("status") in ("pending", "generating")]
    if not pending:
        return data
    room = rooms[0] if rooms else ""
    imagegen = await services.imagegen_for_room(room)
    if imagegen is None:
        _fail_all_pending(home, "not_configured")
        logger.warning("[module-media] no imagegen provider; failed pending jobs for %s", pack_id)
        return load_jobs(home)

    done: list[dict[str, Any]] = []
    # The worker loops with a FRESH view of the sidecar every round: a retry/requeue that
    # lands while an earlier render is still in flight must be picked up, never skipped by a
    # stale in-memory snapshot of the jobs list.
    while True:
        data = load_jobs(home)
        jobs = data.get("jobs", [])
        job = next(
            (j for j in jobs if isinstance(j, dict) and j.get("status") in ("pending", "generating")),
            None,
        )
        if job is None:
            break
        job_id = str(job.get("id") or "")
        if not allow_imagegen_request(services, room):
            # The room's hourly image cap is exhausted: fail the whole remaining queue with a
            # retryable reason (same stop-and-report stance as the old synchronous pass), so
            # every pending plate surfaces on the detail page with its retry button.
            _fail_all_pending(home, "rate_limited")
            break
        _update_job(home, job_id, status="generating")
        try:
            data_bytes, mime = await asyncio.wait_for(
                _imagegen_generate_retry(
                    imagegen, str(job.get("prompt") or ""), size=services.settings.imagegen.size
                ),
                # One illustration gets a bounded budget even though the provider client's own
                # timeout can be minutes (qwen: 300s) and the retry lane multiplies that: a hung
                # plate must FAIL and let the queue continue, never stall every later job.
                timeout=_JOB_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — provider down after bounded retries
            logger.warning("[module-media] imagegen failed for %s (%s): %s", job.get("id"), job.get("subject"), exc)
            _update_job(home, job_id, status="failed", error=("job_timeout" if isinstance(exc, TimeoutError) else str(exc)[:300]))
            continue
        stem = str(job.get("asset_stem") or f"module-{pack_id}-{job.get('kind')}")
        asset_name = f"{stem}{_IMAGE_MIME_EXTS.get(mime, '.png')}"
        asset_path = f"assets/{asset_name}"
        try:
            atomic_write_private(home / asset_path, data_bytes)
        except OSError as exc:
            _update_job(home, job_id, status="failed", error=f"asset_write: {exc}")
            continue
        digest = hashlib.sha256(data_bytes).hexdigest()
        subject = str(job.get("subject") or "")
        caption = str(job.get("caption") or "")
        _update_job(home, job_id, status="done", asset=asset_name, hash=digest, mime=mime)
        done.append(
            {
                "kind": str(job.get("kind") or ""),
                "subject": subject,
                "prompt": str(job.get("prompt") or ""),
                "caption": caption,
                "name": asset_name,
            }
        )
        # Wire the finished image into the pack: manifest entry (module detail + scene art +
        # asset fetch) and world-card bindings (pregens / worldbook entries).
        if not _append_manifest_asset(home, asset_path, digest, mime, len(data_bytes), subject or caption or None):
            logger.warning("[module-media] manifest append failed for %s", asset_name)
        await _bind_card_images(home, done)
        await _register_in_rooms(services, rooms, data_bytes, mime, asset_name, str(job.get("kind") or ""), subject)
        # A regenerated pregen portrait repoints the claimed character's avatar in every room
        # that has claimed the pregen — the table sees the new portrait immediately.
        if str(job.get("kind") or "") == "pregens":
            await _update_claimed_avatars(services, rooms, subject, data_bytes, mime, asset_name)
        logger.info("[module-media] %s rendered %s (%s)", pack_id, asset_name, subject)
    return load_jobs(home)
