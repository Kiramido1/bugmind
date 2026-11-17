"""End-to-end service flow with the fake agent (real subprocess, deterministic output)."""

import json
import zipfile

import pytest

from bugmind.analysis import ResponseParseError
from bugmind.export import ExportError, export_knowledge
from bugmind.models import ConfirmationStatus
from bugmind.providers import ProviderError
from bugmind.service import DuplicateBugError
from bugmind.storage.bug_store import BugStore

BUG1 = "When I send an empty ordering parameter to GET /api/v1/orders?ordering= the server returns HTTP 500."
BUG2 = "GET /api/v1/products?ordering=-unknown also returns 500 instead of a 400 validation error."


async def add(service, text, **kw):
    draft = await service.analyze(text, force_bug=True, **kw)
    return service.save(draft)


async def test_analyze_save_persists_report_and_memory(service):
    res = await add(service, BUG1)
    rec = res.record
    assert rec.id == "BUG-0001" and rec.pattern_ids == ["PAT-001"]
    assert rec.original_input == BUG1 and rec.provider == "custom"
    assert rec.analysis.category == "input-validation" and rec.status == ConfirmationStatus.UNCONFIRMED
    assert res.report_path.read_text(encoding="utf-8").startswith("- **Bug Title:** [Orders API]")
    mem = service.store.layout.memory_dir
    prevention = (mem / "BACKEND_PREVENTION.md").read_text(encoding="utf-8")
    assert "Candidate Rules" in prevention and "BUG-0001" in prevention  # single unconfirmed -> candidate
    assert "PAT-001" in (mem / "BUG_PATTERNS.md").read_text(encoding="utf-8")
    assert "**Total recorded bugs:** 1" in (mem / "STATISTICS.md").read_text(encoding="utf-8")


async def test_recurrence_updates_existing_pattern_and_activates_rule(service, monkeypatch):
    await add(service, BUG1)
    monkeypatch.setenv("FAKE_AGENT_RELATION", "recurrence")
    monkeypatch.setenv("FAKE_AGENT_MATCH", "PAT-001")
    monkeypatch.setenv("FAKE_AGENT_RULE_ITEMS", '["Validate ordering at the API boundary", "Return structured 4xx errors"]')
    res = await add(service, BUG2)
    assert res.record.pattern_ids == ["PAT-001"]
    k = service.knowledge()
    assert len(k.patterns) == 1 and k.patterns[0].occurrence_count == 2
    rule = k.active_rules[0]
    assert rule.bug_ids == ["BUG-0001", "BUG-0002"]
    assert "Return structured 4xx errors" in rule.required_behavior
    prevention = service.memory_text("BACKEND_PREVENTION.md")
    assert "## RULE-001" in prevention and "Candidate Rules" not in prevention
    assert len(list(service.store.layout.backups_dir.iterdir())) >= 3  # backups before each rewrite


async def test_distinct_pattern_gets_new_id(service, monkeypatch):
    await add(service, BUG1)
    monkeypatch.setenv("FAKE_AGENT_RELATION", "related")
    monkeypatch.setenv("FAKE_AGENT_PATTERN", "Pagination limit not bounded")
    res = await add(service, "GET /api/v1/orders?limit=999999999 times out")
    assert res.record.pattern_ids == ["PAT-002"]
    assert {r.id for r in service.knowledge().rules} == {"RULE-001", "RULE-002"}


async def test_insufficient_evidence_stays_unclassified(service, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_RELATION", "insufficient")
    res = await add(service, "something is weird on the dashboard")
    assert res.record.pattern_ids == [] and service.knowledge().rules == []


async def test_exact_duplicate_blocked_before_ai_and_on_save(service):
    await add(service, BUG1)
    with pytest.raises(DuplicateBugError):
        await service.analyze("  " + BUG1.upper() + " ", force_bug=True)
    draft = await service.analyze(BUG1, force_bug=True, allow_duplicate=True)
    assert draft.duplicates and draft.duplicates[0].exact
    with pytest.raises(DuplicateBugError):
        service.save(draft)
    assert service.save(draft, force=True).record.possible_duplicate_of == ["BUG-0001"]


async def test_clarification_questions_mark_needs_info(service, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_QUESTIONS", '["Which user role?"]')
    draft = await service.analyze(BUG1, force_bug=True)
    assert draft.needs_clarification
    assert service.save(draft).record.status == ConfirmationStatus.NEEDS_INFO


async def test_secrets_redacted_before_ai_and_storage(service):
    res = await add(service, BUG1 + " Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")
    assert "abcdefghijklmnop" not in res.record.original_input and res.record.redactions == 1
    assert "abcdefghijklmnop" not in service.store.layout.bugs_file.read_text(encoding="utf-8")


async def test_status_change_adds_revision_and_updates_memory(service):
    await add(service, BUG1)
    rec = service.set_status("bug-0001", ConfirmationStatus.CONFIRMED)
    assert rec.revision == 2
    assert "Candidate Rules" not in service.memory_text("BACKEND_PREVENTION.md")  # confirmed -> active
    service.set_status("BUG-0001", ConfirmationStatus.INVALID)
    assert service.knowledge().patterns == []
    with pytest.raises(KeyError):
        service.set_status("BUG-0404", ConfirmationStatus.CONFIRMED)


async def test_memory_rebuild_from_archive_after_deletion(service, monkeypatch):
    await add(service, BUG1)
    monkeypatch.setenv("FAKE_AGENT_RELATION", "recurrence")
    monkeypatch.setenv("FAKE_AGENT_MATCH", "PAT-001")
    await add(service, BUG2)
    mem = service.store.layout.memory_dir
    before = (mem / "BUG_PATTERNS.md").read_text(encoding="utf-8").split("\n", 3)[3]  # skip timestamp line
    for f in mem.glob("*.md"):
        f.unlink()
    (service.store.layout.reports_dir / "BUG-0001.md").unlink()
    service.rebuild_memory()
    assert (mem / "BUG_PATTERNS.md").read_text(encoding="utf-8").split("\n", 3)[3] == before
    assert (service.store.layout.reports_dir / "BUG-0001.md").exists()


async def test_persistence_across_restart(service, cfg, tmp_path):
    from bugmind.service import BugMindService
    await add(service, BUG1)
    fresh = BugMindService(cfg, store=BugStore(service.store.layout.root), engine=service.engine)
    assert [r.id for r in fresh.store.all()] == ["BUG-0001"]
    assert fresh.knowledge().patterns[0].bug_ids == ["BUG-0001"]


async def test_arabic_input_preserved_and_language_recorded(service):
    text = "لما ببعت ordering فاضي في GET /api/v1/orders السيرفر بيرجع 500"
    res = await add(service, text)
    assert res.record.original_input == text and res.record.language == "ar"
    assert "Bug Title" in res.record.formatted_report  # English template output


async def test_question_intent_is_not_saved(service, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "question")
    draft = await service.analyze("what patterns do we have?")
    assert not draft.is_bug and "PAT-001" in draft.response.reply
    with pytest.raises(ValueError):
        service.save(draft)


async def test_provider_failures_surface_cleanly(service, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "garbage")
    with pytest.raises(ResponseParseError):
        await service.analyze(BUG1, force_bug=True)
    monkeypatch.setenv("FAKE_AGENT_MODE", "fail")
    with pytest.raises(ProviderError):
        await service.analyze(BUG1, force_bug=True)
    assert service.store.all() == []  # nothing persisted on failure


async def test_export_single_and_package(service, tmp_path):
    await add(service, BUG1)
    out = tmp_path / "out"
    out.mkdir()
    p = export_knowledge(service, "prevention", out)
    assert p.name == "BACKEND_PREVENTION.md" and "RULE-001" in p.read_text(encoding="utf-8")
    with pytest.raises(ExportError):
        export_knowledge(service, "prevention", out)
    assert export_knowledge(service, "prevention", out, force=True) == p
    pkg = export_knowledge(service, "all", out)
    assert {f.name for f in pkg.iterdir()} == {"BACKEND_PREVENTION.md", "BUG_PATTERNS.md", "STATISTICS.md",
                                               "bugs.jsonl", "reports", "MANIFEST.json"}
    assert json.loads((pkg / "MANIFEST.json").read_text(encoding="utf-8"))["bugs"] == 1
    z = export_knowledge(service, "all", tmp_path / "zips" if (tmp_path / "zips").mkdir() is None else out, as_zip=True)
    assert z.suffix == ".zip" and any(n.endswith("reports/BUG-0001.md") for n in zipfile.ZipFile(z).namelist())
    with pytest.raises(ExportError):
        export_knowledge(service, "monthly", out)
