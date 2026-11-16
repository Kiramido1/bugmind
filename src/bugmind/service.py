"""Application service shared by the CLI and the TUI.

Flow: analyze (AI) -> Draft -> save (Python: validate, ID, dedupe, persist,
report file, memory rebuild). The AI never writes to storage.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from bugmind.analysis import DuplicateMatch, build_system_prompt, build_task_prompt, find_duplicates, parse_response
from bugmind.config import AppConfig, config_dir, project_dir
from bugmind.images import EXTENSIONS, Attachment, check_count, load_image
from bugmind.knowledge import Knowledge, assign_pattern_ids, build_knowledge, write_memory
from bugmind.models import AgentResponse, BugRecord, ConfirmationStatus, Intent, utcnow
from bugmind.providers import Provider, ProviderRequest, get_provider
from bugmind.security import redact_secrets
from bugmind.storage.bug_store import BugStore
from bugmind.templates import ReportTemplate, TemplateEngine


MAX_INPUT_CHARS = 100_000  # ≈ 25k tokens; real bug reports with logs fit comfortably

class DuplicateBugError(Exception):
    def __init__(self, matches: list[DuplicateMatch]) -> None:
        super().__init__(f"Exact duplicate of {', '.join(m.bug_id for m in matches)}")
        self.matches = matches


@dataclass
class Draft:
    original_input: str
    response: AgentResponse
    provider: str
    template_id: str
    report: str = ""
    redactions: int = 0
    duplicates: list[DuplicateMatch] = field(default_factory=list)
    cost_usd: float | None = None
    duration_s: float = 0.0
    images: list[Attachment] = field(default_factory=list)

    @property
    def is_bug(self) -> bool:
        return self.response.intent == Intent.BUG_REPORT and self.response.bug is not None

    @property
    def needs_clarification(self) -> bool:
        return bool(self.is_bug and self.response.bug.clarification_questions)  # type: ignore[union-attr]


@dataclass
class SaveResult:
    record: BugRecord
    report_path: Path
    memory_files: list[Path]


class BugMindService:
    def __init__(self, cfg: AppConfig, store: BugStore | None = None, engine: TemplateEngine | None = None,
                 provider_factory: Callable[[AppConfig, str | None], Provider] = get_provider) -> None:
        self.cfg = cfg
        self.store = store or BugStore(project_dir(cfg))
        self.engine = engine or TemplateEngine(config_dir() / "templates")
        self.provider_factory = provider_factory

    # ------------------------------------------------------------- queries
    def knowledge(self) -> Knowledge:
        return build_knowledge(self.store.all())

    def template(self) -> ReportTemplate:
        return self.engine.resolve(self.cfg.output.template)

    # ------------------------------------------------------------- analysis
    async def analyze(self, text: str, *, force_bug: bool = False, context: str = "",
                      allow_duplicate: bool = False, provider_name: str | None = None,
                      on_chunk: Callable[[str], None] | None = None,
                      images: list[Attachment] | None = None) -> Draft:
        images = list(images or [])
        check_count(images)
        text = text.strip()
        if not text:
            raise ValueError("Empty bug description")
        if len(text) > MAX_INPUT_CHARS:  # protects against accidental huge pastes billed to the agent
            raise ValueError(f"Bug description is {len(text):,} characters; the limit is {MAX_INPUT_CHARS:,}. "
                             "Trim the logs to the relevant part.")
        redactions = 0
        if self.cfg.analysis.redact_secrets:
            text, redactions = redact_secrets(text)

        records = self.store.all()
        duplicates = [] if context else find_duplicates(text, records, self.cfg.analysis.duplicate_threshold)
        exact = [d for d in duplicates if d.exact]
        if exact and not allow_duplicate and force_bug:
            raise DuplicateBugError(exact)  # don't spend an AI call on a known duplicate

        provider = self.provider_factory(self.cfg, provider_name)
        template = self.template()
        k = build_knowledge(records)
        request = ProviderRequest(
            system_prompt=build_system_prompt(template, k.patterns, k.rules),
            prompt=build_task_prompt(text, force_bug=force_bug, context=context, images=len(images)),
            timeout=float(self.cfg.provider.timeout_seconds),
            images=images,
        )
        result = await provider.run(request, on_chunk)
        response = parse_response(result.text)
        draft = Draft(original_input=text, response=response, provider=provider.name, template_id=template.id,
                      redactions=redactions, duplicates=duplicates, cost_usd=result.cost_usd,
                      duration_s=result.duration_s, images=images)
        if draft.is_bug:
            draft.report = self.engine.render(template, response.bug)  # type: ignore[arg-type]
        return draft

    # ------------------------------------------------------------- persistence
    def save(self, draft: Draft, *, force: bool = False) -> SaveResult:
        if not draft.is_bug:
            raise ValueError("Only bug reports can be saved")
        bug = draft.response.bug
        assert bug is not None
        with self.store.lock:
            records = self.store.all()
            exact = [d for d in find_duplicates(draft.original_input, records, 1.0) if d.exact]
            if exact and not force:
                raise DuplicateBugError(exact)
            k = build_knowledge(records)
            known_ids = {pid for r in records for pid in r.pattern_ids}
            pattern_ids = assign_pattern_ids(bug.pattern, k.patterns, known_ids)
            status = ConfirmationStatus.NEEDS_INFO if bug.clarification_questions else ConfirmationStatus.UNCONFIRMED
            now = utcnow()

            def build(bug_id: str) -> BugRecord:
                # Copy screenshots first: if that fails, no record is written (all-or-nothing).
                attachments = self._store_images(bug_id, draft.images)
                return BugRecord(
                    id=bug_id, original_input=draft.original_input, language=draft.response.language,
                    template=draft.template_id, provider=draft.provider, project=self.cfg.storage.project,
                    analysis=bug, formatted_report=draft.report, pattern_ids=pattern_ids, status=status,
                    possible_duplicate_of=[d.bug_id for d in draft.duplicates], redactions=draft.redactions,
                    attachments=attachments,
                    created_at=now, updated_at=now,
                )

            record = self.store.create(build)
            report_path = self.store.write_report(record)
            memory = self._refresh_memory()
        return SaveResult(record, report_path, memory)

    def _store_images(self, bug_id: str, images: list[Attachment]) -> list[str]:
        """Keep the evidence: copy screenshots into <project>/attachments/<BUG-ID>/ (re-validated)."""
        if not images:
            return []
        dest = self.store.layout.attachments_dir / bug_id
        dest.mkdir(parents=True, exist_ok=True)
        out = []
        for n, img in enumerate(images, 1):
            fresh = load_image(img.path)  # the file may have changed since it was attached
            target = dest / f"{n}{EXTENSIONS[fresh.media_type]}"
            shutil.copyfile(fresh.path, target)
            out.append(str(target.relative_to(self.store.layout.root)))
        return out

    def set_status(self, bug_id: str, status: ConfirmationStatus, note: str = "") -> BugRecord:
        with self.store.lock:
            rec = self.store.update(bug_id.upper(), lambda r: r.model_copy(
                update={"status": status, "note": note or r.note}))
            self._refresh_memory()
        return rec

    def rebuild_memory(self) -> list[Path]:
        with self.store.lock:
            for rec in self.store.all():  # also regenerate any missing report files
                if not (self.store.layout.reports_dir / f"{rec.id}.md").exists():
                    self.store.write_report(rec)
            return self._refresh_memory()

    def _refresh_memory(self) -> list[Path]:
        return write_memory(self.store.layout, build_knowledge(self.store.all()), self.cfg.storage.backups_to_keep)

    def memory_text(self, name: str) -> str:
        path = self.store.layout.memory_dir / name
        if not path.exists():
            self.rebuild_memory()
        return path.read_text(encoding="utf-8")
