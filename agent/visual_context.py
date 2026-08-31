"""Public module-world context used by image prompt assembly.

The image provider must receive the module's setting as an explicit visual
constraint.  This module deliberately accepts only an explicit ``visual_world``
field (plus a few documented aliases); it never derives context from keeper-only
story prose, so adding visual continuity cannot leak a module secret.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_MAX_CONTEXT_CHARS = 1200
_CONTEXT_KEYS = (
    "visual_world",
    "worldview",
    "world_setting",
    "setting",
)
_STRUCTURED_KEYS = (
    "world",
    "setting",
    "system",
    "era",
    "region",
    "culture",
    "visual_style",
    "tone",
)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _value_text(value: Any) -> str:
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key in _STRUCTURED_KEYS:
            item = _text(value.get(key))
            if item:
                parts.append(f"{key}: {item}")
        if not parts:
            parts = [_text(item) for item in value.values()]
        return "; ".join(item for item in parts if item)
    if isinstance(value, (list, tuple)):
        return "; ".join(item for item in (_value_text(entry) for entry in value) if item)
    return _text(value)


def visual_world_text(source: Mapping[str, Any] | None) -> str:
    """Return the bounded, explicit public visual-world description in ``source``."""
    if not isinstance(source, Mapping):
        return ""
    for key in _CONTEXT_KEYS:
        value = _value_text(source.get(key))
        if value:
            return value[:_MAX_CONTEXT_CHARS]
    return ""


def visual_context_block(source: Mapping[str, Any] | None, *, locale: str = "zh") -> str:
    """Build the instruction appended to a provider-facing image prompt.

    The declared worldview is treated as the visual source of truth. When a module has no
    named setting, the prompt still asks the provider to stay consistent with the supplied
    era, region, culture, and visual direction instead of inventing unrelated context.
    """
    context = visual_world_text(source)
    english = str(locale).casefold().startswith("en")
    if english:
        label = "WORLDVIEW AND VISUAL ANCHOR"
        absent = "No named setting was supplied; follow the era, region, culture, and visual direction stated in the prompt."
        guard = "Treat the declared worldview and visual anchor as binding across people, clothing, architecture, props, era, and overall art direction."
    else:
        label = "世界观与视觉锚点"
        absent = "模组没有提供明确的世界观专名；请遵循提示词中已有的时代、地域、文化与美术方向。"
        guard = "上述世界观与视觉锚点是硬约束，请统一落实到人物、服饰、建筑、道具、时代和整体美术方向。"
    setting = context or absent
    return f"{label}（必须遵守）：{setting}\n{guard}"


def append_visual_context(
    prompt: str,
    source: Mapping[str, Any] | None,
    *,
    locale: str = "zh",
) -> str:
    """Append one canonical visual-context block to a provider prompt."""
    text = str(prompt or "").strip()
    block = visual_context_block(source, locale=locale)
    return f"{text}\n\n{block}" if text else block
