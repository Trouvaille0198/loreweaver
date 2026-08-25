"""Tests for agent.forge's module generator (Layer B.3b -- `docs/plugins.md` "Layer B").

Unlike the skill/rulepack generators (a global, discovery-based user data-dir), a generated module
installs PER-ROOM through the EXISTING module-ingestion pipeline
(`agent.kp_tools_knowledge.DocumentTools.upload_document`), so this exercises TWO scripted `FakeLLM`
responses in order: the module-authoring call (`generate_and_install_module`'s own `services.llm.chat`)
and the full-text analysis call `upload_document` triggers via `services.module_init.initialize` --
mirroring `tests/agent/test_kp_tools_knowledge.py`'s "sentinel never leaks to the player pool"
pattern to confirm the room's REAL knowledge-pool pipeline ran, not some parallel bespoke path.

Covers: (a) happy path -- the generated Markdown is written to a confined file under a tmp
`_USER_MODULE_DIR` and the room (`ctx.chat_key`)'s module knowledge pools end up populated by the
scripted analysis; (b) an empty LLM response / an unsluggable title+description is rejected with
`ok=False` and nothing written; (c) path/id confinement holds for a traversal-shaped title; (d)
with no `_USER_MODULE_DIR` configured at all, generation fails cleanly instead of raising.

Every test that swaps `agent.forge._USER_MODULE_DIR` restores it in a `finally` block -- never
leaking a tmp path into another test's module-forge generation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import agent.forge as forge_module
import core.rulepacks as rulepacks_module
import core.skills as skills_module
from agent.context import AgentCtx, LocalFs
from agent.forge import generate_and_install_module, generate_and_install_pack_module
from agent.services import build_services
from core.dice_engine import seed_dice
from core.pregen_roster import pregen_entries, pregen_pristine_sheet
from gateway.imagegen import reset_imagegen_limiters
from infra.config import ImageGenSettings, Settings
from infra.embeddings import FakeEmbeddings
from infra.imagegen import FakeImageGen, ImageGenError
from infra.llm import ChatResult, FakeLLM, Usage, assistant_text
from infra.media_store import MediaStore

CHAT_KEY = "module-forge-chat"
SENTINEL = "THE FERRYMAN IS THE FEY BOUND TO THE OLD PACT"

GENERATED_MODULE_MD = f"""# The Salt Marsh Vanishing

## Player-facing premise
Fisherfolk have gone missing near the marsh town of Greyreed. The only way across
the marsh at night is the ferryman's boat.

## KEEPER-ONLY
{SENTINEL}: the ferryman who rows travelers across the marsh is himself the culprit,
bound centuries ago into a pact he must now feed to survive.
"""


def _versioned_module(version: str) -> str:
    return f"""---
id: stable-marsh-module
---
# The Salt Marsh Vanishing

Runtime source version: {version}.
"""


def _versioned_analysis(version: str) -> str:
    return json.dumps(
        {
            "scenes": [{"name": f"Scene {version}", "description": f"Pool version {version}"}],
            "summary": f"Catalog version {version}",
        }
    )


def _scripted_analysis_json() -> str:
    """A minimal well-formed module-analysis JSON (the shape `module.analysis_prompt` asks the LLM
    to emit) whose keeper-only NPC secret carries the sentinel -- `agent.module_initializer`
    normalizes any missing list/str fields, so this doesn't need every field populated."""
    return json.dumps(
        {
            "npcs": [
                {
                    "name": "The Ferryman",
                    "description": "A quiet old man who never speaks above a whisper.",
                    "secret": SENTINEL,
                    "role": "antagonist",
                }
            ],
            "summary": "Investigators uncover the truth behind the marsh disappearances.",
        }
    )


def _services(authoring_text: str) -> object:
    """Two scripted responses in order: the module-authoring call, then the analysis call
    `upload_document` triggers via `services.module_init.initialize`."""
    return build_services(
        Settings(locale="en"),
        llm=FakeLLM(script=[assistant_text(authoring_text), assistant_text(_scripted_analysis_json())]),
        embeddings=FakeEmbeddings(8),
    )


def _ctx(fs_base: Path) -> AgentCtx:
    return AgentCtx(chat_key=CHAT_KEY, user_id="kp", locale="en", fs=LocalFs(base_dir=fs_base))


# ---------------------------------------------------------------------------
# (a) Happy path: written to a confined file, installed into THIS room via the existing pipeline.
# ---------------------------------------------------------------------------


async def test_happy_path_writes_and_installs_into_the_calling_room(tmp_path: Path) -> None:
    services = _services(GENERATED_MODULE_MD)
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path / "modules"
    try:
        result = await generate_and_install_module(services, ctx, "a marsh-town disappearance mystery")

        assert result.ok, result.error
        assert result.skill_id == "the-salt-marsh-vanishing"
        assert result.name == "The Salt Marsh Vanishing"
        assert Path(result.path).is_file()
        assert Path(result.path).parent == (tmp_path / "modules").resolve()
        assert result.detail  # upload_document's own confirmation, the "room summary"

        # The EXISTING module pipeline actually ran for THIS room's chat_key.
        status = await services.store.state_get(CHAT_KEY, "module_init_status")
        assert status == "ready"

        pool_doc = await services.documents.get_singleton(CHAT_KEY, "module_pool")
        keeper_raw = json.dumps(pool_doc.data.get("keeper") if pool_doc else {}, ensure_ascii=False)
        player_raw = json.dumps(pool_doc.data.get("player") if pool_doc else {}, ensure_ascii=False)
        assert SENTINEL in keeper_raw
        assert SENTINEL not in player_raw  # red line: the secret never reaches the player pool
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_repeat_description_short_circuits_without_regeneration(tmp_path: Path) -> None:
    services = _services(GENERATED_MODULE_MD)
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path / "modules"
    try:
        first = await generate_and_install_module(
            services,
            ctx,
            "A Marsh-Town   Disappearance Mystery",
        )
        repeated = await generate_and_install_module(
            services,
            ctx,
            "  a marsh-town disappearance mystery  ",
        )

        assert first.ok
        assert repeated.ok
        assert repeated.reused is True
        assert repeated.skill_id == first.skill_id
        assert repeated.path == first.path
        assert len(services.llm.calls) == 2  # one authoring call and one analysis call

        record_raw = await services.store.state_get(CHAT_KEY, "forge_module_last")
        record = json.loads(record_raw)
        assert record["installed_id"] == first.skill_id
        assert record["description_hash"]
        assert record["timestamp"] > 0
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_reinstall_same_room_id_overwrites_one_consistent_content_version(tmp_path: Path) -> None:
    version_1 = _versioned_module("v1")
    version_2 = _versioned_module("v2")
    services = build_services(
        Settings(locale="en"),
        llm=FakeLLM(
            script=[
                assistant_text(version_1),
                assistant_text(_versioned_analysis("v1")),
                assistant_text(version_2),
                assistant_text(_versioned_analysis("v2")),
            ]
        ),
        embeddings=FakeEmbeddings(8),
    )
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path / "modules"
    try:
        first = await generate_and_install_module(services, ctx, "first source request")
        second = await generate_and_install_module(services, ctx, "revised source request")

        assert first.ok and second.ok
        assert first.skill_id == second.skill_id == "stable-marsh-module"
        assert first.path == second.path
        assert sorted(path.name for path in (tmp_path / "modules").glob("*.md")) == [
            "stable-marsh-module.md"
        ]
        assert Path(second.path).read_text(encoding="utf-8") == version_2.strip()

        fulltext = await services.store.state_get(CHAT_KEY, "module_fulltext")
        pool_doc = await services.documents.get_singleton(CHAT_KEY, "module_pool")
        keeper = json.dumps(pool_doc.data.get("keeper") if pool_doc else {}, ensure_ascii=False)
        player = json.dumps(pool_doc.data.get("player") if pool_doc else {}, ensure_ascii=False)
        assert fulltext == version_2.strip()
        assert "v2" in keeper and "v1" not in keeper
        assert "v2" in player and "v1" not in player
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


# ---------------------------------------------------------------------------
# (b) Invalid output -- rejected, nothing written, room untouched.
# ---------------------------------------------------------------------------


async def test_empty_llm_response_is_rejected(tmp_path: Path) -> None:
    services = _services("   ")
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path / "modules"
    try:
        result = await generate_and_install_module(services, ctx, "anything")

        assert not result.ok
        assert result.error == "empty_response"
        assert not (tmp_path / "modules").exists() or list((tmp_path / "modules").iterdir()) == []

        status = await services.store.state_get(CHAT_KEY, "module_init_status")
        assert not status  # the room's module pipeline never ran
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_non_markdown_llm_response_is_rejected(tmp_path: Path) -> None:
    services = _services("The lantern gutters low. Tell me what you do next.")
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path / "modules"
    try:
        result = await generate_and_install_module(services, ctx, "anything")

        assert not result.ok
        assert result.error == "invalid_module_output"
        assert not (tmp_path / "modules").exists() or list((tmp_path / "modules").iterdir()) == []
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_cjk_title_without_usable_id_gets_stable_content_hash_id(tmp_path: Path) -> None:
    generated = "# 黄泉归影\n\n一场发生在黄泉渡口的调查。"
    expected_id = f"module-{hashlib.sha256(generated.encode('utf-8')).hexdigest()[:8]}"
    services = _services(generated)
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path / "modules"
    try:
        result = await generate_and_install_module(services, ctx, "黄泉渡口的怪谈")

        assert result.ok, result.error
        assert result.skill_id == expected_id
        assert result.name == "黄泉归影"
        assert Path(result.path).is_file()
        assert len(services.llm.calls) == 2  # authoring + analysis, never a second generation
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_cjk_title_uses_explicit_ascii_id_without_regeneration(tmp_path: Path) -> None:
    generated = """---
id: echoes-from-yellow-springs
---
# 黄泉归影

一场发生在黄泉渡口的调查。
"""
    services = _services(generated)
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path / "modules"
    try:
        result = await generate_and_install_module(services, ctx, "黄泉渡口的怪谈")

        assert result.ok, result.error
        assert result.skill_id == "echoes-from-yellow-springs"
        assert result.name == "黄泉归影"
        assert len(services.llm.calls) == 2
        system_prompt = services.llm.calls[0][0][0]["content"]
        assert "ASCII" in system_prompt
        assert "id:" in system_prompt
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_module_forge_and_analysis_usage_are_both_recorded(tmp_path: Path) -> None:
    llm = FakeLLM(
        script=[
            ChatResult(
                content=GENERATED_MODULE_MD,
                tool_calls=[],
                usage=Usage(prompt_tokens=40, completion_tokens=10, total_tokens=50),
            ),
            ChatResult(
                content=_scripted_analysis_json(),
                tool_calls=[],
                usage=Usage(prompt_tokens=80, completion_tokens=20, total_tokens=100),
            ),
        ]
    )
    services = build_services(Settings(locale="en"), llm=llm, embeddings=FakeEmbeddings(8))
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path / "modules"
    try:
        result = await generate_and_install_module(services, ctx, "a marsh-town disappearance mystery")

        assert result.ok, result.error
        stats = json.loads(await services.store.state_get(CHAT_KEY, "usage_stats"))
        assert stats["session"]["turns"] == 2
        assert stats["session"]["prompt"] == 120
        assert stats["session"]["completion"] == 30
        assert stats["last"]["prompt"] == 80
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


# ---------------------------------------------------------------------------
# (c) Security: path/id confinement for a traversal-shaped title.
# ---------------------------------------------------------------------------


async def test_traversal_title_is_sanitized_to_a_safe_id_never_a_path(tmp_path: Path) -> None:
    traversal_md = GENERATED_MODULE_MD.replace("The Salt Marsh Vanishing", "../../etc/passwd")
    services = _services(traversal_md)
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path / "modules"
    try:
        result = await generate_and_install_module(services, ctx, "anything")

        if result.ok:
            assert "/" not in result.skill_id
            assert ".." not in result.skill_id
            written = Path(result.path).resolve()
            assert written.is_relative_to((tmp_path / "modules").resolve())
        else:
            assert not result.error.startswith("path_escape")
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


# ---------------------------------------------------------------------------
# (d) No data dir configured at all.
# ---------------------------------------------------------------------------


async def test_no_data_dir_configured_fails_cleanly(tmp_path: Path) -> None:
    services = _services(GENERATED_MODULE_MD)
    ctx = _ctx(tmp_path)
    assert forge_module._USER_MODULE_DIR is None  # the default in every test unless opted in

    result = await generate_and_install_module(services, ctx, "anything")

    assert not result.ok
    assert result.error == "no_data_dir"
    assert result.skill_id == ""
    assert result.path == ""


# ---------------------------------------------------------------------------
# (e) Keeper-selectable extra content: the `media` / `companion` options. Both passes run AFTER
# the module is installed and NEVER fail it -- every error degrades to fewer/zero artifacts plus
# a localized note in `detail`.
# ---------------------------------------------------------------------------


def _shot(kind: str, subject: str) -> dict:
    return {
        "kind": kind,
        "subject": subject,
        "prompt": f"{kind} of {subject}, moody digital painting",
        "caption": f"Behold: {subject}",
    }


def _option_services(
    tmp_path: Path,
    replies: list[str],
    *,
    per_hour: int = 100,
    imagegen: bool = True,
):
    """Module-forge services with a confined data_dir (media blobs must not leak into the repo's
    default ./data). `imagegen=True` installs a recording fake as the room's generator;
    `imagegen=False` leaves the imagegen settings at their unconfigured default, so
    `imagegen_for_room` returns None exactly like a server with no image provider."""
    settings = Settings(locale="en", data_dir=str(tmp_path))
    if imagegen:
        settings = Settings(
            locale="en",
            data_dir=str(tmp_path),
            imagegen=ImageGenSettings(provider="fake", api_key="fake", model="fake", per_room_per_hour=per_hour),
        )
    services = build_services(
        settings,
        llm=FakeLLM(script=[assistant_text(reply) for reply in replies]),
        embeddings=FakeEmbeddings(8),
    )
    if imagegen:
        services.imagegen = _UniqueImageGen()
    return services


class _UniqueImageGen(FakeImageGen):
    """A FakeImageGen whose every call returns DISTINCT bytes -- the media store dedupes by
    content hash, so constant fake bytes would collapse every render into the first record."""

    async def generate(self, prompt: str, **kwargs):
        data, mime = await super().generate(prompt, **kwargs)
        return data + len(self.calls).to_bytes(4, "big"), mime


async def test_media_pass_generates_stores_and_reports_images(tmp_path: Path) -> None:
    reset_imagegen_limiters()
    shots = json.dumps(
        [
            _shot("cover", "Greyreed at dusk"),
            _shot("scenes", "The ferry crossing"),
            _shot("scenes", "The drowned chapel"),
        ]
    )
    services = _option_services(tmp_path, [GENERATED_MODULE_MD, _scripted_analysis_json(), shots])
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_module(
            services, ctx, "a marsh mystery", media=["cover", "scenes"]
        )

        assert result.ok, result.error
        assert len(services.imagegen.calls) == 3
        expected = {
            "module-the-salt-marsh-vanishing-cover-1.png",
            "module-the-salt-marsh-vanishing-scenes-2.png",
            "module-the-salt-marsh-vanishing-scenes-3.png",
        }
        for name in expected:
            assert name in result.detail
        records = await MediaStore(services.store, str(tmp_path)).list_room_records(CHAT_KEY)
        assert {record.name for record in records} == expected

        # The module_media_index maps each illustration to its subject (scene/NPC/item)
        # so the runtime can reuse it as a `.image` reference.
        index = json.loads(await services.store.state_get(CHAT_KEY, "module_media_index"))
        by_name = {e["name"]: e for e in index}
        assert by_name["module-the-salt-marsh-vanishing-cover-1.png"]["subject"] == "Greyreed at dusk"
        assert by_name["module-the-salt-marsh-vanishing-scenes-2.png"]["subject"] == "The ferry crossing"
        assert by_name["module-the-salt-marsh-vanishing-scenes-3.png"]["subject"] == "The drowned chapel"
        # Every index entry points at a stored record.
        record_hashes = {record.hash for record in records}
        assert {e["hash"] for e in index} == record_hashes
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_media_pass_without_imagegen_degrades_to_a_note(tmp_path: Path) -> None:
    services = _option_services(
        tmp_path, [GENERATED_MODULE_MD, _scripted_analysis_json()], imagegen=False
    )
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_module(services, ctx, "a marsh mystery", media=["cover"])

        assert result.ok, result.error
        assert "no image-generation provider is configured" in result.detail
        # No provider -> no wasted shot-list call: only the authoring + analysis calls happened.
        assert len(services.llm.calls) == 2
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_media_pass_malformed_shot_list_degrades(tmp_path: Path) -> None:
    reset_imagegen_limiters()
    services = _option_services(
        tmp_path, [GENERATED_MODULE_MD, _scripted_analysis_json(), "no json here, sorry"]
    )
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_module(services, ctx, "a marsh mystery", media=["scenes"])

        assert result.ok, result.error
        assert "no usable shot list" in result.detail
        assert services.imagegen.calls == []
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_media_pass_enforces_kind_caps_one_portrait_per_npc_and_total_cap(tmp_path: Path) -> None:
    reset_imagegen_limiters()
    entries = (
        [_shot("cover", "Cover A"), _shot("cover", "Cover B")]
        + [_shot("scenes", f"Scene {i}") for i in range(7)]
        + [_shot("npcs", "The Ferryman"), _shot("npcs", "The Ferryman")]
        + [_shot("npcs", f"NPC {i}") for i in range(6)]
        + [_shot("items", f"Item {i}") for i in range(7)]
    )
    services = _option_services(tmp_path, [GENERATED_MODULE_MD, _scripted_analysis_json(), json.dumps(entries)])
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_module(
            services, ctx, "a marsh mystery", media=["cover", "scenes", "npcs", "items"]
        )

        assert result.ok, result.error
        # 1 cover + 6 scenes + 6 unique NPC portraits = 13 valid shots; the 12-image total cap
        # bites last. The second Ferryman portrait never renders (one portrait per NPC).
        assert len(services.imagegen.calls) == 12
        ferryman_prompts = [call["prompt"] for call in services.imagegen.calls if "The Ferryman" in call["prompt"]]
        assert len(ferryman_prompts) == 1
        assert not any("Item" in call["prompt"] for call in services.imagegen.calls)
        assert "cover-1" in result.detail
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_media_pass_room_rate_cap_stops_renders_keeping_earlier_images(tmp_path: Path) -> None:
    reset_imagegen_limiters()
    shots = json.dumps([_shot("scenes", f"Scene {i}") for i in range(3)])
    services = _option_services(tmp_path, [GENERATED_MODULE_MD, _scripted_analysis_json(), shots], per_hour=1)
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_module(services, ctx, "a marsh mystery", media=["scenes"])

        assert result.ok, result.error
        assert len(services.imagegen.calls) == 1
        assert "hourly image budget" in result.detail
        assert "scenes-1" in result.detail
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


class _FailingImageGen(FakeImageGen):
    async def generate(self, prompt: str, **kwargs):
        raise ImageGenError("image_http_error", "boom")


async def test_media_pass_provider_error_degrades_without_failing_module(tmp_path: Path) -> None:
    reset_imagegen_limiters()
    shots = json.dumps([_shot("cover", "Greyreed at dusk")])
    services = _option_services(
        tmp_path, [GENERATED_MODULE_MD, _scripted_analysis_json(), shots], imagegen=False
    )
    services.imagegen = _FailingImageGen()
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_module(services, ctx, "a marsh mystery", media=["cover"])

        assert result.ok, result.error
        assert "the image provider failed" in result.detail
        records = await MediaStore(services.store, str(tmp_path)).list_room_records(CHAT_KEY)
        assert records == []
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


VALID_COMPANION_SKILL_MD = """---
name: Marsh Horror Pacing
description: >
  Enable for running a marshland mystery of slow-burn dread: isolation, fog, and weather as
  pressure on every choice.
allowed-tools: []
metadata:
  scope: room
---

# Marsh horror pacing

Keep the marsh oppressive: name the fog, the cold, and the distance from help at every scene turn.
"""

VALID_COMPANION_RULEPACK_YAML = """
names: [marsh-mystery-rules]
set_keys: [marsh]
defaults:
  力量: 10
  敏捷: 10
  意志: 10
  生命值: 20
alias:
  力量: [STR, strength]
st_show:
  top: [力量, 敏捷, 意志, 生命值]
  itemsPerLine: 4
creation_constraints:
  method: point-buy
  points: 12
derived:
  生命值上限:
    half_of: 意志
"""


async def test_companion_pass_installs_skill_rulepack_and_pregen_cards(tmp_path: Path) -> None:
    seed_dice(2029)
    concepts = json.dumps(
        [
            {"name": "Ada Marsh", "description": "A sharp-eyed reporter hooked by the disappearances."},
            {"name": "Bob Grey", "description": "A retired ferryman who knows every marsh path."},
        ]
    )
    # Script order: module authoring, module analysis, skill, rulepack, card concepts, then one
    # `_ask_concept` call per card (empty reply -> sheet from system defaults, name kept).
    services = _option_services(
        tmp_path,
        [
            GENERATED_MODULE_MD,
            _scripted_analysis_json(),
            VALID_COMPANION_SKILL_MD,
            VALID_COMPANION_RULEPACK_YAML,
            concepts,
            "",
            "",
        ],
        imagegen=False,
    )
    ctx = _ctx(tmp_path)

    skill_dir = tmp_path / "skills"
    rulepack_dir = tmp_path / "rulepacks"
    original_module_dir = forge_module._USER_MODULE_DIR
    original_skill_dir = skills_module._USER_SKILL_DIR
    original_rulepack_dir = rulepacks_module._USER_RULEPACK_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    skills_module._USER_SKILL_DIR = skill_dir
    rulepacks_module._USER_RULEPACK_DIR = rulepack_dir
    try:
        result = await generate_and_install_module(
            services, ctx, "a marsh mystery", companion=["skills", "rulepacks", "cards"]
        )

        assert result.ok, result.error
        assert "Companion KP skill installed" in result.detail
        assert "Companion rule system installed" in result.detail
        assert "Pre-generated 2 character card(s)" in result.detail
        assert (skill_dir / "marsh-horror-pacing" / "SKILL.md").is_file()
        assert (rulepack_dir / "marsh-mystery-rules.yaml").is_file()
        entries = await pregen_entries(services.documents, CHAT_KEY)
        assert {entry["name"] for entry in entries} == {"Ada Marsh", "Bob Grey"}
        assert all(entry["source"].startswith("forge-module:") for entry in entries)
    finally:
        forge_module._USER_MODULE_DIR = original_module_dir
        skills_module._USER_SKILL_DIR = original_skill_dir
        rulepacks_module._USER_RULEPACK_DIR = original_rulepack_dir
        skills_module._discover_registry.cache_clear()
        rulepacks_module._discover_registry.cache_clear()
        rulepacks_module._alias_resolver.cache_clear()


async def test_companion_pass_installs_cjk_named_skill_and_rulepack(tmp_path: Path) -> None:
    """The zh-locale companion path: the generated skill/rulepack come back with CJK names, which
    have no ASCII slug -- they must install under the stable content-hash fallback ids, not fail
    with bad_id (the exact defect that made zh companion content silently missing)."""
    seed_dice(2030)
    cjk_skill_md = VALID_COMPANION_SKILL_MD.replace("Marsh Horror Pacing", "消失的图书管理员").replace(
        "Marsh horror pacing", "消失的图书管理员"
    )
    cjk_rulepack_yaml = VALID_COMPANION_RULEPACK_YAML.replace("names: [marsh-mystery-rules]", "names: [消失的图书管理员]")
    concepts = json.dumps([{"name": "沈仲卿", "description": "一位在旧上海查案的报馆记者。"}])
    services = _option_services(
        tmp_path,
        [
            GENERATED_MODULE_MD,
            _scripted_analysis_json(),
            cjk_skill_md,
            cjk_rulepack_yaml,
            concepts,
            "",
        ],
        imagegen=False,
    )
    ctx = _ctx(tmp_path)

    skill_dir = tmp_path / "skills"
    rulepack_dir = tmp_path / "rulepacks"
    original_module_dir = forge_module._USER_MODULE_DIR
    original_skill_dir = skills_module._USER_SKILL_DIR
    original_rulepack_dir = rulepacks_module._USER_RULEPACK_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    skills_module._USER_SKILL_DIR = skill_dir
    rulepacks_module._USER_RULEPACK_DIR = rulepack_dir
    try:
        result = await generate_and_install_module(
            services, ctx, "一个上海图书馆密室谜案", companion=["skills", "rulepacks", "cards"]
        )

        assert result.ok, result.error
        assert "Companion KP skill installed" in result.detail
        assert "Companion rule system installed" in result.detail
        assert "Pre-generated 1 character card(s)" in result.detail
        skill_dirs = [p.name for p in skill_dir.iterdir() if p.is_dir()]
        rulepack_files = [p.stem for p in rulepack_dir.glob("*.yaml")]
        assert len(skill_dirs) == 1 and skill_dirs[0].startswith("skill-")
        assert len(rulepack_files) == 1 and rulepack_files[0].startswith("pack-")
        entries = await pregen_entries(services.documents, CHAT_KEY)
        assert [entry["name"] for entry in entries] == ["沈仲卿"]
    finally:
        forge_module._USER_MODULE_DIR = original_module_dir
        skills_module._USER_SKILL_DIR = original_skill_dir
        rulepacks_module._USER_RULEPACK_DIR = original_rulepack_dir
        skills_module._discover_registry.cache_clear()
        rulepacks_module._discover_registry.cache_clear()
        rulepacks_module._alias_resolver.cache_clear()


async def test_companion_pass_failure_degrades_without_failing_module(tmp_path: Path) -> None:
    services = _option_services(
        tmp_path, [GENERATED_MODULE_MD, _scripted_analysis_json(), "not a skill at all"], imagegen=False
    )
    ctx = _ctx(tmp_path)

    original_module_dir = forge_module._USER_MODULE_DIR
    original_skill_dir = skills_module._USER_SKILL_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    skills_module._USER_SKILL_DIR = tmp_path / "skills"
    try:
        result = await generate_and_install_module(services, ctx, "a marsh mystery", companion=["skills"])

        assert result.ok, result.error
        assert 'Companion content "skill" could not be generated' in result.detail
    finally:
        forge_module._USER_MODULE_DIR = original_module_dir
        skills_module._USER_SKILL_DIR = original_skill_dir
        skills_module._discover_registry.cache_clear()


async def test_different_options_bypass_the_repeat_request_suppression(tmp_path: Path) -> None:
    reset_imagegen_limiters()
    shots = json.dumps([_shot("cover", "Greyreed at dusk")])
    services = _option_services(
        tmp_path,
        [GENERATED_MODULE_MD, _scripted_analysis_json(), GENERATED_MODULE_MD, _scripted_analysis_json(), shots],
    )
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        first = await generate_and_install_module(services, ctx, "a marsh mystery")
        assert first.ok, first.error

        # Same description seconds later would normally hit the repeat-request suppression; a
        # different option selection is a NEW request, so the module regenerates and the media
        # pass runs.
        second = await generate_and_install_module(services, ctx, "a marsh mystery", media=["cover"])

        assert second.ok, second.error
        assert not second.reused
        assert len(services.imagegen.calls) == 1
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_code_fenced_module_is_unwrapped_and_installed(tmp_path: Path) -> None:
    """A module reply wrapped in a whole-reply code fence is unwrapped before title/id extraction
    and installs through the normal pipeline (before this unwrap it died as invalid_module_output)."""
    fenced = f"```markdown\n{GENERATED_MODULE_MD}\n```"
    services = _option_services(tmp_path, [fenced, _scripted_analysis_json()], imagegen=False)
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_module(services, ctx, "a marsh mystery")

        assert result.ok, result.error
        assert result.skill_id == "the-salt-marsh-vanishing"
        assert Path(result.path).is_file()
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_media_pass_writes_module_asset_dir_and_reimport_picks_it_up(tmp_path: Path) -> None:
    """Path one: forge-generated illustrations also land in the module's own `assets/` directory
    (next to the source md), so they travel WITH the module. Re-importing that module into another
    room registers those images into that room's media deck."""
    reset_imagegen_limiters()
    shots = json.dumps([_shot("cover", "Greyreed at dusk")])
    services = _option_services(
        tmp_path,
        [
            GENERATED_MODULE_MD,
            _scripted_analysis_json(),
            shots,
            _scripted_analysis_json(),  # re-import's module analysis
        ],
    )
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_module(services, ctx, "a marsh mystery", media=["cover"])
        assert result.ok, result.error

        # The module's source + its asset illustrations live side by side on disk.
        md_path = Path(result.path)
        assert md_path.is_file()
        assets_dir = md_path.parent / f"{md_path.stem}.assets"
        assert assets_dir.is_dir()
        asset_files = sorted(p.name for p in assets_dir.iterdir() if p.is_file())
        assert asset_files == ["module-the-salt-marsh-vanishing-cover-1.png"]
        # The asset copy is real rendered bytes, not a stub.
        assert (assets_dir / asset_files[0]).read_bytes()

        # A DIFFERENT room imports the same module source -> the asset illustrations are
        # registered into that room's media deck (module assets are not room-trapped).
        from agent.kp_tools_knowledge import DocumentTools

        other_room = AgentCtx(chat_key="module-forge-other-room", user_id="kp", locale="en", fs=LocalFs(base_dir=tmp_path))
        doc_tools = DocumentTools(services)
        install_note = await doc_tools.upload_document(other_room, file_path=md_path.name, doc_type="module")
        assert "Imported 1 module illustration" in install_note
        other_records = await MediaStore(services.store, str(tmp_path)).list_room_records("module-forge-other-room")
        assert {record.name for record in other_records} == {"module-the-salt-marsh-vanishing-cover-1.png"}
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_module_asset_dir_missing_is_silently_skipped(tmp_path: Path) -> None:
    """A module uploaded with no sibling assets directory (a plain hand-written md) must import
    cleanly with no asset note -- the asset import is strictly additive."""
    reset_imagegen_limiters()
    services = _option_services(tmp_path, [GENERATED_MODULE_MD, _scripted_analysis_json()], imagegen=False)
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_module(services, ctx, "a marsh mystery")
        assert result.ok, result.error
        assert "Imported" not in result.detail
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


# ---------------------------------------------------------------------------
# (f) Path two: `generate_and_install_pack_module` — author a native world card, wrap it in a
# complete `.lwpack`, install the pack, and populate the room through the keeper world-import path.
# ---------------------------------------------------------------------------

_VALID_PACK_LORECARD = json.dumps(
    {
        "format": "loreweaver.card",
        "format_version": 1,
        "name": "The Salt Marsh Vanishing",
        "description": "A coastal-town disappearance mystery.",
        "scenario": "Investigators arrive in Greyreed at dusk.",
        "opening": "The ferryman is waiting.",
        "tags": ["mystery", "coc"],
        "worldbook": [
            {
                "content": SENTINEL + ": the ferryman is the culprit bound to the old pact.",
                "keys": ["ferryman"],
                "secret": True,
                "category": "truth",
            },
            {
                "content": "The marsh town of Greyreed sits at the mouth of the river.",
                "keys": ["greyreed"],
                "secret": False,
                "category": "lore",
            },
        ],
        "variables": [
            {
                "name": "fear",
                "kind": "number",
                "labels": {"en": "Fear", "zh": "恐惧"},
                "default": 0,
                "min": 0,
                "max": 10,
            }
        ],
        "pregens": [{"name": "Ada Marsh", "concept": "A sharp-eyed reporter hooked by the disappearances."}],
    },
    ensure_ascii=False,
)


async def test_pack_module_generates_lwpack_and_populates_room(tmp_path: Path) -> None:
    """Path two happy path: authoring a world card yields a real `.lwpack` file AND the room gets
    populated through the keeper world-import path — secret lore lands in the worldbook, the
    module brief carries the prose, and the pregen cast is claimable."""
    services = _option_services(
        tmp_path,
        [_VALID_PACK_LORECARD],  # pack-module authoring only; no module-pool analysis
        imagegen=False,
    )
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_pack_module(services, ctx, "a marsh-town disappearance mystery")

        assert result.ok, result.error
        assert result.path.endswith(".lwpack")
        pack_file = Path(result.path)
        assert pack_file.is_file()
        assert pack_file.read_bytes().startswith(b"PK")  # it is a real zip archive

        # The pack source tree held a world card next to the manifest.
        assert (tmp_path / f"{result.skill_id}.pack-src" / "pack.yaml").is_file()

        # The room was populated through the keeper world-import path: secret lore in the
        # worldbook, the module brief carrying the prose, the pregen cast claimable.
        entries = await services.worldbook.list(ctx.chat_key)
        texts = [e.content for e in entries]
        assert any(SENTINEL in text for text in texts), "secret lore must reach the worldbook"
        assert any("ferryman" in text for text in texts)
        pregens = await pregen_entries(services.documents, ctx.chat_key)
        assert {entry["name"] for entry in pregens} == {"Ada Marsh"}
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_pack_module_media_pass_persists_reference_index(tmp_path: Path) -> None:
    """Path two with media: rendered illustrations land in the room's media store AND the
    `module_media_index` provenance is persisted — `.image <kind> <subject>` reuses the
    room's illustration as a generation reference, exactly like the module-creation path."""
    reset_imagegen_limiters()
    shots = json.dumps(
        [
            _shot("cover", "Greyreed at dusk"),
            _shot("scenes", "The ferry crossing"),
            _shot("scenes", "The drowned chapel"),
        ]
    )
    services = _option_services(
        tmp_path,
        [_VALID_PACK_LORECARD, shots],  # world card + media shot list
        imagegen=True,
    )
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_pack_module(
            services, ctx, "a marsh-town disappearance mystery", media=["cover", "scenes"]
        )

        assert result.ok, result.error
        assert len(services.imagegen.calls) == 3
        records = await MediaStore(services.store, str(tmp_path)).list_room_records(CHAT_KEY)
        names = {record.name for record in records}
        assert any(name.startswith("module-") and "-cover-" in name for name in names)
        assert any(name.startswith("module-") and "-scenes-" in name for name in names)

        index = json.loads(await services.store.state_get(CHAT_KEY, "module_media_index"))
        by_name = {e["name"]: e for e in index}
        assert by_name[next(n for n in names if "-scenes-" in n and "-2" in n)]["subject"] == "The ferry crossing"
        # Every index entry points at a stored record (hash rides along for `.image`).
        # The room may also hold records the pack-import path registered (pregen avatars), so
        # index hashes are a SUBSET of stored records, not the whole set.
        record_hashes = {record.hash for record in records}
        assert {e["hash"] for e in index} <= record_hashes
        assert all(e["hash"] for e in index)
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


def test_pack_module_manifest_preserves_media_subject_titles() -> None:
    """Generated pack assets retain the subject that the detail page uses as their label."""
    from core.yaml_safety import safe_load_no_aliases

    manifest = safe_load_no_aliases(
        forge_module._pack_module_manifest(
            "fog-manor",
            "Fog Manor",
            assets=["assets/module-fog-manor-npcs-1.jpg", "assets/module-fog-manor-scenes-2.jpg"],
            asset_titles={
                "assets/module-fog-manor-npcs-1.jpg": "The Tailor",
                "assets/module-fog-manor-scenes-2.jpg": "The ballroom",
            },
        )
    )

    assert manifest["assets"] == [
        {"path": "assets/module-fog-manor-npcs-1.jpg", "title": "The Tailor"},
        {"path": "assets/module-fog-manor-scenes-2.jpg", "title": "The ballroom"},
    ]


# coc7 base values for the skills the normalization test feeds in (game data, i18n-exempt).
_BASE = {"侦查": 25, "话术": 5, "图书馆": 20, "心理学": 10, "潜行": 20, "聆听": 20, "急救": 30, "神秘学": 5, "汽车驾驶": 20}


async def test_pack_module_normalizes_pregen_skills_to_the_budget(tmp_path: Path) -> None:
    """The model's pregen `skills` are shaped by the engine, never trusted: unknown names are
    dropped, values clamp to the rulepack's creation max, and the spend above base is scaled
    down to the nominal skill-point budget (CoC defaults: INT 50 x2 + EDU 50 x4 = 300) while
    preserving the author's profile. The normalized values reach the imported pregen sheet."""
    card = json.loads(_VALID_PACK_LORECARD)
    card["pregens"] = [
        {
            "name": "Ada Marsh",
            "concept": "A sharp-eyed reporter.",
            "skills": {
                "Spot Hidden": 999,  # over the 90 creation max -> clamped, then budget-scaled
                "Fast Talk": 80,
                "not-a-skill": 60,  # not a coc7 skill -> dropped
                "Library Use": 70,
                "Psychology": 60,
                "Stealth": 55,
                "Listen": 50,
                "First Aid": 40,
                "Occult": 35,
                "Drive Auto": 30,
            },
        },
        {
            "name": "Harvey Cole",
            "concept": "A quiet archivist.",
            "skills": {"Spot Hidden": 999},  # single skill: clamp survives (spend fits the budget)
        },
    ]
    services = _option_services(tmp_path, [json.dumps(card, ensure_ascii=False)], imagegen=False)
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_pack_module(services, ctx, "a marsh mystery")
        assert result.ok, result.error
        assert "skill-point budget" in result.detail

        # The world card written into the pack source carries the normalized profile.
        src_dir = tmp_path / f"{result.skill_id}.pack-src"
        card_files = list((src_dir / "cards").glob("*.lorecard.json"))
        assert card_files, "the pack source must ship the world card"
        written = json.loads(card_files[0].read_text(encoding="utf-8"))
        ada, harvey = written["pregens"][0], written["pregens"][1]
        skills = ada["skills"]
        assert "侦查" in skills, "Spot Hidden must resolve to the canonical coc7 name"
        assert "not-a-skill" not in skills
        assert all(0 <= value <= 90 for value in skills.values())
        spent = sum(value - _BASE[key] for key, value in skills.items())
        assert spent <= 300, f"spend {spent} must fit the nominal coc7 skill-point budget of 300"
        # The author's signature skills stay on top after scaling (profile preserved).
        ordered = sorted(skills, key=skills.get, reverse=True)
        assert ordered[:2] == ["侦查", "话术"]
        # A profile that fits the budget keeps its clamped value untouched: 999 -> 90.
        assert harvey["skills"]["侦查"] == 90

        # The room import applied the normalized profile to the claimable pregen sheet.
        sheet = await pregen_pristine_sheet(services.documents, ctx.chat_key, "ada-marsh")
        assert sheet is not None
        assert sheet.skills.get("侦查") == skills["侦查"]
        assert sheet.skills.get("话术") == skills["话术"]
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


def test_pack_module_schema_and_prompts_advertise_pregen_skills() -> None:
    """The model-facing contract must keep advertising pregen `skills` and the budget rule —
    if the schema or prompts stop asking for them, the feature silently degrades."""
    assert '"skills"' in forge_module._PACK_MODULE_CARD_SCHEMA
    assert '"name_en"' in forge_module._PACK_MODULE_CARD_SCHEMA
    root = Path(__file__).resolve().parents[2]
    for locale, needle in (("en", "skill-point budget"), ("zh", "技能点预算")):
        data = json.loads((root / "locales" / locale / "agent.json").read_text(encoding="utf-8"))
        assert needle in data["agent.forge.pack_module_system_prompt"]


def test_pack_module_schema_and_prompts_require_keeper_only_entries() -> None:
    """A generated module must carry the two keeper-only skeleton entries a hand-authored module
    ships — an ending plan (结局门) and an NPC knowledge boundary (人物所知边界) — so a keeper who
    runs it can finish the story and never over-shares. Pins the forge contract to it."""
    assert '"category": "lore|npc|clue|truth|secret"' in forge_module._PACK_MODULE_CARD_SCHEMA
    root = Path(__file__).resolve().parents[2]
    for locale, needles in (
        ("en", ("结局门", "人物所知边界", "ending", "knowledge")),
        ("zh", ("结局门与信物", "人物所知边界", "secret: true")),
    ):
        data = json.loads((root / "locales" / locale / "agent.json").read_text(encoding="utf-8"))
        prompt = data["agent.forge.pack_module_system_prompt"]
        assert all(needle in prompt for needle in needles)


async def test_pack_module_uses_name_en_for_the_module_id(tmp_path: Path) -> None:
    """A CJK module name has no ASCII to slug, so the model's `name_en` supplies the stable id —
    without it a Chinese-only name degrades to stray ASCII from the description (a module whose id
    became "coc")."""
    card = json.loads(_VALID_PACK_LORECARD)
    card["name"] = "雾钟镇的午夜钟声"
    card["name_en"] = "Midnight Bells of Mist Town"
    services = _option_services(tmp_path, [json.dumps(card, ensure_ascii=False)], imagegen=False)
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_pack_module(services, ctx, "a marsh mystery")
        assert result.ok, result.error
        assert result.skill_id == "midnight-bells-of-mist-town"
        assert (tmp_path / "midnight-bells-of-mist-town.pack-src" / "pack.yaml").is_file()
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_pack_module_malformed_card_degrades(tmp_path: Path) -> None:
    """A pack-module authoring reply that is not a usable world card must fail cleanly with
    nothing installed."""
    services = _option_services(
        tmp_path,
        ["not a json object at all"],
        imagegen=False,
    )
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_pack_module(services, ctx, "a marsh mystery")

        assert not result.ok
        assert result.error.startswith("invalid_pack_module")
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_pack_module_rejects_variables_without_requested_locale_labels(tmp_path: Path) -> None:
    card = json.loads(_VALID_PACK_LORECARD)
    card["variables"][0]["labels"].pop("en")
    services = _option_services(tmp_path, [json.dumps(card, ensure_ascii=False)], imagegen=False)
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_pack_module(services, ctx, "a marsh mystery")

        assert not result.ok
        assert "missing en labels" in result.error
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_pack_module_injects_format_when_model_omits_it(tmp_path: Path) -> None:
    """A real LLM reliably forgets the `format`/`format_version` machine contract. The pack
    engine must inject them before the native parser, so an authoring reply without them still
    builds a valid .lwpack (this is the exact failure the first live run hit)."""
    no_format = json.loads(_VALID_PACK_LORECARD)
    no_format.pop("format")
    no_format.pop("format_version")
    services = _option_services(tmp_path, [json.dumps(no_format, ensure_ascii=False)], imagegen=False)
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_pack_module(services, ctx, "a marsh mystery")
        assert result.ok, result.error
        assert result.path.endswith(".lwpack")
        assert Path(result.path).is_file()
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_pack_module_bundles_companion_skill_and_rulepack(tmp_path: Path) -> None:
    """A complete .lwpack module bundles companion skill + rulepack INTO the pack source (under
    contents.skills/contents.rulepacks), so the distributable pack is truly complete — not just a
    world card. The built manifest must declare them and the pack must build cleanly."""
    services = _option_services(
        tmp_path,
        [
            _VALID_PACK_LORECARD,  # world card authoring
            VALID_COMPANION_SKILL_MD,  # companion skill
            VALID_COMPANION_RULEPACK_YAML,  # companion rulepack
        ],
        imagegen=False,
    )
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_pack_module(
            services, ctx, "a marsh mystery", companion=["skills", "rulepacks"]
        )

        assert result.ok, result.error
        assert result.path.endswith(".lwpack")
        # The companion content landed inside the pack source tree.
        pack_id = result.skill_id
        assert (tmp_path / f"{pack_id}.pack-src" / "skills" / "marsh-horror-pacing" / "SKILL.md").is_file()
        assert (tmp_path / f"{pack_id}.pack-src" / "rulepacks" / "marsh-mystery-rules.yaml").is_file()
        # The manifest declares them under contents.
        from core.yaml_safety import safe_load_no_aliases

        manifest = safe_load_no_aliases((tmp_path / f"{pack_id}.pack-src" / "pack.yaml").read_text())
        assert "skills/marsh-horror-pacing" in manifest["contents"]["skills"]
        assert "rulepacks/marsh-mystery-rules.yaml" in manifest["contents"]["rulepacks"]
        # And the pack builds cleanly with those contents (validation passes).
        from core.pack import inspect_pack

        m = inspect_pack(Path(result.path))
        assert "skills/marsh-horror-pacing" in m.contents["skills"]
        assert "rulepacks/marsh-mystery-rules.yaml" in m.contents["rulepacks"]
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_pack_rulepack_retries_after_transient_llm_failure(tmp_path: Path) -> None:
    """A transient LLM failure (empty response) on the companion rulepack must be retried once
    before degrading — so a provider hiccup doesn't silently drop the rulepack from an otherwise
    complete .lwpack."""
    services = _option_services(
        tmp_path,
        [
            _VALID_PACK_LORECARD,  # world card
            "",  # companion rulepack attempt 1 -> empty_response (transient)
            VALID_COMPANION_RULEPACK_YAML,  # companion rulepack attempt 2 -> success
        ],
        imagegen=False,
    )
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_pack_module(
            services, ctx, "a marsh mystery", companion=["rulepacks"]
        )

        assert result.ok, result.error
        assert result.path.endswith(".lwpack")
        pack_id = result.skill_id
        assert (tmp_path / f"{pack_id}.pack-src" / "rulepacks" / "marsh-mystery-rules.yaml").is_file()
        # The retry note was logged; the rulepack still landed.
        assert "Bundled a rule system" in result.detail
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_pack_module_rulepack_failure_is_surfaced_not_silent(tmp_path: Path) -> None:
    """A failed companion rulepack must be REPORTED in the result detail — never silently dropped
    so a user believes the .lwpack is complete when its rule system is missing."""
    services = _option_services(
        tmp_path,
        [
            _VALID_PACK_LORECARD,  # world card
            "not a valid rulepack at all",  # companion rulepack -> parse failure
        ],
        imagegen=False,
    )
    ctx = _ctx(tmp_path)

    original_user_dir = forge_module._USER_MODULE_DIR
    forge_module._USER_MODULE_DIR = tmp_path
    try:
        result = await generate_and_install_pack_module(
            services, ctx, "a marsh mystery", companion=["rulepacks"]
        )
        assert result.ok, result.error
        # The failure is surfaced in detail, not silently swallowed.
        assert "rulepack" in result.detail
        assert "could not be generated" in result.detail
    finally:
        forge_module._USER_MODULE_DIR = original_user_dir


async def test_rulepack_messages_inject_extends_instruction(tmp_path: Path) -> None:
    """`_build_rulepack_messages` with an `extends_base` appends the extends PATCH instruction to
    the system prompt (and keeps the base id out when none is requested), so a generated module
    rulepack can `extends: coc7` instead of silently replacing the base system."""
    services = _option_services(tmp_path, [], imagegen=False)

    base = forge_module._build_rulepack_messages(services, "a mystery", extends_base="")
    assert len(base) == 2
    assert base[0]["role"] == "system"
    assert "extends:" not in base[0]["content"]

    patched = forge_module._build_rulepack_messages(services, "a mystery", extends_base="coc7")
    assert "extends: coc7" in patched[0]["content"]
    assert "coc7" in patched[0]["content"]
    assert patched[1]["content"] == "a mystery"


def test_repair_skill_frontmatter_appends_missing_closing_fence():
    """A SKILL.md that opens frontmatter with `---` but forgets the closing fence is repaired by
    appending it (then still parsed strictly); already-closed or non-frontmatter content is
    untouched."""
    from agent.forge import _repair_skill_frontmatter

    # Missing closing fence: repaired.
    text = "---\nname: My Skill\ndescription: Test\n\nBody text"
    repaired = _repair_skill_frontmatter(text)
    assert repaired.count("---") == 2
    assert repaired.rstrip().endswith("---")

    # Already closed: untouched.
    closed = "---\nname: X\n---\nBody"
    assert _repair_skill_frontmatter(closed) == closed

    # No leading fence (not a frontmatter doc): untouched.
    plain = "Just a body without frontmatter"
    assert _repair_skill_frontmatter(plain) == plain
