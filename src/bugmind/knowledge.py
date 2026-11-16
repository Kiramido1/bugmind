"""Bug intelligence: patterns, prevention rules and statistics.

Everything here is a pure, deterministic fold over the canonical bug archive:
each BugRecord stores the AI's pattern assessment + rule proposal and the
pattern IDs Python assigned at save time. Rebuilding memory therefore never
needs the AI, never loses references, and always yields the same result.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bugmind.models import (
    PATTERN_ID_RE, BugRecord, Confidence, ConfirmationStatus, Pattern, PatternAssessment, PatternRelation,
    PreventionRule, Severity, utcnow,
)
from bugmind.storage.atomic import atomic_write_text, backup_file
from bugmind.storage.bug_store import StoreLayout

MAX_RULE_ITEMS = 20
MEMORY_FILES = ("BACKEND_PREVENTION.md", "BUG_PATTERNS.md", "STATISTICS.md")


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", s.lower()).strip()


def _similar(a: str, b: str, threshold: float = 0.6) -> bool:
    wa, wb = set(_norm(a).split()), set(_norm(b).split())
    if not wa or not wb:
        return wa == wb
    return len(wa & wb) / len(wa | wb) >= threshold


def _merge_list(base: list[str], new: list[str], cap: int = MAX_RULE_ITEMS, *, newest_first: bool = False) -> list[str]:
    """Union of two lists, dropping near-duplicates (word-overlap >= 60%).

    newest_first=True puts the latest wording first and keeps older items only when
    they add something not already covered, so refinements replace stale phrasing
    without ever silently dropping an instruction that is still distinct.
    """
    first, second = (new, base) if newest_first else (base, new)
    out: list[str] = []
    for item in [*first, *second]:
        if _norm(item) and not any(_similar(item, kept) for kept in out) and len(out) < cap:
            out.append(item)
    return out


def _pnum(pid: str) -> int:
    m = PATTERN_ID_RE.match(pid)
    return int(m.group(1)) if m else 0


def _counts_toward_knowledge(rec: BugRecord) -> bool:
    return rec.status != ConfirmationStatus.INVALID


def assign_pattern_ids(assessment: PatternAssessment, patterns: list[Pattern],
                       all_known_ids: set[str] | None = None) -> list[str]:
    """Python-side validation of the AI's pattern decision -> concrete pattern IDs.

    * recurrence with a known id      -> that id
    * any relation whose pattern name matches an existing pattern -> that id (no duplicate rules)
    * new / related / unknown recurrence with a name -> fresh PAT id
    * insufficient (or no name)       -> no pattern
    """
    by_id = {p.id: p for p in patterns}
    if assessment.relation == PatternRelation.RECURRENCE and assessment.matched_pattern_id in by_id:
        return [assessment.matched_pattern_id]  # type: ignore[list-item]
    if assessment.relation == PatternRelation.INSUFFICIENT or not assessment.pattern_name:
        return []
    name = _norm(assessment.pattern_name)
    for p in patterns:
        if _norm(p.name) == name:
            return [p.id]
    known = set(by_id) | (all_known_ids or set())
    nxt = max((_pnum(i) for i in known), default=0) + 1
    return [f"PAT-{nxt:03d}"]


@dataclass
class Knowledge:
    patterns: list[Pattern] = field(default_factory=list)
    rules: list[PreventionRule] = field(default_factory=list)
    records: list[BugRecord] = field(default_factory=list)

    @property
    def active_rules(self) -> list[PreventionRule]:
        return [r for r in self.rules if r.is_active]

    @property
    def candidate_rules(self) -> list[PreventionRule]:
        return [r for r in self.rules if not r.is_active]

    def pattern(self, pid: str) -> Pattern | None:
        return next((p for p in self.patterns if p.id == pid), None)


def _confidence(n: int, confirmed: int) -> Confidence:
    if n >= 2:
        return Confidence.CONFIRMED_RECURRING if confirmed else Confidence.RECURRING
    return Confidence.SINGLE_CONFIRMED if confirmed else Confidence.SUSPECTED


def build_knowledge(records: list[BugRecord]) -> Knowledge:
    patterns: dict[str, Pattern] = {}
    rules: dict[str, PreventionRule] = {}
    ordered = sorted(records, key=lambda r: (r.created_at, r.id))
    for rec in ordered:
        if not _counts_toward_knowledge(rec):
            continue
        a = rec.analysis
        pa = a.pattern
        for pid in rec.pattern_ids:
            p = patterns.get(pid)
            if p is None:
                p = patterns[pid] = Pattern(
                    id=pid, name=pa.pattern_name or a.title, category=pa.pattern_category or a.category,
                    description=pa.pattern_description, signature=list(pa.signature),
                    first_observed=rec.created_at, last_observed=rec.created_at,
                )
            else:
                p.last_observed = max(p.last_observed, rec.created_at)
                if pa.pattern_description:
                    p.description = pa.pattern_description  # latest refinement wins; name stays stable
                p.signature = _merge_list(p.signature, pa.signature, 12)
            if rec.id not in p.bug_ids:
                p.bug_ids.append(rec.id)
            if rec.status == ConfirmationStatus.CONFIRMED and rec.id not in p.confirmed_bug_ids:
                p.confirmed_bug_ids.append(rec.id)
            if a.component:
                p.components = _merge_list(p.components, [a.component])
            p.endpoints = _merge_list(p.endpoints, a.api_endpoints)
            if pa.related_pattern_id and pa.related_pattern_id != pid:
                p.related_patterns = _merge_list(p.related_patterns, [pa.related_pattern_id])

            proposal = pa.rule
            rid = f"RULE-{_pnum(pid):03d}"
            r = rules.get(rid)
            if proposal is not None and not proposal.is_empty():
                if r is None:
                    r = rules[rid] = PreventionRule(id=rid, pattern_id=pid, title=proposal.title,
                                                    last_updated=rec.created_at)
                r.title = proposal.title or r.title
                r.problem = proposal.problem or r.problem
                r.required_behavior = _merge_list(r.required_behavior, proposal.required_behavior, newest_first=True)
                r.validation_requirements = _merge_list(r.validation_requirements, proposal.validation_requirements, newest_first=True)
                r.edge_cases = _merge_list(r.edge_cases, proposal.edge_cases, newest_first=True)
                r.regression_tests = _merge_list(r.regression_tests, proposal.regression_tests, newest_first=True)
                r.last_updated = max(r.last_updated, rec.updated_at)
            if r is not None and rec.id not in r.bug_ids:
                r.bug_ids.append(rec.id)

    for p in patterns.values():
        p.confidence = _confidence(len(p.bug_ids), len(p.confirmed_bug_ids))
    for r in rules.values():
        p = patterns[r.pattern_id]
        r.confidence = p.confidence
        r.evidence = (f"{p.occurrence_count} occurrence(s), {len(p.confirmed_bug_ids)} confirmed by QA. "
                      f"Confidence: {p.confidence.value}.")
    return Knowledge(
        patterns=sorted(patterns.values(), key=lambda p: _pnum(p.id)),
        rules=sorted(rules.values(), key=lambda r: r.id),
        records=list(records),
    )


# --------------------------------------------------------------------------- statistics


@dataclass
class Statistics:
    total: int
    counted: int
    by_severity: dict[str, int]
    by_category: dict[str, int]
    by_status: dict[str, int]
    top_components: list[tuple[str, int]]
    top_endpoints: list[tuple[str, int]]
    recurring_patterns: list[Pattern]
    new_patterns: list[Pattern]
    unclassified: int
    active_rules: int
    candidate_rules: int


def compute_statistics(k: Knowledge) -> Statistics:
    counted = [r for r in k.records if _counts_toward_knowledge(r)]
    sev = Counter(r.severity.value for r in counted)
    return Statistics(
        total=len(k.records),
        counted=len(counted),
        by_severity={s.value: sev.get(s.value, 0) for s in Severity},
        by_category=dict(Counter(r.category for r in counted).most_common()),
        by_status=dict(Counter(r.status.value for r in k.records).most_common()),
        top_components=Counter(r.component for r in counted if r.component).most_common(10),
        top_endpoints=Counter(e for r in counted for e in r.analysis.api_endpoints).most_common(10),
        recurring_patterns=sorted((p for p in k.patterns if p.occurrence_count >= 2),
                                  key=lambda p: -p.occurrence_count),
        new_patterns=[p for p in k.patterns if p.occurrence_count == 1],
        unclassified=sum(1 for r in counted if not r.pattern_ids),
        active_rules=len(k.active_rules),
        candidate_rules=len(k.candidate_rules),
    )


# --------------------------------------------------------------------------- rendering


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _il(text: object) -> str:
    """AI-supplied text, flattened to one line: it can't forge headings/rules or break tables
    in files that coding agents read as instructions."""
    return " ".join(str(text).split()).replace("|", "\\|")


def _section(title: str, items: list[str]) -> list[str]:
    return [f"### {title}", *(f"- {_il(i)}" for i in items), ""] if items else []


def _render_rule(r: PreventionRule, k: Knowledge) -> list[str]:
    p = k.pattern(r.pattern_id)
    lines = [f"## {r.id}: {_il(r.title)}", "",
             f"*Pattern:* {r.pattern_id}{f' — {_il(p.name)}' if p else ''} · *Confidence:* {r.confidence.value} · "
             f"*Last updated:* {_ts(r.last_updated)}", ""]
    if r.problem:
        lines += ["### Problem", _il(r.problem), ""]
    lines += _section("Required Instructions", r.required_behavior)
    lines += _section("Validation Requirements", r.validation_requirements)
    lines += _section("Edge Cases", r.edge_cases)
    lines += _section("Regression Requirements", r.regression_tests)
    lines += ["### Related Bugs", *(f"- {b}" for b in r.bug_ids), "", "### Evidence", _il(r.evidence), ""]
    return lines


def render_prevention(k: Knowledge) -> str:
    lines = ["# Backend Engineering Prevention Rules", "",
             f"_Generated by BugMind from {len(k.records)} archived bug(s) on {_ts(utcnow())}. "
             "Each rule is traceable to the bugs it was derived from._", "",
             "> Instructions for backend developers and AI coding agents. Rules below are backed by a confirmed "
             "or recurring defect. Treat them as requirements when touching the related components.", ""]
    if not k.active_rules:
        lines += ["_No rules yet: rules become active once a pattern recurs or a bug is confirmed._", ""]
    for r in k.active_rules:
        lines += _render_rule(r, k)
    if k.candidate_rules:
        lines += ["---", "", "# Candidate Rules (suspected — single unconfirmed observation)", "",
                  "> Not yet definitive. Confirm the source bug (`bugmind status BUG-XXXX confirmed`) or wait for a "
                  "recurrence before treating these as requirements.", ""]
        for r in k.candidate_rules:
            lines += _render_rule(r, k)
    return "\n".join(lines).rstrip() + "\n"


def render_patterns(k: Knowledge) -> str:
    lines = ["# Bug Patterns", "", f"_Generated by BugMind on {_ts(utcnow())}._", ""]
    if not k.patterns:
        return "\n".join(lines + ["_No patterns detected yet._"]) + "\n"
    lines += ["| ID | Pattern | Category | Occurrences | Confidence | Last observed |",
              "|---|---|---|---|---|---|"]
    for p in sorted(k.patterns, key=lambda p: (-p.occurrence_count, p.id)):
        lines.append(f"| {p.id} | {_il(p.name)} | {_il(p.category)} | {p.occurrence_count} | {p.confidence.value} | "
                     f"{p.last_observed:%Y-%m-%d} |")
    lines.append("")
    for p in k.patterns:
        lines += [f"## {p.id}: {_il(p.name)}", "",
                  f"- **Category:** {_il(p.category)}",
                  f"- **Occurrences:** {p.occurrence_count} ({len(p.confirmed_bug_ids)} confirmed)",
                  f"- **Confidence:** {p.confidence.value}",
                  f"- **First observed:** {_ts(p.first_observed)}",
                  f"- **Last observed:** {_ts(p.last_observed)}",
                  f"- **Related bugs:** {', '.join(p.bug_ids)}",
                  f"- **Affected components:** {_il(', '.join(p.components)) or 'N/A'}",
                  f"- **Endpoints:** {', '.join(f'`{_il(e).replace(chr(96), chr(39))}`' for e in p.endpoints) or 'N/A'}",
                  f"- **Signature:** {_il(', '.join(p.signature)) or 'N/A'}"]
        if p.related_patterns:
            lines.append(f"- **Related patterns:** {', '.join(p.related_patterns)}")
        lines += ["", _il(p.description) or "_No description._", ""]
    return "\n".join(lines).rstrip() + "\n"


def render_statistics(k: Knowledge) -> str:
    s = compute_statistics(k)

    def table(header: str, rows: list[tuple[str, int]]) -> list[str]:
        if not rows:
            return ["_None._", ""]
        return [f"| {header} | Bugs |", "|---|---|", *(f"| {_il(a)} | {b} |" for a, b in rows), ""]

    lines = ["# Statistics", "", f"_Derived from the bug archive on {_ts(utcnow())}._", "",
             f"- **Total recorded bugs:** {s.total}",
             f"- **Counted (excluding invalid):** {s.counted}",
             f"- **Patterns:** {len(k.patterns)} ({len(s.recurring_patterns)} recurring, {len(s.new_patterns)} single-occurrence)",
             f"- **Unclassified bugs:** {s.unclassified}",
             f"- **Prevention rules:** {s.active_rules} active, {s.candidate_rules} candidate", "",
             "## Bugs by Severity", *table("Severity", list(s.by_severity.items())),
             "## Bugs by Category", *table("Category", list(s.by_category.items())),
             "## Bugs by Status", *table("Status", list(s.by_status.items())),
             "## Most Frequently Affected Components", *table("Component", s.top_components),
             "## Most Frequently Affected Endpoints", *table("Endpoint", [(f"`{e}`", n) for e, n in s.top_endpoints]),
             "## Recurring Patterns"]
    lines += [f"- {p.id} {p.name}: {p.occurrence_count} bugs ({', '.join(p.bug_ids)})" for p in s.recurring_patterns] or ["_None yet._"]
    lines += ["", "## New (Single-Occurrence) Patterns"]
    lines += [f"- {p.id} {p.name} ({p.bug_ids[0]})" for p in s.new_patterns] or ["_None._"]
    return "\n".join(lines).rstrip() + "\n"


def write_memory(layout: StoreLayout, k: Knowledge, backups_to_keep: int = 20) -> list[Path]:
    """Back up then atomically rewrite the three memory files. Caller holds the store lock."""
    rendered = {
        "BACKEND_PREVENTION.md": render_prevention(k),
        "BUG_PATTERNS.md": render_patterns(k),
        "STATISTICS.md": render_statistics(k),
    }
    written = []
    for name, text in rendered.items():
        path = layout.memory_dir / name
        backup_file(path, layout.backups_dir, keep=backups_to_keep)
        atomic_write_text(path, text)
        written.append(path)
    return written
