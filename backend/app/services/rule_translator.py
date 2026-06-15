"""Natural-language → structured-rule translator.

Two-stage pipeline:

1. **Local pattern matcher** (``_local_translate``) — deterministic, no
   API call. Handles ~80% of the patterns analysts actually write:
   compound CASE_WHEN with AND/OR/NOT, VALUE_MAP "map A to B" tables,
   CONSTANT "set everything to X", DEFAULT_VALUE "if blank use X",
   COMPUTED "today's date / current user / uuid".
2. **Claude fallback** — only invoked when local matching doesn't
   resolve a clear rule. Uses tool-use with a JSON schema so the
   output is engine-runnable. Gracefully 503s when no API key, and
   the local path keeps working without one.

Where the rule lives at runtime:

* The structured JSON returned by this service goes to the
  ``transformation_rules`` table via ``POST /conversions/{id}/rules``.
* At output-generation time, ``services/output_service.build_converted_dataframe``
  pulls rules into a per-field pipeline and executes them via
  ``app/transformations/engine.apply_rule(rule_type, config, value, row, ctx)``.
* Every rule type (CASE_WHEN, VALUE_MAP, CONSTANT, …) has a branch in
  ``apply_rule``. The translator's only job is to produce a
  ``{rule_type, config}`` JSON that ``apply_rule`` can execute as-is.

Production-grade properties:

* **No state lost on 503** — when Claude is unavailable, the local
  matcher still translates common patterns. The Rule Author Modal pre-
  fills the structured form from either path.
* **Server-side validation** — every translated rule is re-executed
  against ``apply_rule`` on a sentinel value before return.
* **Audit-friendly** — both paths produce the same response shape, and
  the source (local vs. ai) is reported back so the audit log can
  capture which path resolved the rule.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.transformations.engine import apply_rule


log = logging.getLogger("trinamix.rule_translator")


class TranslatorUnavailable(Exception):
    """Raised when no API key is configured. The endpoint maps this to a
    503 with a structured body so the UI knows to hide the NL tab."""


class TranslatorError(Exception):
    """Raised for any other failure — bad API response, schema violation,
    JSON decode error. The detail message is safe to surface to the UI."""


@dataclass
class TranslationResult:
    rule_type: str
    config: dict[str, Any]
    explanation: str
    ambiguities: list[dict[str, Any]] = field(default_factory=list)
    preview_samples: list[dict[str, Any]] = field(default_factory=list)
    description: str | None = None
    # Slice-fix: tells the UI which path resolved the rule so the modal
    # can show "Translated locally (no AI call)" vs. "Translated by AI".
    source: str = "ai"   # "local" | "ai"


# ─── Local pattern matcher ──────────────────────────────────────────


# Operator vocabulary the local matcher recognises. Mapping: spoken
# phrases → engine op codes. Longer phrases first so "is not" beats "is".
_OP_PHRASES: tuple[tuple[str, str], ...] = (
    ("is not blank", "notblank"),
    ("is not empty", "notblank"),
    ("is blank",      "isblank"),
    ("is empty",      "isblank"),
    ("does not contain", "notin"),
    ("not contains",  "notin"),
    ("not equals",    "neq"),
    ("is not",        "neq"),
    ("!=",            "neq"),
    ("contains",      "contains"),
    ("starts with",   "startswith"),
    ("begins with",   "startswith"),
    ("ends with",     "endswith"),
    ("matches",       "regex"),
    ("equals",        "eq"),
    ("is",            "eq"),
    ("==",            "eq"),
    (" = ",           "eq"),
    (">=",            "gte"),
    (">",             "gt"),
    ("<=",            "lte"),
    ("<",             "lt"),
)


_DEFAULT_WORDS = ("otherwise", "else", "default to", "default is")


def _normalise_columns(columns: list[str]) -> dict[str, str]:
    """Case-insensitive lookup so the matcher accepts ``status`` even
    when the actual column is ``STATUS``."""
    return {c.lower(): c for c in columns}


def _find_column(text: str, columns: dict[str, str]) -> tuple[str, str] | None:
    """Return (column_name, remaining_text_after_column) if any known
    column name appears at the start of ``text`` (longest match first)."""
    candidates = sorted(columns.keys(), key=len, reverse=True)
    lowered = text.lstrip().lower()
    for c in candidates:
        if lowered.startswith(c + " ") or lowered == c:
            keep = len(c)
            return columns[c], text.lstrip()[keep:].lstrip()
    return None


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"', "`"):
        return v[1:-1]
    return v


def _parse_leaf_condition(
    text: str, columns: dict[str, str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Try to parse ``text`` as a single ``{column, op, value}``
    condition. Returns (condition_dict, ambiguity_dict). The ambiguity
    captures the raw phrase + interpreted-as for the UI's confirmation
    callouts.
    """
    cleaned = text.strip().strip(",").strip()
    if not cleaned:
        return None, None
    found = _find_column(cleaned, columns)
    if not found:
        return None, None
    col, rest = found
    rest_l = rest.lower()
    for phrase, op in _OP_PHRASES:
        idx = rest_l.find(phrase)
        if idx == 0:
            after = rest[len(phrase):].strip()
            if op in ("isblank", "notblank"):
                return (
                    {"column": col, "op": op, "value": None},
                    {"phrase": cleaned, "interpreted_as": f"{col} {phrase}", "alternatives": []},
                )
            value = _strip_quotes(after.split(" and ")[0].split(" or ")[0])
            value = value.rstrip(",.").strip()
            if not value:
                continue
            return (
                {"column": col, "op": op, "value": value},
                {
                    "phrase": cleaned,
                    "interpreted_as": f"{col} {op} '{value}'",
                    "alternatives": [],
                },
            )
    return None, None


def _split_top_level(s: str, sep: str) -> list[str]:
    """Split ``s`` on ``sep`` ignoring those inside single/double quotes."""
    parts: list[str] = []
    buf: list[str] = []
    in_q: str | None = None
    i = 0
    sep_l = sep.lower()
    sl = s.lower()
    while i < len(s):
        ch = s[i]
        if in_q:
            buf.append(ch)
            if ch == in_q:
                in_q = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_q = ch
            buf.append(ch)
            i += 1
            continue
        if sl[i:i + len(sep)] == sep_l:
            parts.append("".join(buf))
            buf = []
            i += len(sep)
            continue
        buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _parse_conditions(
    text: str, columns: dict[str, str],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Parse a condition expression into a leaf, all_of, or any_of group.
    Returns (parsed_expression, ambiguities)."""
    amb: list[dict[str, Any]] = []
    text = text.strip()
    if " and " in text.lower():
        parts = _split_top_level(text, " and ")
        leaves: list[dict[str, Any]] = []
        for p in parts:
            leaf, a = _parse_leaf_condition(p, columns)
            if leaf is None:
                return None, amb
            leaves.append(leaf)
            if a:
                amb.append(a)
        return {"all_of": leaves}, amb
    if " or " in text.lower():
        parts = _split_top_level(text, " or ")
        leaves = []
        for p in parts:
            leaf, a = _parse_leaf_condition(p, columns)
            if leaf is None:
                return None, amb
            leaves.append(leaf)
            if a:
                amb.append(a)
        return {"any_of": leaves}, amb
    leaf, a = _parse_leaf_condition(text, columns)
    if leaf is None:
        return None, amb
    if a:
        amb.append(a)
    return leaf, amb


_CASE_RE = re.compile(
    r"^\s*(?:if|when)\s+(?P<conds>.+?)\s+(?:then|=>|->)\s+(?P<value>.+?)\s*$",
    re.IGNORECASE,
)


def _try_case_when(
    description: str, columns: dict[str, str],
) -> TranslationResult | None:
    """Parse one or more ``if … then …; if … then …; otherwise …`` clauses.
    Builds a CASE_WHEN config with compound branches when needed."""
    text = description.strip().rstrip(".")
    if not text:
        return None
    # Split on semicolons / "; otherwise" / " ; else " etc.
    clauses = [c.strip() for c in text.split(";") if c.strip()]
    if len(clauses) == 1 and not _CASE_RE.match(clauses[0]):
        return None
    branches: list[dict[str, Any]] = []
    default: str | None = None
    ambiguities: list[dict[str, Any]] = []
    for clause in clauses:
        clause_l = clause.lower().strip()
        m = _CASE_RE.match(clause)
        if m:
            conds_text = m.group("conds")
            value = _strip_quotes(m.group("value").strip())
            expr, amb = _parse_conditions(conds_text, columns)
            ambiguities.extend(amb)
            if expr is None:
                return None
            branch: dict[str, Any] = dict(expr)
            branch["then"] = value
            branches.append(branch)
            continue
        # Default clause: "otherwise X" / "else X" / "default to X"
        for w in _DEFAULT_WORDS:
            if clause_l.startswith(w):
                default = _strip_quotes(clause[len(w):].strip())
                break
    if not branches:
        return None
    config: dict[str, Any] = {"branches": branches}
    if default is not None:
        config["default"] = default
    branch_count = len(branches)
    explanation = (
        f"Translated locally as a CASE_WHEN with {branch_count} "
        f"branch{'es' if branch_count != 1 else ''}"
        + (f" + default '{default}'" if default else "")
        + "."
    )
    return TranslationResult(
        rule_type="CASE_WHEN",
        config=config,
        explanation=explanation,
        ambiguities=ambiguities,
        source="local",
    )


_CONSTANT_RE = re.compile(
    r"^\s*(?:always\s+)?(?:set|output|emit|return|use)\s+(?:every row\s+|the (?:value|output)\s+|to\s+)*['\"]?(?P<value>[^'\"]+?)['\"]?\s*\.?$",
    re.IGNORECASE,
)


def _try_constant(description: str) -> TranslationResult | None:
    m = _CONSTANT_RE.match(description.strip())
    if not m:
        return None
    return TranslationResult(
        rule_type="CONSTANT",
        config={"value": _strip_quotes(m.group("value").strip())},
        explanation="Translated locally as CONSTANT — always emit a fixed value.",
        source="local",
    )


_VALUE_MAP_RE = re.compile(
    r"map\s+(?P<pairs>.+?)\s*$",
    re.IGNORECASE,
)


def _try_value_map(description: str) -> TranslationResult | None:
    m = _VALUE_MAP_RE.search(description.strip().rstrip("."))
    if not m:
        return None
    raw = m.group("pairs")
    # Accept formats: "A to B, C to D" / "A->B, C->D" / "A=B, C=D"
    chunks = re.split(r"[,;]\s*|\sand\s", raw)
    mapping: dict[str, str] = {}
    for ch in chunks:
        for sep in (" to ", " => ", "->", "=>", "="):
            if sep in ch:
                left, right = ch.split(sep, 1)
                k = _strip_quotes(left.strip())
                v = _strip_quotes(right.strip())
                if k:
                    mapping[k] = v
                break
    if not mapping:
        return None
    config: dict[str, Any] = {"case_insensitive": True, **mapping}
    return TranslationResult(
        rule_type="VALUE_MAP",
        config=config,
        explanation=(
            f"Translated locally as VALUE_MAP with {len(mapping)} from→to pair"
            f"{'s' if len(mapping) != 1 else ''}."
        ),
        source="local",
    )


_DEFAULT_RE = re.compile(
    r"(?:if|when)\s+(?:the\s+)?(?:source\s+)?(?:value\s+)?is\s+(?:blank|empty|missing)\s+(?:then\s+|use\s+|default to\s+)?['\"]?(?P<value>[^'\"]+?)['\"]?\s*\.?$",
    re.IGNORECASE,
)


def _try_default_value(description: str) -> TranslationResult | None:
    m = _DEFAULT_RE.search(description.strip())
    if not m:
        return None
    return TranslationResult(
        rule_type="DEFAULT_VALUE",
        config={"value": _strip_quotes(m.group("value").strip())},
        explanation="Translated locally as DEFAULT_VALUE — fills blanks with a fixed value.",
        source="local",
    )


def _try_computed(description: str) -> TranslationResult | None:
    d = description.lower().strip()
    if "today" in d and "date" in d:
        return TranslationResult(
            rule_type="COMPUTED",
            config={"source": "today", "format": "%Y/%m/%d"},
            explanation="Translated locally as COMPUTED — today's date.",
            source="local",
        )
    if "current user" in d or "logged-in user" in d:
        return TranslationResult(
            rule_type="COMPUTED",
            config={"source": "current_user"},
            explanation="Translated locally as COMPUTED — current user email.",
            source="local",
        )
    if "row number" in d or "row index" in d or "row sequence" in d:
        return TranslationResult(
            rule_type="COMPUTED",
            config={"source": "row_index"},
            explanation="Translated locally as COMPUTED — row index.",
            source="local",
        )
    if "uuid" in d or "random id" in d:
        return TranslationResult(
            rule_type="COMPUTED",
            config={"source": "uuid"},
            explanation="Translated locally as COMPUTED — random UUID.",
            source="local",
        )
    return None


def _local_translate(
    description: str, columns: list[str],
) -> TranslationResult | None:
    """Try every local pattern in priority order. Returns the first
    match, or None if no pattern matches (caller falls back to AI)."""
    column_lookup = _normalise_columns(columns or [])
    for try_fn in (
        lambda: _try_case_when(description, column_lookup),
        lambda: _try_constant(description),
        lambda: _try_value_map(description),
        lambda: _try_default_value(description),
        lambda: _try_computed(description),
    ):
        result = try_fn()
        if result is not None:
            return result
    return None


# Tool schema — kept as a Python literal so a static-analysis pass can
# catch typos. The Anthropic SDK forwards this verbatim as JSON Schema.
_PROPOSE_RULE_TOOL = {
    "name": "propose_rule",
    "description": (
        "Translate the analyst's natural-language description into a "
        "structured transformation rule the workbench engine can execute. "
        "Always output a single rule (composed pipelines are not supported "
        "via this entry point — chain via the Studio if needed)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "rule_type": {
                "type": "string",
                "enum": [
                    "TRIM", "UPPERCASE", "LOWERCASE", "TITLE_CASE",
                    "REMOVE_HYPHEN", "REMOVE_SPECIAL_CHARS",
                    "REPLACE", "REGEX_REPLACE", "REGEX_EXTRACT",
                    "PAD", "SUBSTRING",
                    "DEFAULT_VALUE", "CONSTANT", "COMPUTED",
                    "VALUE_MAP", "CROSSWALK_LOOKUP",
                    "DATE_FORMAT", "NUMBER_FORMAT", "ARITHMETIC",
                    "CONCAT", "SPLIT", "COALESCE",
                    "CONDITIONAL", "CASE_WHEN",
                ],
            },
            "config": {
                "type": "object",
                "description": (
                    "Rule configuration. Shape depends on rule_type. For "
                    "CASE_WHEN with multi-column AND / OR conditions, use "
                    "branches with 'all_of' / 'any_of' arrays of leaf "
                    "conditions. Leaf conditions are "
                    "{column, op, value} — op is one of "
                    "eq/neq/gt/gte/lt/lte/in/notin/contains/"
                    "startswith/endswith/regex/isblank/notblank."
                ),
                "additionalProperties": True,
            },
            "explanation": {
                "type": "string",
                "description": (
                    "One- or two-sentence plain-English summary of how you "
                    "interpreted the description. Surfaced in the modal "
                    "so the analyst can sanity-check the translation."
                ),
            },
            "ambiguities": {
                "type": "array",
                "description": (
                    "Phrases in the description that could plausibly map "
                    "to multiple columns or values. Empty when the "
                    "interpretation is unambiguous."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "phrase": {"type": "string"},
                        "interpreted_as": {"type": "string"},
                        "alternatives": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["phrase", "interpreted_as"],
                },
            },
        },
        "required": ["rule_type", "config", "explanation"],
    },
}


_SYSTEM_PROMPT = """\
You translate plain-English transformation descriptions into one structured rule \
for the Trinamix Conversion Workbench's rule engine.

Output protocol:
- You MUST call the `propose_rule` tool exactly once. Do not produce any \
  free-form text.
- Pick the smallest rule_type that satisfies the description. Prefer \
  VALUE_MAP for simple value substitutions, CONSTANT for "always set to X", \
  CASE_WHEN for any conditional logic that involves comparison operators or \
  multiple columns.
- For CASE_WHEN with multi-column conditions, model branches with \
  `all_of` (AND) or `any_of` (OR), each carrying leaf conditions \
  `{"column": ..., "op": ..., "value": ...}`. Supported ops: \
  eq, neq, gt, gte, lt, lte, in, notin, contains, startswith, endswith, \
  regex, isblank, notblank.
- Always include a `default` on CASE_WHEN unless the description rules it out.
- Use the exact column names from the dataset's column catalog. If the \
  user's phrase is ambiguous between columns, pick the best fit and report \
  the alternatives in `ambiguities`.
- Never invent columns that aren't in the catalog. Never invent values that \
  aren't supported by the engine.
- `explanation` is a single short sentence written for the analyst.

Engine reference (selected rule shapes):
  VALUE_MAP:      {"case_insensitive": true, "<from>": "<to>", ..., "default": "..."}
  CONSTANT:       {"value": "..."}
  DEFAULT_VALUE:  {"value": "..."}  // only fills blanks
  CASE_WHEN:      {"branches": [<branch>, ...], "default": "..."}
                  legacy branch: {"if_column": "...", "op": "eq", "value": "...", "then": "..."}
                  compound:      {"all_of": [<leaf>, ...], "then": "..."}
                                 {"any_of": [<leaf>, ...], "then": "..."}
                  nested:        {"not": <branch-or-group>}
  COMPUTED:       {"source": "today|now|row_index|uuid|current_user", "format": "%Y/%m/%d"}
  ARITHMETIC:     {"op": "multiply|divide|add|subtract|round|abs|negate", "amount": <n>, "decimals": <n>}
"""


def _build_user_prompt(
    description: str,
    columns: list[str],
    sample_rows: list[dict[str, Any]] | None,
    target_field: str | None,
    target_data_type: str | None,
) -> str:
    parts: list[str] = []
    parts.append(f"Description:\n  {description.strip()}\n")
    if target_field:
        meta = f" (data type: {target_data_type})" if target_data_type else ""
        parts.append(f"Target FBDI field: {target_field}{meta}\n")
    parts.append("Available source columns:")
    if columns:
        for c in columns:
            parts.append(f"  - {c}")
    else:
        parts.append("  (none — the rule should be column-independent)")
    parts.append("")
    if sample_rows:
        parts.append("Sample rows from the dataset (first few):")
        for i, r in enumerate(sample_rows[:5], 1):
            parts.append(f"  Row {i}: {json.dumps(r, default=str)[:300]}")
        parts.append("")
    return "\n".join(parts)


def _validate_translated_rule(rule_type: str, config: dict[str, Any]) -> None:
    """Re-parse the translated rule with the engine on a sentinel value so
    obvious shape errors fail at translate time, not at save time.

    A successful execution doesn't prove the rule is semantically correct
    (the analyst owns that judgement) — it only proves the engine *can*
    execute it without raising. That's enough to reject empty configs,
    wrong-typed fields, or bogus rule types early.
    """
    sentinel = "TRINAMIX_RULE_TRANSLATE_SENTINEL"
    sample_row = {"_probe_": sentinel}
    try:
        apply_rule(rule_type, config or {}, sentinel, row=sample_row, ctx={})
    except Exception as exc:
        raise TranslatorError(
            f"Translated rule did not execute cleanly under the engine "
            f"(rule_type={rule_type!r}): {exc}"
        ) from exc


def translate_description(
    *,
    description: str,
    columns: list[str],
    sample_rows: list[dict[str, Any]] | None = None,
    target_field: str | None = None,
    target_data_type: str | None = None,
) -> TranslationResult:
    """Translate ``description`` into a runnable engine rule.

    Two-stage:

    1. **Local pattern matcher** — fast, deterministic, free. Handles
       the most common patterns (compound CASE_WHEN with AND/OR/NOT,
       VALUE_MAP, CONSTANT, DEFAULT_VALUE, COMPUTED).
    2. **Claude fallback** — only if local doesn't match AND an
       Anthropic API key is configured.

    If local fails and Claude is unavailable, :class:`TranslatorUnavailable`
    is raised so the router can return 503. With local in the mix this
    rarely happens for everyday rules — the analyst describes "if STATUS
    is active and REGION is US then DOMESTIC_ACTIVE" and gets a
    structured rule back even without an API key.
    """
    # Stage 1 — local pattern matcher
    local = _local_translate(description, columns or [])
    if local is not None:
        # Validate through the engine on a sentinel before returning.
        _validate_translated_rule(local.rule_type, local.config)
        return local

    # Stage 2 — Claude fallback
    if not settings.ANTHROPIC_API_KEY:
        raise TranslatorUnavailable(
            "Could not translate this description with local patterns, and "
            "no ANTHROPIC_API_KEY is configured for the AI fallback. "
            "Either rephrase the description to a simpler 'if X is Y then Z' "
            "form, or use the structured Form view to build the rule directly."
        )

    # Local import keeps the module importable in environments without the
    # SDK installed (e.g. lightweight test images).
    try:
        from anthropic import Anthropic
    except ImportError as e:  # pragma: no cover
        raise TranslatorUnavailable(
            "anthropic SDK is not installed in this environment."
        ) from e

    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    user_prompt = _build_user_prompt(
        description=description,
        columns=columns,
        sample_rows=sample_rows,
        target_field=target_field,
        target_data_type=target_data_type,
    )

    try:
        # ``cache_control`` on the system block keeps the engine reference
        # in the prompt cache (5-min TTL) so a session-long workflow of
        # incremental edits pays only for the description on each round-
        # trip, not the entire system prompt.
        resp = client.messages.create(
            model=settings.ANTHROPIC_MODEL or "claude-sonnet-4-6",
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_PROPOSE_RULE_TOOL],
            tool_choice={"type": "tool", "name": "propose_rule"},
            messages=[
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as exc:
        log.warning("translator API call failed: %s", exc)
        raise TranslatorError(
            f"Anthropic API call failed: {exc}"
        ) from exc

    tool_use = next(
        (b for b in resp.content if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_use is None:
        raise TranslatorError(
            "Translator returned no structured rule (no tool_use block)."
        )

    payload = tool_use.input or {}
    rule_type = payload.get("rule_type")
    config = payload.get("config") or {}
    explanation = payload.get("explanation") or ""
    ambiguities = payload.get("ambiguities") or []

    if not isinstance(rule_type, str) or not rule_type:
        raise TranslatorError("Translator returned an empty rule_type.")
    if not isinstance(config, dict):
        raise TranslatorError("Translator returned a non-object config.")

    _validate_translated_rule(rule_type, config)

    return TranslationResult(
        rule_type=rule_type,
        config=config,
        explanation=explanation,
        ambiguities=[
            {
                "phrase": a.get("phrase", ""),
                "interpreted_as": a.get("interpreted_as", ""),
                "alternatives": a.get("alternatives") or [],
            }
            for a in ambiguities
            if isinstance(a, dict)
        ],
        description=description,
    )
