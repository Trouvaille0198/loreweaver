"""Read source scenario bundles for native-module conversion.

This module is deliberately limited to the deterministic ingestion half of the
conversion pipeline. It does not ask a model to interpret a scenario and it
does not install anything into a room. A bundle is first reduced to page-aware
source evidence and a small, reviewable asset manifest; a later authoring lane
can use that result to build a native world card.

The existing ``DocumentProcessor`` remains the compatibility path for one
ordinary document. This reader exists because a ZIP containing a scenario is a
different input contract: the main script, cast sheets, rule appendices,
handouts and images have different roles and must not be flattened together.
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import pypdf
except ImportError:  # pragma: no cover - the application declares pypdf; kept import-safe for tooling
    pypdf = None


MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_FILES = 128
MAX_UNPACKED_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_PAGE_TEXT_CHARS = 200_000

SOURCE_SUFFIXES = frozenset({".pdf", ".txt", ".md", ".markdown"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"})
AUDIO_SUFFIXES = frozenset({".mp3", ".ogg", ".oga", ".opus", ".wav", ".flac", ".m4a", ".aac"})

_MAIN_MARKS = ("主剧本", "模组", "剧本", "scenario", "module", "adventure", "轻量版")
_CAST_MARKS = ("演职员", "角色", "人物", "npc", "cast", "character")
_RULE_MARKS = ("咒文", "神话生物", "规则", "法术", "rule", "spell", "monster")
_AUTHOR_MARKS = ("采访", "问答", "后记", "致谢", "说明", "readme", "faq", "author")
_MUSIC_MARKS = ("广播", "音乐", "soundtrack", "music", "bgm")

_MAP_MARKS = ("地图", "流程图", "关系图", "map", "flow", "relation")
_HANDOUT_MARKS = ("档案", "学籍", "笔仙", "报纸", "报告", "handout", "clue", "letter")
_PORTRAIT_MARKS = ("头像", "立绘", "portrait", "avatar")
_LOCATION_MARKS = ("地点", "场景", "校园", "宿舍", "location", "scene")
_COVER_MARKS = ("封面", "宣传", "cover", "poster")
_REFERENCE_MARKS = ("校服", "服装", "服饰", "reference", "style")


class ScenarioBundleError(ValueError):
    """The source bundle is not safe or does not contain a readable source."""


@dataclass(frozen=True)
class PageEvidence:
    page: int
    text: str

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    @property
    def needs_visual_review(self) -> bool:
        """Short or empty pages often contain a cover, handout or diagram.

        This is deliberately a review hint, not an OCR claim: a page with a
        short title may be perfectly readable, while a visually complex page
        can still have a long text layer.
        """
        return len(self.text.strip()) < 20

    def to_dict(self, *, include_text: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "page": self.page,
            "text_chars": len(self.text),
            "has_text": self.has_text,
            "needs_visual_review": self.needs_visual_review,
        }
        if include_text:
            value["text"] = self.text
        return value


@dataclass(frozen=True)
class ScenarioSource:
    path: str
    role: str
    mime: str
    sha256: str
    size: int
    text: str
    pages: tuple[PageEvidence, ...] = ()

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def to_dict(self, *, include_page_text: bool = False) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "mime": self.mime,
            "sha256": self.sha256,
            "size": self.size,
            "page_count": self.page_count,
            "text_chars": len(self.text),
            "pages": [page.to_dict(include_text=include_page_text) for page in self.pages],
        }


@dataclass(frozen=True)
class ScenarioAsset:
    path: str
    output_name: str
    role: str
    priority: str
    mime: str
    sha256: str
    size: int
    data: bytes = field(repr=False, compare=False)

    def to_dict(self, *, include_data: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "path": self.path,
            "output_name": self.output_name,
            "role": self.role,
            "priority": self.priority,
            "mime": self.mime,
            "sha256": self.sha256,
            "size": self.size,
        }
        if include_data:
            value["data"] = self.data
        return value


@dataclass(frozen=True)
class ScenarioBundle:
    filename: str
    sha256: str
    size: int
    sources: tuple[ScenarioSource, ...]
    assets: tuple[ScenarioAsset, ...]
    warnings: tuple[str, ...] = ()

    @property
    def main_source(self) -> ScenarioSource | None:
        candidates = [source for source in self.sources if source.role == "main"]
        if not candidates:
            candidates = list(self.sources)
        return max(candidates, key=lambda source: (len(source.text), source.path.casefold()), default=None)

    @property
    def selected_assets(self) -> tuple[ScenarioAsset, ...]:
        return tuple(asset for asset in self.assets if asset.priority in {"essential", "useful"})

    def combined_text(self) -> str:
        """Return source text with file/page boundaries for legacy text import.

        Author Q&A, soundtrack lists and download instructions are intentionally
        excluded: they are conversion metadata, not scenario prose. The native
        converter can still inspect them through ``sources`` and their original
        files.
        """
        allowed_roles = {"main", "cast", "rules", "supplement"}
        sections: list[str] = []
        for source in self.sources:
            if source.role not in allowed_roles or not source.text.strip():
                continue
            if source.pages:
                body = "\n\n".join(
                    f"## Page {page.page}\n\n{page.text.strip()}"
                    for page in source.pages
                    if page.text.strip()
                )
            else:
                body = source.text.strip()
            if body:
                sections.append(f"# Source: {source.path}\n\n{body}")
        return "\n\n---\n\n".join(sections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "loreweaver.scenario-bundle",
            "format_version": 1,
            "filename": self.filename,
            "sha256": self.sha256,
            "size": self.size,
            "sources": [source.to_dict() for source in self.sources],
            "assets": [asset.to_dict() for asset in self.assets],
            "selected_asset_count": len(self.selected_assets),
            "warnings": list(self.warnings),
        }


def read_scenario_bundle(data: bytes, filename: str = "scenario.zip") -> ScenarioBundle:
    """Read one ZIP scenario bundle without installing or executing its contents."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ScenarioBundleError("empty scenario bundle")
    if len(data) > MAX_BUNDLE_BYTES:
        raise ScenarioBundleError(f"scenario bundle exceeds {MAX_BUNDLE_BYTES} bytes")
    if Path(filename).name != filename or Path(filename).suffix.casefold() != ".zip":
        raise ScenarioBundleError("scenario bundle filename must be a plain .zip name")

    try:
        archive = zipfile.ZipFile(io.BytesIO(bytes(data)))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ScenarioBundleError(f"invalid scenario bundle: {exc}") from exc

    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) > MAX_BUNDLE_FILES:
            raise ScenarioBundleError(f"scenario bundle contains too many files (max {MAX_BUNDLE_FILES})")
        total_unpacked = sum(info.file_size for info in members)
        if total_unpacked > MAX_UNPACKED_BYTES:
            raise ScenarioBundleError(f"scenario bundle contents exceed {MAX_UNPACKED_BYTES} bytes")

        entries: list[tuple[str, bytes, str]] = []
        for info in members:
            member_name = _safe_member_name(info.filename)
            if info.file_size > MAX_MEMBER_BYTES:
                raise ScenarioBundleError(f"bundle member is too large: {member_name}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ScenarioBundleError(f"symlink bundle member is not allowed: {member_name}")
            try:
                member_data = archive.read(info)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ScenarioBundleError(f"cannot read bundle member {member_name}: {exc}") from exc
            if len(member_data) != info.file_size:
                raise ScenarioBundleError(f"bundle member size changed while reading: {member_name}")
            entries.append((member_name, member_data, _member_kind(member_name)))

    sources: list[ScenarioSource] = []
    raw_assets: list[tuple[str, bytes]] = []
    warnings: list[str] = []
    for path, member_data, kind in entries:
        if kind == "source":
            source = _read_source(path, member_data, warnings)
            if source is not None:
                sources.append(source)
        elif kind == "image":
            raw_assets.append((path, member_data))

    if not sources:
        raise ScenarioBundleError("scenario bundle has no readable PDF/TXT/Markdown source")

    sources = _assign_source_roles(sources)
    assets = _build_assets(raw_assets, sources)
    return ScenarioBundle(
        filename=filename,
        sha256=hashlib.sha256(bytes(data)).hexdigest(),
        size=len(data),
        sources=tuple(sources),
        assets=tuple(assets),
        warnings=tuple(warnings),
    )


def read_scenario_input(data: bytes, filename: str) -> ScenarioBundle:
    """Dispatch a single PDF/text document or a multi-file ZIP bundle."""
    if Path(filename).suffix.casefold() == ".zip":
        return read_scenario_bundle(data, filename)
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ScenarioBundleError("empty scenario document")
    if len(data) > MAX_MEMBER_BYTES:
        raise ScenarioBundleError(f"scenario document exceeds {MAX_MEMBER_BYTES} bytes")
    safe_name = Path(filename).name
    if safe_name != filename or Path(filename).suffix.casefold() not in SOURCE_SUFFIXES:
        raise ScenarioBundleError("scenario input must be a plain PDF, TXT or Markdown filename")
    warnings: list[str] = []
    source = _read_source(filename, bytes(data), warnings)
    if source is None:
        raise ScenarioBundleError("scenario document has no readable source text")
    source = ScenarioSource(**{**source.__dict__, "role": "main"})
    return ScenarioBundle(
        filename=filename,
        sha256=hashlib.sha256(bytes(data)).hexdigest(),
        size=len(data),
        sources=(source,),
        assets=(),
        warnings=tuple(warnings),
    )


def _safe_member_name(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ScenarioBundleError("bundle contains an invalid filename")
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ScenarioBundleError(f"bundle path escapes archive: {raw}")
    return str(path)


def _member_kind(path: str) -> str:
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix in SOURCE_SUFFIXES:
        return "source"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    return "other"


def _mime_for(path: str) -> str:
    suffix = PurePosixPath(path).suffix.casefold()
    known = {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
    }
    return known.get(suffix) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_source(path: str, data: bytes, warnings: list[str]) -> ScenarioSource | None:
    suffix = PurePosixPath(path).suffix.casefold()
    digest = hashlib.sha256(data).hexdigest()
    pages: list[PageEvidence] = []
    if suffix == ".pdf":
        if pypdf is None:
            raise ScenarioBundleError("PDF support is unavailable")
        try:
            reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
            for index, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").strip()
                if len(text) > MAX_PAGE_TEXT_CHARS:
                    warnings.append(f"{path} page {index}: text truncated")
                    text = text[:MAX_PAGE_TEXT_CHARS]
                pages.append(PageEvidence(page=index, text=text))
        except Exception as exc:  # pypdf exposes several parser-specific exception classes
            warnings.append(f"{path}: PDF text extraction failed: {type(exc).__name__}")
    else:
        text = _decode_text(data).strip()
        pages = [PageEvidence(page=1, text=text)] if text else []

    text = "\n\n".join(page.text for page in pages if page.text)
    if not text and not pages:
        warnings.append(f"{path}: no readable text")
        return None
    return ScenarioSource(
        path=path,
        role="supplement",
        mime=_mime_for(path),
        sha256=digest,
        size=len(data),
        text=text,
        pages=tuple(pages),
    )


def _assign_source_roles(sources: list[ScenarioSource]) -> list[ScenarioSource]:
    if len(sources) == 1:
        source = sources[0]
        return [ScenarioSource(**{**source.__dict__, "role": "main"})]

    ranked: list[tuple[int, ScenarioSource]] = []
    for source in sources:
        name = source.path.casefold()
        if _contains_any(name, _AUTHOR_MARKS) or _contains_any(name, _MUSIC_MARKS):
            role = "author_notes"
        elif _contains_any(name, _CAST_MARKS):
            role = "cast"
        elif _contains_any(name, _RULE_MARKS):
            role = "rules"
        elif _contains_any(name, _MAIN_MARKS):
            role = "main"
        else:
            role = "supplement"
        score = len(source.text)
        if role == "main":
            score += 10_000_000
        elif role in {"cast", "rules"}:
            score += 1_000_000
        ranked.append((score, ScenarioSource(**{**source.__dict__, "role": role})))

    if not any(source.role == "main" for _, source in ranked):
        largest = max(ranked, key=lambda item: (len(item[1].text), item[1].path.casefold()))[1]
        ranked = [
            (score, ScenarioSource(**{**source.__dict__, "role": "main" if source.path == largest.path else source.role}))
            for score, source in ranked
        ]
    return [source for _score, source in sorted(ranked, key=lambda item: item[1].path.casefold())]


def _build_assets(raw_assets: list[tuple[str, bytes]], sources: list[ScenarioSource]) -> list[ScenarioAsset]:
    assets: list[ScenarioAsset] = []
    used_names: set[str] = set()
    for index, (path, data) in enumerate(sorted(raw_assets, key=lambda item: item[0].casefold()), 1):
        role, priority = _asset_classification(path)
        # A file with an explicit, meaningful name is useful even when the author did not put it
        # in the PDF text. Unknown assets stay in the report but are not copied to the playable deck.
        stem = _safe_asset_stem(Path(path).stem)
        suffix = PurePosixPath(path).suffix.casefold()
        output_name = f"{index:03d}-{role}-{stem}{suffix}"
        if output_name in used_names:
            output_name = f"{index:03d}-{role}-{stem}-{hashlib.sha256(data).hexdigest()[:8]}{suffix}"
        used_names.add(output_name)
        assets.append(
            ScenarioAsset(
                path=path,
                output_name=output_name,
                role=role,
                priority=priority,
                mime=_mime_for(path),
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
                data=data,
            )
        )
    return assets


def _asset_classification(path: str) -> tuple[str, str]:
    text = unicodedata.normalize("NFKC", path).casefold()
    if _contains_any(text, _MAP_MARKS):
        return "map", "essential"
    if _contains_any(text, _HANDOUT_MARKS):
        return "handout", "essential"
    if _contains_any(text, _PORTRAIT_MARKS):
        return "portrait", "essential"
    if _contains_any(text, _LOCATION_MARKS):
        return "location", "useful"
    if _contains_any(text, _COVER_MARKS):
        return "cover", "useful"
    if _contains_any(text, _REFERENCE_MARKS):
        return "reference", "useful"
    return "unknown", "unknown"


def _safe_asset_stem(raw: str) -> str:
    normalized = unicodedata.normalize("NFKC", raw).strip()
    normalized = re.sub(r"[^0-9A-Za-z\u3400-\u9fff_-]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-_")
    return normalized[:80] or "asset"


def _contains_any(value: str, marks: tuple[str, ...]) -> bool:
    return any(mark.casefold() in value for mark in marks)
