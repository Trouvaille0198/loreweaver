"""Layer B.3 — the KP self-extension engines ("a skill that creates skills/rule systems/modules").

See ``docs/plugins.md`` "Layer B". Three generators share one shape: ask the room's LLM to author
a complete artifact for a natural-language description, validate the result through the SAME
parser real discovery/ingestion uses -- writing NOTHING until that succeeds -- then install it and
make it immediately live.

- `generate_and_install_skill` (B.3a) -- a ``SKILL.md`` bundle, validated via
  `core.skills.parse_skill_text`, installed under `core.skills._USER_SKILL_DIR`.
- `generate_and_install_rulepack` (B.3b) -- a flat ``<id>.yaml`` rulepack, validated via
  `core.rulepacks.parse_rulepack_text` (including its `derived:` section compiling through the
  safe DSL), installed under `core.rulepacks._USER_RULEPACK_DIR`.
- `generate_and_install_module` (B.3b) -- a Markdown module/scenario document, installed as a flat
  ``<id>.md`` file under `_USER_MODULE_DIR` and then run through the EXISTING module-ingestion
  pipeline (`agent.kp_tools_knowledge.DocumentTools.upload_document`) so it lands in the CALLING
  room's own knowledge pool -- unlike the other two, this is per-room content, not a new global
  discovery registry. On keeper request (`media=`/`companion=`), the module generator ALSO runs
  two optional post-install passes, both degrade-never-fail: a media pass (a scoped shot-list
  call, then the room's imagegen lane + media store -- scene/NPC/item/cover illustrations like a
  hand-authored pack's assets) and a companion pass that reuses the OTHER two forge engines plus
  the `.genchar` sheet pipeline (a KP skill, a rulepack, claimable pregen cards) driven by the
  module's own text.

Trust boundary (``docs/plugins.md`` "The trust boundary"): all three are still **data plugins**
even though the model wrote them -- no code ever runs. Nothing here `eval`/`exec`s anything; a
skill/rulepack is parsed with `yaml.safe_load` (via `core.yaml_safety.safe_load_no_aliases`, which
additionally rejects alias/anchor nodes -- an alias-bomb document can expand into an exponential
in-memory structure once something stringifies it) exactly like a hand-authored one, and a module
is opaque Markdown text handed to the same analysis pipeline a manual upload uses. LLM-authored
skill/rulepack content is ALSO capped at `_MAX_FORGE_CONTENT_BYTES` before that first parse call --
independent, defense-in-depth protection against a merely large (non-aliased) document costing
real CPU/memory on the shared event loop. The one privileged operation each performs is a scoped
filesystem write, confined by construction (`_confined_target`/`_confined_file_target` assert the
resolved path never escapes its directory) and gated behind a `generate_*` tool
(`agent.kp_tools_forge`), each itself gated (Layer B.2) and invisible until its forge skill unlocks
it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

import core.rulepacks as rulepacks
import core.skills as skills
from agent.char_from_persona import build_sheet_from_description
from agent.context import AgentCtx, LocalFs
from agent.kp_tools_charcard import CharcardTools
from agent.kp_tools_knowledge import DocumentTools
from agent.module_initializer import ProgressCb, _emit
from agent.services import Services
from core.character_manager import CharacterSheet
from core.character_rules import validate_sheet
from core.condexpr import CondExprError, compile_expression
from core.documents import KEEPER_VIEWER, MODULE_POOL_ID
from core.lorecard import parse_lorecard_bytes
from core.pack import build_pack
from core.pregen_roster import pregen_add
from core.sheets import canonical_values
from core.yaml_safety import safe_load_no_aliases
from gateway.imagegen import allow_imagegen_request
from infra.file_permissions import atomic_write_private
from infra.imagegen import ImageGenError
from infra.media_store import ALLOWED_IMAGE_MIMES, MediaStore
from infra.model_call_trace import lane_scope
from infra.room_facets import STORAGE_ROOM_STATE, RoomStateFacet
from infra.usage_stats import record_usage_stats

logger = logging.getLogger(__name__)
# The pack-forge engine is a long, multi-stage pipeline (LLM authoring -> imagegen ->
# companion generation -> build -> install -> room import). Stage progress is logged at INFO so
# an operator watching `docker logs` can see exactly where a slow/failed generation is — without
# it, a stuck forge is a silent black box. Other modules keep their default level.
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)

# A placeholder id used only to probe generated content for a name/title before the real id is
# known (see step (c) in each `generate_and_install_*` function) -- never written to disk, never
# shown to a user; chosen unlikely to collide with a real generated name.
_PROBE_ID = "_forge_probe"

_SLUG_OK_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Cap the derived id so a pathologically long generated name can't slugify into a directory/file
# component longer than the filesystem allows (NAME_MAX) — which would raise OSError at write time.
_MAX_SLUG_LEN = 64

# Hard byte cap on LLM-authored skill/rulepack content, enforced BEFORE the first parse call
# (`skills.parse_skill_text`/`rulepacks.parse_rulepack_text`) in each generator below. This is
# independent of, and in addition to, `core.yaml_safety.NoAliasSafeLoader`'s alias rejection: even
# with aliases banned outright, a sufficiently large plain (non-aliased) YAML/frontmatter document
# still costs real CPU/memory to parse and validate on the shared event loop, so a malicious or
# runaway LLM response is refused by size alone before it ever reaches the parser.
_MAX_FORGE_CONTENT_BYTES = 64 * 1024


def _content_too_large(content: str) -> int | None:
    """Return the content's UTF-8 byte length if it exceeds `_MAX_FORGE_CONTENT_BYTES`, else `None`.

    Measured in encoded bytes (not `len(content)` characters) so the cap means what "64KB" says
    regardless of how much of the content is multi-byte (e.g. CJK) text.
    """
    size = len(content.encode("utf-8"))
    return size if size > _MAX_FORGE_CONTENT_BYTES else None


_CODE_FENCE_RE = re.compile(r"\A```[^\n]*\r?\n(?P<body>.*?)\r?\n?```[ \t]*\Z", re.DOTALL)


def _strip_code_fence(content: str) -> str:
    """Unwrap a whole-reply markdown code fence (````yaml\n…\n````) when the model emits one
    despite the prompts' "no code fences" rule. Only a fence wrapping the ENTIRE reply is
    stripped -- a partial fence is the model's real (invalid) output and must fail validation
    rather than be silently rewritten."""
    match = _CODE_FENCE_RE.match(content.strip())
    return match.group("body") if match else content


def _repair_skill_frontmatter(content: str) -> str:
    """Best-effort repair of an LLM that opens a SKILL.md frontmatter with ``---`` but forgets
    the closing fence (``core.skills._split_frontmatter`` demands one). Only the trivially
    recoverable case is handled: content that BEGINS with the opening fence yet contains no
    second ``---`` line gets the closing fence appended. Anything else is left untouched and must
    still pass the strict `core.skills.parse_skill_text` validation — this never weakens the
    check, it only fixes the single most common authoring slip."""
    from core.skills import _FRONTMATTER_FENCE

    if not content.lstrip().startswith(_FRONTMATTER_FENCE):
        return content
    body_lines = content.splitlines()
    for line in body_lines[1:]:
        if line.strip() == _FRONTMATTER_FENCE:
            return content  # already closed
    return content.rstrip() + "\n" + _FRONTMATTER_FENCE + "\n"


# Layer B.3b (`generate_and_install_module`) discovery target: a user data-dir `modules/`
# directory, set once at startup (`app.py`: `agent.forge._USER_MODULE_DIR =
# Path(settings.data_dir) / "modules"`). Unlike `_USER_SKILL_DIR`/`_USER_RULEPACK_DIR` this is NOT
# a discovery registry with built-ins to protect -- a generated module is per-room content
# (`ctx.chat_key`-scoped via the existing module-ingestion pipeline), so this directory is only a
# confined place to persist the generated Markdown before/while it is ingested. `None` (the
# default, and every test unless it opts in) means `generate_and_install_module` refuses with
# `"no_data_dir"`, exactly like the other two generators.
_USER_MODULE_DIR: Path | None = None

# A repeated tool call in the next few model turns is almost certainly the same requested forge,
# not an intentional revision. Different descriptions still install immediately (last write wins).
_MODULE_FORGE_REPEAT_WINDOW_SECONDS = 5 * 60

# The room_state key carrying generated module illustration provenance: a list of
# ``{kind, subject, hash, name}`` entries mapping each forge illustration to the
# scene/NPC/item it depicts, so the runtime can reuse it as a reference image.
MODULE_MEDIA_INDEX_KEY = "module_media_index"

# Keeper-selectable extra content for a generated module (the `media`/`companion` options). Two
# closed vocabularies, ordered so a normalized selection is deterministic; unknown ids are
# ignored by `_normalize_option_ids`, never an error. Audio is deliberately absent (keeper veto).
MEDIA_OPTION_IDS: tuple[str, ...] = ("cover", "scenes", "npcs", "items", "pregens")
COMPANION_OPTION_IDS: tuple[str, ...] = ("skills", "rulepacks", "cards")

# Per-kind and total caps for the media pass: a cover is singular, every other kind illustrates
# the KEY subjects only, and one generation never renders more than a dozen images.
_MEDIA_KIND_CAPS: dict[str, int] = {"cover": 1, "scenes": 6, "npcs": 6, "items": 6, "pregens": 6}
# Render at most a few shots concurrently: image providers rate-limit (HTTP 429) when a full
# cast's portraits fan out at once, and serializing a dozen images is a 10+ minute slog. Three
# in flight is a reasonable middle ground.
_MEDIA_CONCURRENCY = 3
_MEDIA_RETRIES = 3


def _should_retry_imagegen(exc: BaseException) -> bool:
    """Retry transient image-provider failures (timeout / rate-limit / 5xx), not permanent
    rejections. A timeout is a hung or overloaded provider — the most transient failure there
    is — and an HTTP 429/5xx is a provider-side hiccup; both deserve the bounded backoff retry.

    Uses ``ImageGenError.code`` (never ``args[0]``): the constructor stores ``detail or code``
    as the single args entry, so a status-carrying error's ``args[0]`` is the STATUS, and a
    bare code error's ``args[0]`` is the code itself.
    """
    if not isinstance(exc, ImageGenError):
        return True
    code = exc.code
    if code == "imagegen_http_error":
        status = str(exc.args[0]) if exc.args else ""
        return status in {"429", "500", "502", "503", "504"}
    return code == "imagegen_timeout"


async def _imagegen_generate_retry(imagegen: Any, prompt: str, *, size: str) -> tuple[bytes, str]:
    """`imagegen.generate` with bounded backoff retry for transient failures (timeout / 429/5xx)."""
    last: BaseException | None = None
    for attempt in range(_MEDIA_RETRIES):
        try:
            return await imagegen.generate(prompt, size=size)
        except Exception as exc:  # noqa: BLE001 — provider errors are retried or surfaced
            last = exc
            if not _should_retry_imagegen(exc):
                raise
            await asyncio.sleep(min(2**attempt, 8))
    raise last  # type: ignore[misc]
_MEDIA_TOTAL_CAP = 12

# The cards lane ships a small claimable cast, like a published scenario's pregen cards.
_MAX_COMPANION_CARDS = 4

# File-extension map for stored module illustrations (the blob is keyed by content hash; the
# extension is cosmetic, for the keeper browsing the room's media deck).
_IMAGE_MIME_EXTS: dict[str, str] = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def _normalize_option_ids(values: list[str] | None, vocabulary: tuple[str, ...]) -> list[str]:
    """Filter a keeper-supplied option list to its closed vocabulary: unknown ids are ignored,
    duplicates collapse, and the result follows the vocabulary's canonical order so the same
    selection always hashes and renders identically."""
    requested = {str(value).strip().casefold() for value in values or []}
    return [kind for kind in vocabulary if kind in requested]


@dataclass(frozen=True)
class ForgeResult:
    """Outcome of a `generate_and_install_*` call (skill / rulepack / module).

    `skill_id`/`name` are the generic "installed id" / "display name" slots each generator fills in
    (a rulepack's system id/its first declared name, a module's slug/title) -- kept as one shared
    shape across all three generators rather than three near-identical dataclasses. `detail` is an
    optional extra payload only the module generator uses (the room-install confirmation from
    `agent.kp_tools_knowledge.DocumentTools.upload_document`); it is always `""` for skills/rulepacks.

    `error` is an internal (English, untranslated) diagnostic -- `agent.kp_tools_forge.ForgeTools`
    maps it to a localized string for the model/player. `"no_data_dir"` and a `"bad_id: ..."` /
    `"invalid_skill: ..."` / `"invalid_rulepack: ..."` / `"path_escape: ..."` / `"write_failed:
    ..."` prefix are the recognized shapes; callers that only care about success/failure should
    just check `ok`.
    """

    ok: bool
    skill_id: str
    name: str
    path: str
    error: str
    detail: str = ""
    reused: bool = False


async def _llm_authored(
    services: Services,
    messages: list[dict],
    *,
    chat_key: str | None = None,
) -> tuple[str | None, ForgeResult | None]:
    """Call the LLM to author an artifact. Returns `(content, None)` on success, or
    `(None, ForgeResult)` when the call itself failed (timeout / rate-limit / auth error) or came
    back empty — so a backend LLM failure becomes a clean `ForgeResult` the admin/tool path reports,
    NOT an uncaught exception that surfaces as a generic `error` frame (which would leave a client's
    "generating…" spinner stuck forever)."""
    try:
        llm = await services.main_llm(chat_key) if chat_key else services.llm
        with lane_scope("authoring", chat_key=chat_key or None):
            result = await llm.chat(messages)
    except Exception as exc:
        return None, ForgeResult(False, "", "", "", f"llm_failed: {exc}")  # i18n-exempt
    if chat_key:
        await record_usage_stats(
            services.store,
            chat_key,
            result.usage,
            model=services.settings.llm.chat_model,
            context_window=services.settings.llm.context_window,
        )
    content = (result.content or "").strip()
    if not content:
        return None, ForgeResult(False, "", "", "", "empty_response")
    return content, None


async def _llm_authored_retry(
    services: Services,
    messages: list[dict],
    *,
    chat_key: str | None = None,
    attempts: int = 2,
) -> tuple[str | None, ForgeResult | None]:
    """`_llm_authored`, retried on failure. A live provider (e.g. a remote LLM) can transiently
    time out or return empty for a long structured prompt (a rulepack YAML) — the pack engine's
    companion content is best-effort, so a single retry materially raises the chance a bundled
    skill/rulepack lands, at the cost of one extra slow call on a genuinely hard failure."""
    last: ForgeResult | None = None
    for attempt in range(max(1, attempts)):
        content, failure = await _llm_authored(services, messages, chat_key=chat_key)
        if failure is None:
            return content, None
        last = failure
        if attempt + 1 < max(1, attempts):
            logger.warning("[pack-forge] authoring attempt %d/%d failed: %s; retrying",
                           attempt + 1, attempts, failure.error)
    return None, last


def _slugify(text: str) -> str:
    """Lowercase, collapse whitespace/underscores to `-`, strip everything else, and require the
    result to match `^[a-z0-9][a-z0-9-]*$`. Returns `""` when nothing safe survives (e.g. an
    all-CJK or all-punctuation name) -- the caller treats that as a rejection, never a fallback
    to unsafe input. Any path-shaped character (`/`, `\\`, `.`) is stripped, not preserved, so a
    traversal attempt (e.g. `"../../etc"`) sanitizes down to a plain, safe token (here: `"etc"`)
    rather than smuggling a path separator through into a directory name.
    """
    lowered = text.strip().lower()
    collapsed = re.sub(r"[\s_]+", "-", lowered)
    stripped = re.sub(r"[^a-z0-9-]", "", collapsed)
    slug = re.sub(r"-{2,}", "-", stripped).strip("-")
    if len(slug) > _MAX_SLUG_LEN:
        slug = slug[:_MAX_SLUG_LEN].rstrip("-")
    return slug if _SLUG_OK_RE.match(slug) else ""


def _unique_user_id(user_dir: Path, base: str) -> str:
    """Return `base`, else `base-2`, `base-3`, ... — the first id whose user-dir directory does
    NOT already exist and is not a built-in — so installing a generated skill never silently
    clobbers an existing user skill (or a built-in) of the same name."""
    candidate = base
    counter = 2
    while (user_dir / candidate).exists() or candidate in skills.built_in_skill_ids():
        candidate = f"{base}-{counter}"
        counter += 1
        if counter > 999:  # pathological guard; effectively unreachable
            return candidate
    return candidate


def _confined_target(user_dir: Path, skill_id: str) -> Path:
    """Resolve `<user_dir>/<skill_id>/SKILL.md`, asserting the result stays inside `user_dir`.

    Independent of `_slugify`: this rejects any `skill_id` that is not a plain safe slug — so `.`,
    `..`, `""`, and anything with a path separator are refused here directly (not merely by relying
    on `_slugify` never having a bug), which makes the confinement guard true and self-standing
    (see `tests/agent/test_forge.py`'s path-confinement test).
    """
    if not _SLUG_OK_RE.match(skill_id):
        raise ValueError(f"unsafe skill id (not a plain slug): {skill_id!r}")  # i18n-exempt
    base = user_dir.resolve()
    target = (user_dir / skill_id / "SKILL.md").resolve()
    if not target.is_relative_to(base):
        # Internal diagnostic only -- never shown raw; `generate_and_install_skill` folds it into
        # a `"path_escape: ..."` `ForgeResult.error`, localized by `agent.kp_tools_forge`.
        raise ValueError(f"refusing to write outside the user skill directory: {skill_id!r}")  # i18n-exempt
    return target


def _confined_file_target(user_dir: Path, entry_id: str, filename: str) -> Path:
    """Resolve `<user_dir>/<filename>`, asserting the result stays inside `user_dir`.

    A flat-file sibling of `_confined_target` (which assumes a `<id>/SKILL.md` directory shape):
    the rulepack (`<id>.yaml`) and module (`<id>.md`) generators each install a single flat file
    rather than a subdirectory, so they confine directly by filename. Independent of `_slugify`,
    same as `_confined_target`: `entry_id` (the id `filename` was derived from) must itself already
    be a plain safe slug -- `.`, `..`, `""`, and anything with a path separator are refused here
    directly, not merely by relying on `_slugify` never having a bug.
    """
    if not _SLUG_OK_RE.match(entry_id):
        raise ValueError(f"unsafe id (not a plain slug): {entry_id!r}")  # i18n-exempt
    base = user_dir.resolve()
    target = (user_dir / filename).resolve()
    if not target.is_relative_to(base):
        # Internal diagnostic only -- never shown raw; folded into a `"path_escape: ..."`
        # `ForgeResult.error`, localized by `agent.kp_tools_forge`.
        raise ValueError(f"refusing to write outside the user directory: {entry_id!r}")  # i18n-exempt
    return target


def _build_messages(services: Services, description: str) -> list[dict]:
    """The two-message prompt sent to `services.llm.chat`: the schema+example framing text
    (localized, mirroring `agent.module_initializer._build_analysis_prompt`'s "framing text is
    localized" convention -- see `locales/{en,zh}/agent.json`'s `agent.forge.system_prompt`) as
    the system message, and the keeper's raw play-style `description` as the user message.
    """
    return [
        {"role": "system", "content": services.i18n.t("agent.forge.system_prompt")},
        {"role": "user", "content": description},
    ]


async def generate_and_install_skill(
    services: Services,
    description: str,
    *,
    chat_key: str | None = None,
) -> ForgeResult:
    """Ask `services.llm` to author a SKILL.md for `description`, validate it, and install it.

    Never writes anything to disk before the generated text validates as a real `Skill` via the
    same parser `core.skills` discovery uses, never `eval`/`exec`s the model's output, and refuses
    both an empty/unsafe derived id and a collision with a built-in skill id. On success, installs
    under `core.skills._USER_SKILL_DIR` and reloads discovery (`core.skills.reload_skills()`) so
    the new skill is immediately visible to `.skill list` / `.skill enable`.
    """
    user_dir = skills._USER_SKILL_DIR
    if user_dir is None:
        return ForgeResult(False, "", "", "", "no_data_dir")

    content, failure = await _llm_authored(
        services,
        _build_messages(services, description),
        chat_key=chat_key,
    )
    if failure is not None:
        return failure
    assert content is not None  # _llm_authored returns content XOR failure
    content = _strip_code_fence(content)
    content = _repair_skill_frontmatter(content)

    # Hard size cap BEFORE any parse call (see `_MAX_FORGE_CONTENT_BYTES`'s docstring): refuse
    # oversized LLM output outright rather than ever handing it to the YAML parser.
    oversize = _content_too_large(content)
    if oversize is not None:
        return ForgeResult(
            False,
            "",
            "",
            "",
            f"invalid_skill: generated SKILL.md is too large ({oversize} bytes, max {_MAX_FORGE_CONTENT_BYTES})",  # i18n-exempt
        )

    # Step (c): derive the slug BEFORE full validation, from the frontmatter `name` (falling back
    # to the caller's own description when the model omitted one). Reuses the same parser as the
    # real validation below; a parse failure here is reported as "invalid", not "bad_id" -- the id
    # can't be trusted to have been derived from anything meaningful when the frontmatter itself
    # doesn't parse.
    try:
        probe = skills.parse_skill_text(_PROBE_ID, content)
    except Exception as exc:
        return ForgeResult(False, "", "", "", f"invalid_skill: {exc}")

    name_source = probe.name if probe.name and probe.name != _PROBE_ID else description
    skill_id = _slugify(name_source)
    if not skill_id:
        # No ASCII slug survives (e.g. an all-CJK generated name): fall back to a stable
        # content-hash id, mirroring the module generator's `module-<digest>` fallback, instead
        # of rejecting the whole artifact. A hex-suffixed id can never collide with a built-in.
        skill_id = f"skill-{hashlib.sha256(content.encode('utf-8')).hexdigest()[:8]}"
    if skill_id in skills.built_in_skill_ids():
        return ForgeResult(False, "", "", "", f"bad_id: '{skill_id}' collides with a built-in skill")  # i18n-exempt

    # Step (d): the AUTHORITATIVE validation, re-parsed with the real id so `Skill.id` matches the
    # directory it will be written under. Nothing is written to disk before this succeeds.
    try:
        parsed = skills.parse_skill_text(skill_id, content)
    except Exception as exc:
        return ForgeResult(False, "", "", "", f"invalid_skill: {exc}")
    if not parsed.name.strip():
        return ForgeResult(False, "", "", "", "invalid_skill: generated SKILL.md has no name")  # i18n-exempt

    # Non-destructive install: if a user skill of this id already exists, uniquify (base-2, ...)
    # rather than silently overwriting someone's existing custom skill.
    skill_id = _unique_user_id(user_dir, skill_id)

    # Step (e): write, confined to the user skill directory.
    try:
        target = _confined_target(user_dir, skill_id)
    except ValueError as exc:
        return ForgeResult(False, "", "", "", f"path_escape: {exc}")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        # A filesystem-level failure (permissions, name-too-long, disk) is reported through the same
        # ForgeResult contract as every other failure instead of escaping as an unhandled OSError.
        return ForgeResult(False, "", "", "", f"write_failed: {exc}")  # i18n-exempt

    # Step (f): make it discoverable immediately.
    skills.reload_skills()

    return ForgeResult(True, skill_id, parsed.name, str(target), "")


# ---------------------------------------------------------------------------
# Layer B.3b -- the rulepack generator: a "skill that creates rule systems."
# ---------------------------------------------------------------------------


def _unique_user_rulepack_id(user_dir: Path, base: str) -> str:
    """Rulepack analogue of `_unique_user_id`: a rulepack installs as a flat `<id>.yaml` file (not
    a `<id>/` directory), so existence is checked against the FILE. Also mirrors `_unique_user_id`
    in never landing on a built-in id, so a generated pack can never silently occupy a bundled
    system's name even after the earlier explicit collision rejection in
    `generate_and_install_rulepack` -- defense in depth against the same class of bug.
    """
    candidate = base
    counter = 2
    while (user_dir / f"{candidate}.yaml").exists() or candidate in rulepacks.built_in_rulepack_ids():
        candidate = f"{base}-{counter}"
        counter += 1
        if counter > 999:  # pathological guard; effectively unreachable
            return candidate
    return candidate


def _build_rulepack_messages(services: Services, description: str, extends_base: str = "") -> list[dict]:
    """The two-message prompt sent to `services.llm.chat` for rulepack authoring: the localized
    schema+example framing text (`agent.forge.rulepack_system_prompt`) as the system message, and
    the keeper's raw rule-system `description` as the user message -- mirrors `_build_messages`.

    When ``extends_base`` is given (for example a known base pack id), the system prompt gains an instruction to
    author the rulepack as a PATCH on that base system (``extends: <base>`` + only the deltas),
    so the module reuses a known system's attributes/skills/checks instead of inventing a
    standalone replacement."""
    system = services.i18n.t("agent.forge.rulepack_system_prompt")
    if extends_base:
        system += "\n\n" + services.i18n.t(
            "agent.forge.rulepack_extends_instruction", base=extends_base
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": description},
    ]


async def generate_and_install_rulepack(
    services: Services,
    description: str,
    *,
    chat_key: str | None = None,
) -> ForgeResult:
    """Ask `services.llm` to author a rulepack YAML for `description`, validate it, and install it.

    Mirrors `generate_and_install_skill` step for step, adapted for a rulepack's FLAT-FILE shape
    (`<id>.yaml`, not a `<id>/SKILL.md` directory): never writes anything to disk before the
    generated YAML validates as a real `RulePack` via the same builder
    (`core.rulepacks.parse_rulepack_text`) real discovery uses -- including its `derived:` section
    compiling through the safe DSL / named-computer vocabulary, so a bad derived spec raises and is
    rejected here -- and refuses both an empty/unsafe derived id and a collision with a built-in
    system id. On success, installs under `core.rulepacks._USER_RULEPACK_DIR` and
    reloads discovery (`core.rulepacks.reload_rulepacks()`) so the new system is immediately visible
    to `available_systems()`/`load_rulepack()`.
    """
    user_dir = rulepacks._USER_RULEPACK_DIR
    if user_dir is None:
        return ForgeResult(False, "", "", "", "no_data_dir")

    content, failure = await _llm_authored(
        services,
        _build_rulepack_messages(services, description),
        chat_key=chat_key,
    )
    if failure is not None:
        return failure
    assert content is not None  # _llm_authored returns content XOR failure
    content = _strip_code_fence(content)

    # Hard size cap BEFORE any parse call (see `_MAX_FORGE_CONTENT_BYTES`'s docstring): refuse
    # oversized LLM output outright rather than ever handing it to the YAML parser.
    oversize = _content_too_large(content)
    if oversize is not None:
        return ForgeResult(
            False,
            "",
            "",
            "",
            f"invalid_rulepack: generated rulepack YAML is too large ({oversize} bytes, max {_MAX_FORGE_CONTENT_BYTES})",  # i18n-exempt
        )

    # Step (c): derive the slug BEFORE full validation, from the pack's declared `names:` (falling
    # back to the caller's own description when the model omitted any). A parse failure here is
    # reported as "invalid", not "bad_id" -- the id can't be trusted to have been derived from
    # anything meaningful when the YAML itself doesn't parse.
    try:
        probe = rulepacks.parse_rulepack_text(_PROBE_ID, content)
    except Exception as exc:
        return ForgeResult(False, "", "", "", f"invalid_rulepack: {exc}")

    name_source = probe.names[0] if probe.names else description
    pack_id = _slugify(name_source)
    if not pack_id:
        # Same content-hash fallback as the skill generator (all-CJK `names:` have no ASCII
        # slug): install under a stable derived id rather than rejecting a valid pack.
        pack_id = f"pack-{hashlib.sha256(content.encode('utf-8')).hexdigest()[:8]}"
    if pack_id in rulepacks.built_in_rulepack_ids():
        return ForgeResult(False, "", "", "", f"bad_id: '{pack_id}' collides with a built-in rulepack")  # i18n-exempt

    # Step (d): the AUTHORITATIVE validation, re-parsed with the real id. Nothing is written to
    # disk before this succeeds.
    try:
        parsed = rulepacks.parse_rulepack_text(pack_id, content)
    except Exception as exc:
        return ForgeResult(False, "", "", "", f"invalid_rulepack: {exc}")

    # Also refuse a pack that DECLARES a built-in's name/alias (not just a colliding id): the
    # built-in wins resolution anyway, so such a claim would be a dead alias -- reject it explicitly
    # rather than silently write a pack that half-shadows a built-in. Nothing is written yet.
    if rulepacks.claims_built_in_alias((*parsed.names, *parsed.set_keys)):
        return ForgeResult(False, "", "", "", "bad_id: the generated pack claims a name/alias reserved by a built-in system")  # i18n-exempt

    # Non-destructive install: if a user rulepack of this id already exists, uniquify (base-2, ...)
    # rather than silently overwriting someone's existing custom pack.
    pack_id = _unique_user_rulepack_id(user_dir, pack_id)

    # Step (e): write, confined to the user rulepack directory.
    try:
        target = _confined_file_target(user_dir, pack_id, f"{pack_id}.yaml")
    except ValueError as exc:
        return ForgeResult(False, "", "", "", f"path_escape: {exc}")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        # A filesystem-level failure (permissions, name-too-long, disk) is reported through the same
        # ForgeResult contract as every other failure instead of escaping as an unhandled OSError.
        return ForgeResult(False, "", "", "", f"write_failed: {exc}")  # i18n-exempt

    # Step (f): make it discoverable immediately.
    rulepacks.reload_rulepacks()

    display_name = parsed.names[0] if parsed.names else pack_id
    return ForgeResult(True, pack_id, display_name, str(target), "")


# ---------------------------------------------------------------------------
# Layer B.3b -- the module generator: a "skill that creates modules," installed PER-ROOM via the
# existing module-ingestion pipeline (not a global discovery registry like skills/rulepacks).
# ---------------------------------------------------------------------------

_MODULE_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_MODULE_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def _extract_module_title(content: str) -> str:
    """Best-effort title extraction: the first level-1 Markdown heading (`# Title`) in the
    generated document, or `""` if it has none -- callers fall back to the keeper's own
    description in that case, same as the skill/rulepack generators falling back from an omitted
    frontmatter `name`/`names`.
    """
    match = _MODULE_TITLE_RE.search(content)
    return match.group(1).strip() if match else ""


def _extract_module_id(content: str) -> str:
    """Read an explicit module id from YAML frontmatter at the document start.

    Uses `core.yaml_safety.safe_load_no_aliases` (not plain `yaml.safe_load`): this frontmatter is
    LLM-authored content like the skill/rulepack generators', so it must reject alias/anchor nodes
    the same way, closing off the alias-bomb class of attack here too. A YAML error (bad syntax OR
    a rejected alias) degrades to `""`, same as before -- the caller falls back to a hash-derived id.
    """
    match = _MODULE_FRONTMATTER_RE.match(content)
    if match is None:
        return ""
    try:
        frontmatter = safe_load_no_aliases(match.group("body")) or {}
    except yaml.YAMLError:
        return ""
    if not isinstance(frontmatter, dict):
        return ""
    value = frontmatter.get("id")
    return str(value).strip() if value is not None else ""


def _unique_user_module_id(user_dir: Path, base: str) -> str:
    """Module analogue of `_unique_user_id`/`_unique_user_rulepack_id`: a generated module installs
    as a flat `<id>.md` file. Unlike skills/rulepacks there is no built-in-id namespace to protect
    here -- a module is per-room content ingested through the normal document pipeline, not a
    discovery registry with built-ins -- so only existing files in the shared user module
    directory are avoided.
    """
    candidate = base
    counter = 2
    while (user_dir / f"{candidate}.md").exists():
        candidate = f"{base}-{counter}"
        counter += 1
        if counter > 999:  # pathological guard; effectively unreachable
            return candidate
    return candidate


def _normalized_description_hash(description: str) -> str:
    """Return a stable hash for semantically identical forge request text."""
    normalized = " ".join(unicodedata.normalize("NFKC", description).casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _module_forge_last_key(chat_key: str) -> str:
    return "forge_module_last"


def _module_forge_owner_key(chat_key: str, requested_id: str) -> str:
    return f"forge_module_owner.{requested_id}"


def _load_json_object(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


async def _recent_module_forge_result(
    services: Services,
    ctx: AgentCtx,
    user_dir: Path,
    description_hash: str,
    *,
    now: float,
) -> ForgeResult | None:
    record = _load_json_object(
        await services.store.state_get(ctx.chat_key, _module_forge_last_key(ctx.chat_key))
    )
    try:
        age = now - float(record.get("timestamp", 0))
    except (TypeError, ValueError):
        return None
    installed_id = str(record.get("installed_id", ""))
    name = str(record.get("name", ""))
    if not installed_id or not name:
        return None
    try:
        path = _confined_file_target(user_dir, installed_id, f"{installed_id}.md")
    except ValueError:
        return None
    if (
        record.get("description_hash") != description_hash
        or not 0 <= age <= _MODULE_FORGE_REPEAT_WINDOW_SECONDS
        or str(path) != str(record.get("path", ""))
        or not path.is_file()
    ):
        return None
    try:
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    if source_hash != record.get("source_hash"):
        return None
    return ForgeResult(True, installed_id, name, str(path), "", reused=True)


async def _owned_module_id(
    services: Services,
    ctx: AgentCtx,
    user_dir: Path,
    requested_id: str,
) -> str:
    """Reuse this room's prior path for an id; never overwrite another room's file."""
    owner = _load_json_object(
        await services.store.state_get(ctx.chat_key, _module_forge_owner_key(ctx.chat_key, requested_id))
    )
    installed_id = str(owner.get("installed_id", ""))
    if installed_id:
        try:
            owned_target = _confined_file_target(user_dir, installed_id, f"{installed_id}.md")
        except ValueError:
            pass
        else:
            if str(owned_target) == str(owner.get("path", "")):
                return installed_id
    return _unique_user_module_id(user_dir, requested_id)


def _build_module_messages(
    services: Services,
    description: str,
    *,
    locale: str | None = None,
) -> list[dict]:
    """Build the module-authoring messages in the caller's locale.

    The page/admin path can use a client locale different from the server default. The
    conversational tool already carries its locale in ``AgentCtx``; both paths must therefore
    bind the authoring framing text explicitly instead of reading only ``services.i18n``.
    """
    i18n = services.i18n.with_locale(locale) if locale else services.i18n
    system_prompt = "\n\n".join(
        (
            i18n.t("agent.forge.module_system_prompt"),
            i18n.t("agent.forge.module_language_requirement"),
            i18n.t("agent.forge.module_id_requirement"),
        )
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": description},
    ]


def _build_module_prompt_messages(
    services: Services,
    idea: str,
    *,
    mode: str,
    rule_strategy: str = "",
    room_system: str = "",
    locale: str | None = None,
) -> list[dict]:
    """Build the plain-text prompt-assistant request without invoking module authoring."""
    i18n = services.i18n.with_locale(locale) if locale else services.i18n
    request_key = (
        "agent.forge.module_prompt_suggest"
        if mode == "suggest"
        else "agent.forge.module_prompt_rewrite"
    )
    rule_requirement = _module_prompt_rule_requirement(i18n, rule_strategy, room_system)
    return [
        {"role": "system", "content": i18n.t("agent.forge.module_prompt_system_prompt")},
        {
            "role": "user",
            "content": i18n.t(request_key, idea=idea, rule_requirement=rule_requirement),
        },
    ]


def _module_prompt_rule_requirement(i18n: Any, rule_strategy: str, room_system: str) -> str:
    """Turn the forge selector into a terse, model-facing rule constraint: NAME the system
    (CoC / DnD / WoD or the room's own), one short line — the keeper asked for the rule in one
    breath, not a full spec dump (no roll/target/outcome/parameter enumeration)."""
    strategy = rule_strategy.strip()
    selected_system = (strategy.split(":", 1)[1] if ":" in strategy else room_system.strip()).strip()
    if not selected_system:
        selected_system = room_system.strip()
    # Friendly one-line system names — the keeper wants the rule in one breath ("CoC/DnD 就行").
    display = {
        "coc7": "CoC 7e",
        "coc": "CoC",
        "dnd5e": "DnD 5e",
        "dnd": "DnD",
        "wod": "WoD",
    }.get(selected_system.casefold(), selected_system or i18n.t("agent.forge.module_prompt_room_system"))
    system = display
    if strategy == "standalone":
        return i18n.t("agent.forge.module_prompt_rule_standalone")
    if strategy.startswith("patch:"):
        return i18n.t("agent.forge.module_prompt_rule_patch", system=system)
    if strategy.startswith("use:"):
        return i18n.t("agent.forge.module_prompt_rule_use", system=system)
    return i18n.t("agent.forge.module_prompt_rule_follow", system=system)


async def generate_module_prompt(
    services: Services,
    idea: str,
    *,
    mode: str,
    rule_strategy: str = "",
    room_system: str = "",
    locale: str | None = None,
    chat_key: str | None = None,
) -> ForgeResult:
    """Generate a module description only; never parse, persist, or install the result."""
    if mode not in {"suggest", "rewrite"}:
        return ForgeResult(False, "", "", "", "bad_request")
    content, failure = await _llm_authored(
        services,
        _build_module_prompt_messages(
            services,
            idea,
            mode=mode,
            rule_strategy=rule_strategy,
            room_system=room_system,
            locale=locale,
        ),
        chat_key=chat_key,
    )
    if failure is not None:
        return failure
    assert content is not None
    return ForgeResult(True, "", "", "", "", detail=_strip_code_fence(content).strip())


async def generate_and_install_module(
    services: Services,
    ctx: AgentCtx,
    description: str,
    *,
    media: list[str] | None = None,
    companion: list[str] | None = None,
    progress: ProgressCb = None,
    auto_import: bool = True,
) -> ForgeResult:
    """Ask `services.llm` to author a module/scenario document for `description`, then install it
    into THIS ROOM's (`ctx.chat_key`) knowledge pool via the EXISTING module pipeline -- never a
    new bespoke one.

    Unlike the skill/rulepack generators (a global, discovery-based data-dir), a module is per-room
    content: the generated Markdown is written to a confined file under `_USER_MODULE_DIR`
    (path-confined + id-sanitized exactly like the other two generators), then handed to
    `agent.kp_tools_knowledge.DocumentTools.upload_document(ctx, ..., doc_type="module")` -- the
    SAME ingestion + full-text-analysis path the `.module` command / a manual upload uses -- so the
    resulting keeper/player knowledge pools land under `ctx.chat_key`, not some new store shape.
    `ForgeResult.detail` carries `upload_document`'s own localized confirmation (chunk count,
    module-init status, etc.) -- the room-install summary. `ok=True` reflects that a valid module
    document was authored and written to disk; if the room-install step itself couldn't complete
    (e.g. no filesystem adapter on this `ctx`, or the vector DB disabled), `detail` carries THAT
    explanation instead of a success confirmation -- callers should surface `detail` to the keeper
    either way rather than only checking `ok`.

    `media`/`companion` are the keeper's per-generation opt-ins (ids from MEDIA_OPTION_IDS /
    COMPANION_OPTION_IDS; unknown ids are ignored). They run AFTER the module itself is installed
    and NEVER fail it: any error inside a pass (provider down, rate limit, unparseable model
    reply) degrades to fewer/zero artifacts and is reported as extra lines in `detail`. The
    selection folds into the repeat-request hash, so re-asking with different options is a real
    new request, not a suppressed duplicate.
    """
    user_dir = _USER_MODULE_DIR
    if user_dir is None:
        return ForgeResult(False, "", "", "", "no_data_dir")

    media_kinds = _normalize_option_ids(media, MEDIA_OPTION_IDS)
    companion_kinds = _normalize_option_ids(companion, COMPANION_OPTION_IDS)
    options_suffix = f"\noptions:{','.join(media_kinds)}|{','.join(companion_kinds)}"

    now = time.time()
    description_hash = _normalized_description_hash(description + options_suffix)
    repeated = await _recent_module_forge_result(
        services,
        ctx,
        user_dir,
        description_hash,
        now=now,
    )
    if repeated is not None:
        return repeated

    await _emit(progress, "authoring")
    content, failure = await _llm_authored(
        services,
        _build_module_messages(services, description, locale=ctx.locale),
        chat_key=ctx.chat_key,
    )
    if failure is not None:
        return failure
    assert content is not None  # _llm_authored returns content XOR failure
    content = _strip_code_fence(content)

    title = _extract_module_title(content) or description
    if not _extract_module_title(content):
        # A provider fallback or a terse refusal is still non-empty text, but it
        # is not a module source. Reject it before writing or replacing the
        # room's current scenario.
        return ForgeResult(False, "", "", "", "invalid_module_output")
    module_id = _slugify(_extract_module_id(content)) or _slugify(title)
    used_hash_id = not module_id
    if used_hash_id:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
        module_id = f"module-{digest}"

    # A module generated by this room owns its stable path and is replaced in place on an
    # intentional revision. A same-named file owned by another room remains protected by the
    # suffixing behavior.
    requested_id = module_id
    module_id = await _owned_module_id(services, ctx, user_dir, requested_id)

    # Write, confined to the user module directory. Nothing downstream (the room install) runs
    # before this succeeds.
    try:
        target = _confined_file_target(user_dir, module_id, f"{module_id}.md")
    except ValueError as exc:
        return ForgeResult(False, "", "", "", f"path_escape: {exc}")

    try:
        previous_file = target.read_bytes() if target.is_file() else None
    except OSError as exc:
        return ForgeResult(False, "", "", "", f"write_failed: {exc}")  # i18n-exempt
    runtime_keys = (
        "module_fulltext",
        "module_init_status",
        "module_init_error",
        "module_source",
    )
    previous_runtime = {
        key: await services.store.state_get(ctx.chat_key, key) for key in runtime_keys
    }
    previous_pool_doc = await services.documents.get(ctx.chat_key, "module_pool", MODULE_POOL_ID)

    try:
        atomic_write_private(target, content)
    except OSError as exc:
        # A filesystem-level failure (permissions, name-too-long, disk) is reported through the same
        # ForgeResult contract as every other failure instead of escaping as an unhandled OSError.
        return ForgeResult(False, "", "", "", f"write_failed: {exc}")  # i18n-exempt

    # Reuse the EXISTING module-ingestion pipeline verbatim (docs/plugins.md, this module's own
    # docstring): chunk + embed into the vector store, and (since doc_type="module") auto-trigger
    # `services.module_init.initialize` -- so `ctx.chat_key`'s keeper/player knowledge pools are
    # built by the exact same code a manual `.module` upload runs, not a parallel bespoke path.
    doc_tools = DocumentTools(services)
    # `target` is a server-authored path under `_USER_MODULE_DIR`, not untrusted caller input,
    # so the upload runs with an fs scoped to that directory — the transport's confined
    # LocalFs base (cwd) need not contain data_dir (e.g. an absolute data_dir under systemd).
    install_ctx = replace(ctx, fs=LocalFs(user_dir))
    await _emit(progress, "analyzing")
    install_note = ""
    if auto_import:
        install_note = await doc_tools.upload_document(install_ctx, file_path=str(target), doc_type="module")
        status = await services.store.state_get(ctx.chat_key, "module_init_status")
        installed_fulltext = await services.store.state_get(ctx.chat_key, "module_fulltext")
        pool_view = await services.documents.get_view(
            ctx.chat_key, "module_pool", MODULE_POOL_ID, KEEPER_VIEWER
        )
        consistent = (
            status in {"ready", "ready_fallback"}
            and installed_fulltext == content
            and bool((pool_view or {}).get("keeper"))
            and bool((pool_view or {}).get("player"))
        )
        if not consistent:
            # Publish the file and runtime state as one logical installation. If analysis/persistence
            # did not finish coherently, restore the prior version instead of leaving a mixed module.
            if previous_file is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write_private(target, previous_file)
            for key, value in previous_runtime.items():
                if value is None:
                    await services.store.state_delete(ctx.chat_key, key)
                else:
                    await services.store.state_set(ctx.chat_key, key, value)
            if previous_pool_doc is None:
                await services.documents.delete(ctx.chat_key, "module_pool", MODULE_POOL_ID)
            else:
                await services.documents.put_singleton(
                    ctx.chat_key, "module_pool", previous_pool_doc.data, source=previous_pool_doc.source
                )
            return ForgeResult(False, "", "", "", f"install_inconsistent: status={status}")  # i18n-exempt

    if used_hash_id:
        fallback_note = services.i18n.with_locale(ctx.locale).t(
            "agent.forge.module_id_fallback",
            module_id=module_id,
        )
        install_note = f"{fallback_note}\n{install_note}" if install_note else fallback_note

    record = json.dumps(
        {
            "description_hash": description_hash,
            "installed_id": module_id,
            "name": title,
            "path": str(target),
            "source_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "timestamp": time.time(),
        },
        ensure_ascii=False,
    )
    owner = json.dumps(
        {"installed_id": module_id, "path": str(target)},
        ensure_ascii=False,
    )
    await services.store.state_set(ctx.chat_key, _module_forge_last_key(ctx.chat_key), record)
    await services.store.state_set(ctx.chat_key, _module_forge_owner_key(ctx.chat_key, requested_id), owner)
    if auto_import:
        await services.store.state_set(ctx.chat_key, "module_source", f"{module_id}.md")

    # Keeper-opted extra content, AFTER the module itself is safely installed. Neither pass may
    # fail the module: each degrades to fewer/zero artifacts and reports its outcome as extra
    # detail lines instead of ever turning a good module install into an error.
    i18n = services.i18n.with_locale(ctx.locale)
    extra_notes: list[str] = []
    if media_kinds:
        extra_notes.append(
            await _module_media_pass(
                services,
                ctx,
                content,
                module_id,
                media_kinds,
                i18n,
                assets_dir=user_dir / f"{module_id}.assets",
            )
        )
    if companion_kinds:
        extra_notes.extend(
            await _module_companion_pass(services, ctx, content, title, description, module_id, companion_kinds, i18n)
        )
    if not auto_import:
        install_note = i18n.t("agent.forge.module_generated_not_imported", name=title, path=str(target))
    detail = "\n".join(part for part in [install_note, *extra_notes] if part)
    return ForgeResult(True, module_id, title, str(target), "", detail=detail)


# ---------------------------------------------------------------------------
# Keeper-selectable extra content (the `media`/`companion` options). Both passes share one
# stance: the module is already installed when they run, so NOTHING here may fail the forge --
# every error degrades to fewer/zero artifacts plus a localized detail line naming the outcome.
# ---------------------------------------------------------------------------


def _option_reason(i18n, code: str) -> str:
    """The localized one-line reason a media/cards pass produced less than requested."""
    return i18n.t(f"agent.forge.module_option_reason.{code}")


def _parse_json_array(raw: str) -> list:
    """Best-effort extraction of a JSON array from an LLM reply: the whole reply first, then the
    widest `[...]` span (models love wrapping JSON in prose), then a `{shots: [...]}`-style
    envelope. `[]` on any failure -- the caller degrades to "no shots/concepts", never an error."""
    text = raw.strip()
    candidates = [text]
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ("shots", "cards", "characters", "concepts"):
                if isinstance(value.get(key), list):
                    return value[key]
    return []


@dataclass(frozen=True)
class _Shot:
    """One planned module illustration: which kind, what it depicts, the self-contained imagegen
    prompt, and an optional keeper-facing caption."""

    kind: str
    subject: str
    prompt: str
    caption: str


def _parse_shot_list(raw: str, kinds: list[str]) -> list[_Shot]:
    """Validate the model's shot list against the keeper's selection: only requested kinds, per-
    kind caps, one portrait per NPC (owner verdict 2026-08-22 -- enforced here as well as in the
    prompt), and a hard total cap. Malformed entries are skipped, never fatal."""
    selected = set(kinds)
    shots: list[_Shot] = []
    per_kind: dict[str, int] = {}
    npc_subjects: set[str] = set()
    for entry in _parse_json_array(raw):
        if len(shots) >= _MEDIA_TOTAL_CAP:
            break
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip().casefold()
        prompt = str(entry.get("prompt") or "").strip()
        if kind not in selected or not prompt:
            continue
        if per_kind.get(kind, 0) >= _MEDIA_KIND_CAPS[kind]:
            continue
        subject = str(entry.get("subject") or "").strip()
        if kind == "npcs":
            key = subject.casefold()
            if key and key in npc_subjects:
                continue
            npc_subjects.add(key)
        per_kind[kind] = per_kind.get(kind, 0) + 1
        shots.append(_Shot(kind=kind, subject=subject, prompt=prompt, caption=str(entry.get("caption") or "").strip()))
    return shots


# Which media kind illustrates which worldbook category. The world card's entries are
# category-tagged (lore/npc/clue/truth/secret); a generated `npcs` shot depicts a `npc`
# entry, `scenes` shots depict `lore` (place/setting) entries, `items` shots depict `clue`
# (item/clue) entries. Truth/secret (keeper-only) entries are never illustrated.
_WORLDBOOK_CATEGORY_TO_MEDIA_KIND: dict[str, str] = {"npc": "npcs", "lore": "scenes", "clue": "items"}


def _worldbook_subject_names(card_text: dict[str, Any]) -> dict[str, list[str]]:
    """Real scene/NPC/item names per media kind, from the world card's worldbook entries.

    Each entry's PRIMARY name is its first trigger key (the worldbook schema has no dedicated
    `name` field; keys[0] is the canonical name the module refers to). Returns e.g.
    ``{"npcs": ["以赛亚·哈德利", …], "scenes": […], "items": […]}`` so the shot-designer is
    told to use the ACTUAL cast/places/objects — otherwise it invents names that cannot bind
    back to the worldbook entries (the pregens already had this guard; scenes/NPCs/items did
    not)."""
    names: dict[str, list[str]] = {}
    for entry in card_text.get("worldbook") or []:
        if not isinstance(entry, dict):
            continue
        category = str(entry.get("category") or "lore").strip().casefold()
        kind = _WORLDBOOK_CATEGORY_TO_MEDIA_KIND.get(category)
        if not kind:
            continue
        keys = entry.get("keys") or []
        primary = next((str(k).strip() for k in keys if str(k).strip()), "")
        if primary and primary not in names.setdefault(kind, []):
            names[kind].append(primary)
    return names


def _bind_worldbook_images(card_text: dict[str, Any], media_index: list[dict[str, str]]) -> int:
    """Stamp generated npc/scene/item illustrations onto their matching worldbook entries.

    Mirrors the pregen-portrait binding (match shot.subject to the entry's canonical name and
    write the asset filename onto the entry). Without it the scene/NPC/item images stayed
    orphans — generated and stored, but never attached to the worldbook entry they depict. The
    ``image`` field rides the card and survives import into the room's lore documents (see
    `core.worldbook.LoreEntry.image`). Returns how many entries were stamped."""
    bound = 0
    shots_by_kind: dict[str, list[dict[str, str]]] = {}
    for shot in media_index:
        kind = shot.get("kind")
        if kind in ("npcs", "scenes", "items") and shot.get("name"):
            shots_by_kind.setdefault(kind, []).append(shot)
    for entry in card_text.get("worldbook") or []:
        if not isinstance(entry, dict):
            continue
        category = str(entry.get("category") or "lore").strip().casefold()
        kind = _WORLDBOOK_CATEGORY_TO_MEDIA_KIND.get(category)
        if not kind or entry.get("image"):
            continue
        keys = {str(k).strip().casefold() for k in (entry.get("keys") or []) if str(k).strip()}
        for shot in shots_by_kind.get(kind, []):
            subject = str(shot.get("subject") or "").strip().casefold()
            if subject and subject in keys:
                entry["image"] = shot.get("name", "")
                bound += 1
                break
    return bound


def _build_module_media_messages(
    services: Services,
    content: str,
    kinds: list[str],
    i18n,
    pregen_names: list[str] | None = None,
    subject_names: dict[str, list[str]] | None = None,
) -> list[dict]:
    """The two-message shot-list prompt, mirroring `_build_module_messages`: the localized
    shot-designer framing as the system message, and the kinds-with-caps request plus the module
    document as the user message. ``pregen_names`` (the world card's CLAIMABLE INVESTIGATORS)
    are appended so `pregens` shots name the ACTUAL cast — otherwise the shot designer invents its
    own names and the portraits cannot bind back to the investigators. ``subject_names`` extends
    the same discipline to the other kinds: real scene/NPC/item names from the world card, so
    `npcs`/`scenes`/`items` shots depict characters/places/objects that actually exist in the
    module and can bind back to their worldbook entries."""
    system_prompt = "\n\n".join(
        (
            i18n.t("agent.forge.module_media_system_prompt"),
            i18n.t("agent.forge.module_media_language_requirement"),
        )
    )
    kinds_text = ", ".join(f"{kind} ≤ {_MEDIA_KIND_CAPS[kind]}" for kind in kinds)
    user_prompt = i18n.t("agent.forge.module_media_request", kinds=kinds_text, module=content)
    if pregen_names:
        user_prompt += "\n\n" + i18n.t(
            "agent.forge.module_media_pregen_list", names="\n".join(f"- {name}" for name in pregen_names)
        )
    if subject_names:
        groups = "\n".join(
            f"- {kind}: {'、'.join(names)}" for kind, names in subject_names.items() if names
        )
        if groups:
            user_prompt += "\n\n" + i18n.t("agent.forge.module_media_subject_names", groups=groups)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


async def _module_media_pass(
    services: Services,
    ctx: AgentCtx,
    content: str,
    module_id: str,
    kinds: list[str],
    i18n,
    *,
    assets_dir: Path | None = None,
) -> str:
    """Generate the keeper-selected module illustrations: one scoped shot-list call, then the
    room's own imagegen lane per shot, stored into the room's media deck under a
    `module-<id>-<kind>-<n>` provenance name. NOT auto-broadcast -- the keeper pushes handouts
    when the table calls for them, same stance as a pack's `assets:`. The first stop condition
    (hourly room cap, provider error, store error) ends the loop; earlier images are kept.

    When ``assets_dir`` is given (the module's own `modules/<id>.assets/` directory), each
    rendered image is ALSO written there, so the module's illustrations travel with the module
    source itself rather than being trapped in the room that happened to generate them — a
    re-import into another room picks them up (see `DocumentTools.upload_document`'s module
    branch). Best-effort: a filesystem failure writing the asset copy is reported but never
    fails the pass or the module."""
    imagegen = await services.imagegen_for_room(ctx.chat_key)
    if imagegen is None:
        return i18n.t("agent.forge.module_media_none", reason=_option_reason(i18n, "not_configured"))

    raw, failure = await _llm_authored(
        services,
        _build_module_media_messages(services, content, kinds, i18n),
        chat_key=ctx.chat_key,
    )
    if failure is not None:
        return i18n.t("agent.forge.module_media_none", reason=_option_reason(i18n, "shot_list_failed"))
    assert raw is not None  # _llm_authored returns content XOR failure

    shots = _parse_shot_list(raw, kinds)
    if not shots:
        return i18n.t("agent.forge.module_media_none", reason=_option_reason(i18n, "no_shots"))

    tui_settings = services.settings.tui
    store = MediaStore(
        services.store,
        services.settings.data_dir,
        max_file_bytes=tui_settings.media_max_file_bytes,
        room_quota_bytes=tui_settings.media_room_quota_bytes,
        allowed_mimes=ALLOWED_IMAGE_MIMES,
    )
    generated: list[str] = []
    stop_reason = ""
    media_index: list[dict[str, str]] = []
    for index, shot in enumerate(shots, 1):
        if not allow_imagegen_request(services, ctx.chat_key):
            stop_reason = "rate_limited"
            break
        try:
            data, mime = await imagegen.generate(shot.prompt, size=services.settings.imagegen.size)
        except Exception:  # provider down / refusal: stop, keep earlier images, report
            stop_reason = "provider_error"
            break
        name = f"module-{module_id}-{shot.kind}-{index}{_IMAGE_MIME_EXTS.get(mime, '.png')}"
        try:
            record = await store.register_blob(
                room=ctx.chat_key,
                data=data,
                mime=mime,
                name=name,
                uploader=ctx.uid(),
            )
        except Exception:  # quota/mime rejection: same degrade-and-report stance
            stop_reason = "store_error"
            break
        generated.append(record.name)
        # The module's own asset copy: the illustration travels with the module source, not the
        # generating room. Keyed by name (the same provenance name registered in the room), so a
        # re-import can read it back verbatim. Best-effort — a write failure keeps the room copy.
        if assets_dir is not None:
            try:
                assets_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_private(assets_dir / name, data)
            except Exception:  # noqa: BLE001 — an asset-copy failure must never fail the forge
                pass
        # Persist the shot's subject (which scene/NPC/item) with the stored image so
        # the runtime can reuse it as a REFERENCE for `.image <kind> <subject>` — the
        # provenance name alone (`module-<id>-<kind>-<n>`) cannot name the subject.
        media_index.append(
            {"kind": shot.kind, "subject": shot.subject, "hash": record.hash, "name": record.name}
        )
    if media_index:
        await _append_module_media_index(services, ctx.chat_key, media_index)

    names = ", ".join(generated)
    if generated and not stop_reason:
        return i18n.t("agent.forge.module_media_done", count=len(generated), names=names)
    if generated:
        return i18n.t(
            "agent.forge.module_media_partial",
            count=len(generated),
            names=names,
            reason=_option_reason(i18n, stop_reason),
        )
    return i18n.t("agent.forge.module_media_none", reason=_option_reason(i18n, stop_reason or "no_shots"))


async def _append_module_media_index(services: Services, chat_key: str, entries: list[dict[str, str]]) -> None:
    """Append shot→image provenance to the room's `module_media_index` state.

    Each entry names the illustrated kind + subject and the stored image's hash/name, so
    the runtime can fetch a subject's reference image for `.image` consistency. Best-effort
    and never fatal: an unreadable index just means the next append starts fresh.
    """
    raw = await services.store.state_get(chat_key, MODULE_MEDIA_INDEX_KEY)
    existing: list[dict[str, str]] = []
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, list):
                existing = [e for e in value if isinstance(e, dict)]
        except (json.JSONDecodeError, TypeError):
            pass
    merged = existing + entries
    try:
        await services.store.state_set(chat_key, MODULE_MEDIA_INDEX_KEY, json.dumps(merged))
    except Exception:  # noqa: BLE001 — index persistence must never break a forge
        return


def _build_module_cards_messages(services: Services, content: str, i18n) -> list[dict]:
    """The two-message pregen-concept prompt, mirroring `_build_module_media_messages`."""
    system_prompt = "\n\n".join(
        (
            i18n.t("agent.forge.module_cards_system_prompt"),
            i18n.t("agent.forge.module_cards_language_requirement"),
        )
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": i18n.t("agent.forge.module_cards_request", module=content)},
    ]


def _parse_card_concepts(raw: str) -> list[dict[str, str]]:
    """Validate the model's pregen concepts: name + description required, names deduplicated,
    capped at `_MAX_COMPANION_CARDS`. Malformed entries are skipped, never fatal."""
    concepts: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in _parse_json_array(raw):
        if len(concepts) >= _MAX_COMPANION_CARDS:
            break
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        description = str(entry.get("description") or "").strip()
        if not name or not description or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        concepts.append({"name": name, "description": description})
    return concepts


async def _module_cards_pass(services: Services, ctx: AgentCtx, content: str, module_id: str, i18n) -> str:
    """Generate claimable pregen character cards for the module: one scoped concept call, then
    each concept through the EXISTING `.genchar` pipeline (`build_sheet_from_description` +
    `validate_sheet`) onto the room's pregen roster (`core.pregen_roster`), where players pick
    cards up with `.pc claim`. The sheets are built on the room's active rule system."""
    raw, failure = await _llm_authored(
        services,
        _build_module_cards_messages(services, content, i18n),
        chat_key=ctx.chat_key,
    )
    if failure is not None:
        return i18n.t("agent.forge.module_companion_cards_none", reason=_option_reason(i18n, "concepts_failed"))
    assert raw is not None  # _llm_authored returns content XOR failure

    concepts = _parse_card_concepts(raw)
    if not concepts:
        return i18n.t("agent.forge.module_companion_cards_none", reason=_option_reason(i18n, "no_concepts"))

    system = (await services.room_rulepack(ctx)).system
    names: list[str] = []
    for concept in concepts:
        try:
            sheet = await build_sheet_from_description(
                services, concept["description"], system, name=concept["name"]
            )
            sheet, _violations = validate_sheet(sheet, system, initialize_vitals=True)
            entry = await pregen_add(
                services.documents,
                ctx.chat_key,
                sheet,
                source=f"forge-module:{module_id}",
                blurb=concept["description"][:200],
            )
        except Exception:  # one bad concept must not sink the rest of the cast
            continue
        if entry is not None:
            names.append(sheet.name)
    if names:
        return i18n.t("agent.forge.module_companion_cards_ok", count=len(names), names=", ".join(names))
    return i18n.t("agent.forge.module_companion_cards_none", reason=_option_reason(i18n, "build_failed"))


async def _module_companion_pass(
    services: Services,
    ctx: AgentCtx,
    content: str,
    title: str,
    description: str,
    module_id: str,
    kinds: list[str],
    i18n,
) -> list[str]:
    """Generate the keeper-selected companion content, each through its OWN existing engine --
    no bespoke pipelines: the skill and rulepack ride this module's other two forge generators
    with a description the module's own text supplies; the cards lane is `_module_cards_pass`
    above. Each item is independent: one failing never blocks the others or the module."""
    notes: list[str] = []
    if "skills" in kinds:
        request = i18n.t(
            "agent.forge.module_companion_skill_request",
            title=title,
            description=description,
            module=content,
        )
        result = await generate_and_install_skill(services, request, chat_key=ctx.chat_key)
        notes.append(
            i18n.t("agent.forge.module_companion_skill_ok", name=result.name, id=result.skill_id)
            if result.ok
            else i18n.t("agent.forge.module_companion_failed", kind="skill", error=result.error)
        )
    if "rulepacks" in kinds:
        request = i18n.t(
            "agent.forge.module_companion_rulepack_request",
            title=title,
            description=description,
            module=content,
        )
        result = await generate_and_install_rulepack(services, request, chat_key=ctx.chat_key)
        notes.append(
            i18n.t("agent.forge.module_companion_rulepack_ok", name=result.name, id=result.skill_id)
            if result.ok
            else i18n.t("agent.forge.module_companion_failed", kind="rulepack", error=result.error)
        )
    if "cards" in kinds:
        notes.append(await _module_cards_pass(services, ctx, content, module_id, i18n))
    return notes


# ---------------------------------------------------------------------------
# Layer B.3c -- the pack-module generator: author a complete `.lwpack` content pack.
#
# Where `generate_and_install_module` (B.3b) produces a flat Markdown scenario analysed into a
# room's knowledge pool, this lane asks the LLM to author a native WORLD CARD
# (`*.lorecard.json`) and wraps it in a real `.lwpack` content pack — the engine's canonical
# full-module shape. The card carries everything a hand-authored pack module does: keeper-trust
# lorebook entries (secret-flagged), typed variable specs, a claimable pregen cast, and the
# module's prose (pitch / scenario / openings) — which lands as a keeper-only `module_brief`.
# Asset illustrations (generated alongside, like the md lane's `media` pass) ride inside the
# pack's `assets/`, so they travel WITH the module, never trapped in one room.
#
# Trust boundary is the same data-plugin posture as the other three generators: nothing is
# executed — the authored JSON is parsed by the REAL `core.lorecard` parser, the pack is built
# by `core.pack.build_pack` (which runs every declared content file through the same validators
# a hand-authored pack runs), and the room is populated through the REAL keeper-only
# `.import … world` path (`CharcardTools.import_world_card`). No `eval`, no `exec`, no bespoke
# ingestion. The one privileged write is a confined filesystem write under the user data dir.
# ---------------------------------------------------------------------------

# Pack-module id/slug and version are machine-derived (stable per content hash), not model-trusted,
# so a hand-rolled author name can never collide or smuggle path separators.
_PACK_MODULE_VERSION = "0.1.0"
_PACK_MODULE_DEFAULT_AUTHOR = "AI Forge"

# The fixed JSON contract the pack-module authoring prompt asks the LLM to emit. Field names stay
# byte-identical regardless of locale so `core.lorecard.parse_lorecard_bytes` can consume the
# result unchanged (the same discipline as `_ANALYSIS_JSON_SCHEMA` in `module_initializer`).
_PACK_MODULE_CARD_SCHEMA = """{
    "name": "module title (in the module's own language)",
    "name_en": "a short ENGLISH title of the module, for stable ids and discovery",
    "description": "one-sentence pitch",
    "scenario": "the situation at turn zero (players' starting point)",
    "opening": "the module's opening text the keeper can quote at the table",
    "alternate_openings": ["other ways to enter the scenario (optional)"],
    "tags": ["free-form keywords"],
    "worldbook": [
        {
            "content": "a lore entry the keeper uses to run the module (setting, NPC, clue, truth, or a keeper-only secret: an ending plan, an NPC knowledge boundary)",
            "keys": ["trigger keywords that pull this entry into the keeper's context"],
            "secret": true,
            "category": "lore|npc|clue|truth|secret"
        }
    ],
    "variables": [
        {
            "id": "a stable snake_case id for a module tracker (e.g. 'fear')",
            "kind": "number|text|bool|enum",
            "labels": {"en": "English display label", "zh": "Chinese display label"},
            "default": 0,
            "minimum": 0,
            "maximum": 10,
            "options": ["only for enum kind, list the allowed values"],
            "description": "what this tracker tracks"
        }
    ],
    "pregens": [
        {
            "name": "a claimable investigator",
            "concept": "one-line character concept",
            "skills": {"Spot Hidden": 60, "Fast Talk": 45, "Library Use": 50}
        }
    ],
    "items": [
        {
            "name": "an item characters can actually obtain (e.g. 'The Bronze Mirror')",
            "kind": "weapon|armor|consumable|gem|tool|quest|misc",
            "slot": "the equip slot when worn (e.g. 'weapon', 'armor', 'accessory'; leave empty for a non-equippable item)",
            "description": "short player-visible intro (what it is, how it looks)",
            "effect": "the mechanical effect (e.g. '+2 attack', 'heals 1d4', '+1 to Spot Hidden'); empty for purely narrative items",
            "lore": "background story — ONLY for notable items, else leave empty",
            "origin": "where the item comes from (optional)",
            "bonus": {"SheetCanonical": 1, "AnotherCanonical": -1},
            "quantity": 1
        }
    ]
}"""


def _spent_above_base(skills: Mapping[str, int], base_skills: Mapping[str, int]) -> int:
    """Points a skill map spends above the fresh-sheet base values."""
    return sum(value - base_skills.get(key, 0) for key, value in skills.items())


def _nominal_skill_budget(pack: Any) -> int | None:
    """The skill-point budget evaluated over the pack's DEFAULT sheet values.

    Budget formulas reference canonical attribute values (CoC: ``智力 * 2`` + the
    occupation family), which for a pregen are only known at import time, when the
    sheet's attributes are rolled. Forge normalizes against the pack's declared
    DEFAULTS instead — a deterministic, system-agnostic ceiling (CoC: 300 with all
    stats at 50) that keeps an LLM-authored cast budget-sane without knowing the
    dice. ``None`` when the pack declares no budget or no default sheet.
    """
    creation = pack.creation_constraints or {}
    budgets = creation.get("budgets") or {}
    parts: Any = None
    for rule in budgets.values():
        if isinstance(rule, dict) and isinstance(rule.get("parts"), list) and rule["parts"]:
            parts = rule["parts"]
            break
    if not isinstance(parts, list):
        return None

    sheet = CharacterSheet(name="", system=pack.system)  # seeds the pack's default attributes
    namespace = canonical_values(sheet, pack)

    def resolve(path: str) -> int:
        try:
            value = namespace.get(path, pack.defaults.get(path, 0))
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    total = 0
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("max"), list):
            best = 0
            for formula in part["max"]:
                try:
                    best = max(best, int(compile_expression(formula)(resolve)))
                except (CondExprError, TypeError, ValueError):
                    continue
            total += best
        elif isinstance(part, str):
            try:
                total += int(compile_expression(part)(resolve))
            except (CondExprError, TypeError, ValueError):
                continue
    return total


def _normalize_pregen_skills(card: dict, pack: Any) -> int:
    """Deterministically shape the LLM's ``pregens[].skills`` into rule-legal values.

    The model authors skill numbers from taste; numeric constraints are engine work
    (iron rule #1). For every pregen: keep only keys the pack resolves as skills
    (aliases become canonical), floor each at its base value, clamp to the pack's
    creation max, and — when the pack declares a skill-point budget — scale the
    points spent above base down to the nominal budget (pack defaults), preserving
    the author's relative profile. Returns how many pregens were adjusted.
    """
    spec = pack.sheet_spec
    if spec is None:
        return 0
    base_skills: dict[str, int] = {}
    for key, value in (spec.skills or {}).items():
        try:
            base_skills[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    if not base_skills:
        return 0
    creation = pack.creation_constraints or {}
    skills_rule = creation.get("skills")
    skill_max = 90
    if isinstance(skills_rule, dict) and isinstance(skills_rule.get("default"), dict):
        skill_max = int(skills_rule["default"].get("max", 90) or 90)
    budget = _nominal_skill_budget(pack)

    pregens = card.get("pregens")
    if not isinstance(pregens, list):
        return 0
    adjusted = 0
    for pregen in pregens:
        if not isinstance(pregen, dict):
            continue
        raw = pregen.get("skills")
        if not isinstance(raw, dict) or not raw:
            continue
        cleaned: dict[str, int] = {}
        for key, value in raw.items():
            canonical = pack.resolve_skill(str(key))
            if canonical is None:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            base = base_skills.get(canonical, 0)
            cleaned[canonical] = max(base, min(skill_max, number))
        if not cleaned:
            # Nothing resolved (junk keys): drop the field so no garbage reaches the sheet.
            pregen.pop("skills", None)
            continue
        if budget is not None and _spent_above_base(cleaned, base_skills) > budget:
            spent = _spent_above_base(cleaned, base_skills)
            scale = budget / spent
            for key in list(cleaned):
                base = base_skills.get(key, 0)
                cleaned[key] = base + int((cleaned[key] - base) * scale)
            # Floor-scaling drifts under the budget; trim the largest remaining
            # overage one point at a time until the spend is within budget.
            guard = 0
            while _spent_above_base(cleaned, base_skills) > budget and guard < 4096:
                guard += 1
                key = max(cleaned, key=lambda k: (cleaned[k] - base_skills.get(k, 0), cleaned[k]))
                if cleaned[key] <= base_skills.get(key, 0):
                    break
                cleaned[key] -= 1
        pregen["skills"] = cleaned
        adjusted += 1
    return adjusted


def _missing_pack_module_labels(card: dict, locale: str | None) -> list[int]:
    """Return variable indexes that lack a display label for the requested locale."""
    variables = card.get("variables")
    if not isinstance(variables, list):
        return []
    requested = (locale or "en").split("-", 1)[0].lower()
    missing: list[int] = []
    for index, variable in enumerate(variables):
        if not isinstance(variable, dict):
            continue
        labels = variable.get("labels")
        if not isinstance(labels, dict) or not isinstance(labels.get(requested), str) or not labels[requested].strip():
            missing.append(index)
    return missing


async def generate_and_install_pack_module(
    services: Services,
    ctx: AgentCtx,
    description: str,
    *,
    media: list[str] | None = None,
    companion: list[str] | None = None,
    progress: ProgressCb = None,
    auto_import: bool = True,
    extends_base: str = "",
    system: str = "",
) -> ForgeResult:
    """Ask `services.llm` to author a complete module as a native world card, wrap it in a
    `.lwpack` content pack, install the pack, and populate the CALLING room through the keeper
    world-import path.

    Returns a `ForgeResult` whose `path` is the built `.lwpack` and whose `detail` names every
    artifact (the pack install summary + the room-import summary + generated illustrations).

    ``media`` renders illustrations into the pack's OWN `assets/` (they travel with the module).
    ``companion`` additionally bundles skill / rulepack / pregen-card content into the pack under
    their `contents.*` kinds, so the `.lwpack` is a COMPLETE module (lorebook + trackers + cast +
    optional assets + companion systems), exactly like a hand-authored pack. Audio is
    deliberately absent (keeper veto), same as the md lane.

    ``extends_base`` makes a generated companion rulepack a PATCH on that base
    system. ``system`` instead declares that the module DIRECTLY uses a
    built-in rule system: the world card carries ``system: <id>`` and, on import, the room pins
    that system WITHOUT shipping or generating any rulepack. ``system`` and ``extends_base``
    are mutually exclusive; ``system`` takes precedence and skips rulepack generation."""
    user_dir = _USER_MODULE_DIR
    if user_dir is None:
        return ForgeResult(False, "", "", "", "no_data_dir")

    i18n = services.i18n.with_locale(ctx.locale)
    media_kinds = _normalize_option_ids(media, MEDIA_OPTION_IDS)
    companion_kinds = _normalize_option_ids(companion, COMPANION_OPTION_IDS)
    logger.info("[pack-forge] start room=%s media=%s companion=%s", ctx.chat_key, media_kinds, companion_kinds)
    await _emit(progress, "authoring")

    # Author the world card JSON. A live provider can transiently time out or return empty for
    # this long structured prompt (same failure mode as the companion skill/rulepack lanes), so
    # retry once before failing the whole module — a provider hiccup shouldn't sink a complete
    # .lwpack the way it used to.
    raw, failure = await _llm_authored_retry(services, _build_pack_module_messages(services, description, ctx.locale), chat_key=ctx.chat_key)
    if failure is not None:
        logger.warning("[pack-forge] world-card authoring failed: %s", failure.error)
        return failure
    assert raw is not None
    card_text = _extract_json_object(raw)
    if not card_text:
        return ForgeResult(False, "", "", "", "invalid_pack_module:no JSON object")

    # `format`/`format_version` are MACHINE contract, not author content — inject them so the
    # model never needs to remember them (a real LLM reliably omits them), and the native
    # parser accepts the bundle. The author-provided name/pitch/lore/variables ride through.
    card_text.setdefault("format", "loreweaver.card")
    card_text.setdefault("format_version", 1)
    if system:
        # The module DIRECTLY uses a built-in rule system (no rulepack shipped/generated):
        # record it in the card so the room pins it on import. Mutually exclusive with
        # `extends_base` — a card that declares a system is not also generating a patch.
        card_text["system"] = system

    missing_labels = _missing_pack_module_labels(card_text, ctx.locale)
    if missing_labels:
        requested = (ctx.locale or "en").split("-", 1)[0].lower()
        indexes = ", ".join(str(index) for index in missing_labels)
        return ForgeResult(
            False,
            "",
            "",
            "",
            f"invalid_pack_module: variables missing {requested} labels at indexes {indexes}",
        )

    # Validate through the REAL parser before anything is written.
    try:
        lorecard = parse_lorecard_bytes(json.dumps(card_text, ensure_ascii=False).encode("utf-8"), "pack-module")
    except Exception as exc:  # noqa: BLE001 — a malformed authoring reply must degrade cleanly
        return ForgeResult(False, "", "", "", f"invalid_pack_module: {exc}")
    logger.info("[pack-forge] world card parsed: %r (%d lore, %d vars, %d pregens)",
                lorecard.card.name, len(lorecard.card.character_book), len(lorecard.variable_specs), len(lorecard.pregens))
    await _emit(progress, "world_card")

    # Deterministic skills sanity pass: the model authors `pregens[].skills` from taste; numeric
    # constraints (base floors, creation caps, the skill-point budget) are engine work — iron
    # rule #1. Normalize against the pack the module will actually land on.
    skills_pack = None
    if system:
        try:
            skills_pack = rulepacks.load_rulepack(system)
        except Exception:
            skills_pack = None
    if skills_pack is None:
        try:
            skills_pack = await services.room_rulepack(ctx)
        except Exception:
            skills_pack = None
    skills_adjusted = _normalize_pregen_skills(card_text, skills_pack) if skills_pack is not None else 0
    if skills_adjusted:
        logger.info("[pack-forge] normalized %d pregen skill profile(s)", skills_adjusted)

    name = lorecard.card.name or description
    # A CJK `name` has no ASCII to slug, so the model also supplies `name_en` — a short English
    # title the id can be built from (a Chinese-only fallback used to degrade to whatever ASCII
    # happened to survive the keeper's description, e.g. a module whose id became "coc").
    module_id = (
        _slugify(card_text.get("name_en") or "")
        or _slugify(lorecard.card.name)
        or _slugify(description)
    )
    if not module_id:
        digest = hashlib.sha256(json.dumps(card_text).encode("utf-8")).hexdigest()[:8]
        module_id = f"module-{digest}"

    # Assemble a pack source tree under the user module dir.
    data_dir = Path(services.settings.data_dir)
    pack_id = module_id
    source = user_dir / f"{pack_id}.pack-src"
    card_rel = f"cards/{pack_id}.lorecard.json"
    logger.info("[pack-forge] assembling pack source at %s", source)
    try:
        source.mkdir(parents=True, exist_ok=True)
        (source / "cards").mkdir(parents=True, exist_ok=True)
        (source / "assets").mkdir(parents=True, exist_ok=True)
        _safe_write(source / card_rel, json.dumps(card_text, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        return ForgeResult(False, "", "", "", f"write_failed: {exc}")

    # Optional illustrations: render into the pack's OWN assets/ (travel with the module).
    media_index: list[dict[str, str]] = []
    if media_kinds:
        assets_dir = source / "assets"
        logger.info("[pack-forge] media pass: %s", media_kinds)
        await _emit(progress, "media")
        try:
            pregen_names = [
                str(p.get("name"))
                for p in (card_text.get("pregens") or [])
                if isinstance(p, dict) and p.get("name")
            ]
            # Real scene/NPC/item names from the world card, so `npcs`/`scenes`/`items`
            # shots use the ACTUAL cast/places/objects and can bind back to their entries.
            subject_names = _worldbook_subject_names(card_text)
            shots_raw, shots_failure = await _llm_authored(
                services,
                _build_module_media_messages(
                    services,
                    description,
                    media_kinds,
                    i18n,
                    pregen_names=pregen_names or None,
                    subject_names=subject_names or None,
                ),
                chat_key=ctx.chat_key,
            )
        except Exception:  # noqa: BLE001 — media is never load-bearing
            shots_raw, shots_failure = None, ForgeResult(False, "", "", "", "shot_list_failed")
        if shots_failure is None and shots_raw:
            imagegen = await services.imagegen_for_room(ctx.chat_key)
            if imagegen is not None:
                shots = _parse_shot_list(shots_raw, media_kinds)
                total = len(shots)
                done = 0
                sem = asyncio.Semaphore(_MEDIA_CONCURRENCY)
                media_settings = services.settings.tui
                media_store = MediaStore(
                    services.store,
                    services.settings.data_dir,
                    max_file_bytes=media_settings.media_max_file_bytes,
                    room_quota_bytes=media_settings.media_room_quota_bytes,
                    allowed_mimes=ALLOWED_IMAGE_MIMES,
                )

                async def _render_one(index: int, shot: Any) -> dict[str, str] | None:
                    nonlocal done
                    logger.info("[pack-forge] rendering %s #%d (%s)", shot.kind, index, shot.subject)
                    data: bytes | None = None
                    mime = ""
                    async with sem:
                        try:
                            data, mime = await _imagegen_generate_retry(
                                imagegen, shot.prompt, size=services.settings.imagegen.size
                            )
                        except Exception as exc:  # noqa: BLE001 — one bad shot never fails the pass
                            logger.warning("[pack-forge] imagegen failed for %s: %s", shot.subject, exc)
                    done += 1
                    await _emit(progress, "media", detail=f"{done}/{total}")
                    if data is None:
                        return None
                    asset_name = f"module-{pack_id}-{shot.kind}-{index}{_IMAGE_MIME_EXTS.get(mime, '.png')}"
                    try:
                        _safe_write(assets_dir / asset_name, data)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[pack-forge] asset write failed %s: %s", asset_name, exc)
                        return None
                    # Register in the room's media store so `.image` can reuse this illustration
                    # as a REFERENCE (content-addressed: the pack-import path re-registers the
                    # same bytes and dedupes). Best-effort — a quota/mime rejection drops only
                    # this image from the reference pool, never the forge.
                    try:
                        record = await media_store.register_blob(
                            room=ctx.chat_key,
                            data=data,
                            mime=mime,
                            name=asset_name,
                            uploader=ctx.uid(),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[pack-forge] media register failed %s: %s", asset_name, exc)
                        return None
                    return {"kind": shot.kind, "subject": shot.subject, "name": asset_name, "hash": record.hash}

                # Render at most `_MEDIA_CONCURRENCY` shots in flight, with per-shot retry.
                rendered = await asyncio.gather(*(_render_one(i, s) for i, s in enumerate(shots, 1)))
                media_index = [r for r in rendered if r]
                if media_index:
                    # Persist shot→image provenance so `.image <kind> <subject>` can reuse the
                    # room's illustration as a generation reference (same contract as the
                    # module-creation path's `_append_module_media_index`).
                    await _append_module_media_index(services, ctx.chat_key, media_index)
                logger.info("[pack-forge] media pass done: %d images", len(media_index))
            else:
                logger.warning("[pack-forge] no imagegen provider for this room; media skipped")

    # Bind generated images to the world card. Investigator portraits stamp onto the matching
    # pregen's `avatar` (a player claiming the pregen inherits the portrait); npc/scene/item
    # shots stamp onto their matching worldbook entry's `image` (so the illustrations are no
    # longer orphans — they ride the card, survive re-import, and appear beside the entry).
    # One rewrite covers both bindings.
    card_rewritten = False
    pregen_portraits = {
        str(shot.get("subject")): str(shot.get("name")) for shot in media_index if shot.get("kind") == "pregens"
    }
    if pregen_portraits:
        for pregen in card_text.get("pregens") or []:
            if isinstance(pregen, dict) and pregen.get("name") in pregen_portraits:
                pregen["avatar"] = pregen_portraits[pregen["name"]]
                card_rewritten = True
    if _bind_worldbook_images(card_text, media_index):
        card_rewritten = True
    if card_rewritten:
        try:
            _safe_write(source / card_rel, json.dumps(card_text, ensure_ascii=False, indent=2) + "\n")
        except OSError as exc:
            return ForgeResult(False, "", "", "", f"write_failed: {exc}")

    # Build the .lwpack.
    # Optional companion content: bundle skills / rulepacks INTO the pack (a complete .lwpack
    # carries them like a hand-authored pack). The world card already ships the pregen cast via
    # its `pregens:` — so the "cards" companion option is satisfied by the card itself, and only
    # skills/rulepacks need generating into the pack source tree.
    packed_skills: list[str] = []
    packed_rulepacks: list[str] = []
    companion_notes: list[str] = []
    for kind in companion_kinds:
        if kind == "skills":
            await _emit(progress, "skill")
            logger.info("[pack-forge] generating companion skill")
            skill_dir, note = await _pack_skill(services, ctx, description, source, i18n)
            if skill_dir is not None:
                packed_skills.append(skill_dir)
            # A failure note is still surfaced so the keeper knows the bundle is missing it.
            companion_notes.append(note)
        elif kind == "rulepacks":
            if system:
                # `system` (directly use a built-in rule system, declared on the card) and
                # `extends_base` (generate a patch) are mutually exclusive. When a system is
                # declared, do NOT also generate a rulepack — the module runs that system as-is.
                logger.info("[pack-forge] system=%s declared; skipping rulepack generation", system)
                companion_notes.append(i18n.t("agent.forge.pack_module_system_declared", system=system))
                continue
            await _emit(progress, "rulepack")
            logger.info("[pack-forge] generating companion rulepack (extends=%s)", extends_base or "none")
            rp_path, note = await _pack_rulepack(services, ctx, description, source, i18n, extends_base=extends_base)
            if rp_path is not None:
                packed_rulepacks.append(rp_path)
            # Surface a rulepack generation failure explicitly — never silently ship a "complete"
            # pack that is missing its rule system.
            companion_notes.append(note)
    logger.info("[pack-forge] companion done: skills=%s rulepacks=%s", packed_skills, packed_rulepacks)

    # Write the manifest LAST, once every bundled content file is in place.
    asset_paths = [f"assets/{f.name}" for f in sorted((source / "assets").iterdir()) if f.is_file()] if (source / "assets").is_dir() else []
    asset_titles = {
        f"assets/{entry['name']}": entry["subject"]
        for entry in media_index
        if entry.get("name") and entry.get("subject")
    }
    try:
        _safe_write(
            source / "pack.yaml",
            _pack_module_manifest(
                pack_id,
                name,
                skills=packed_skills,
                rulepacks=packed_rulepacks,
                assets=asset_paths,
                asset_titles=asset_titles,
            ),
        )
    except OSError as exc:
        return ForgeResult(False, "", "", "", f"write_failed: {exc}")

    out_path = user_dir / f"{pack_id}-{_PACK_MODULE_VERSION}.lwpack"
    logger.info("[pack-forge] building .lwpack -> %s", out_path)
    await _emit(progress, "building")
    try:
        built = build_pack(source, out_path=out_path)
    except Exception as exc:  # noqa: BLE001 — validation failure is a clean rejection
        logger.warning("[pack-forge] build_pack failed: %s", exc)
        return ForgeResult(False, "", "", "", f"invalid_pack_module: {exc}")

    # Install the pack into `data/packs/` so it appears in the module library and its detail is
    # resolvable — this is REGISTRATION, not a room binding. Room binding is `import_world_card`
    # below, which `auto_import=False` skips (the keeper imports into a room explicitly).
    from gateway.pack_install import install_pack_here

    logger.info("[pack-forge] installing pack into %s", data_dir)
    await _emit(progress, "installing")
    install_report = install_pack_here(data_dir, built.path)

    parts = [i18n.t("agent.forge.pack_module_installed", name=name, path=str(built.path))]
    if skills_adjusted:
        parts.append(i18n.t("agent.forge.pack_module_skills_normalized", count=skills_adjusted))
    if auto_import:
        # Populate the calling room through the REAL keeper world-import path.
        logger.info("[pack-forge] importing world card into room %s", ctx.chat_key)
        await _emit(progress, "importing")
        installed_home = install_report.pack_dir
        if installed_home is None:
            return ForgeResult(
                False, pack_id, name, str(built.path), "", detail="installed_pack_missing"
            )
        install_ctx = replace(ctx, fs=LocalFs(user_dir, extra_bases=(data_dir,)))
        card_host = installed_home / card_rel
        room_line = await CharcardTools(services).import_world_card(install_ctx, file_path=str(card_host))
        logger.info("[pack-forge] room import done: %s", room_line[:120])
        parts.append(i18n.t("agent.forge.pack_module_room", detail=room_line))
    else:
        logger.info("[pack-forge] generated without room import (auto_import=False)")
        parts.append(i18n.t("agent.forge.pack_module_generated_not_imported", name=name, path=str(built.path)))
    if media_index:
        parts.append(i18n.t("agent.forge.pack_module_media", count=len(media_index)))
    parts.extend(companion_notes)
    return ForgeResult(True, pack_id, name, str(built.path), "", detail="\n".join(parts))


def _build_pack_module_messages(services: Services, description: str, locale: str | None = None) -> list[dict]:
    """The two-message pack-module authoring prompt: localized framing + the fixed JSON schema."""
    i18n = services.i18n.with_locale(locale) if locale else services.i18n
    system_prompt = "\n\n".join(
        (
            i18n.t("agent.forge.pack_module_system_prompt"),
            i18n.t("agent.forge.pack_module_language_requirement"),
        )
    )
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": i18n.t(
                "agent.forge.pack_module_request",
                description=description,
                schema=_PACK_MODULE_CARD_SCHEMA,
            ),
        },
    ]


def _extract_json_object(raw: str) -> dict | None:
    """Best-effort JSON-object extraction from a raw LLM reply (tolerates code fences)."""
    text = _strip_code_fence(raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _pack_module_manifest(
    pack_id: str,
    name: str,
    *,
    skills: list[str] | None = None,
    rulepacks: list[str] | None = None,
    assets: list[str] | None = None,
    asset_titles: dict[str, str] | None = None,
) -> str:
    """The author-side `pack.yaml` for a generated module pack (id/version/authors/license +
    its world card + bundled companion skill/rulepack + asset illustrations). `build_pack` stamps
    assets/trust/files from the real files. Serialized as YAML so the machine-generated manifest
    needs no hand-written natural-language literal."""
    contents: dict[str, object] = {"cards": [f"cards/{pack_id}.lorecard.json"]}
    if skills:
        contents["skills"] = skills
    if rulepacks:
        contents["rulepacks"] = rulepacks
    manifest: dict[str, object] = {
        "id": pack_id,
        "version": _PACK_MODULE_VERSION,
        "authors": [_PACK_MODULE_DEFAULT_AUTHOR],
        "license": "CC-BY-4.0",
        "name": {"en": name},
        "description": {"en": "AI-authored module generated by the pack forge."},  # i18n-exempt  pack metadata, not UI text
        "contents": contents,
    }
    if assets:
        titles = asset_titles or {}
        manifest["assets"] = [
            {"path": path, **({"title": titles[path]} if titles.get(path) else {})}
            for path in assets
        ]
    return yaml.safe_dump(
        manifest,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
    )


def _safe_write(path: Path, data: str | bytes) -> None:
    """Confined write inside an already-created source tree (private mode)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_private(path, data)


async def _pack_skill(
    services: Services,
    ctx: AgentCtx,
    description: str,
    source: Path,
    i18n,
) -> tuple[str | None, str]:
    """Generate a KP skill and write it into the pack source tree as `skills/<id>/SKILL.md`.
    Returns `(pack-relative skill dir, localized note)` or `(None, note)` when generation fails
    (best-effort: a bad skill never fails the pack)."""
    request = i18n.t(
        "agent.forge.module_companion_skill_request",
        title=description,
        description=description,
        module=description,
    )
    content, failure = await _llm_authored_retry(services, _build_messages(services, request), chat_key=ctx.chat_key)
    if failure is not None:
        logger.warning("[pack-forge] skill LLM failed: %s", failure.error)
        return None, i18n.t("agent.forge.module_companion_failed", kind="skill", error=failure.error)
    assert content is not None
    content = _strip_code_fence(content)
    # Tolerate an LLM that opens frontmatter with `---` but forgets the closing fence — a common
    # authoring slip — by appending it, then validating through the SAME strict parser below.
    # Nothing weaker passes: a repaired body still has to parse as a real Skill.
    content = _repair_skill_frontmatter(content)
    try:
        probe = skills.parse_skill_text(_PROBE_ID, content)
    except Exception as exc:
        return None, i18n.t("agent.forge.module_companion_failed", kind="skill", error=str(exc))
    skill_id = _slugify(probe.name if probe.name and probe.name != _PROBE_ID else description)
    if not skill_id:
        skill_id = f"skill-{hashlib.sha256(content.encode('utf-8')).hexdigest()[:8]}"
    try:
        skills.parse_skill_text(skill_id, content)  # authoritative validation
    except Exception as exc:
        return None, i18n.t("agent.forge.module_companion_failed", kind="skill", error=str(exc))
    try:
        _safe_write(source / f"skills/{skill_id}/SKILL.md", content)
    except OSError as exc:
        return None, i18n.t("agent.forge.module_companion_failed", kind="skill", error=str(exc))
    return f"skills/{skill_id}", i18n.t("agent.forge.pack_module_skill", name=probe.name or skill_id)


async def _pack_rulepack(
    services: Services,
    ctx: AgentCtx,
    description: str,
    source: Path,
    i18n,
    *,
    extends_base: str = "",
) -> tuple[str | None, str]:
    """Generate a rulepack and write it into the pack source tree as `rulepacks/<id>.yaml`.
    Returns `(pack-relative rulepack path, localized note)` or `(None, note)` on failure.
    ``extends_base`` makes the rulepack a PATCH on that base system."""
    request = i18n.t(
        "agent.forge.module_companion_rulepack_request",
        title=description,
        description=description,
        module=description,
    )
    content, failure = await _llm_authored_retry(
        services, _build_rulepack_messages(services, request, extends_base=extends_base), chat_key=ctx.chat_key
    )
    if failure is not None:
        logger.warning("[pack-forge] rulepack LLM failed: %s", failure.error)
        return None, i18n.t("agent.forge.module_companion_failed", kind="rulepack", error=failure.error)
    assert content is not None
    content = _strip_code_fence(content)
    logger.info("[pack-forge] rulepack raw length=%d bytes", len(content.encode("utf-8")))
    try:
        probe = rulepacks.parse_rulepack_text(_PROBE_ID, content)
    except Exception as exc:
        logger.warning("[pack-forge] rulepack probe parse failed: %s", exc)
        return None, i18n.t("agent.forge.module_companion_failed", kind="rulepack", error=str(exc))
    rp_id = _slugify(probe.names[0] if probe.names else description)
    if not rp_id:
        rp_id = f"pack-{hashlib.sha256(content.encode('utf-8')).hexdigest()[:8]}"
    try:
        rulepacks.parse_rulepack_text(rp_id, content)  # authoritative validation
    except Exception as exc:
        logger.warning("[pack-forge] rulepack validation failed: %s", exc)
        return None, i18n.t("agent.forge.module_companion_failed", kind="rulepack", error=str(exc))
    try:
        _safe_write(source / f"rulepacks/{rp_id}.yaml", content)
    except OSError as exc:
        logger.warning("[pack-forge] rulepack write failed: %s", exc)
        return None, i18n.t("agent.forge.module_companion_failed", kind="rulepack", error=str(exc))
    return f"rulepacks/{rp_id}.yaml", i18n.t("agent.forge.pack_module_rulepack", name=probe.names[0] if probe.names else rp_id)


# --- Room lifecycle (M23 WS1) -----------------------------------------------
ROOM_FACETS = (
    RoomStateFacet(
        name="forge_modules",
        owner="agent.forge",
        reset_scope="all",
        # Which generated module this room installed, and who owns each generated id.
        state_keys=frozenset({"forge_module_last", MODULE_MEDIA_INDEX_KEY}),
        state_prefixes=frozenset({"forge_module_owner."}),
        storages=frozenset({STORAGE_ROOM_STATE}),
    ),
)
