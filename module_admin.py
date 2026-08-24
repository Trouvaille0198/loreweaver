"""Web-only keeper admin extension for room module source files.

The engine's published protocol predates this surface. The bridge deliberately
uses the existing ``admin_generated`` reply lane so older clients keep parsing
all responses; ``serve_both.py`` installs it on the web server's AdminService.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import core.pack as core_pack
from agent.context import AgentCtx, LocalFs
from agent.kp_tools_knowledge import DocumentTools
from agent.module_lifecycle import active_module
from core.documents import KEEPER_VIEWER, MODULE_POOL_ID
from core.worldbook import LORE_DOC_TYPE
from core.yaml_safety import safe_load_no_aliases
from infra.media_store import ALLOWED_MEDIA_MIMES, MediaStore
from net.admin import AdminService, _error
from net.room_backup import chat_key_for_room

logger = logging.getLogger(__name__)

_ALLOWED_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
_WORLDBOOK_SUFFIXES = frozenset({".json"})
_BUNDLE_SUFFIX = ".zip"
_CUSTOM_KINDS = frozenset(
    {
        "module_list",
        "module_detail",
        "module_upload",
        "module_update",
        "module_bundle_upload",
        "module_import",
        "module_delete",
        "worldbook_list",
        "worldbook_detail",
        "worldbook_upload",
        "worldbook_select",
        "worldbook_disable",
    }
)
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_MAX_BUNDLE_FILES = 128


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
        emit_frame: Any = None,
    ) -> dict[str, Any]:
        kind = frame.get("kind") if frame.get("type") == "admin_generate" else None
        if kind not in _CUSTOM_KINDS:
            return await self.inner.dispatch(
                role,
                caller_room,
                frame,
                i18n,
                reauthorize=reauthorize,
                emit_frame=emit_frame,
            )
        if role != "keeper":
            return _error("forbidden", i18n)
        try:
            # NO room lock here: the transport choke point (`net.session._on_frame`) already
            # holds the room's `turn_lock` around the ENTIRE `admin_generate` dispatch. That
            # lock is a plain (non-reentrant) `asyncio.Lock`; re-acquiring the same one in
            # this task self-deadlocks the import and strands the room lock forever, which
            # also wedges every later module request for the room (the import never answers
            # and never releases). Taking the lock exactly once, at the choke point, is what
            # keeps module_import serialized against concurrent turns.
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
        if kind == "module_update":
            return await self._update(caller_room, root, payload)
        if kind == "module_bundle_upload":
            return await self._bundle_upload(root, payload)
        if kind == "module_import":
            requested_locale = str(payload.get("locale") or "").replace("_", "-").split("-", 1)[0].casefold()
            import_i18n = i18n.with_locale(requested_locale) if requested_locale in {"en", "zh"} else i18n
            return await self._import(caller_room, root, payload, import_i18n)
        if kind == "module_delete":
            return await self._delete(caller_room, root, payload)
        worldbook_root = self._worldbook_root()
        worldbook_root.mkdir(parents=True, exist_ok=True)
        if kind == "worldbook_list":
            return await self._worldbook_list(caller_room, worldbook_root)
        if kind == "worldbook_detail":
            return await self._worldbook_detail(caller_room, worldbook_root, str(payload.get("name") or ""))
        if kind == "worldbook_upload":
            return await self._worldbook_upload(worldbook_root, payload)
        if kind == "worldbook_select":
            return await self._worldbook_select(caller_room, worldbook_root, payload, i18n)
        if kind == "worldbook_disable":
            return await self._worldbook_disable(caller_room)
        raise ValueError("unknown admin action")

    def _root(self) -> Path:
        return Path(self.services.settings.data_dir).resolve() / "modules"

    def _worldbook_root(self) -> Path:
        return Path(self.services.settings.data_dir).resolve() / "worldbooks"

    @staticmethod
    def _pack_dir_size(home: Path) -> int:
        """Total bytes of an installed pack home (cards + assets + manifest), for the library row."""
        total = 0
        try:
            for entry in home.rglob("*"):
                if entry.is_file():
                    total += entry.stat().st_size
        except OSError:
            pass
        return total

    @staticmethod
    def _pack_world_cards(home: Path) -> tuple[Any | None, list[tuple[Any, Path]]]:
        """Manifest-declared world cards, preserving nested paths and file formats."""
        manifest_path = home / core_pack.MANIFEST_NAME
        if not manifest_path.is_file():
            return None, []
        text = manifest_path.read_text(encoding="utf-8")
        manifest = None
        for expect_trust in (True, False):
            try:
                manifest = core_pack.parse_manifest_text(text, expect_trust=expect_trust)
                break
            except Exception:
                continue
        if manifest is None:
            return None, []
        cards: list[tuple[Any, Path]] = []
        is_dev = home in core_pack.DEV_PACK_HOMES.values()
        base = home.resolve()
        for card in manifest.card_entries:
            try:
                path = (home / card.path).resolve(strict=True)
                path.relative_to(base)
            except (OSError, ValueError):
                continue
            kind = card.kind
            if is_dev:
                try:
                    kind = core_pack.detect_card_kind(card.path, path.read_bytes())
                except Exception:
                    continue
            if kind == "world" and path.is_file():
                cards.append((card, path))
        return manifest, cards

    async def _active_module(self, caller_room: str) -> dict[str, Any]:
        from agent.module_lifecycle import active_module

        return await active_module(self.services, chat_key_for_room(caller_room)) or {}
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

    @staticmethod
    def _title(content: str, name: str) -> str:
        for line in content.splitlines():
            candidate = line.strip()
            if candidate.startswith("# "):
                title = candidate[2:].strip()
                if title:
                    return title
        return Path(name).stem

    async def _pack_detail(self, caller_room: str, raw_name: str) -> dict[str, Any]:
        """Detail for an installed .lwpack content pack, read directly from its bundled world
        card (no room import needed): the card's name/description/scenario/opening, its worldbook
        entries, typed variables, and pregen cast."""
        from gateway.panels import installed_pack_homes

        pack_id, _, selected_path = raw_name.strip().partition("/")
        homes = installed_pack_homes(Path(self.services.settings.data_dir))
        home = homes.get(pack_id)
        if home is None:
            return _module_reply("module_detail", False, raw_name, {"error": "source_not_found"})
        manifest, world_cards = self._pack_world_cards(home)
        if manifest is None or not world_cards:
            return _module_reply("module_detail", False, raw_name, {"error": "source_not_found"})
        if selected_path:
            world_cards = [pair for pair in world_cards if pair[0].path == selected_path]
            if not world_cards:
                return _module_reply("module_detail", False, raw_name, {"error": "source_not_found"})
        title = dict(manifest.name).get("en") or pack_id
        description = dict(manifest.description).get("en") or ""

        # Read the pack's own world card(s): its lore, variables, pregens, and prose. The world
        # card is the pack's module content — showing it needs no room import.
        entries: list[dict[str, Any]] = []
        variables: list[dict[str, Any]] = []
        pregens: list[dict[str, str]] = []
        scenario = ""
        opening = ""
        for _card_entry, card_path in world_cards:
            try:
                card = json.loads(card_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — skip an unreadable card
                continue
            if not isinstance(card, dict):
                continue
            for entry in card.get("worldbook") or []:
                if not isinstance(entry, dict):
                    continue
                keys = entry.get("keys") or []
                if isinstance(keys, str):
                    keys = [keys]
                keys = [str(k) for k in keys if str(k).strip()]
                category = str(entry.get("category") or "lore")
                entry_title = str(entry.get("title") or entry.get("comment") or entry.get("name") or "").strip()
                if not entry_title:
                    entry_title = keys[0] if keys else category
                entries.append(
                    {
                        "title": entry_title,
                        "content": str(entry.get("content") or ""),
                        "keys": keys,
                        "secret": bool(entry.get("secret", False)),
                    }
                )
            variables.extend([dict(v) for v in (card.get("variables") or []) if isinstance(v, dict)])
            pregens.extend(
                {"name": str(p.get("name", "")), "concept": str(p.get("concept") or p.get("blurb") or "")}
                for p in (card.get("pregens") or [])
                if isinstance(p, dict) and p.get("name")
            )
            scenario = str(card.get("scenario") or "") or scenario
            opening = str(card.get("opening") or "") or opening

        # The pack's bundled rulepack(s) and KP skill(s) install into the shared discovery dirs
        # (`data/rulepacks/`, `data/skills/`) — not the pack home — so resolve them by the
        # manifest's declared `contents` and read from those dirs.
        data_dir = Path(self.services.settings.data_dir)
        rulepacks_dir = data_dir / "rulepacks"
        skills_dir = data_dir / "skills"
        rulepacks: list[dict[str, Any]] = []
        for declared in manifest.contents["rulepacks"]:
            name = PurePosixPath(declared).name
            rp_file = rulepacks_dir / name
            if not rp_file.is_file():
                continue
            try:
                rp_text = rp_file.read_text(encoding="utf-8")
            except OSError:
                continue
            names: list[str] = []
            try:
                rp = safe_load_no_aliases(rp_text)
                if isinstance(rp, dict):
                    names = [str(n) for n in (rp.get("names") or []) if str(n).strip()]
            except Exception:  # noqa: BLE001
                pass
            rulepacks.append(
                {
                    "name": rp_file.stem,
                    "title": names[0] if names else rp_file.stem,
                    "content": rp_text[:8000],
                }
            )
        skills: list[dict[str, str]] = []
        for declared in manifest.contents["skills"]:
            skill_id = PurePosixPath(declared).name
            skill_file = skills_dir / skill_id / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                skills.append({"name": skill_id, "content": skill_file.read_text(encoding="utf-8")[:8000]})
            except OSError:
                continue

        content_parts = [part for part in (description, scenario, opening) if part]
        content = "\n\n".join(content_parts)
        # The pack's bundled asset illustrations (installed under the pack home, content-addressed
        # by path) — the images a module ships with, shown in the detail view.
        media: list[dict[str, Any]] = []
        for asset_path in manifest.assets:
            p = home / asset_path.path
            if not p.is_file():
                continue
            # Figure kind from the provenance name (`module-<id>-<kind>-<n>.jpg`): cover/scenes/npcs/items.
            kind = "asset"
            stem = p.stem
            for token in ("cover", "scenes", "npcs", "items", "item"):
                if token in stem:
                    kind = token
                    break
            try:
                data = p.read_bytes()
            except OSError:
                continue
            media.append(
                {
                    "name": p.name,
                    "hash": asset_path.sha256,
                    "mime": asset_path.mime,
                    "size": asset_path.size,
                    "kind": kind,
                    "data": base64.b64encode(data[:512 * 1024]).decode("ascii"),
                }
            )
        return _module_reply(
            "module_detail",
            True,
            raw_name,
            {
                "name": raw_name,
                "title": title,
                "size": self._pack_dir_size(home),
                "modified": int(home.stat().st_mtime * 1000),
                "content": content,
                "source_kind": "pack",
                "current": (
                    (await self._active_module(caller_room)).get("pack_id") == pack_id
                    and (
                        not selected_path
                        or (await self._active_module(caller_room)).get("card_path") == selected_path
                    )
                ),
                "status": "ready",
                "import_status": "",
                "importing": False,
                "pool": None,
                "media": media,
                "worldbook_entries": entries,
                "variables": variables,
                "pregens": pregens,
                "rulepacks": rulepacks,
                "skills": skills,
            },
        )

    async def _current_name(self, caller_room: str, root: Path, files: list[tuple[str, Path]]) -> str:
        chat_key = chat_key_for_room(caller_room)
        active = await self._active_module(caller_room)
        if active.get("kind") == "text":
            source = str(active.get("source") or "")
            if any(candidate == source for candidate, _ in files):
                return source
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
        chat_key = chat_key_for_room(caller_room)
        active = await self._active_module(caller_room)
        import_status = str(await self.services.store.state_get(chat_key, "module_import_status") or "")
        importing_name = str(await self.services.store.state_get(chat_key, "module_import_name") or "")
        modules = []
        for name, path in files:
            stat = path.stat()
            modules.append(
                {
                    "name": name,
                    "title": self._title(path.read_text(encoding="utf-8"), name),
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime * 1000),
                    "source_kind": "text",
                    "current": name == current,
                    "importing": name == importing_name and import_status == "processing",
                }
            )
        # Installed .lwpack content packs are module sources too — list them alongside the
        # Markdown text sources so the library shows every importable module in one place,
        # distinguished by `source_kind`.
        from gateway.panels import installed_pack_homes

        for pack_id, home in sorted(installed_pack_homes(Path(self.services.settings.data_dir)).items()):
            manifest, world_cards = self._pack_world_cards(home)
            if manifest is None or not world_cards:
                continue
            display = dict(manifest.name).get("en") or pack_id
            stat = home.stat()
            # Summarize the pack's content for the library row: lore entry count + pregen cast size.
            entry_count = 0
            pregen_count = 0
            for _card_entry, card_path in world_cards:
                if card_path.suffix.casefold() == ".json":
                    try:
                        card = json.loads(card_path.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001 — skip an unreadable card
                        continue
                    if not isinstance(card, dict):
                        continue
                    entry_count += len([e for e in (card.get("worldbook") or []) if isinstance(e, dict)])
                    pregen_count += len([p for p in (card.get("pregens") or []) if isinstance(p, dict)])
            for card_entry, _card_path in world_cards:
                module_name = pack_id if len(world_cards) == 1 else f"{pack_id}/{card_entry.path}"
                modules.append(
                    {
                    "name": module_name,
                    "title": display if len(world_cards) == 1 else f"{display} — {PurePosixPath(card_entry.path).stem}",
                    "size": self._pack_dir_size(home),
                    "modified": int(stat.st_mtime * 1000),
                    "source_kind": "pack",
                    "entry_count": entry_count,
                    "pregen_count": pregen_count,
                    "current": bool(
                        active.get("pack_id") == pack_id
                        and active.get("card_path") == card_entry.path
                    ),
                    "importing": False,
                    }
                )
        status = str(await self.services.store.state_get(chat_key, "module_init_status") or "")
        visible_current = current
        if not visible_current and active.get("kind") == "world_card":
            pack_id = str(active.get("pack_id") or "")
            card_path = str(active.get("card_path") or "")
            if pack_id:
                active_home = installed_pack_homes(Path(self.services.settings.data_dir)).get(pack_id)
                cards = self._pack_world_cards(active_home)[1] if active_home is not None else []
                visible_current = (
                    pack_id if len(cards) == 1 else f"{pack_id}/{card_path}" if card_path else pack_id
                )
        return _module_reply(
            "module_list",
            True,
            visible_current,
            {
                "modules": modules,
                "current": visible_current,
                "status": status or ("ready" if active else ""),
            },
        )

    async def _detail(self, caller_room: str, root: Path, raw_name: str) -> dict[str, Any]:
        try:
            name, path = self._path(root, raw_name)
        except ValueError:
            name, path = "", None
        if path is None or not path.is_file():
            # Not a Markdown source — it may be an installed .lwpack content pack, which has a
            # detail too (its name, description, size; plus its worldbook entries if the room
            # has imported it). A pack detail is still a valid module detail.
            return await self._pack_detail(caller_room, raw_name)
        content = path.read_text(encoding="utf-8")
        files = self._files(root)
        current = await self._current_name(caller_room, root, files)
        chat_key = chat_key_for_room(caller_room)
        status = str(await self.services.store.state_get(chat_key, "module_init_status") or "")
        import_status = str(await self.services.store.state_get(chat_key, "module_import_status") or "")
        import_name = str(await self.services.store.state_get(chat_key, "module_import_name") or "")
        pool = None
        media_records: list[dict[str, Any]] = []
        if current == name:
            pool = await self.services.documents.get_view(chat_key, "module_pool", MODULE_POOL_ID, KEEPER_VIEWER)
            # Forge-generated illustrations carry the `module-<id>-` provenance prefix; they are
            # room-scoped like the pool, so they surface exactly where the pool does.
            module_id = name[:-3] if name.endswith(".md") else name
            prefix = f"module-{module_id}-"
            tui = self.services.settings.tui
            store = MediaStore(
                self.services.store,
                self.services.settings.data_dir,
                max_file_bytes=max(tui.media_max_file_bytes, tui.audio_max_file_bytes),
                room_quota_bytes=max(tui.media_room_quota_bytes, tui.audio_room_quota_bytes),
                allowed_mimes=ALLOWED_MEDIA_MIMES,
            )
            media_records = [
                {"name": record.name, "hash": record.hash, "mime": record.mime, "size": record.size}
                for record in await store.list_room_records(chat_key)
                if record.name.startswith(prefix)
            ]
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
                "title": self._title(content, name),
                "source_kind": "text",
                "current": current == name,
                "status": status if current == name else "",
                "import_status": import_status if import_name == name else "",
                # The room is unavailable while ANY module is being imported;
                # mark the current source too so a refreshed page cannot show its
                # stale pool during the transition.
                "importing": import_status == "processing",
                "pool": pool,
                "media": media_records,
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

    async def _update(self, caller_room: str, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        name, path = self._path(root, str(payload.get("name") or ""))
        if not path.is_file():
            return _module_reply("module_update", False, name, {"error": "source_not_found"})
        current_before = await self._current_name(caller_room, root, self._files(root))
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            return _module_reply("module_update", False, name, {"error": "empty_module_content"})
        raw = content.encode("utf-8")
        if len(raw) > _MAX_SOURCE_BYTES:
            return _module_reply("module_update", False, name, {"error": "module_source_too_large"})
        path.write_bytes(raw)
        if current_before == name:
            # Keep the room/source association while the edited file waits for
            # an explicit re-import.  Without this marker, a generated module
            # whose source was discovered by content comparison would look
            # inactive after its file changed, making deletion unsafe.
            await self.services.store.state_set(chat_key_for_room(caller_room), "module_source", name)
        stat = path.stat()
        return _module_reply(
            "module_update",
            True,
            name,
            {
                "name": name,
                "size": stat.st_size,
                "modified": int(stat.st_mtime * 1000),
                "current": current_before == name,
            },
        )

    async def _bundle_upload(self, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        import pypdf

        name = str(payload.get("name") or "content-library.zip").strip()
        if Path(name).suffix.casefold() != ".zip" or Path(name).name != name:
            raise ValueError("invalid bundle filename")
        encoded = payload.get("archive")
        if not isinstance(encoded, str):
            raise ValueError("empty module bundle")
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > _MAX_BUNDLE_BYTES:
            raise ValueError("module bundle too large")
        sections: list[str] = []
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if len(members) > _MAX_BUNDLE_FILES:
                raise ValueError("too many bundle files")
            for member in members:
                candidate = Path(member.filename)
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise ValueError("bundle path escapes archive")
                suffix = candidate.suffix.casefold()
                if suffix not in {".md", ".markdown", ".txt", ".pdf"}:
                    continue
                data = archive.read(member)
                if suffix == ".pdf":
                    reader = pypdf.PdfReader(io.BytesIO(data))
                    text = "\n".join(page.extract_text() or "" for page in reader.pages)
                else:
                    text = data.decode("utf-8")
                if text.strip():
                    sections.append(f"# Source: {member.filename}\n\n{text.strip()}")
        if not sections:
            raise ValueError("bundle has no readable module sources")
        output_name = Path(name).stem + ".md"
        _, output = self._path(root, output_name)
        content = "\n\n---\n\n".join(sections)
        if len(content.encode("utf-8")) > _MAX_SOURCE_BYTES:
            raise ValueError("module source too large")
        output.write_text(content, encoding="utf-8")
        return _module_reply(
            "module_bundle_upload",
            True,
            output_name,
            {"name": output_name, "files": len(sections)},
        )

    async def _import(self, caller_room: str, root: Path, payload: dict[str, Any], i18n: Any) -> dict[str, Any]:
        raw_name = str(payload.get("name") or "").strip()
        # An installed .lwpack content pack imports through its WORLD CARD (the keeper world
        # import path) — it is a binary bundle, not a Markdown scenario, so it is handled before
        # the `.md`-only `_path`/`_safe_name` checks reject its suffix-less pack id. The world
        # card loads the pack's lorebook, variables, pregen cast and its declared rule system
        # into the room.
        from gateway.panels import installed_pack_homes

        pack_id, _, selected_card = raw_name.partition("/")
        pack_home = installed_pack_homes(Path(self.services.settings.data_dir)).get(pack_id)
        if pack_home is not None:
            return await self._import_pack(caller_room, pack_id, pack_home, i18n, selected_card=selected_card)
        name, path = self._path(root, raw_name)
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
        else:
            # DocumentTools returns a localized failure string instead of raising when
            # vector extraction/initialization fails before publishing a final state.
            await self.services.store.state_set(chat_key, "module_init_error", receipt[-1000:])
        return _module_reply(
            "module_import",
            ok,
            name,
            {"receipt": receipt, "status": status, "current": ok},
        )

    async def _import_pack(
        self,
        caller_room: str,
        pack_id: str,
        home: Path,
        i18n: Any,
        *,
        selected_card: str = "",
    ) -> dict[str, Any]:
        """Import an installed .lwpack content pack into the room through its bundled world
        card (`core.pack` + `CharcardTools.import_world_card`): the keeper world-import path
        loads the pack's lorebook, typed variables, pregen cast, bundled skill auto-enable and
        its declared rule system. Nothing is imported for a pack with no world card."""
        from agent.kp_tools_charcard import CharcardTools

        _manifest, world_cards = self._pack_world_cards(home)
        if selected_card:
            world_cards = [pair for pair in world_cards if pair[0].path == selected_card]
        if not world_cards:
            return _module_reply("module_import", False, pack_id, {"error": "no_world_card"})
        if len(world_cards) > 1:
            return _module_reply(
                "module_import",
                False,
                pack_id,
                {
                    "error": "multiple_world_cards",
                    "choices": [f"{pack_id}/{card.path}" for card, _path in world_cards],
                },
            )
        card_path = str(world_cards[0][1])
        chat_key = chat_key_for_room(caller_room)
        ctx = AgentCtx(
            chat_key=chat_key,
            user_id="keeper",
            platform="tui",
            locale=i18n.locale,
            fs=LocalFs(home.parent),
            extra={"role": "keeper"},
        )
        try:
            receipt = await CharcardTools(self.services).import_world_card(
                ctx, file_path=card_path, raise_on_failure=True
            )
        except Exception as exc:  # noqa: BLE001 — a failed import degrades to a clean reply
            return _module_reply("module_import", False, pack_id, {"error": f"import_failed: {exc}"})
        if self.hub is not None:
            from gateway.panels import publish_ui_manifests

            await publish_ui_manifests(self.hub, self.services, chat_key)
        return _module_reply("module_import", True, pack_id, {"receipt": receipt, "current": True})


    async def _delete(self, caller_room: str, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        source_kind = str(payload.get("source_kind") or "").strip().casefold()
        # An installed .lwpack content pack is deleted by its pack id (not a module file).
        if source_kind == "pack" or "/" not in name:
            ok, resolved, error = await delete_installed_pack(
                self.services, name.partition("/")[0], caller_room=caller_room
            )
            return _module_reply("module_delete", ok, resolved, {} if ok else {"error": error})
        name, path = self._path(root, name)
        if not path.is_file():
            return _module_reply("module_delete", False, name, {"error": "source_not_found"})
        current = await self._current_name(caller_room, root, self._files(root))
        if current == name:
            return _module_reply("module_delete", False, name, {"error": "module_in_use"})
        path.unlink()
        return _module_reply("module_delete", True, name, {"name": name})

    @staticmethod
    def _safe_worldbook_name(raw: str) -> str:
        name = raw.strip()
        path = Path(name)
        if not name or path.name != name or name in {".", ".."}:
            raise ValueError("invalid worldbook filename")
        if any(ord(char) < 32 for char in name):
            raise ValueError("invalid worldbook filename")
        if path.suffix.casefold() not in _WORLDBOOK_SUFFIXES:
            raise ValueError("unsupported worldbook filename")
        return name

    @classmethod
    def _worldbook_path(cls, root: Path, raw: str) -> tuple[str, Path]:
        name = cls._safe_worldbook_name(raw)
        target = root / name
        if target.is_symlink():
            raise ValueError("symlink worldbook source")
        if target.resolve().parent != root.resolve():
            raise ValueError("worldbook path escapes source directory")
        return name, target

    @classmethod
    def _worldbook_files(cls, root: Path) -> list[tuple[str, Path]]:
        return [
            (path.name, path)
            for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
            if path.is_file() and not path.is_symlink() and path.suffix.casefold() in _WORLDBOOK_SUFFIXES
        ]

    @staticmethod
    def _worldbook_entries(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, dict) and "entries" not in data:
            book = data.get("character_book") or (data.get("data") or {}).get("character_book")
            if isinstance(book, dict):
                data = book
        raw_entries = data.get("entries", []) if isinstance(data, dict) else data
        return [entry for entry in raw_entries if isinstance(entry, dict)] if isinstance(raw_entries, list) else []

    async def _attached_worldbooks(self, chat_key: str) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for document in await self.services.documents.list(chat_key, LORE_DOC_TYPE):
            if not document.source:
                continue
            grouped.setdefault(document.source, []).append(dict(document.data))
        return grouped

    @staticmethod
    def _entry_views(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "title": str(entry.get("title") or entry.get("comment") or entry.get("name") or "Untitled"),
                "content": str(entry.get("content") or ""),
                "keys": entry.get("keys") or entry.get("key") or [],
                "secret": bool(entry.get("secret", False)),
            }
            for entry in entries
        ]

    async def _worldbook_list(self, caller_room: str, root: Path) -> dict[str, Any]:
        chat_key = chat_key_for_room(caller_room)
        current = await self.services.worldbook.active_source(chat_key)
        # The current MODULE is the `world_import` marker (the card name the keeper imported as
        # the room's running module) — use it as the authoritative "current worldbook" so the
        # keeper UI shows which scenario is live.
        imported_module = str(await self.services.store.state_get(chat_key, "world_import") or "")
        attached = await self._attached_worldbooks(chat_key)
        books_by_name: dict[str, dict[str, Any]] = {}
        for name, path in self._worldbook_files(root):
            stat = path.stat()
            books_by_name[name] = {
                "name": name,
                "size": stat.st_size,
                "modified": int(stat.st_mtime * 1000),
                "current": bool(current == name or (imported_module and name == imported_module)),
                "attached": name in attached,
                "origin": "room" if name in attached else "library",
                "entry_count": len(attached.get(name, [])),
                "source_kind": "file",
            }
        for name, entries in attached.items():
            if name in books_by_name:
                continue
            books_by_name[name] = {
                "name": name,
                "size": 0,
                "modified": 0,
                "current": bool(current == name or (imported_module and name == imported_module)),
                "attached": True,
                "origin": "room",
                "entry_count": len(entries),
                "source_kind": "attached",
            }
        books = sorted(books_by_name.values(), key=lambda book: str(book["name"]).casefold())
        visible_current = current if current != "__disabled__" else ""
        return _module_reply(
            "worldbook_list",
            True,
            visible_current,
            {"worldbooks": books, "current": visible_current, "enabled": current != "__disabled__"},
        )

    async def _worldbook_detail(self, caller_room: str, root: Path, raw_name: str) -> dict[str, Any]:
        name = raw_name.strip()
        chat_key = chat_key_for_room(caller_room)
        attached = await self._attached_worldbooks(chat_key)
        path: Path | None = None
        try:
            _, candidate = self._worldbook_path(root, name)
            if candidate.is_file():
                path = candidate
        except ValueError:
            path = None
        current = await self.services.worldbook.active_source(chat_key)
        if path is not None:
            content = path.read_text(encoding="utf-8-sig")
            entries = self._worldbook_entries(json.loads(content))
            stat = path.stat()
            source_kind = "file"
            size = stat.st_size
            modified = int(stat.st_mtime * 1000)
        elif name in attached:
            content = ""
            entries = attached[name]
            source_kind = "attached"
            size = 0
            modified = 0
        else:
            return _module_reply("worldbook_detail", False, name, {"error": "source_not_found"})
        return _module_reply(
            "worldbook_detail",
            True,
            name,
            {
                "name": name,
                "size": size,
                "modified": modified,
                "content": content,
                "current": current == name,
                "attached": name in attached,
                "source_kind": source_kind,
                "entry_count": len(entries),
                "entries": self._entry_views(entries),
            },
        )

    async def _worldbook_upload(self, root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        name, path = self._worldbook_path(root, str(payload.get("name") or ""))
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty worldbook content")
        raw = content.encode("utf-8")
        if len(raw) > _MAX_SOURCE_BYTES:
            raise ValueError("worldbook source too large")
        json.loads(content)
        path.write_bytes(raw)
        return _module_reply("worldbook_upload", True, name, {"name": name})

    async def _worldbook_select(
        self, caller_room: str, root: Path, payload: dict[str, Any], i18n: Any
    ) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        chat_key = chat_key_for_room(caller_room)
        attached = await self._attached_worldbooks(chat_key)
        source_kind = str(payload.get("source_kind") or "")
        if source_kind == "attached" and name in attached:
            await self.services.worldbook.set_active_source(chat_key, name)
            return _module_reply(
                "worldbook_select",
                True,
                name,
                {"count": len(attached[name]), "current": name, "source_kind": "attached"},
            )
        _, path = self._worldbook_path(root, name)
        if not path.is_file():
            return _module_reply("worldbook_select", False, name, {"error": "source_not_found"})
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        count = await self.services.worldbook.import_entries(chat_key, data, source=name, is_keeper=True)
        if count == 0:
            return _module_reply("worldbook_select", False, name, {"error": "worldbook_empty"})
        await self.services.worldbook.set_active_source(chat_key, name)
        return _module_reply("worldbook_select", True, name, {"count": count, "current": name, "source_kind": "file"})

    async def _worldbook_disable(self, caller_room: str) -> dict[str, Any]:
        await self.services.worldbook.set_active_source(chat_key_for_room(caller_room), "__disabled__")
        return _module_reply("worldbook_disable", True, "", {"enabled": False})



async def delete_installed_pack(
    services: Any,
    pack_id: str,
    *,
    caller_room: str,
) -> tuple[bool, str, str]:
    """Delete an installed .lwpack content pack from the server.

    Returns ``(ok, name, error_code)`` — ``error_code`` is ``""`` on success and one of
    ``source_not_found`` / ``dev_mount`` / ``module_in_use`` otherwise. Removes the
    installed home under ``packs/<id>@<version>/``, the forge build artifacts under
    ``modules/``, strips the pack's admitted skills/panels from every room, removes skill
    directories no remaining installed pack declares, and refreshes skill/rulepack discovery.
    ``pack_id`` is matched against the newest installed home; a dev-mount source tree is never
    deleted.
    """
    from core.pack import DEV_PACK_HOMES
    from gateway.panels import installed_pack_homes

    data_dir = Path(services.settings.data_dir)
    home = installed_pack_homes(data_dir).get(pack_id)
    if home is None:
        return False, pack_id, "source_not_found"
    if home.resolve() in {Path(p).resolve() for p in DEV_PACK_HOMES.values()}:
        return False, pack_id, "dev_mount"
    active = await active_module(services, chat_key_for_room(caller_room))
    if active and active.get("kind") == "world_card" and str(active.get("pack_id") or "") == pack_id:
        return False, pack_id, "module_in_use"
    # The pack's own skills (by id) are removed from every room's enabled list alongside
    # the pack-id entries in `panels_enabled`. Shared skill files are removed separately
    # only when no remaining installed pack declares them.
    skill_ids = _pack_skill_ids(home)
    _remove_pack_artifacts(services, pack_id, home, skill_ids=skill_ids)
    await _strip_pack_from_rooms(services, pack_id, skill_ids=skill_ids)
    return True, pack_id, ""


def _pack_skill_ids(home: Path) -> list[str]:
    """The skill ids a pack ships (its ``contents.skills`` directory names), or ``[]``."""
    manifest_path = home / core_pack.MANIFEST_NAME
    if not manifest_path.is_file():
        return []
    text = manifest_path.read_text(encoding="utf-8")
    manifest = None
    for expect_trust in (True, False):
        try:
            manifest = core_pack.parse_manifest_text(text, expect_trust=expect_trust)
            break
        except Exception:
            continue
    if manifest is None:
        return []
    from pathlib import PurePosixPath

    ids: list[str] = []
    for path in (manifest.contents or {}).get("skills", []):
        parent = PurePosixPath(path).parent
        if str(parent) not in ("", "."):
            ids.append(parent.name)
    return ids


def _remove_pack_artifacts(
    services: Any, pack_id: str, home: Path, *, skill_ids: list[str] | None = None
) -> None:
    """Delete an installed pack and its orphaned skill files, then refresh discovery.

    Skills are extracted into a shared directory, so a skill can only be removed when no
    remaining installed pack declares the same id. A manually copied skill cannot be
    distinguished from a pack-owned file; the pack manifest is the ownership boundary used
    by the installer everywhere else.
    """
    import shutil

    data_dir = Path(services.settings.data_dir)
    shutil.rmtree(home, ignore_errors=True)
    # Forge-generated modules leave a `.lwpack` and a `.pack-src` source tree under
    # `modules/`; a hand-installed pack never has them, and both are non-load-bearing
    # once the pack is installed, so removing them is safe either way.
    for entry in (data_dir / "modules").glob(f"{pack_id}-*.lwpack"):
        entry.unlink(missing_ok=True)
    for entry in (data_dir / "modules").glob(f"{pack_id}.pack-src"):
        shutil.rmtree(entry, ignore_errors=True)
    _remove_orphaned_pack_skills(data_dir, skill_ids or [])
    # A just-removed skill/rulepack must stop being discoverable without a restart.
    try:
        from core import rulepacks as core_rulepacks
        from core import skills as core_skills

        core_skills.reload_skills()
        core_rulepacks.reload_rulepacks()
    except Exception:  # noqa: BLE001 — discovery refresh is best-effort
        logger.exception("pack delete: discovery refresh failed")


def _remove_orphaned_pack_skills(data_dir: Path, skill_ids: list[str]) -> None:
    """Remove deleted-pack skill directories that no remaining installed pack owns."""
    import shutil

    if not skill_ids:
        return
    from gateway.panels import installed_pack_homes

    retained_ids: set[str] = set()
    for remaining_home in installed_pack_homes(data_dir).values():
        retained_ids.update(_pack_skill_ids(remaining_home))

    skills_dir = data_dir / "skills"
    for skill_id in set(skill_ids) - retained_ids:
        skill_home = skills_dir / skill_id
        if skill_home.is_symlink() or not skill_home.is_dir():
            continue
        shutil.rmtree(skill_home, ignore_errors=True)


async def _strip_pack_from_rooms(services: Any, pack_id: str, *, skill_ids: list[str] | None = None) -> None:
    """Remove a deleted pack's references from every room: the pack id from each room's
    `panels_enabled`, and the pack's own skill ids from each room's `skills_enabled`."""
    skill_ids = skill_ids or []
    for chat_key in await services.store.state_rooms():
        for state_key, drop in (
            ("panels_enabled", {pack_id}),
            ("skills_enabled", set(skill_ids)),
        ):
            raw = await services.store.state_get(chat_key, state_key)
            if not raw:
                continue
            try:
                items = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(items, list):
                continue
            filtered = [item for item in items if item not in drop]
            if filtered != items:
                await services.store.state_set(
                    chat_key, state_key, json.dumps(filtered, ensure_ascii=False)
                )

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
