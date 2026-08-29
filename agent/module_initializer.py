"""Module (adventure/scenario) initialization: full-text LLM analysis into
keeper/player knowledge pools.

Lives in ``agent/`` (moved from ``core/`` 2026-08-19): it holds an ``LLMClient`` and
the analysis IS a model call — generative work, which iron rule #1 keeps out of the
deterministic ``core/``.

Ported from ``nekro_trpg_dice_plugin``'s ``core/module_initializer.py`` per
the M1 spec (``docs/specs/M1.md`` §5). The module's full text is handed to
the LLM in one shot (vector-store chunking is for retrieval only —
initialization always reassembles the full text first), parsed into a
structured analysis, and split into a keeper-only knowledge pool (full
secrets: NPC ``secret``, scene ``keeper_notes``, ``truths``, ``threats``,
``timeline``) and a player-safe knowledge pool (spoiler-free subset). Only
two things differ from the source:

- ``gen_openai_chat_response`` (a nekro-framework global) is replaced by the
  injected ``infra.llm.LLMClient``'s ``chat()``, called with
  ``temperature=0.3`` and ``model=settings.llm.analysis_model or
  settings.llm.chat_model``;
- the analysis prompt's *framing* text is localized (``module.analysis_prompt``,
  via the injected ``infra.i18n.I18n``) while the JSON schema it instructs the
  model to emit (``_ANALYSIS_JSON_SCHEMA`` below) is a fixed contract, not
  re-localized per locale — the model must always emit these exact field
  names regardless of the operator's chosen locale.

``_build_knowledge_pools`` keeps the source data shapes and literal Chinese
defaults (for example ``"探索"`` / ``"场景{i+1}"``). Player knowledge follows
an explicit discovery contract: only the opening scene identity is seeded;
scene detail, NPCs, clues, background and conclusions must be unlocked during
play. When no usable model
response is available, the fallback also extracts the common Markdown
headings and lists used by module authors, so an offline import still has a
useful NPC/clue/truth/timeline catalog instead of only a paragraph summary.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agent.items import ensure_catalog, normalize_item_links
from core.battle_report import BattleReportManager
from core.documents import DocumentStore
from infra.config import Settings
from infra.i18n import I18n
from infra.llm import LLMClient
from infra.model_call_trace import lane_scope
from infra.room_facets import STORAGE_DOCUMENTS, STORAGE_ROOM_STATE, RoomStateFacet
from infra.store import Store
from infra.usage_stats import record_usage_stats

logger = logging.getLogger(__name__)

# An optional progress reporter the gateway may pass in to surface import STAGES to
# the room while a slow full-module analysis runs. Core only SIGNALS a stage id (+
# an opaque detail string); the gateway formats + publishes it. Best-effort: a
# progress hiccup must never fail the import itself.
ProgressCb = Callable[[str, str], Awaitable[None]] | None


@dataclass(frozen=True)
class _AnalysisOutcome:
    analysis: dict
    used_fallback: bool = False
    error_summary: str = ""


def _exception_summary(exc: Exception) -> str:
    """Return a bounded diagnostic suitable for the module-init sidecar key."""
    message = str(exc).strip()
    summary = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return summary[:1000]


async def _emit(progress: ProgressCb, stage: str, detail: str = "") -> None:
    if progress is None:
        return
    try:
        await progress(stage, detail)
    except Exception:
        pass

# Fields every analysis dict (LLM-produced or fallback) is normalized to
# carry, per the M1 spec's data shape: scenes/npcs/clues/timeline/background/
# threats/truths/opening_facts/summary.
_LIST_FIELDS = ("scenes", "npcs", "clues", "items", "timeline", "threats", "truths", "opening_facts")
_STR_FIELDS = ("background", "summary")

# Cheap safety cap on how much module text gets sent to the LLM in one prompt
# (~1 token/CJK-char, generous margin for the framing text + a large-context
# analysis model). Not part of the source's data shape, just an input guard.
_MAX_ANALYSIS_CHARS = 400_000

_CODE_FENCE_PREFIX_RE = re.compile(r"^```[a-zA-Z]*\s*")
_CODE_FENCE_SUFFIX_RE = re.compile(r"\s*```$")

# The fixed JSON-schema contract the analysis prompt instructs the model to
# emit. Deliberately NOT routed through i18n (see the module docstring): it
# is machine-format instruction, not user-visible text, and must stay
# byte-identical regardless of the operator's locale so downstream parsing
# (`_build_knowledge_pools`) can rely on fixed field names.
_ANALYSIS_JSON_SCHEMA = """{
    "scenes": [
        {
            "name": "scene name (e.g. 'Abandoned Hospital Lobby')",
            "focus": "the scene's current focus (explore/negotiate/chase/combat/horror/stealth/rest - pick the single best fit)",
            "description": "player-visible description (appearance, layout, atmosphere - no spoilers)",
            "keeper_notes": "keeper-only background (hidden rooms, traps, an NPC's true location, danger warnings, etc. - never tell players)",
            "npcs_present": ["names of NPCs present in this scene"],
            "clues": [
                {
                    "name": "clue name",
                    "description": "what the clue reveals",
                    "discovery_method": "how it is found (e.g. a Spot Hidden roll, searching the room, talking to an NPC)"
                }
            ]
        }
    ],
    "npcs": [
        {
            "name": "NPC's full name",
            "description": "outward description (appearance, clothing, mannerisms, accent - anything players can directly observe)",
            "secret": "hidden information (true motive, connection to the case, dark secret, real identity)",
            "role": "the NPC's role in the module (e.g. client, suspect, victim, antagonist)"
        }
    ],
    "clues": [
        {
            "name": "clue name",
            "description": "clue content",
            "location": "the scene or NPC where it is found",
            "leads_to": "what it points to (e.g. the next scene, a truth, an NPC)"
        }
    ],
    "items": [
        {
            "name": "item name (e.g. 'The Sunken Bell')",
            "aliases": ["common short, translated or alternate names"],
            "kind": "weapon/armor/consumable/gem/tool/quest/misc - pick the single best fit",
            "description": "short player-visible intro (what it is, how it looks)",
            "lore": "background story - ONLY for notable/powerful items, else leave empty",
            "origin": "the scene or NPC where it is found - be specific (a scene name, an NPC name); NEVER the investigators' starting gear",
            "original_holder": "who held it before, if the module states it - an NPC, never an investigator",
            "plot_role": "equipment|evidence|quest|prop - the item's role in the scenario",
            "reveals": ["stable clue ids or clue names this item makes known when obtained"]
        }
    ],
    "timeline": [
        {"time": "point in time", "event": "what happens", "involved": ["NPCs involved"]}
    ],
    "background": "the module's background story (setting, history, how the situation came about)",
    "threats": [
        {
            "name": "threat name (e.g. 'Feral Dog Pack Leader')",
            "type": "monster/NPC/environmental/trap",
            "description": "outward description (features players can see)",
            "stats": {"HP": "hit points", "STR": "strength", "CON": "constitution", "DEX": "dexterity", "SIZ": "size"},
            "attacks": ["attack forms (e.g. 'bite 1d6+db')"],
            "san_loss": "Sanity loss (e.g. '0/1d6')",
            "special_abilities": "special abilities (e.g. pack tactics, cannot be tamed)",
            "location": "where it appears"
        }
    ],
    "truths": [
        {
            "name": "truth name",
            "description": "the full behind-the-scenes truth",
            "revealed_by": "which clues/scenes reveal it"
        }
    ],
    "opening_facts": [
        "a fact the investigators already know at the start of the module",
        "another opening fact"
    ],
    "summary": "a one-sentence summary of the module (under 30 words)"
}"""


# Model-facing guidance for the `items` category, appended after the JSON schema so
# the model sees the discipline without polluting the machine-format contract. It is
# model instruction, not user-visible text — i18n-exempt like the schema itself.
_ANALYSIS_ITEMS_GUIDANCE = """
Only list items that MATTER to the module - things investigators can acquire that
carry mechanical or plot significance. Pure scene dressing (a chair, a vase) is not
an item. Include common short, translated and alternate names in 'aliases' so the
same script item cannot be mistaken for a new one. Never assign an item to a specific character: who ends up holding it is
decided in play, not by the script. Make 'origin'/'original_holder' concrete. Only
notable/powerful items get a 'lore'; ordinary items make do with 'description'. An
'plot_role' describes the item's role. Its 'reveals' list links to clue/truth names
that become known when the item is obtained; the item is NOT a clue, it is a thing
with an effect or physical presence. Items must be FINDABLE in the world: their
'origin' is a place or an NPC who holds them, waiting for the investigators to find,
loot or negotiate for them. NEVER list the investigators' own starting gear (items the
script says they begin with, '随身携带'/'自备' gear) - that is character equipment, not
module items."""  # i18n-exempt: model-facing analysis instruction


def _extract_json_object(content: str, i18n: I18n) -> dict:
    """Best-effort JSON-object extraction from a raw LLM response.

    Tolerates markdown code fences and leading/trailing commentary around
    the JSON payload: fences are stripped, then a direct `json.loads` of the
    remaining text is tried, falling back to slicing out the first ``{`` ..
    last ``}`` span and parsing that. Raises `ValueError` — caught by
    `ModuleInitializer._analyze_full_text`, which falls back to
    `_fallback_full_analysis` — if no JSON object can be recovered either way.
    """
    text = _CODE_FENCE_SUFFIX_RE.sub("", _CODE_FENCE_PREFIX_RE.sub("", content.strip())).strip()

    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ValueError(i18n.t("module.analysis_no_json_found"))


class ModuleInitializer:
    """Background LLM-driven full-text module analysis.

    Reads a module's full text (previously uploaded/stored), asks the LLM to
    structure it into scenes/NPCs/clues/timeline/background/threats/truths/
    opening_facts/summary, and splits the result into a keeper-only
    knowledge pool (full secrets) and a player-safe knowledge pool
    (spoiler-free subset) — see the module docstring for the full contract.
    """

    def __init__(
        self,
        store: Store,
        vector_db: Any,
        llm: LLMClient,
        settings: Settings,
        i18n: I18n,
        battles: BattleReportManager | None = None,
    ) -> None:
        self.store = store
        self.documents = DocumentStore(store)
        # Duck-typed: only `list_all_chunks(chat_key, limit=...)` is used
        # (shaped like `agent.document_manager.VectorDatabaseManager`), and
        # only as a fallback when no `module_fulltext.{chat_key}` is stored.
        # May be `None` if the caller never wires a vector store up.
        self.vector_db = vector_db
        self.llm = llm
        self.settings = settings
        self.i18n = i18n
        self.battles = battles

    async def initialize(
        self,
        chat_key: str,
        progress: ProgressCb = None,
        locale: str | None = None,
        llm: LLMClient | None = None,
        model: str | None = None,
        module_id: str = "",
    ) -> None:
        """Run (or skip, if already running) full-module analysis for `chat_key`.

        Orchestrates: read the stored module full text (or reassemble it
        from vector-store chunks) -> analyze (LLM, falling back to the
        offline heuristic on any failure) -> build the keeper/player
        knowledge pools -> persist them -> mark `module_init_status.{chat_key}`
        `"ready"` or `"ready_fallback"`. Sets `"failed"` instead if there is
        nothing to analyze, or if persistence itself raises. Diagnostics live
        separately under `module_init_error.{chat_key}`.
        A concurrent call while one is already `"processing"` is a no-op.

        `module_id` is the importing module's identity (source_id); the analyzed
        `items` templates are stamped with it so module-scoped gear only works
        while that module is the room's active one.
        """
        status_key = "module_init_status"
        error_key = "module_init_error"

        current_status = await self.store.state_get(chat_key, status_key)
        if current_status == "processing":
            return

        await self.store.state_set(chat_key, status_key, "processing")
        try:
            source_key = "module_fulltext"
            source_value = await self.store.state_get(chat_key, source_key)
            full_text, doc_name = await self._load_full_text(chat_key)
            if not full_text:
                await self.store.state_set(chat_key, error_key, "module text unavailable")
                await self.store.state_set(chat_key, status_key, "failed")
                return

            # A module owns one recording boundary. Archive the prior module's active session
            # before any new-module state is published; the next recorded action lazily starts a
            # clean session through BattleReportManager's normal semantics.
            if self.battles is not None:
                await self.battles.generator.end_session(chat_key)

            # An uploaded module owns its own timeline. Clear the prior module's
            # clock as soon as the new source text is confirmed, before the
            # potentially slow analysis can leave stale dates in the sidebar.
            await self.store.state_delete(chat_key, "game_clock")
            await _emit(progress, "analyze")
            outcome = await self._analyze_full_text(
                full_text,
                doc_name,
                chat_key,
                locale=locale,
                llm=llm,
                model=model,
            )
            await _emit(progress, "build")
            keeper_pool, player_pool = self._build_knowledge_pools(outcome.analysis)
            # The script's items seed the room's item catalog (Layer 0 -> Layer 1):
            # designed templates (kind/effect/origin/lore), no holders — instances are
            # created only when play actually obtains them via the grant verbs.
            # Module-scoped items (scope != "universal") are stamped with the importing
            # module's id, so a plot artifact from another module contributes nothing.
            analyzed_items = outcome.analysis.get("items") or []
            if isinstance(analyzed_items, list):
                analyzed_items = [
                    normalize_item_links(
                        {
                            **dict(tpl),
                            "scope": str(tpl.get("scope") or "module"),
                            "module_id": ""
                            if str(tpl.get("scope") or "module") == "universal"
                            else module_id,
                        }
                    )
                    if isinstance(tpl, dict)
                    else tpl
                    for tpl in analyzed_items
                ]
            await ensure_catalog(self.documents, chat_key, analyzed_items)

            if outcome.used_fallback:
                status = "ready_fallback"
            else:
                status = "ready"
            # Publish the pool document FIRST, then commit the status flip with a
            # compare-and-set on the source text + processing marker. A "ready"
            # status therefore always implies the document exists; if the CAS
            # loses (a concurrent re-upload restarted analysis), the newer run
            # owns the document and will overwrite it on its own completion.
            await self.documents.put_singleton(
                chat_key, "module_pool", {"keeper": keeper_pool, "player": player_pool}
            )
            committed = await self.store.state_set_if_values(
                chat_key,
                expected=[
                    (source_key, source_value),
                    (status_key, "processing"),
                ],
                updates=[
                    (status_key, status),
                ],
            )
            if not committed:
                return
            if outcome.used_fallback:
                await self.store.state_set(chat_key, error_key, outcome.error_summary)
            else:
                await self.store.state_delete(chat_key, error_key)
        except Exception as exc:
            summary = _exception_summary(exc)
            logger.exception("module initialization failed for chat_key=%s", chat_key)
            await self.store.state_set(chat_key, error_key, summary)
            await self.store.state_set(chat_key, status_key, "failed")

    async def _load_full_text(self, chat_key: str) -> tuple[str, str]:
        """Return `(full_text, doc_name)` for `chat_key`.

        Prefers the pre-assembled `module_fulltext.{chat_key}` store key;
        falls back to reassembling `self.vector_db`'s stored chunks (sorted
        by filename then chunk index, matching upload order) when that key
        is unset. Returns `("", "")` if neither source has anything.
        """
        stored = await self.store.state_get(chat_key, "module_fulltext")
        if stored:
            return stored, self.i18n.t("module.default_document_name")

        if self.vector_db is None:
            return "", ""

        chunks = await self.vector_db.list_all_chunks(chat_key, limit=1000)
        if not chunks:
            return "", ""

        chunks = sorted(chunks, key=lambda c: (c.get("filename", ""), c.get("chunk_index", 0)))
        full_text = "\n\n".join(c.get("text", "") for c in chunks)
        doc_name = chunks[0].get("filename") or self.i18n.t("module.default_document_name")
        return full_text, doc_name

    async def _analyze_full_text(
        self,
        full_text: str,
        doc_name: str,
        chat_key: str,
        *,
        locale: str | None = None,
        llm: LLMClient | None = None,
        model: str | None = None,
    ) -> _AnalysisOutcome:
        """Analyze with one retry, then return an explicit fallback outcome."""
        prompt = self._build_analysis_prompt(full_text, doc_name, locale=locale)
        client = llm or self.llm
        selected_model = model or self.settings.llm.analysis_model or self.settings.llm.chat_model

        last_error = ""
        for attempt in range(2):
            try:
                with lane_scope("authoring"):
                    result = await client.chat(
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.3,
                        model=selected_model,
                    )
                # A response consumed provider tokens even when its JSON is
                # malformed, so account for usage before parsing it.
                await record_usage_stats(
                    self.store,
                    chat_key,
                    result.usage,
                    model=selected_model,
                    context_window=self.settings.llm.context_window,
                )
                analysis = _extract_json_object(result.content or "", self.i18n)
                for field in _LIST_FIELDS:
                    analysis.setdefault(field, [])
                for field in _STR_FIELDS:
                    analysis.setdefault(field, "")
                return _AnalysisOutcome(analysis=analysis)
            except Exception as exc:
                last_error = _exception_summary(exc)
                logger.warning(
                    "module analysis attempt %d/2 failed for chat_key=%s: %s",
                    attempt + 1,
                    chat_key,
                    last_error,
                    exc_info=True,
                )

        return _AnalysisOutcome(
            analysis=self._fallback_full_analysis(full_text),
            used_fallback=True,
            error_summary=last_error,
        )

    def _build_analysis_prompt(self, full_text: str, doc_name: str, *, locale: str | None = None) -> str:
        """Render the full-text analysis prompt sent to the LLM: localized
        framing text (`module.analysis_prompt`) wrapping the fixed JSON
        schema contract the model must emit (`_ANALYSIS_JSON_SCHEMA`)."""
        truncated = full_text[:_MAX_ANALYSIS_CHARS]
        requested = str(locale or self.i18n.locale).replace("_", "-").split("-", 1)[0].casefold()
        prompt_i18n = self.i18n.with_locale(requested) if requested in {"en", "zh"} else self.i18n
        return prompt_i18n.t(
            "module.analysis_prompt",
            doc_name=doc_name or prompt_i18n.t("module.default_document_name"),
            full_text=truncated,
            schema=_ANALYSIS_JSON_SCHEMA + _ANALYSIS_ITEMS_GUIDANCE,
        )

    def _fallback_full_analysis(self, text: str) -> dict:
        """Build a useful local analysis when the LLM response is unusable.

        The paragraph-derived scenes preserve the original offline behavior;
        Markdown sections provide deterministic structure for the keeper
        catalog when a source contains headings such as ``NPC``/``线索``/
        ``真相``/``时间线``.
        """
        paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 50]
        scenes = []
        for i, para in enumerate(paragraphs[:20]):
            scenes.append(
                {
                    "name": f"场景{i + 1}",
                    "focus": "探索",
                    "description": para[:1200],
                    "keeper_notes": "",
                    "npcs_present": [],
                    "clues": [],
                }
            )

        npc_section = self._markdown_section(text, ("npc", "人物", "角色"))
        clue_section = self._markdown_section(text, ("线索", "clue"))
        truth_section = self._markdown_section(text, ("真相", "truth"))
        timeline_section = self._markdown_section(text, ("时间线", "时间表", "timeline"))
        threat_section = self._markdown_section(text, ("威胁", "反派", "threat"))

        npcs = []
        for heading, body in self._markdown_subsections(npc_section):
            name = self._clean_markdown(heading)
            if name:
                npcs.append(
                    {
                        "name": name,
                        "description": self._clean_markdown(body)[:3000],
                        "secret": "",
                        "role": "",
                    }
                )
        if not npcs:
            for entry in self._markdown_list_entries(npc_section):
                npcs.append({"name": entry[:80], "description": entry, "secret": "", "role": ""})

        clues = []
        for entry in self._markdown_list_entries(clue_section):
            name, description = self._split_label(entry, "线索")
            clues.append(
                {
                    "name": name,
                    "description": description,
                    "location": "",
                    "leads_to": "",
                }
            )

        item_section = self._markdown_section(text, ("物品", "item"))
        items = []
        for entry in self._markdown_list_entries(item_section):
            name, description = self._split_label(entry, "物品")
            items.append(
                {
                    "name": name,
                    "description": description,
                    "kind": "",
                    "effect": "",
                    "origin": "",
                    "original_holder": "",
                    "plot_role": "",
                    "reveals": [],
                }
            )

        truths = []
        if truth_section:
            subsections = self._markdown_subsections(truth_section)
            if subsections:
                truths = [
                    {
                        "name": self._clean_markdown(heading),
                        "description": self._clean_markdown(body)[:6000],
                        "revealed_by": "",
                    }
                    for heading, body in subsections
                    if self._clean_markdown(body)
                ]
            else:
                truths = [{"name": "模组真相", "description": self._clean_markdown(truth_section)[:6000], "revealed_by": ""}]

        timeline = []
        for entry in self._markdown_list_entries(timeline_section):
            time, event = self._split_label(entry, "时间点")
            timeline.append({"time": time, "event": event, "involved": []})

        threats = []
        threat_blocks = self._markdown_subsections(threat_section)
        if threat_blocks:
            threats = [
                {
                    "name": self._clean_markdown(heading),
                    "type": "",
                    "description": self._clean_markdown(body)[:3000],
                    "location": "",
                }
                for heading, body in threat_blocks
                if self._clean_markdown(body)
            ]
        elif threat_section:
            threats = [{"name": "模组威胁", "type": "", "description": self._clean_markdown(threat_section)[:3000], "location": ""}]

        return {
            "scenes": scenes,
            "npcs": npcs,
            "clues": clues,
            "items": items,
            "timeline": timeline,
            "background": text[:4000] if len(text) > 4000 else text,
            "threats": threats,
            "truths": truths,
            "summary": text[:600] if len(text) > 600 else text,
        }

    @staticmethod
    def _markdown_section(text: str, patterns: tuple[str, ...]) -> str:
        """Return the body of the first matching Markdown heading section."""
        lines = text.splitlines()
        heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
        match_index = -1
        level = 0
        for index, line in enumerate(lines):
            match = heading_re.match(line.strip())
            if not match:
                continue
            heading = re.sub(r"[*`_]", "", match.group(2)).casefold()
            if any(pattern.casefold() in heading for pattern in patterns):
                match_index = index
                level = len(match.group(1))
                break
        if match_index < 0:
            return ""
        end = len(lines)
        for index in range(match_index + 1, len(lines)):
            match = heading_re.match(lines[index].strip())
            if match and len(match.group(1)) <= level:
                end = index
                break
        return "\n".join(lines[match_index + 1 : end]).strip()

    @staticmethod
    def _markdown_subsections(text: str) -> list[tuple[str, str]]:
        """Extract child heading blocks from a Markdown section body."""
        if not text:
            return []
        lines = text.splitlines()
        heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
        headings = [(index, len(match.group(1)), match.group(2)) for index, line in enumerate(lines) if (match := heading_re.match(line.strip()))]
        blocks = []
        for position, (start, level, heading) in enumerate(headings):
            end = len(lines)
            for next_start, next_level, _ in headings[position + 1 :]:
                if next_level <= level:
                    end = next_start
                    break
            body = "\n".join(lines[start + 1 : end]).strip()
            if body:
                blocks.append((heading, body))
        return blocks

    @staticmethod
    def _markdown_list_entries(text: str) -> list[str]:
        if not text:
            return []
        entries = []
        for line in text.splitlines():
            match = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$", line)
            if match:
                value = ModuleInitializer._clean_markdown(match.group(1))
                if value:
                    entries.append(value)
        return entries

    @staticmethod
    def _clean_markdown(value: str) -> str:
        value = re.sub(r"[*`_]", "", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    @classmethod
    def _split_label(cls, value: str, default: str) -> tuple[str, str]:
        cleaned = cls._clean_markdown(value)
        clock = re.match(r"^(\d{1,2}:\d{2})\s+(.+)$", cleaned)
        if clock:
            return clock.group(1), clock.group(2).strip()
        match = re.match(r"^([^：:]{1,80})[：:]\s*(.+)$", cleaned)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        return default, cleaned

    def _build_knowledge_pools(self, analysis: dict) -> tuple[dict, dict]:
        """Split `analysis` into `(keeper_pool, player_pool)`.

        Keeper content is complete. Player content starts with only the first
        scene's identity so the room has an opening focus; every descriptive or
        revelatory element is admitted explicitly through `unlock_for_player`.
        """
        keeper_pool: dict[str, Any] = {
            "scenes": [],
            "npcs": [],
            "clues": [],
            "truths": [],
            "timeline": [],
            "background": analysis.get("background", ""),
            "summary": analysis.get("summary", ""),
        }
        player_pool: dict[str, Any] = {
            "scenes": [],
            "npcs": [],
            "clues": [],
            "background": "",
            "summary": "",
        }

        # Scenes are keeper-only until play exposes them.  The first scene's
        # name/focus is a navigation seed, not narrative knowledge.
        for index, scene in enumerate(analysis.get("scenes", [])):
            keeper_pool["scenes"].append(scene)
            if index == 0:
                player_pool["scenes"].append(
                    {
                        "name": scene.get("name", ""),
                        "focus": scene.get("focus", "探索"),
                    }
                )

        # NPCs are not visible merely because they exist in the module.
        for npc in analysis.get("npcs", []):
            keeper_pool["npcs"].append(npc)

        # clues (module-wide catalog): keeper only — player's copy is
        # unlocked incrementally during play, not seeded here.
        keeper_pool["clues"] = analysis.get("clues", [])

        # threats (combat stat blocks): keeper only, never player-visible.
        keeper_pool["threats"] = analysis.get("threats", [])

        # timeline, truths: keeper only.
        keeper_pool["timeline"] = analysis.get("timeline", [])
        keeper_pool["truths"] = analysis.get("truths", [])

        return keeper_pool, player_pool


# --- Room lifecycle (M23 WS1) -----------------------------------------------
ROOM_FACETS = (
    RoomStateFacet(
        name="module_text",
        owner="agent.module_initializer",
        reset_scope="all",
        # The loaded module: its text, the ingestion status/error pair, and the keeper and
        # player knowledge pools built from it. They install together and they leave
        # together — a status without its text is what a half-cleaned room looks like.
        doc_types=frozenset({"module_pool"}),
        state_keys=frozenset(
            {
                "module_fulltext",
                "module_source",
                "module_init_status",
                "module_init_error",
                "module_import_status",
                "module_import_name",
            }
        ),
        storages=frozenset({STORAGE_DOCUMENTS, STORAGE_ROOM_STATE}),
    ),
)
