"""Pydantic schemas.

Two families of models live here:

* **AI output schema** (`AgentResponse`, `BugAnalysis`, `PatternAssessment`,
  `RuleProposal`): what the AI provider must return. Validation is lenient on
  *shape* (a string where a list is expected is coerced, unknown keys are
  dropped, over-long values are truncated) but strict on *types and values*.
  AI output is untrusted.
* **Persisted models** (`BugRecord`) and **derived models** (`Pattern`,
  `PreventionRule`): owned exclusively by Python.
"""

from __future__ import annotations

import re
from datetime import datetime, UTC
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

BUG_ID_RE = re.compile(r"^BUG-(\d{4,})$")
PATTERN_ID_RE = re.compile(r"^PAT-(\d{3,})$")
RULE_ID_RE = re.compile(r"^RULE-(\d{3,})$")

MAX_TEXT = 20_000
MAX_SHORT = 500
MAX_ITEMS = 50


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class ConfirmationStatus(StrEnum):
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    NEEDS_INFO = "needs_info"
    INVALID = "invalid"  # rejected / not a bug; excluded from knowledge


class PatternRelation(StrEnum):
    NEW = "new"
    RECURRENCE = "recurrence"
    RELATED = "related"
    INSUFFICIENT = "insufficient"


class Intent(StrEnum):
    BUG_REPORT = "bug_report"
    QUESTION = "question"
    OTHER = "other"


class Confidence(StrEnum):
    SUSPECTED = "suspected"  # single unconfirmed observation
    SINGLE_CONFIRMED = "single-confirmed"  # one confirmed bug
    RECURRING = "recurring"  # >=2 bugs, none confirmed
    CONFIRMED_RECURRING = "confirmed-recurring"  # >=2 bugs, >=1 confirmed


# --------------------------------------------------------------------------- #
# Coercion helpers (AI output is messy; normalise shape, never invent values)
# --------------------------------------------------------------------------- #

_NA = {"", "n/a", "na", "none", "null", "unknown", "غير متوفر", "غير معروف", "-"}


def _clip(value: str, limit: int) -> str:
    value = value.replace("\x00", "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def coerce_str(value: Any, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "\n".join(str(v) for v in value if v is not None)
    return _clip(str(value).strip(), limit)


def coerce_optional(value: Any, limit: int = MAX_SHORT) -> str | None:
    text = coerce_str(value, limit)
    return None if text.lower() in _NA else text


def coerce_list(value: Any, limit: int = MAX_TEXT) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        lines = [ln.strip() for ln in value.splitlines()]
        items = [re.sub(r"^(\d+[.)]|[-*•])\s+", "", ln) for ln in lines if ln]
    elif isinstance(value, (list, tuple)):
        items = []
        for v in value:
            if v is None:
                continue
            if isinstance(v, dict):  # e.g. {"step": "..."}; keep values verbatim
                v = " ".join(str(x) for x in v.values())
            items.append(str(v).strip())
    else:
        items = [str(value).strip()]
    out: list[str] = []
    for item in items:
        if item and item.lower() not in _NA:
            out.append(_clip(item, limit))
        if len(out) >= MAX_ITEMS:
            break
    return out


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# --------------------------------------------------------------------------- #
# AI output schema
# --------------------------------------------------------------------------- #


class RuleProposal(_Lenient):
    title: str = ""
    problem: str = ""
    required_behavior: list[str] = Field(default_factory=list)
    validation_requirements: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    regression_tests: list[str] = Field(default_factory=list)

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v: Any) -> str:
        return coerce_str(v, 200)

    @field_validator("problem", mode="before")
    @classmethod
    def _problem(cls, v: Any) -> str:
        return coerce_str(v, 4000)

    @field_validator(
        "required_behavior", "validation_requirements", "edge_cases", "regression_tests", mode="before"
    )
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return coerce_list(v, 1000)

    def is_empty(self) -> bool:
        return not (self.title and (self.required_behavior or self.problem))


class PatternAssessment(_Lenient):
    relation: PatternRelation = PatternRelation.INSUFFICIENT
    matched_pattern_id: str | None = None
    related_pattern_id: str | None = None
    pattern_name: str = ""
    pattern_category: str = ""
    pattern_description: str = ""
    signature: list[str] = Field(default_factory=list)
    rationale: str = ""
    rule: RuleProposal | None = None

    @field_validator("relation", mode="before")
    @classmethod
    def _relation(cls, v: Any) -> Any:
        v = coerce_str(v, 40).lower().replace("-", "_").replace(" ", "_")
        aliases = {"existing": "recurrence", "recurring": "recurrence", "duplicate": "recurrence",
                   "related_distinct": "related", "unknown": "insufficient", "none": "insufficient"}
        v = aliases.get(v, v)
        return v if v in PatternRelation._value2member_map_ else PatternRelation.INSUFFICIENT

    @field_validator("matched_pattern_id", "related_pattern_id", mode="before")
    @classmethod
    def _pid(cls, v: Any) -> str | None:
        v = coerce_optional(v, 20)
        return v.upper() if v and PATTERN_ID_RE.match(v.upper()) else None

    @field_validator("pattern_name", "pattern_category", mode="before")
    @classmethod
    def _short(cls, v: Any) -> str:
        return coerce_str(v, 160)

    @field_validator("pattern_description", "rationale", mode="before")
    @classmethod
    def _long(cls, v: Any) -> str:
        return coerce_str(v, 3000)

    @field_validator("signature", mode="before")
    @classmethod
    def _sig(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = re.split(r"[,\n]", v)
        return [s.lower() for s in coerce_list(v, 60)][:15]


class BugAnalysis(_Lenient):
    title: str = ""
    component: str | None = None
    category: str = "uncategorized"
    page_url: str | None = None
    api_endpoints: list[str] = Field(default_factory=list)
    http_method: str | None = None
    actual_status_code: str | None = None
    expected_status_code: str | None = None
    description: str = ""
    steps_to_reproduce: list[str] = Field(default_factory=list)
    actual_result: list[str] = Field(default_factory=list)
    expected_result: list[str] = Field(default_factory=list)
    severity: Severity = Severity.UNKNOWN
    severity_justification: str = ""
    impact: str = ""
    evidence: list[str] = Field(default_factory=list)
    confirmed_facts: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    pattern: PatternAssessment = Field(default_factory=PatternAssessment)

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v: Any) -> str:
        return coerce_str(v, 300)

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v: Any) -> str:
        v = coerce_str(v, 80).lower()
        return re.sub(r"\s+", "-", v) if v and v not in _NA else "uncategorized"

    @field_validator("component", "page_url", "http_method", mode="before")
    @classmethod
    def _opt(cls, v: Any) -> str | None:
        return coerce_optional(v)

    @field_validator("actual_status_code", "expected_status_code", mode="before")
    @classmethod
    def _status(cls, v: Any) -> str | None:
        return coerce_optional(v, 120)

    @field_validator("description", "severity_justification", "impact", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return coerce_str(v)

    @field_validator(
        "api_endpoints", "steps_to_reproduce", "actual_result", "expected_result", "evidence",
        "confirmed_facts", "hypotheses", "missing_information", "clarification_questions",
        mode="before",
    )
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return coerce_list(v)

    @field_validator("severity", mode="before")
    @classmethod
    def _severity(cls, v: Any) -> Any:
        v = coerce_str(v, 20).lower()
        return v if v in Severity._value2member_map_ else Severity.UNKNOWN

    @field_validator("pattern", mode="before")
    @classmethod
    def _pattern(cls, v: Any) -> Any:
        return v if isinstance(v, (dict, PatternAssessment)) else {}


class AgentResponse(_Lenient):
    """Top-level JSON object every provider must return."""

    intent: Intent = Intent.BUG_REPORT
    reply: str = ""
    language: str = "en"  # language of the user's INPUT; output is always English
    bug: BugAnalysis | None = None

    @field_validator("intent", mode="before")
    @classmethod
    def _intent(cls, v: Any) -> Any:
        v = coerce_str(v, 30).lower()
        return v if v in Intent._value2member_map_ else Intent.OTHER

    @field_validator("reply", mode="before")
    @classmethod
    def _reply(cls, v: Any) -> str:
        return coerce_str(v)

    @field_validator("language", mode="before")
    @classmethod
    def _lang(cls, v: Any) -> str:
        v = coerce_str(v, 10).lower()
        return "ar" if v.startswith("ar") else "en"


# --------------------------------------------------------------------------- #
# Persisted record
# --------------------------------------------------------------------------- #


class BugRecord(BaseModel):
    """One revision of a bug. bugs.jsonl is an append-only log of revisions."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    id: str
    revision: int = 1
    original_input: str
    language: str = "en"  # input language; reports are always English
    template: str = "default/professional"
    provider: str = "unknown"
    project: str = "default"
    analysis: BugAnalysis
    formatted_report: str = ""
    pattern_ids: list[str] = Field(default_factory=list)
    status: ConfirmationStatus = ConfirmationStatus.UNCONFIRMED
    possible_duplicate_of: list[str] = Field(default_factory=list)
    redactions: int = 0
    attachments: list[str] = Field(default_factory=list)  # screenshot copies, relative to the project dir
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    note: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not BUG_ID_RE.match(v):
            raise ValueError(f"invalid bug id: {v!r}")
        return v

    @field_validator("pattern_ids")
    @classmethod
    def _pids(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if not PATTERN_ID_RE.match(p)]
        if bad:
            raise ValueError(f"invalid pattern ids: {bad}")
        return v

    # Convenience accessors used by UI/CLI/statistics.
    @property
    def title(self) -> str:
        return self.analysis.title or "(untitled)"

    @property
    def severity(self) -> Severity:
        return self.analysis.severity

    @property
    def category(self) -> str:
        return self.analysis.category

    @property
    def component(self) -> str | None:
        return self.analysis.component

    def search_blob(self) -> str:
        a = self.analysis
        parts = [self.id, self.original_input, a.title, a.category, a.component or "", a.description,
                 " ".join(a.api_endpoints), " ".join(self.pattern_ids), self.status.value, a.severity.value]
        return "\n".join(parts).lower()


# --------------------------------------------------------------------------- #
# Derived knowledge (computed by Python from the archive, never by the AI)
# --------------------------------------------------------------------------- #


class Pattern(BaseModel):
    id: str
    name: str
    category: str
    description: str = ""
    signature: list[str] = Field(default_factory=list)
    bug_ids: list[str] = Field(default_factory=list)
    confirmed_bug_ids: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    endpoints: list[str] = Field(default_factory=list)
    related_patterns: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.SUSPECTED
    first_observed: datetime
    last_observed: datetime

    @property
    def occurrence_count(self) -> int:
        return len(self.bug_ids)


class PreventionRule(BaseModel):
    id: str
    pattern_id: str
    title: str
    problem: str = ""
    required_behavior: list[str] = Field(default_factory=list)
    validation_requirements: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    regression_tests: list[str] = Field(default_factory=list)
    bug_ids: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.SUSPECTED
    evidence: str = ""
    last_updated: datetime

    @property
    def is_active(self) -> bool:
        """A single unconfirmed observation never becomes a definitive rule."""
        return self.confidence != Confidence.SUSPECTED
