"""TRPG_DEBUG__TOOL_TRACE — one JSON line per AI-KP tool call, off unless configured.

Per-room since the operator asked for it: a room's trace lives in its own file
(`.trace on` toggles the CALLING room only), so one table's probe never mixes
with another's. A legacy global file (env `TRPG_DEBUG__TOOL_TRACE`, or a
`.trace on` from before the per-room split) is the `""` room and still works.

Each line carries `ts`, the room's chat_key, the tool (or `model_call` /
`scribe` / `director` for non-tool decisions), and — for real calls — the
phase, keeper-only flag, arguments, result, latency, and the module id that
was active when the call happened (pack_id or source_id; "" in sandbox rooms),
so a room's file can be filtered by scenario after a module switch.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from infra import model_call_trace
from infra.file_permissions import ensure_private_directory, restrict_file

logger = logging.getLogger(__name__)

# Field cap: 1 MiB per field. Real prompts/completions/tool results are far
# smaller; the cap only exists so a pathological single field (a runaway tool
# result, a multi-MiB prompt) degrades instead of unboundedly ballooning the
# JSONL. The operator asked for the COMPLETE record — full input/output, not a
# 20k excerpt — so this is a safety valve, not a summary budget.
MAX_TRACE_FIELD_CHARS = 1_000_000

# room -> trace file. `""` is the legacy global/default room: a trace enabled by
# the env var or a pre-per-room toggle, which every room falls back to when it
# has no dedicated file of its own.
_TRACE_PATHS: dict[str, Path] = {}

# chat_key chars that are unsafe in a file name (`tui:group:table` -> `tui-group-table`).
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _open_private(path: str, flags: int) -> int:
    """`opener=` hook for `open()`: create (or reopen) the trace file at `0600`.

    Default umask leaves a freshly CREATED file at `0644` until `restrict_file`
    chmods it after the write returns — a window in which the first keeper-grade
    line sat in a world-readable file. Passing an explicit mode to `os.open`
    closes that window from the very first byte; `restrict_file` below still
    handles a pre-existing file whose mode predates this fix.
    """
    return os.open(path, flags, 0o600)


# The persisted `.trace` toggle: a server-level kv key (user_key="") holding a
# JSON map `{room: path}` (a bare string = the legacy global path, room "").
TOOL_TRACE_KV_KEY = "runtime_config.tool_trace"


def sanitize_room_key(chat_key: str) -> str:
    """A chat_key made safe as a file-name component."""
    return _FILENAME_UNSAFE.sub("-", str(chat_key or "room")).strip("-") or "room"


def default_trace_path(data_dir: str | Path, room: str = "") -> Path:
    """The default trace root for `room` under `data_dir` (private-mode).

    Per-room traces live in `traces/<sanitized room>/` — one DIRECTORY per room,
    one `.jsonl` file per scenario inside it (`<module>.jsonl`, or `default.jsonl`
    in sandbox rooms), so a room switching scenarios keeps separate files. The
    legacy global default keeps the plain `tool-trace.jsonl` so existing
    operators' env-var paths stay stable."""
    if room:
        return Path(data_dir) / "traces" / sanitize_room_key(room)
    return Path(data_dir) / "tool-trace.jsonl"


def persisted_trace_paths_sync(store: Any) -> dict[str, str]:
    """The `.trace` toggles the operator last persisted: {room: path}.

    Sync read via a short-lived sqlite connection, mirroring
    ``RuntimeConfig.load_sync`` — called from the synchronous ``build_services``
    before the app's event loop exists. A bare string value (the pre-per-room
    format) maps onto the global room ``""``. Best-effort: a missing DB/row
    reads {}."""
    path = str(getattr(store, "path", "") or "")
    if path == ":memory:" or not path or not os.path.exists(path):
        return {}
    try:
        import sqlite3

        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT value FROM kv WHERE user_key = '' AND store_key = ?",
                (TOOL_TRACE_KV_KEY,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {}
        raw = str(row[0] or "").strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"": raw}
        if not isinstance(value, dict):
            return {"": raw}
        return {str(room): str(p).strip() for room, p in value.items() if str(p).strip()}
    except sqlite3.Error:
        return {}


def _resolve_path(data_dir: str | Path, room: str, path: str | Path | None) -> Path | None:
    """Absolute trace path for `room`: `path` as given (absolute or under
    data_dir), else the per-room default under data_dir. None disables."""
    if path is None or str(path) == "":
        return None
    candidate = Path(str(path))
    if candidate.is_absolute():
        return candidate
    return Path(str(data_dir)) / candidate


def enable_tool_trace(path: str | Path | None, *, room: str = "") -> None:
    """Point `room`'s trace at `path` (absolute), or disable it with `None`/empty.

    `room=""` is the legacy global trace every room without its own file falls
    back to — the env-var path (`TRPG_DEBUG__TOOL_TRACE`). The directory is
    created private (`0700`, like every other secret-bearing writer in the repo)
    and the file is held at `0600` after each write. The per-model-call sink is
    installed while ANY trace exists."""
    if path is None or str(path) == "":
        _TRACE_PATHS.pop(room, None)
    else:
        target = Path(str(path))
        try:
            ensure_private_directory(target.parent, tighten_existing=False)
            if not target.suffix:
                # Directory-style trace root (per-room `traces/<room>`): create the
                # directory itself; flat `.jsonl` targets only need their parent.
                ensure_private_directory(target, tighten_existing=False)
        except OSError:
            logger.warning("tool trace directory is unwritable; tracing off: %s", target, exc_info=True)
            _TRACE_PATHS.pop(room, None)
            return
        _TRACE_PATHS[room] = target
    model_call_trace.set_sink(_model_call_sink if _TRACE_PATHS else None)


def disable_tool_trace(room: str = "") -> None:
    """Turn `room`'s trace off (no-op when it was not on)."""
    _TRACE_PATHS.pop(room, None)
    if not _TRACE_PATHS:
        model_call_trace.set_sink(None)


def _path_for(chat_key: str, module: str = "") -> Path | None:
    """The file `chat_key`'s rows land in: its own trace root (a directory —
    `traces/<room>/<module>.jsonl`, or `default.jsonl` without a module — one
    file per scenario), else the legacy flat `.jsonl` global default."""
    base = _TRACE_PATHS.get(chat_key) or _TRACE_PATHS.get("")
    if base is None:
        return None
    if base.suffix == ".jsonl":
        # Legacy flat file: keep the old naming for both variants.
        if not module:
            return base
        return base.with_name(f"{base.stem}-{sanitize_room_key(module)}{base.suffix}")
    name = sanitize_room_key(module) if module else "default"
    return base / f"{name}.jsonl"


def _model_call_sink(payload: dict[str, Any]) -> None:
    """`infra.model_call_trace` → one probe row. `chat_key` becomes the row's room
    column, `module` (stamped into the lane by the loop) the scenario file."""
    row = dict(payload)
    chat_key = str(row.pop("chat_key", "") or "")
    module = str(row.pop("module", "") or "")
    trace_event("model_call", row, chat_key=chat_key, module=module)


def tool_trace_enabled(room: str = "") -> bool:
    """Whether `room`'s calls are traced: its own toggle, or the global default."""
    return bool(_TRACE_PATHS.get(room) or _TRACE_PATHS.get(""))


def _capped(value: Any) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_TRACE_FIELD_CHARS else text[:MAX_TRACE_FIELD_CHARS] + "…"


def trace_event(kind: str, payload: dict[str, Any], *, chat_key: str = "", module: str = "") -> None:
    """Append one NON-tool decision to the same trace, under `tool: <kind>`.

    The tool seam only sees what the model asked for. Two lanes decide things that never
    become a tool call — the Scribe's per-turn 场记 verdict and the Stage Director's
    performance decision — and the 2026-08-19 run-2 could not explain why the whole
    session produced zero images, because neither lane left a trace. One reader, one
    file, one shape: a consumer filters on `tool` exactly as it does for a real call.

    Zero cost when the trace is off (the guard is the first statement) and best-effort
    when it is on, like every other writer here.
    """
    target = _path_for(chat_key, module)
    if target is None:
        return
    try:
        line = json.dumps(
            {
                "ts": round(time.time(), 3),
                "room": chat_key,
                "module": str(module or ""),
                "tool": str(kind),
                "event": _capped(payload or {}),
            },
            ensure_ascii=False,
        )
        with open(target, "a", encoding="utf-8", opener=_open_private) as handle:
            handle.write(line + "\n")
        restrict_file(target)
    except Exception:  # noqa: BLE001 — see module docstring
        logger.debug("tool trace event write failed", exc_info=True)


async def active_module_id(services: Any, chat_key: str) -> str:
    # The active module id (pack_id or source_id; "" in sandbox rooms), for
    # stamping trace rows so a room's files split by scenario. Best-effort —
    # the trace must never cost the call that fed it.
    try:
        from agent.module_lifecycle import active_module

        active = await active_module(services, chat_key)
        return str(active.get("pack_id") or active.get("source_id") or "") if active else ""
    except Exception:
        return ""


def record_tool_call(
    *,
    chat_key: str,
    phase: str | None,
    name: str,
    arguments: Any,
    result: str,
    keeper_only: bool | None,
    started: float,
    module: str = "",
) -> None:
    """Append one call to the trace (`started` is a `time.perf_counter()` reading)."""
    target = _path_for(chat_key, module)
    if target is None:
        return
    try:
        line = json.dumps(
            {
                "ts": round(time.time(), 3),
                "ms": round((time.perf_counter() - started) * 1000, 1),
                "room": chat_key,
                "module": str(module or ""),
                "tool": name,
                "phase": phase or "",
                "keeper_only": keeper_only,
                "args": _capped(arguments or {}),
                "result": _capped(result),
            },
            ensure_ascii=False,
        )
        with open(target, "a", encoding="utf-8", opener=_open_private) as handle:
            handle.write(line + "\n")
        restrict_file(target)
    except Exception:  # noqa: BLE001 — see module docstring
        logger.debug("tool trace write failed", exc_info=True)
