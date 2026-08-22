"""Offline CLI demo Keeper."""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path

from gateway.demo import is_demo_setup_request, is_guided_demo_request
from infra.i18n import t
from infra.llm import ToolCall, assistant_text, assistant_tools

DEMO_SENTINEL = "THE LIGHTHOUSE KEEPER IS THE MURDERER"
DEMO_MODULE_PATH = Path(__file__).with_name("demo_module_en.txt")
DEMO_MODULE_TEXT = DEMO_MODULE_PATH.read_text(encoding="utf-8")

_CALL_IDS = count(1)


def demo_kp_responder(messages, tools):
    if messages and messages[0].get("role") == "user" and "system" not in {item.get("role") for item in messages}:
        prompt = str(messages[0].get("content") or "")
        locale = "zh" if "所有自然语言字段" in prompt else "en"
        if DEMO_MODULE_TEXT not in prompt:
            # The offline responder only knows the built-in sample adventure.
            # Returning non-JSON for every other source lets ModuleInitializer's
            # source-preserving heuristic take over instead of inventing the
            # sample plot for a user's module.
            return assistant_text("offline module analysis unavailable")
        return assistant_text(json.dumps(_demo_analysis(locale), ensure_ascii=False))

    last_user = max((index for index, item in enumerate(messages) if item.get("role") == "user"), default=0)
    user_text = str(messages[last_user].get("content", "")).lower()
    called = _tools_called_this_turn(messages)

    # The TUI's first-run button sends a normal, localized player action. Treat
    # it as one complete guided transaction so the user never has to guess the
    # old upload -> start keyword sequence: setup first, inspect the freshly
    # installed module next, then narrate the opening in the same turn.
    if is_guided_demo_request(user_text):
        if "upload_document" not in called:
            return assistant_tools(
                _tool("upload_document", {"file_path": str(DEMO_MODULE_PATH), "doc_type": "module"}),
                _tool("create_character", {"name": "Nora Vance", "system": "coc7"}),
                _tool("update_character_skill", {"skill_name": "Spot Hidden", "value": 65}),
                _tool("start_session_recording", {"session_name": "The Blackmoor Lighthouse"}),
            )
        if "get_module_summary" not in called:
            return assistant_tools(_tool("get_module_summary", {}))
        return assistant_text(t("cli.demo.opening"))

    if is_demo_setup_request(user_text):
        if "upload_document" not in called:
            return assistant_tools(
                _tool("upload_document", {"file_path": str(DEMO_MODULE_PATH), "doc_type": "module"}),
                _tool("create_character", {"name": "Nora Vance", "system": "coc7"}),
                _tool("update_character_skill", {"skill_name": "Spot Hidden", "value": 65}),
                _tool("start_session_recording", {"session_name": "The Blackmoor Lighthouse"}),
            )
        return assistant_text(t("cli.demo.upload_ready"))

    if "begin" in user_text or "start" in user_text:
        if "get_module_summary" not in called:
            return assistant_tools(_tool("get_module_summary", {}))
        return assistant_text(t("cli.demo.opening"))

    if (
        "search" in user_text
        or "desk" in user_text
        or "look" in user_text
        or "搜索" in user_text
        or "搜查" in user_text
        or "查看" in user_text
    ):
        if "skill_check" not in called:
            return assistant_tools(_tool("skill_check", {"skill_name": "Spot Hidden"}))
        result = _last_tool_result(messages).lower()
        key = "cli.demo.search_miss" if ("fail" in result or "fumble" in result) else "cli.demo.search_hit"
        return assistant_text(t(key))

    if "report" in user_text:
        if "generate_session_report" not in called:
            return assistant_tools(_tool("generate_session_report", {}))
        return assistant_text(_last_tool_result(messages))

    return assistant_text(t("cli.demo.fallback"))


def _tools_called_this_turn(messages: list[dict]) -> list[str]:
    last_user = max((index for index, item in enumerate(messages) if item.get("role") == "user"), default=0)
    called: list[str] = []
    for item in messages[last_user + 1 :]:
        if item.get("role") == "assistant" and item.get("tool_calls"):
            called.extend(call["function"]["name"] for call in item["tool_calls"])
    return called


def _last_tool_result(messages: list[dict]) -> str:
    for item in reversed(messages):
        if item.get("role") == "tool":
            return str(item.get("content", ""))
    return ""


def _demo_analysis(locale: str = "en") -> dict:
    zh = locale == "zh"
    sentinel = "灯塔看守人就是凶手" if zh else DEMO_SENTINEL
    return {
        "summary": t("cli.demo.analysis.summary", locale=locale),
        "background": t("cli.demo.analysis.background", locale=locale),
        "scenes": [
            {
                "name": "盐锚旅店" if zh else "The Salt & Anchor Inn",
                "focus": "调查" if zh else "investigation",
                "description": t("cli.demo.analysis.scene.inn.description", locale=locale),
                "keeper_notes": t("cli.demo.analysis.scene.inn.keeper_notes", locale=locale),
                "npcs_present": ["玛莎" if zh else "Martha"],
                "clues": [
                    {
                        "name": "潮汐表" if zh else "Tide table",
                        "description": t("cli.demo.analysis.clue.tide_table.description", locale=locale),
                        "discovery_method": "侦查" if zh else "Spot Hidden",
                    }
                ],
            }
        ],
        "npcs": [
            {
                "name": "玛莎" if zh else "Martha",
                "description": t("cli.demo.analysis.npc.martha.description", locale=locale),
                "secret": f"{t('cli.demo.analysis.npc.martha.secret', locale=locale)} {sentinel}",
                "role": "旅店老板" if zh else "innkeeper",
            },
            {
                "name": "伊莱亚斯·克兰" if zh else "Elias Crane",
                "description": t("cli.demo.analysis.npc.elias.description", locale=locale),
                "secret": t("cli.demo.analysis.npc.elias.secret", locale=locale),
                "role": "对立者" if zh else "antagonist",
            },
        ],
        "clues": [
            {
                "name": "潮汐表" if zh else "Tide table",
                "description": t("cli.demo.analysis.clue.tide_table.match_description", locale=locale),
                "location": "旅店" if zh else "inn",
                "leads_to": "灯塔" if zh else "lighthouse",
            }
        ],
        "timeline": [
            {
                "time": "第一夜" if zh else "Night 1",
                "event": t("cli.demo.analysis.timeline.night1", locale=locale),
                "involved": ["伊莱亚斯·克兰" if zh else "Elias"],
            }
        ],
        "threats": [
            {
                "name": "深潜者奴仆" if zh else "Deep One thrall",
                "type": "怪物" if zh else "monster",
                "description": t("cli.demo.analysis.threat.deep_one.description", locale=locale),
                "stats": {"HP": "13", "STR": "80"},
                "attacks": ["爪击 1d6+db" if zh else "claw 1d6+db"],
                "san_loss": "1/1d8",
                "special_abilities": "拖入水下" if zh else "drag underwater",
                "location": "灯塔" if zh else "lighthouse",
            }
        ],
        "truths": [
            {
                "name": "灯光的真相" if zh else "The Truth of the Light",
                "description": t("cli.demo.analysis.truth.light.description", locale=locale, sentinel=sentinel),
                "revealed_by": t("cli.demo.analysis.truth.light.revealed_by", locale=locale),
            }
        ],
        "opening_facts": [
            t("cli.demo.analysis.opening_fact.sailors", locale=locale),
            t("cli.demo.analysis.opening_fact.light", locale=locale),
        ],
    }


def _tool(name: str, arguments: dict) -> ToolCall:
    return ToolCall(id=f"demo_call_{next(_CALL_IDS)}", name=name, arguments=arguments)
