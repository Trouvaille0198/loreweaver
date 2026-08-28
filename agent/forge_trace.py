"""Per-module-generation trace: one JSONL per forge session.

A module generation (``agent.forge.generate_and_install_module`` /
``generate_and_install_pack_module``) opens a session; every LLM call the forge
makes records its FULL input messages and output content, and every key
tool/function call (media planning, pack build, pack install, room import,
document upload) records its inputs and result — unconditionally, independent
of the room's ``.trace`` toggle. The room tool trace only carries model-call
METADATA (lane, latency, tokens), not messages; the operator asked for the
complete module-authoring record.

Rows land under ``<data_dir>/traces/forge/<ts>-<pid>.jsonl`` — one file per
generation run, one JSON object per line:

- ``forge_start`` — the session's premise and options.
- ``forge_llm_call`` — one authoring call: ``lane`` (pack_world_card /
  media_shot_list / companion_skill / …), the FULL ``messages`` sent, the
  ``content`` returned (or ``ok: false`` + ``error``), wall-clock ``duration_s``.
- ``forge_tool_call`` — one function/tool invocation: ``tool`` name, its
  ``input`` and ``output``, ``duration_s``.
- ``forge_end`` — the outcome: ``ok``, ``pack_id``, artifact ``path``.

No active session → every helper is a free no-op (one ContextVar read), so the
skill/rulepack/prompt-assistant generators that never open a session cost
nothing.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from infra.file_permissions import ensure_private_directory

logger = logging.getLogger(__name__)

# Field cap: 1 MiB per field — a safety valve so a pathological single field (a
# runaway tool result, a multi-MiB prompt) degrades instead of ballooning the
# JSONL unboundedly. Real prompts/completions are far smaller; the operator
# asked for the COMPLETE record, not a 20k excerpt.
MAX_FORGE_TRACE_FIELD_CHARS = 1_000_000

_SESSION: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "forge_trace_session", default=None
)


def _capped(value: Any) -> Any:
    """Cap one field to the trace budget — RECURSIVELY, so a dict/list field (the FULL
    ``messages`` array) keeps its structure while oversized strings inside it are truncated.
    A blanket ``json.dumps`` of a non-string field would flatten ``messages`` into a string,
    destroying the very structure the operator asked to record."""
    if isinstance(value, str):
        return value if len(value) <= MAX_FORGE_TRACE_FIELD_CHARS else value[:MAX_FORGE_TRACE_FIELD_CHARS] + "…"
    if isinstance(value, dict):
        return {key: _capped(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_capped(item) for item in value]
    return value


def log_forge(kind: str, payload: dict[str, Any]) -> None:
    """Append one row to the active session's JSONL. Never raises; free when no session."""
    session = _SESSION.get()
    if session is None:
        return
    try:
        row = {"type": kind, "ts": round(time.time(), 3), **{k: _capped(v) for k, v in payload.items()}}
        with open(session["path"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 — the trace must never cost the generation it records
        logger.debug("forge trace write failed", exc_info=True)


def set_forge_pack(pack_id: str) -> None:
    """Stamp the session's pack/module id once it is known (mid-run)."""
    session = _SESSION.get()
    if session is not None:
        session["pack_id"] = pack_id


def set_forge_result(ok: bool, path: str = "", error: str = "") -> None:
    """Stamp the session's outcome as its closing row — call right before the
    generator returns, from inside the session."""
    session = _SESSION.get()
    if session is None:
        return
    log_forge(
        "forge_end",
        {"ok": ok, "pack_id": session.get("pack_id", ""), "path": path, "error": error},
    )


@contextlib.asynccontextmanager
async def forge_session(
    data_dir: Path | str,
    description: str,
    options: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Open a module-generation session: create ``traces/forge/`` and start a
    JSONL whose path is visible to every ``_llm_authored`` call and tool hook in
    this task (a ContextVar), so nested coroutines record into the same file.
    A session that cannot start (unwritable data dir) degrades to no trace —
    generation never fails over logging."""
    session: dict[str, Any] | None = None
    try:
        root = Path(data_dir) / "traces" / "forge"
        ensure_private_directory(root)
        path = root / f"{int(time.time() * 1000)}-{os.getpid()}.jsonl"
        start_row = {
            "type": "forge_start",
            "ts": round(time.time(), 3),
            "description": description,
            "options": options or {},
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(start_row, ensure_ascii=False, default=str) + "\n")
        session = {"path": path, "pack_id": ""}
    except Exception:  # noqa: BLE001 — a trace that cannot start must not fail the generation
        logger.debug("forge trace session start failed", exc_info=True)
    if session is None:
        yield {}
        return
    token = _SESSION.set(session)
    try:
        yield session
    finally:
        _SESSION.reset(token)
