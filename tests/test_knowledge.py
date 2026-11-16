from datetime import datetime, timedelta, UTC

from bugmind.knowledge import (
    assign_pattern_ids, build_knowledge, compute_statistics, render_patterns, render_prevention, render_statistics,
)
from bugmind.models import (
    BugAnalysis, BugRecord, Confidence, PatternAssessment, RuleProposal, Severity,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def rec(n, pids, *, relation="new", name="Ordering not validated", items=("Validate ordering",), status="unconfirmed",
        severity="medium", component="Orders API", related=None, title=None):
    pa = PatternAssessment(relation=relation, pattern_name=name, pattern_category="input-validation",
                           pattern_description=f"desc {n}", related_pattern_id=related,
                           rule=RuleProposal(title=title or f"Rule v{n}", problem="p", required_behavior=list(items)))
    return BugRecord(id=f"BUG-{n:04d}", original_input=f"bug {n}", pattern_ids=pids, status=status,
                     analysis=BugAnalysis(title=f"t{n}", category="input-validation", severity=severity,
                                          component=component, api_endpoints=[f"/api/{n}"], pattern=pa),
                     created_at=T0 + timedelta(days=n), updated_at=T0 + timedelta(days=n))


def test_assign_pattern_ids():
    k = build_knowledge([rec(1, ["PAT-001"])])
    pats = k.patterns
    assert assign_pattern_ids(PatternAssessment(relation="recurrence", matched_pattern_id="PAT-001"), pats) == ["PAT-001"]
    # recurrence pointing at a non-existent id but same name -> existing pattern (no duplicate)
    assert assign_pattern_ids(PatternAssessment(relation="recurrence", matched_pattern_id="PAT-099",
                                                pattern_name="ordering NOT validated!"), pats) == ["PAT-001"]
    # "new" with an existing name is deduplicated
    assert assign_pattern_ids(PatternAssessment(relation="new", pattern_name="Ordering not validated"), pats) == ["PAT-001"]
    # genuinely new / related -> fresh id
    assert assign_pattern_ids(PatternAssessment(relation="related", pattern_name="Pagination overflow"), pats) == ["PAT-002"]
    # insufficient evidence -> unclassified
    assert assign_pattern_ids(PatternAssessment(relation="insufficient", pattern_name="x"), pats) == []
    assert assign_pattern_ids(PatternAssessment(relation="new"), pats) == []
    # ids of patterns that exist only on invalid bugs are never reused
    assert assign_pattern_ids(PatternAssessment(relation="new", pattern_name="Other"), pats, {"PAT-005"}) == ["PAT-006"]


def test_single_unconfirmed_bug_gives_candidate_rule_only():
    k = build_knowledge([rec(1, ["PAT-001"])])
    assert k.patterns[0].confidence == Confidence.SUSPECTED
    assert k.active_rules == [] and len(k.candidate_rules) == 1
    text = render_prevention(k)
    assert "Candidate Rules" in text and "RULE-001" in text


def test_recurrence_activates_rule_merges_and_keeps_traceability():
    recs = [rec(1, ["PAT-001"], items=["Validate ordering at the API boundary"]),
            rec(2, ["PAT-001"], relation="recurrence", items=["Validate ordering at API boundary", "Return 400 on bad ordering"],
                title="Validate and reject bad ordering")]
    k = build_knowledge(recs)
    p = k.patterns[0]
    assert p.occurrence_count == 2 and p.bug_ids == ["BUG-0001", "BUG-0002"]
    assert p.name == "Ordering not validated" and p.description == "desc 2"  # stable name, refined description
    assert p.confidence == Confidence.RECURRING
    r = k.active_rules[0]
    assert r.id == "RULE-001" and r.title == "Validate and reject bad ordering"
    assert r.bug_ids == ["BUG-0001", "BUG-0002"]
    # near-duplicate instruction merged, distinct one kept
    assert len(r.required_behavior) == 2 and "Return 400 on bad ordering" in r.required_behavior
    assert p.endpoints == ["/api/1", "/api/2"]


def test_confirmation_and_invalid_status():
    k = build_knowledge([rec(1, ["PAT-001"], status="confirmed")])
    assert k.patterns[0].confidence == Confidence.SINGLE_CONFIRMED and len(k.active_rules) == 1
    k = build_knowledge([rec(1, ["PAT-001"], status="confirmed"), rec(2, ["PAT-001"])])
    assert k.patterns[0].confidence == Confidence.CONFIRMED_RECURRING
    k = build_knowledge([rec(1, ["PAT-001"], status="invalid")])
    assert k.patterns == [] and k.rules == []


def test_rebuild_is_deterministic_and_order_independent():
    recs = [rec(1, ["PAT-001"]), rec(2, ["PAT-002"], name="Auth bypass", related="PAT-001"), rec(3, ["PAT-001"])]
    a, b = build_knowledge(recs), build_knowledge(list(reversed(recs)))
    assert [p.model_dump() for p in a.patterns] == [p.model_dump() for p in b.patterns]
    assert [r.model_dump() for r in a.rules] == [r.model_dump() for r in b.rules]
    assert a.pattern("PAT-002").related_patterns == ["PAT-001"]


def test_statistics_are_derived_from_records():
    recs = [rec(1, ["PAT-001"], severity="high"), rec(2, ["PAT-001"], severity="high"),
            rec(3, [], severity="low", component="Auth"), rec(4, ["PAT-002"], status="invalid", severity="critical")]
    s = compute_statistics(build_knowledge(recs))
    assert s.total == 4 and s.counted == 3
    assert s.by_severity[Severity.HIGH] == 2 and s.by_severity[Severity.CRITICAL] == 0
    assert s.top_components[0] == ("Orders API", 2)
    assert s.unclassified == 1
    assert [p.id for p in s.recurring_patterns] == ["PAT-001"]
    text = render_statistics(build_knowledge(recs))
    assert "**Total recorded bugs:** 4" in text and "| Orders API | 2 |" in text
    assert "PAT-001" in render_patterns(build_knowledge(recs))
