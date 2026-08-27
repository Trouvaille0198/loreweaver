from __future__ import annotations

import base64
import json
import struct
import zlib
from types import SimpleNamespace
from typing import Any

import pytest

from agent.char_from_persona import build_sheet_from_description, build_sheet_from_persona, infer_pronoun_note
from core.character_manager import CharacterManager
from core.charcard import parse_card_bytes
from core.dice_engine import seed_dice
from infra.llm import FakeLLM, assistant_text
from infra.store import Store


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    head = struct.pack(">I", len(payload)) + kind + payload
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return head + struct.pack(">I", crc)


def _v2_png_card() -> bytes:
    raw = {
        "spec": "chara_card_v2",
        "data": {
            "name": "Ada",
            "description": "A scholar of forbidden lore",
            "character_book": {"entries": [{"keys": ["arkham"], "content": "A cursed town"}]},
        },
    }
    encoded = base64.b64encode(json.dumps(raw).encode("utf-8"))
    text = b"chara\x00" + encoded
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"tEXt", text) + _png_chunk(b"IEND", b"")


def test_infer_pronoun_note_reads_gender_from_prose_and_stays_silent_when_unclear():
    # CJK 他/她 (singular) and English he/she drive a deterministic, dominant-marker choice.
    assert infer_pronoun_note("他穿一件灰布长衫，他自己从不细说。") == "he/him"
    assert infer_pronoun_note("她是一位民俗学者，她记录乡野怪谈。") == "she/her"
    assert infer_pronoun_note("He tips his hat and grins to himself.") == "he/him"
    assert infer_pronoun_note("She adjusts her glasses and frowns.") == "she/her"
    # No clear signal -> "" (never a coin-flip guess); the plural 们 forms carry no gender.
    assert infer_pronoun_note("The scholar records local legends.") == ""
    assert infer_pronoun_note("他们一起上路，谁也不说话。") == ""
    assert infer_pronoun_note("") == ""


def test_parse_sillytavern_v2_png_and_v1_json():
    card = parse_card_bytes(_v2_png_card(), filename="ada.png")

    assert card.name == "Ada"
    assert card.description == "A scholar of forbidden lore"
    assert len(card.character_book) == 1
    assert card.character_book[0]["keys"] == ["arkham"]

    v1 = parse_card_bytes(json.dumps({"name": "Bert", "description": "A valet"}).encode(), filename="bert.json")
    assert v1.name == "Bert"
    assert v1.description == "A valet"


@pytest.mark.asyncio
async def test_build_sheet_from_persona_coc7_is_rule_legal_and_biased():
    seed_dice(2026)
    manager = CharacterManager(Store(":memory:"))
    llm = FakeLLM(
        script=[
            assistant_text(
                json.dumps(
                    {
                        "occupation": "Professor",
                        "attribute_emphasis": ["INT", "EDU"],
                        "signature_skills": ["Library Use", "Occult"],
                        "backstory": "A professor chasing forbidden marginalia.",
                    }
                )
            )
        ]
    )
    services = SimpleNamespace(characters=manager, llm=llm)
    card = parse_card_bytes(
        json.dumps({"name": "Ada", "description": "A scholar of forbidden lore"}).encode(),
        filename="ada.json",
    )

    sheet = await build_sheet_from_persona(services, card, "coc7")

    assert sheet.name == "Ada"
    assert sheet.system == "coc7"
    assert sheet.occupation == "Professor"

    rolled_attrs = ["STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU", "LUC"]
    for attr in rolled_attrs:
        low = 40 if attr in {"SIZ", "INT", "EDU"} else 15
        assert low <= sheet.attributes[attr] <= 90

    # Emphasis places INT/EDU at the top of their OWN rolled group -- SIZ/INT/EDU
    # share one roll/min/max in coc7's creation_constraints and are redistributed
    # as a unit, so this is a structural guarantee of the algorithm, not a
    # coincidence of the seed or of the pack's attribute declaration order.
    high_min_group = sorted(sheet.attributes[attr] for attr in ("SIZ", "INT", "EDU"))
    assert sheet.attributes["INT"] == high_min_group[-1]
    assert sheet.attributes["EDU"] == high_min_group[-2]
    assert sheet.skills["图书馆"] >= 60
    assert sheet.skills["神秘学"] >= 60
    assert sheet.attributes["SAN"] == sheet.attributes["POW"]
    assert sheet.attributes["IDEA"] == sheet.attributes["INT"]


@pytest.mark.asyncio
async def test_build_sheet_from_description_wraps_text_as_minimal_persona_card():
    seed_dice(2027)
    manager = CharacterManager(Store(":memory:"))
    llm = FakeLLM(
        script=[
            assistant_text(
                json.dumps(
                    {
                        "class": "Rogue",
                        "attribute_emphasis": ["DEX", "INT"],
                        "signature_skills": ["Stealth"],
                        "backstory": "A streetwise courier with too many secrets.",
                    }
                )
            )
        ]
    )
    services = SimpleNamespace(characters=manager, llm=llm)
    description = "She is a careful rooftop courier who survives by stealth and quick study."

    sheet = await build_sheet_from_description(services, description, "dnd5e", name="Mira")

    assert sheet.name == "Mira"
    assert sheet.system == "dnd5e"
    assert sheet.character_class == "rogue"  # normalized to the pack's class id
    assert sheet.attributes["DEX"] == 15
    assert sheet.attributes["INT"] == 14
    assert sheet.background == "A streetwise courier with too many secrets."
    assert description in sheet.notes

@pytest.mark.asyncio
async def test_pregen_creation_places_the_pack_standard_array_deterministically():
    """A shipped module pregen is an author-fixed sheet, not a dice roll: with
    `creation="pregen"` on a pack declaring a standard array (coc7), two builds
    come out byte-identical WITHOUT seeding the dice, and the concept's emphasis
    lands the array's top values. The player's rolled default is untouched (the
    coc7 bias test above still passes on the same pack)."""
    concept = json.dumps(
        {
            "occupation": "Professor",
            "attribute_emphasis": ["INT", "EDU"],
            "signature_skills": ["Library Use"],
            "backstory": "A professor chasing forbidden marginalia.",
        }
    )
    # No seed_dice on purpose: whatever the rolls land on, the array overwrites
    # every attribute, so the final sheet must not depend on them.
    manager = CharacterManager(Store(":memory:"))
    llm = FakeLLM(script=[assistant_text(concept), assistant_text(concept)])
    services = SimpleNamespace(characters=manager, llm=llm)
    card = parse_card_bytes(
        json.dumps({"name": "Ada", "description": "A scholar of forbidden lore"}).encode(),
        filename="ada.json",
    )

    first = await build_sheet_from_persona(services, card, "coc7", creation="pregen")
    second = await build_sheet_from_persona(services, card, "coc7", creation="pregen")

    assert first.attributes == second.attributes
    assert first.attributes["INT"] == 80
    assert first.attributes["EDU"] == 70
    assert first.occupation == "Professor"
    assert first.skills["图书馆"] >= 60
    # Every placed value sits inside its attribute's declared creation range.
    for attr, value in first.attributes.items():
        if attr in {"STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU", "LUC"}:
            low = 40 if attr in {"SIZ", "INT", "EDU"} else 15
            assert low <= value <= 90


@pytest.mark.asyncio
async def test_pregen_creation_stays_deterministic_when_the_concept_call_fails():
    """An LLM outage mid-forge must not silently downgrade a pregen to raw dice:
    the concept-less path still places the standard array (default archetype)."""
    manager = CharacterManager(Store(":memory:"))
    services = SimpleNamespace(characters=manager, llm=None)
    card = parse_card_bytes(
        json.dumps({"name": "Ada", "description": "A scholar of forbidden lore"}).encode(),
        filename="ada.json",
    )

    first = await build_sheet_from_persona(services, card, "coc7", creation="pregen")
    second = await build_sheet_from_persona(services, card, "coc7", creation="pregen")

    assert first.attributes == second.attributes
    # Default archetype (investigator) order: INT, POW, DEX, EDU, CON, APP, STR, SIZ, LUC.
    assert first.attributes["INT"] == 80
    assert first.attributes["LUC"] == 40

_PREGEN_ARRAY_ATTRS = ("STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU", "LUC")


async def _pregen_sheet(concept: dict[str, Any], system: str = "coc7") -> Any:
    manager = CharacterManager(Store(":memory:"))
    llm = FakeLLM(script=[assistant_text(json.dumps(concept))])
    services = SimpleNamespace(characters=manager, llm=llm)
    card = parse_card_bytes(
        json.dumps({"name": "Ada", "description": "A scholar of forbidden lore"}).encode(),
        filename="ada.json",
    )
    return await build_sheet_from_persona(services, card, system, creation="pregen")


@pytest.mark.asyncio
async def test_pregen_attribute_tweaks_shift_values_zero_sum():
    """A valid tweak set moves points between attributes without changing the total:
    the array's budget (510 for coc7) is an invariant, the persona decides the shape."""
    # emphasis STR first against the default investigator archetype places STR=80;
    # the tweak then trades 10 APP for 10 STR.
    sheet = await _pregen_sheet(
        {"occupation": "Brawler", "attribute_emphasis": ["STR"], "attribute_tweaks": {"STR": 10, "APP": -10}}
    )

    assert sheet.attributes["STR"] == 90
    assert sheet.attributes["APP"] == 40
    assert sum(sheet.attributes[attr] for attr in _PREGEN_ARRAY_ATTRS) == 510


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tweaks",
    [
        {"STR": 10},  # not zero-sum
        {"STR": 7, "APP": -7},  # off the tweak_step grid
        {"STR": 25, "APP": -25},  # beyond tweak_max
        {"STR": 10, "APP": -10, "XXX": 0},  # unknown attribute
        {"STR": 15, "APP": -15},  # legal deltas, but STR 80+15 would exceed max 90
    ],
)
async def test_pregen_attribute_tweaks_violations_discard_the_whole_set(tweaks: dict[str, int]):
    """Any violation drops the ENTIRE tweak set -- never a partial application."""
    sheet = await _pregen_sheet({"occupation": "Brawler", "attribute_emphasis": ["STR"], "attribute_tweaks": tweaks})

    assert sheet.attributes["STR"] == 80
    assert sheet.attributes["APP"] == 50


@pytest.mark.asyncio
async def test_attribute_tweaks_do_not_leak_into_rolled_creation():
    """Player-facing rolled creation ignores tweaks entirely: same seed, tweak or not,
    the rolled redistribution comes out identical."""
    base_concept = {"occupation": "Professor", "attribute_emphasis": ["INT", "EDU"]}

    seed_dice(2041)
    clean = await build_sheet_from_persona(
        SimpleNamespace(characters=CharacterManager(Store(":memory:")), llm=FakeLLM(script=[assistant_text(json.dumps(base_concept))])),
        parse_card_bytes(json.dumps({"name": "Ada", "description": "scholar"}).encode(), filename="ada.json"),
        "coc7",
    )
    seed_dice(2041)
    tweaked = await build_sheet_from_persona(
        SimpleNamespace(
            characters=CharacterManager(Store(":memory:")),
            llm=FakeLLM(script=[assistant_text(json.dumps({**base_concept, "attribute_tweaks": {"INT": 10, "EDU": -10}}))]),
        ),
        parse_card_bytes(json.dumps({"name": "Ada", "description": "scholar"}).encode(), filename="ada.json"),
        "coc7",
    )

    assert tweaked.attributes == clean.attributes


@pytest.mark.asyncio
async def test_attribute_tweaks_require_a_pack_tweak_policy():
    """dnd5e declares a standard array but no tweak policy: array placement still
    happens, tweaks are ignored outright."""
    sheet = await _pregen_sheet(
        {
            "class": "Rogue",
            "attribute_emphasis": ["DEX", "INT"],
            "attribute_tweaks": {"DEX": 5, "INT": -5},
        },
        system="dnd5e",
    )

    assert sheet.attributes["DEX"] == 15
    assert sheet.attributes["INT"] == 14

@pytest.mark.asyncio
async def test_pregen_skill_allocations_apply_within_budget():
    """Concept-proposed skill targets land verbatim when they fit the sheet's real
    budget (scholar placement INT=80/EDU=70 -> 智力*2 + 教育*4 = 440)."""
    sheet = await _pregen_sheet(
        {
            "occupation": "Professor",
            "attribute_emphasis": ["INT", "EDU"],
            "skill_allocations": {"图书馆使用": 70, "侦查": 65},
        }
    )

    assert sheet.skills["图书馆"] == 70  # alias resolved, base 20
    assert sheet.skills["侦查"] == 65  # base 25


@pytest.mark.asyncio
async def test_pregen_skill_allocations_scale_down_to_the_sheet_budget():
    """An over-budget proposal is scaled down proportionally, never rejected: the
    final sheet's total spend above base stays within its REAL budget."""
    from core.character_rules import skill_point_budget
    from core.rulepacks import load_rulepack

    allocations = {name: 90 for name in ("斗殴", "侦查", "聆听", "图书馆", "潜行", "急救", "攀爬", "游泳")}
    sheet = await _pregen_sheet(
        {"occupation": "Brawler", "attribute_emphasis": ["STR", "CON"], "skill_allocations": allocations}
    )

    pack = load_rulepack("coc7")
    base = {str(k): int(v) for k, v in (pack.sheet_spec.skills or {}).items()}
    spent = sum(
        max(0, int(sheet.skills[skill]) - base.get(skill, 0))
        for skill in sheet.skills
        if skill in base and skill not in set(pack.sheet_spec.derived_skills)
    )
    budget = skill_point_budget(sheet, pack)
    assert budget is not None
    assert spent <= budget
    # Scaling really happened (raw proposal was ~530 points over base): every
    # allocated skill moved off the raw 90, and the spread stays tight (the
    # above-base portions scaled by one shared factor; only base values differ).
    values = [sheet.skills[name] for name in allocations]
    assert all(value < 90 for value in values)
    assert max(values) - min(values) <= 3


@pytest.mark.asyncio
async def test_pregen_skill_allocations_drop_unknown_skill_names():
    """Unresolvable skill names are dropped silently; resolvable ones still land."""
    sheet = await _pregen_sheet(
        {
            "occupation": "Professor",
            "attribute_emphasis": ["INT", "EDU"],
            "skill_allocations": {"不存在的技能": 80, "侦查": 60},
        }
    )

    assert "不存在的技能" not in sheet.skills
    assert sheet.skills["侦查"] == 60


@pytest.mark.asyncio
async def test_skill_allocations_require_a_declared_budget():
    """dnd5e declares no skill-point budget: allocations are ignored outright —
    the prompt never advertises the field there either."""
    sheet = await _pregen_sheet(
        {"class": "Rogue", "attribute_emphasis": ["DEX", "INT"], "skill_allocations": {"Stealth": 15}},
        system="dnd5e",
    )
    assert sheet.skills.get("Stealth", 0) == 0


@pytest.mark.asyncio
async def test_concept_prompt_advertises_the_skill_budget_rules():
    """The concept call hears the allocation contract up front (value range plus
    the nominal budget), so the model's proposal lands inside what the engine
    will enforce — the 'tell the LLM the rules' half of the lane."""
    concept = json.dumps({"occupation": "Professor", "attribute_emphasis": ["INT"]})
    manager = CharacterManager(Store(":memory:"))
    llm = FakeLLM(script=[assistant_text(concept)])
    services = SimpleNamespace(characters=manager, llm=llm)
    card = parse_card_bytes(
        json.dumps({"name": "Ada", "description": "A scholar of forbidden lore"}).encode(),
        filename="ada.json",
    )

    await build_sheet_from_persona(services, card, "coc7", creation="pregen")

    system_prompt = llm.calls[0][0][0]["content"]
    assert "skill_allocations" in system_prompt
    assert "300" in system_prompt  # nominal coc7 budget at default attributes
    assert "{skill_rules}" not in system_prompt
    assert "{system}" not in system_prompt  # the template actually rendered


@pytest.mark.asyncio
async def test_dnd_creation_chain_fills_class_slots_and_known_spells() -> None:
    """The full D&D creation chain, locked end-to-end: the AI concept's class
    lands on the sheet, the level table fills spell slots, the class spellbook
    seeds known spells, and the AI toolset carries every mechanics tool — so a
    freshly forged D&D character always has a class, slots and spells."""
    from agent.kp_tools import build_kp_toolset
    from core.resources import resource_values
    from core.rulepacks import load_rulepack

    concept = json.dumps(
        {
            "class": "wizard",
            "attribute_emphasis": ["INT", "CON", "DEX"],
            "signature_skills": ["Arcana"],
            "backstory": "An academy-trained wizard.",
        }
    )
    manager = CharacterManager(Store(":memory:"))
    llm = FakeLLM(script=[assistant_text(concept)])
    services = SimpleNamespace(characters=manager, llm=llm)
    card = parse_card_bytes(
        json.dumps({"name": "Mage", "description": "an apprentice wizard"}).encode(),
        filename="mage.json",
    )

    sheet = await build_sheet_from_persona(services, card, "dnd5e")

    # 1. The concept's class lands on the sheet's identity field.
    assert sheet.character_class == "wizard"
    # 2. Spell slots follow the full-caster level table (level 1 → 2 slots).
    values = resource_values(sheet, load_rulepack("dnd5e"))
    assert values["spell_slot_1"].maximum == 2
    assert values["spell_slot_1"].current == 2  # topped like after a long rest
    # 3. The class spellbook seeds known spells.
    assert "magic_missile" in sheet.known_spells
    assert "fire_bolt" in sheet.known_spells
    # 4. The AI keeper toolset exposes every mechanics lane (no more narrating
    #    without resolving: cast/rest/attack/advance/resource/spells).
    toolset = build_kp_toolset(services)
    names = set(toolset.names())
    for tool in ("cast_spell", "rest_manager", "attack_target", "advance_level", "manage_resource", "manage_spells"):
        assert tool in names, f"AI toolset missing {tool}"
