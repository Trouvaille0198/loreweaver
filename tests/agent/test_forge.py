"""Tests for agent.forge: the Layer B.3a skill-generation engine (`docs/plugins.md` "Layer B").

Covers: (a) happy path -- a valid LLM-generated SKILL.md is written under a tmp `_USER_SKILL_DIR`
and immediately discoverable via `core.skills.load_skill` after the engine's own
`reload_skills()` call; (b) invalid output (no frontmatter fences, or frontmatter that isn't a
YAML mapping) is rejected with `ok=False` and NOTHING written; (c) security -- a name that would
naively slugify to a path-escaping id is sanitized to a safe id (never smuggling a path separator
through) or rejected, a generated id colliding with a BUILT-IN skill id (`mature-mode`) is
rejected before any write, and `_confined_target` independently rejects a path-escaping id
outright (defense in depth, tested directly rather than only through the sanitizer); (d) with no
`_USER_SKILL_DIR` configured at all, generation fails cleanly instead of raising.

Every test that swaps `core.skills._USER_SKILL_DIR` restores it and clears the `@cache`d discovery
registry in a `finally` block, mirroring `tests/core/test_skills.py`'s convention -- never leaking
a tmp path into another test's (or the real `skills/`) discovery.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import core.skills as skills_module
from agent.forge import (
    _MAX_FORGE_CONTENT_BYTES,
    _confined_target,
    _should_retry_imagegen,
    _slugify,
    generate_and_install_skill,
)
from agent.services import build_services
from infra.config import Settings
from infra.embeddings import FakeEmbeddings
from infra.imagegen import ImageGenError
from infra.llm import FakeLLM, assistant_text

# Wall-clock bound for rejecting an alias-bomb/oversized generated SKILL.md (see the two tests
# near the bottom of this file): both rejections happen BEFORE any YAML parse call, so they must
# stay fast regardless of how deep/large the (rejected) content is.
_FAST_REJECTION_BOUND_SECONDS = 1.0


def _alias_bomb_skill_md(levels: int = 6, branch: int = 10) -> str:
    """A "billion laughs"-style YAML alias bomb assigned to frontmatter `name:`, wrapped as a
    full SKILL.md -- the shape `generate_and_install_skill` would receive as a malicious/runaway
    LLM response."""
    lines = ["a: &a [x,x,x,x,x,x,x,x,x,x]"]
    prev = "a"
    for i in range(1, levels):
        current = chr(ord("a") + i)
        refs = ",".join(f"*{prev}" for _ in range(branch))
        lines.append(f"{current}: &{current} [{refs}]")
        prev = current
    lines.append(f"name: *{prev}")
    frontmatter = "\n".join(lines)
    return f"---\n{frontmatter}\n---\n\n# Body\n"

VALID_SKILL_MD = """---
name: Grim Survival Horror
description: >
  Enable for a campaign about grinding, resource-scarce survival horror: supplies run out,
  wounds linger, and every choice costs something.
allowed-tools: []
metadata:
  scope: room
  content-rating: mature
---

# Grim survival horror

Track scarcity relentlessly: ammunition, food, and light sources are real, finite resources --
say so plainly when a character is down to their last of something.
"""

NO_FRONTMATTER = "Just a plain markdown document with no frontmatter fences at all.\n"

NOT_A_MAPPING = """---
- just
- a
- list
---

# Body
"""


def _services(content: str):
    return build_services(
        Settings(locale="en"),
        llm=FakeLLM(script=[assistant_text(content)]),
        embeddings=FakeEmbeddings(8),
    )


# ---------------------------------------------------------------------------
# (a) Happy path.
# ---------------------------------------------------------------------------


async def test_happy_path_generates_validates_writes_and_is_discoverable(tmp_path: Path) -> None:
    services = _services(VALID_SKILL_MD)

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        result = await generate_and_install_skill(services, "a grim survival horror campaign")

        assert result.ok, result.error
        assert result.skill_id == "grim-survival-horror"
        assert result.name == "Grim Survival Horror"
        assert result.path == str(tmp_path / "grim-survival-horror" / "SKILL.md")
        assert Path(result.path).is_file()

        loaded = skills_module.load_skill("grim-survival-horror")
        assert loaded is not None
        assert loaded.name == "Grim Survival Horror"
        assert loaded.content_rating == "mature"
        assert "resource-scarce" in loaded.description
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


async def test_cjk_named_skill_installs_with_stable_content_hash_id(tmp_path: Path) -> None:
    """An all-CJK generated name has no ASCII slug: the skill installs under a stable
    `skill-<hash>` fallback id (the module generator's `module-<digest>` pattern) instead of
    being rejected -- this is what makes zh-locale generation installable at all."""
    cjk_skill_md = VALID_SKILL_MD.replace("Grim Survival Horror", "消失的图书管理员").replace(
        "Grim survival horror", "消失的图书管理员"
    )
    services = _services(cjk_skill_md)

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        result = await generate_and_install_skill(services, "一个上海图书馆密室谜案的技能")

        assert result.ok, result.error
        assert result.skill_id.startswith("skill-") and len(result.skill_id) == len("skill-") + 8
        assert result.name == "消失的图书管理员"
        assert Path(result.path).is_file()
        loaded = skills_module.load_skill(result.skill_id)
        assert loaded is not None
        assert loaded.name == "消失的图书管理员"
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


# ---------------------------------------------------------------------------
# (b) Invalid output -- rejected, nothing written.
# ---------------------------------------------------------------------------


async def test_invalid_output_no_frontmatter_writes_nothing(tmp_path: Path) -> None:
    services = _services(NO_FRONTMATTER)

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        result = await generate_and_install_skill(services, "anything")

        assert not result.ok
        assert result.error.startswith("invalid_skill")
        assert list(tmp_path.iterdir()) == []
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


async def test_invalid_output_frontmatter_not_a_mapping_writes_nothing(tmp_path: Path) -> None:
    services = _services(NOT_A_MAPPING)

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        result = await generate_and_install_skill(services, "anything")

        assert not result.ok
        assert list(tmp_path.iterdir()) == []
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


async def test_empty_llm_response_is_rejected(tmp_path: Path) -> None:
    services = _services("   ")

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        result = await generate_and_install_skill(services, "anything")

        assert not result.ok
        assert result.error == "empty_response"
        assert list(tmp_path.iterdir()) == []
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


class _RaisingLLM:
    """An LLM whose chat() raises — models a real backend failure (timeout / rate-limit / 401)."""

    async def chat(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("backend exploded (e.g. rate limit)")


async def test_llm_failure_is_a_clean_forge_result_not_an_uncaught_exception(tmp_path: Path) -> None:
    """A backend LLM failure during authoring must become a clean ForgeResult(ok=False), NOT an
    uncaught exception — otherwise it surfaces as a generic `error` frame and hangs the client's
    generate spinner. Nothing is written on failure."""
    services = build_services(Settings(locale="en"), llm=_RaisingLLM(), embeddings=FakeEmbeddings(8))

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        result = await generate_and_install_skill(services, "anything")

        assert not result.ok
        assert result.error.startswith("llm_failed")
        assert list(tmp_path.iterdir()) == []
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


# ---------------------------------------------------------------------------
# (c) Security: id sanitization, built-in collision rejection, path confinement.
# ---------------------------------------------------------------------------


async def test_traversal_name_is_sanitized_to_a_safe_id_never_a_path(tmp_path: Path) -> None:
    traversal_skill = VALID_SKILL_MD.replace("Grim Survival Horror", "../../etc/passwd")
    services = _services(traversal_skill)

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        result = await generate_and_install_skill(services, "anything")

        if result.ok:
            # Sanitized to a safe id: no path separators/traversal survived, and the write
            # landed strictly inside the user skill dir.
            assert "/" not in result.skill_id
            assert ".." not in result.skill_id
            written = Path(result.path).resolve()
            assert written.is_relative_to(tmp_path.resolve())
        else:
            # Rejecting outright is also an acceptable outcome -- but it must be a clean
            # rejection (bad_id/invalid), never the path-confinement guard tripping, which
            # would mean sanitization let something dangerous through this far.
            assert not result.error.startswith("path_escape")
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


async def test_slugified_traversal_name_contains_no_path_characters() -> None:
    assert _slugify("../../etc/passwd") == "etcpasswd"


async def test_generated_id_colliding_with_a_built_in_is_rejected(tmp_path: Path) -> None:
    collision_skill = VALID_SKILL_MD.replace("Grim Survival Horror", "Romance Relationships")
    services = _services(collision_skill)

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        result = await generate_and_install_skill(services, "anything")

        assert not result.ok
        assert result.error.startswith("bad_id")
        assert "romance-relationships" in result.error
        assert list(tmp_path.iterdir()) == []  # nothing written
        # The real built-in must still be exactly what resolves -- unshadowed.
        loaded = skills_module.load_skill("romance-relationships")
        assert loaded is not None
        assert loaded.content_rating == "mature"
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


def test_confined_target_rejects_a_path_escaping_id_directly(tmp_path: Path) -> None:
    """Direct unit test of the path-confinement guard itself (defense in depth): even if a
    path-escaping id somehow bypassed `_slugify`, `_confined_target` must still refuse it."""
    with pytest.raises(ValueError):
        _confined_target(tmp_path, "../../etc/passwd")


def test_confined_target_accepts_a_safe_id(tmp_path: Path) -> None:
    target = _confined_target(tmp_path, "a-safe-id")
    assert target == (tmp_path / "a-safe-id" / "SKILL.md").resolve()


def test_should_retry_imagegen_treats_timeout_as_transient() -> None:
    """A provider timeout is the most transient failure there is — it must be retried, not
    treated as a permanent rejection that drops the illustration on the first attempt."""
    assert _should_retry_imagegen(ImageGenError("imagegen_timeout"))
    # HTTP rate-limit / 5xx remain retryable.
    for status in ("429", "500", "502", "503", "504"):
        assert _should_retry_imagegen(ImageGenError("imagegen_http_error", status))
    # Permanent rejections and bad payloads are not retried.
    assert not _should_retry_imagegen(ImageGenError("imagegen_http_error", "400"))
    assert not _should_retry_imagegen(ImageGenError("imagegen_http_error", "403"))
    assert not _should_retry_imagegen(ImageGenError("imagegen_bad_response"))
    assert not _should_retry_imagegen(ImageGenError("imagegen_refused"))
    # A non-provider exception is retried (anything unexpected gets one more chance).
    assert _should_retry_imagegen(ValueError("boom"))


@pytest.mark.parametrize("bad_id", [".", "..", "", "a/b", "a\\b", "foo/../bar", "-leading-hyphen"])
def test_confined_target_rejects_degenerate_ids_independently(tmp_path: Path, bad_id: str) -> None:
    """The guard is self-standing: `.`/`..`/empty/path-separator ids are refused directly (not
    only via `_slugify`), so the confinement invariant holds even if sanitization regressed."""
    with pytest.raises(ValueError):
        _confined_target(tmp_path, bad_id)


async def test_pathologically_long_name_is_capped_and_installs(tmp_path: Path) -> None:
    """A very long generated name must not blow up the filesystem NAME_MAX at write time: the id
    is capped, so generation succeeds and writes cleanly instead of raising an unhandled OSError."""
    long_name = ("Endless " * 60).strip()  # slugifies to a ~480-char token before capping
    services = _services(VALID_SKILL_MD.replace("Grim Survival Horror", long_name))

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        result = await generate_and_install_skill(services, "anything")

        assert result.ok, result.error
        assert 0 < len(result.skill_id) <= 64
        assert Path(result.path).is_file()  # wrote cleanly, no OSError
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


async def test_second_skill_with_same_id_is_uniquified_not_clobbered(tmp_path: Path) -> None:
    """Installing a second skill whose name slugs to an existing user id must NOT overwrite the
    first — it uniquifies (base-2), leaving the original file intact."""
    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        first = await generate_and_install_skill(_services(VALID_SKILL_MD), "first")
        assert first.ok
        assert first.skill_id == "grim-survival-horror"

        second = await generate_and_install_skill(_services(VALID_SKILL_MD), "second")
        assert second.ok
        assert second.skill_id == "grim-survival-horror-2"

        # Both survive; the first was never clobbered.
        assert (tmp_path / "grim-survival-horror" / "SKILL.md").is_file()
        assert (tmp_path / "grim-survival-horror-2" / "SKILL.md").is_file()
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


# ---------------------------------------------------------------------------
# (d) No data dir configured at all.
# ---------------------------------------------------------------------------


async def test_no_data_dir_configured_fails_cleanly() -> None:
    services = _services(VALID_SKILL_MD)
    assert skills_module._USER_SKILL_DIR is None  # the default in every test unless opted in

    result = await generate_and_install_skill(services, "anything")

    assert not result.ok
    assert result.error == "no_data_dir"
    assert result.skill_id == ""
    assert result.path == ""


# ---------------------------------------------------------------------------
# (e) Alias-bomb / oversized LLM output -- regression tests for the CPU/memory-exhaustion finding.
# ---------------------------------------------------------------------------


async def test_alias_bomb_generated_skill_is_rejected_fast_and_writes_nothing(tmp_path: Path) -> None:
    """A malicious/runaway LLM response whose frontmatter `name:` aliases a deeply-nested anchor
    chain must be rejected -- via `core.yaml_safety.NoAliasSafeLoader`, reached through
    `core.skills.parse_skill_text` -- fast, not parsed and then blown up by `_build_skill`'s
    `str(frontmatter.get("name") ...)`. Before the fix, this would neither fail nor stay within
    the time bound (plain `yaml.safe_load` resolves the alias, and `str()` on it explodes)."""
    services = _services(_alias_bomb_skill_md())

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        start = time.monotonic()
        result = await generate_and_install_skill(services, "anything")
        elapsed = time.monotonic() - start

        assert not result.ok
        assert result.error.startswith("invalid_skill")
        assert list(tmp_path.iterdir()) == []
        assert elapsed < _FAST_REJECTION_BOUND_SECONDS, (
            f"alias-bomb skill generation took {elapsed:.3f}s (bound {_FAST_REJECTION_BOUND_SECONDS}s)"
        )
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


async def test_oversized_generated_skill_content_is_refused_before_parsing(tmp_path: Path) -> None:
    """LLM-authored SKILL.md content over `_MAX_FORGE_CONTENT_BYTES` must be refused BEFORE any
    YAML parse call -- a hard byte cap independent of the alias-bomb rejection, guarding against a
    merely large (non-aliased) document costing real CPU/memory on the shared event loop."""
    oversized = VALID_SKILL_MD + ("x" * (_MAX_FORGE_CONTENT_BYTES + 1))
    services = _services(oversized)

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        start = time.monotonic()
        result = await generate_and_install_skill(services, "anything")
        elapsed = time.monotonic() - start

        assert not result.ok
        assert result.error.startswith("invalid_skill")
        assert str(_MAX_FORGE_CONTENT_BYTES) in result.error
        assert list(tmp_path.iterdir()) == []
        assert elapsed < _FAST_REJECTION_BOUND_SECONDS, (
            f"oversized skill rejection took {elapsed:.3f}s (bound {_FAST_REJECTION_BOUND_SECONDS}s)"
        )
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


async def test_content_at_exactly_the_cap_is_not_rejected_for_size(tmp_path: Path) -> None:
    """The cap is `> _MAX_FORGE_CONTENT_BYTES` (strictly over), so content sized exactly at the
    cap must proceed to normal validation rather than being refused for size."""
    padding_needed = _MAX_FORGE_CONTENT_BYTES - len(VALID_SKILL_MD.encode("utf-8"))
    assert padding_needed > 0
    # Pad inside the markdown body (after the closing frontmatter fence) so the frontmatter
    # itself -- and thus the derived name/id -- is untouched.
    exactly_at_cap = VALID_SKILL_MD + ("x" * padding_needed)
    assert len(exactly_at_cap.encode("utf-8")) == _MAX_FORGE_CONTENT_BYTES
    services = _services(exactly_at_cap)

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        result = await generate_and_install_skill(services, "anything")

        assert result.ok, result.error
        assert result.skill_id == "grim-survival-horror"
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()


async def test_code_fenced_skill_md_is_unwrapped_and_installed(tmp_path: Path) -> None:
    """Same whole-reply fence unwrap as the rulepack generator: a fenced SKILL.md installs."""
    fenced = f"```markdown\n{VALID_SKILL_MD}\n```"
    services = _services(fenced)

    original_user_dir = skills_module._USER_SKILL_DIR
    skills_module._USER_SKILL_DIR = tmp_path
    skills_module._discover_registry.cache_clear()
    try:
        result = await generate_and_install_skill(services, "a grim survival horror campaign")

        assert result.ok, result.error
        assert result.skill_id == "grim-survival-horror"
        assert Path(result.path).is_file()
    finally:
        skills_module._USER_SKILL_DIR = original_user_dir
        skills_module._discover_registry.cache_clear()

def test_persona_bare_name_strips_the_name_en_gloss() -> None:
    """The forge's `name_en` convention glosses a CJK persona name in parentheses; the bare
    form (what the media shot designer typically writes as a shot subject) strips it."""
    from agent.forge import _persona_bare_name

    assert _persona_bare_name("薇拉·月影（Vera Moonshadow）") == "薇拉·月影"
    assert _persona_bare_name("薇拉·月影(Vera Moonshadow)") == "薇拉·月影"
    assert _persona_bare_name("薇拉·月影") == "薇拉·月影"
    assert _persona_bare_name("Bob Bolton") == "Bob Bolton"  # no gloss, untouched
    assert _persona_bare_name(" 芬恩·石棘 ") == "芬恩·石棘"


def test_bind_pregen_portraits_tolerates_gloss_dropped_from_shot_subject() -> None:
    """The regression: a world card whose pregen names carry the `name_en` gloss
    (`薇拉·月影（Vera Moonshadow）`) while the media shot subjects keep only the bare CJK name
    (`薇拉·月影`) — the old exact-match binding left every `avatar` null. The bare-form
    fallback binds them."""
    from agent.forge import _bind_pregen_portraits

    card = {
        "pregens": [
            {"name": "薇拉·月影（Vera Moonshadow）"},
            {"name": "鲍勃·波顿（Bob Bolton）"},
            {"name": "菲奥娜·贝格（Fiona Bagg）"},
        ]
    }
    media_index = [
        {"kind": "pregens", "subject": "薇拉·月影", "name": "module-x-pregens-1.png"},
        {"kind": "pregens", "subject": "鲍勃·波顿", "name": "module-x-pregens-2.png"},
        {"kind": "pregens", "subject": "菲奥娜·贝格", "name": "module-x-pregens-3.png"},
    ]

    assert _bind_pregen_portraits(card, media_index) == 3
    assert card["pregens"][0]["avatar"] == "module-x-pregens-1.png"
    assert card["pregens"][1]["avatar"] == "module-x-pregens-2.png"
    assert card["pregens"][2]["avatar"] == "module-x-pregens-3.png"


def test_bind_pregen_portraits_exact_match_wins_and_mismatch_leaves_untouched() -> None:
    """An exact subject match still binds first (a full-gloss subject is fine); a pregen with
    no matching shot keeps `avatar` unset and the count reflects only real bindings."""
    from agent.forge import _bind_pregen_portraits

    card = {
        "pregens": [
            {"name": "薇拉·月影（Vera Moonshadow）"},
            {"name": "奥尔加·铁心（Olga Ironheart）"},
        ]
    }
    media_index = [
        {"kind": "pregens", "subject": "薇拉·月影（Vera Moonshadow）", "name": "module-x-pregens-1.png"},
        {"kind": "pregens", "subject": "德雷克·烬", "name": "module-x-pregens-2.png"},
    ]

    assert _bind_pregen_portraits(card, media_index) == 1
    assert card["pregens"][0]["avatar"] == "module-x-pregens-1.png"
    assert "avatar" not in card["pregens"][1]

    assert _bind_pregen_portraits(card, []) == 0  # no pregen shots at all


def test_normalize_pregen_names_strips_gloss_into_aliases() -> None:
    """The regression: a model that wrote `薇拉·月影（Vera Moonshadow）` as the pregen name
    despite the schema — the engine strips the parenthetical gloss, keeps the clean CJK name,
    and preserves the English name as an alias."""
    from agent.forge import _normalize_pregen_names

    card = {
        "pregens": [
            {"name": "薇拉·月影（Vera Moonshadow）"},
            {"name": "鲍勃·波顿(Bob Bolton)"},
            {"name": "奥尔加·铁心"},  # clean name untouched
        ]
    }
    assert _normalize_pregen_names(card) == 2
    assert card["pregens"][0]["name"] == "薇拉·月影"
    assert card["pregens"][0]["aliases"] == ["Vera Moonshadow"]
    assert card["pregens"][1]["name"] == "鲍勃·波顿"
    assert card["pregens"][1]["aliases"] == ["Bob Bolton"]
    assert card["pregens"][2]["name"] == "奥尔加·铁心"
    assert "aliases" not in card["pregens"][2]

