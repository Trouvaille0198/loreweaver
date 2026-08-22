"""Web-only keeper admin extension for room module source files.

The engine's published protocol predates this surface. The bridge deliberately
uses the existing ``admin_generated`` reply lane so older clients keep parsing
all responses; ``serve_both.py`` installs it on the web server's AdminService.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.context import AgentCtx, LocalFs
from agent.kp_tools_knowledge import DocumentTools
from core.documents import KEEPER_VIEWER, MODULE_POOL_ID
from net.admin import AdminService, _error
from net.room_backup import chat_key_for_room

_ALLOWED_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
_CUSTOM_KINDS = frozenset(
    {
        "module_list",
        "module_detail",
        "module_upload",
        "module_import",
        "module_delete",
    }
)
_MAX_SOURCE_BYTES = 2 * 1024 * 1024


def install_module_admin(admin: AdminService) -> AdminService:
    """Wrap the engine admin service with module-source operations."""
    if isinstance(admin, ModuleAdminService):
        return admin
    return ModuleAdminService(admin)


class ModuleAdminService:
    """Intercept module-source actions and delegate every other admin frame."""

    def __init__(self, inner: AdminService) -> None:
        self.inner = inner
        self.services = inner.services
        self.keystore = inner.keystore
        self.fs = inner.fs
        self.hub = inner.hub

    async def dispatch(
        self,
        role: str,
        caller_room: str,
        frame: dict[str, Any],
        i18n: Any,
        *,
        reauthorize: Any = None,
    ) -> dict[str, Any]:
        kind = frame.get("kind") if frame.get("type") == "admin_generate" else None
        if kind not in _CUSTOM_KINDS:
            return await self.inner.dispatch(
                role,
                caller_room,
                frame,
                i18n,
                reauthorize=reauthorize,
            )
        if role != "keeper":
            return _error("forbidden", i18n)
        try:
            return await self._dispatch_module(caller_room, frame, i18n)
        except ValueError:
            return _error("bad_request", i18n)
        except OSError as exc:
            return _module_reply(str(kind), False, "", {"error": str(exc)})

    async def _dispatch_module(self, caller_room: str, frame: dict[str, Any], i18n: Any) -> dict[str, Any]:
        kind = str(frame.get("kind"))
        root = self._root()
        root.mkdir(parents=True, exist_ok=True)
        payload = self._payload(frame)
        if kind == "module_list":
            return await self._list(caller_room, root)
        if kind == "module_detail":
            return await self._detail(caller_room, root, str(payload.get("name") or ""))
        if kind == "module_upload":
            return await self._upload(root, payload)
        if kind == "module_import":
            return await self._import(caller_room, root, payload, i18n)
        if kind == "module_delete":
            return await self._delete(caller_room, root, payload)
        raise ValueError("unknown module action")

    def _root(self) -> Path:
        return Path(self.services.settings.data_dir).resolve() / "modules"

    @staticmethod
    def _payload(frame: dict[str, Any]) -> dict[str, Any]:
        raw = frame.get("description")
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"name": raw.strip()}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _safe_name(raw: str) -> str:
        name = raw.strip()
        path = Path(name)
        if not name or path.name != name or name in {".", ".."}:
            raise ValueError("invalid module filename")
        if any(ord(char) < 32 for char in name):
            raise ValueError("invalid module filename")
        if path.suffix.casefold() not in _ALLOWED_SUFFIXES:
            raise ValueError("unsupported module filename")
        return name

    @classmethod
    def _path(cls, root: Path, raw: str) -> tuple[str, Path]:
        name = cls._safe_name(raw)
        target = root / name
        if target.is_symlink():
            raise ValueError("symlink module source")
        resolved = target.resolve()
        if resolved.parent != root.resolve():
            raise ValueError("module path escapes source directory")
        return name, target

    @classmethod
    def _files(cls, root: Path) -> list[tuple[str, Path]]:
        root.mkdir(parents=True, exist_ok=True)
        return [
            (path.name, path)
            for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
            if path.is_file() and not path.is_symlink() and path.suffix.casefold() in _ALLOWED_SUFFIXES
        ]

    async def _current_name(self, caller_room: str, root: Path, files: list[tuple[str, Path]]) -> str:
        chat_key = chat_key_for_room(caller_room)
        recorded = str(await self.services.store.state_get(chat_key, "module_source") or "")
        if recorded:
            try:
                name, _ = self._path(root, recorded)
            except ValueError:
                name = ""
            if name and any(candidate == name for candidate, _ in files):
                return name
        fulltext = str(await self.services.store.state_get(chat_key, "module_fulltext") or "")
        if not fulltext:
            return ""
        for name, path in files:
            try:
                if path.read_text(encoding="utf-8") == fulltext:
                    return name
            except (OSError, UnicodeDecodeError):
                continue
        return ""

    async def _list(self, caller_room: str, root: Path) -> dict[str, Any]:
        files = self._files(root)
        current = await self._current_name(caller_room, root, files)
        modules = []
        for name, path in files:
            stat = path.stat()
            modules.append(
                {
                    "name": name,
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime * 1000),
                    "current": name == current,
                }
            )
        chat_key = chat_key_for_room(caller_room)
        status = str(await self.services.store.state_get(chat_key, "module_init_status") or "")
        return _module_reply(
            "module_list",
            True,
            current,
            {"modules": modules, "current": current, "status": status},
        )

    async def _detail(self, caller_room: str, root: Path, raw_name: str) -> dict[str, Any]:
        name, path = self._path(root, raw_name)
        if not path.is_file():
            return _module_reply("module_detail", False, name, {"error": "source_not_found"})
        content = path.read_text(encoding="utf-8")
        files = self._files(root)
        current = await self._current_name(caller_room, root, files)
        chat_key = chat_key_for_room(caller_room)
        status = str(await self.services.store.state_get(chat_key, "module_init_status") or "")
        pool = None
        if current == name:
            pool = await self.services.documents.get_view(chat_key, "module_pool", MODULE_POOL_ID, KEEPER_VIEWER)
        stat = path.stat()
        return _module_reply(
            "module_detail",
            True,
            name,
            {
                "name": name,
                "size": stat.st_size,
                "modified": int(stat.st_mtime * 1000),
                "content": content,
                "current": current == name,
                "status": status if current == name else "",
                "pool": pool,
            },
        )

    async def _upload(self, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        name, path = self._path(root, str(payload.get("name") or ""))
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty module content")
        raw = content.encode("utf-8")
        if len(raw) > _MAX_SOURCE_BYTES:
            raise ValueError("module source too large")
        path.write_bytes(raw)
        return _module_reply("module_upload", True, name, {"name": name})

    async def _import(self, caller_room: str, root: Path, payload: dict[str, Any], i18n: Any) -> dict[str, Any]:
        name, path = self._path(root, str(payload.get("name") or ""))
        if not path.is_file():
            return _module_reply("module_import", False, name, {"error": "source_not_found"})
        source_text = path.read_text(encoding="utf-8")
        chat_key = chat_key_for_room(caller_room)
        ctx = AgentCtx(
            chat_key=chat_key,
            user_id="keeper",
            platform="tui",
            locale=i18n.locale,
            fs=LocalFs(root),
            extra={"role": "keeper"},
        )
        receipt = await DocumentTools(self.services).upload_document(ctx, file_path=name, doc_type="module")
        status = str(await self.services.store.state_get(chat_key, "module_init_status") or "")
        installed_text = str(await self.services.store.state_get(chat_key, "module_fulltext") or "")
        ok = status in {"ready", "ready_fallback"} and installed_text == source_text
        if ok:
            await self.services.store.state_set(chat_key, "module_source", name)
        return _module_reply(
            "module_import",
            ok,
            name,
            {"receipt": receipt, "status": status, "current": ok},
        )

    async def _delete(self, caller_room: str, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        name, path = self._path(root, str(payload.get("name") or ""))
        if not path.is_file():
            return _module_reply("module_delete", False, name, {"error": "source_not_found"})
        current = await self._current_name(caller_room, root, self._files(root))
        if current == name:
            return _module_reply("module_delete", False, name, {"error": "module_in_use"})
        path.unlink()
        return _module_reply("module_delete", True, name, {"name": name})


def _module_reply(kind: str, ok: bool, name: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "admin_generated",
        "kind": kind,
        "ok": ok,
        "id": name,
        "name": name,
        "error": "" if ok else str(detail.get("error") or "module_operation_failed"),
        "detail": json.dumps(detail, ensure_ascii=False),
    }
