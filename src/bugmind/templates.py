"""Report template engine: Markdown files with YAML front matter, rendered by a
sandboxed Jinja2 environment (templates cannot execute code or touch files).

Template ids map to files: "default/professional" ->
  <dir>/default/professional_en.md   (falls back to <dir>/default/professional.md)
Output is always English, so the "en" variant is used.
Search order: user templates (~/.config/bugmind/templates), then built-ins.
Project templates live under "projects/<project>/<name>".

Front matter keys:
  name:      display name
  language:  en | ar
  guidance:  formatting rules handed to the AI (optional)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from bugmind.models import BugAnalysis
from bugmind.security import safe_join, validate_slug

BUILTIN_DIR = Path(__file__).parent / "builtin_templates"


class TemplateNotFound(Exception):
    pass


@dataclass
class ReportTemplate:
    id: str
    language: str
    path: Path
    name: str
    guidance: str
    body: str


class TemplateValidationError(ValueError):
    pass


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    text = text.replace("\r\n", "\n")
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end == -1:
            raise TemplateValidationError("Front matter starts with '---' but has no closing '---' line")
        try:
            meta = yaml.safe_load(text[4:end]) or {}
        except yaml.YAMLError as exc:
            raise TemplateValidationError(f"Invalid YAML front matter: {exc}") from exc
        if not isinstance(meta, dict):
            raise TemplateValidationError("Front matter must be a YAML mapping (key: value)")
        return meta, text[end + 5:]
    return {}, text


def parse_template(text: str, template_id: str, path: Path, language: str = "en") -> ReportTemplate:
    meta, body = _split_front_matter(text)
    return ReportTemplate(
        id=template_id, language=str(meta.get("language", language)), path=path,
        name=str(meta.get("name", template_id)), guidance=str(meta.get("guidance", "")).strip(), body=body,
    )


# Fully populated example used for previews and to validate templates before saving.
SAMPLE_ANALYSIS = BugAnalysis(
    title="[Orders API] Example: empty `ordering` returns 500 instead of 400",
    component="Orders API", category="input-validation", page_url="https://app.example.com/orders",
    api_endpoints=["/api/v1/orders?ordering="], http_method="GET", actual_status_code="500",
    expected_status_code="400", description="Example description rendered for preview purposes only.",
    steps_to_reproduce=["Send `GET /api/v1/orders?ordering=`", "Observe the status code"],
    actual_result=["`500 Internal Server Error`"], expected_result=["`400 Bad Request` with a validation error"],
    severity="medium", severity_justification="Server error on user input.", impact="List views fail.",
    evidence=['{"detail": "Internal server error"}'], confirmed_facts=["Empty ordering returns 500"],
    hypotheses=["Ordering is likely passed unvalidated to the ORM"], missing_information=["User role"],
    clarification_questions=["Which user role was used?"],
)


def _na(value: Any, default: str = "N/A") -> Any:
    return value if value not in (None, "", []) else default


def _bullets(items: list[str], default: str = "N/A") -> str:
    return "\n".join(f"  - {i}" for i in items) if items else f"  - {default}"


def _numbered(items: list[str], default: str = "N/A") -> str:
    return "\n".join(f"  {n}. {i}" for n, i in enumerate(items, 1)) if items else f"  1. {default}"


def _env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(undefined=StrictUndefined, keep_trailing_newline=True,
                               trim_blocks=True, lstrip_blocks=True, autoescape=False)
    env.filters.update(na=_na, bullets=_bullets, numbered=_numbered)
    return env


class TemplateEngine:
    def __init__(self, user_dir: Path | None = None, builtin_dir: Path = BUILTIN_DIR) -> None:
        self.user_dir = user_dir
        self.builtin_dir = builtin_dir
        self.dirs = [d for d in (user_dir, builtin_dir) if d]

    @staticmethod
    def _parts(template_id: str) -> list[str]:
        parts = template_id.strip("/").split("/")
        return [validate_slug(p, "template path segment") for p in parts]

    def resolve(self, template_id: str, language: str = "en") -> ReportTemplate:
        parts = self._parts(template_id)
        for base in self.dirs:
            for stem in (f"{parts[-1]}_{language}", parts[-1]):
                try:
                    path = safe_join(base, *parts[:-1], f"{stem}.md")
                except ValueError:
                    continue
                if path.is_file():
                    return parse_template(path.read_text(encoding="utf-8"), template_id, path, language)
        raise TemplateNotFound(f"Template {template_id!r} ({language}) not found in {', '.join(map(str, self.dirs))}")

    def list(self) -> list[tuple[str, str, Path]]:
        """Return (template_id, language, path) for every available template file."""
        seen: dict[tuple[str, str], Path] = {}
        for base in self.dirs:
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.md")):
                rel = path.relative_to(base).with_suffix("")
                stem, lang = rel.name, "any"
                for suffix in ("_en", "_ar"):
                    if stem.endswith(suffix):
                        stem, lang = stem[: -len(suffix)], suffix[1:]
                tid = "/".join([*rel.parts[:-1], stem])
                seen.setdefault((tid, lang), path)
        return [(tid, lang, p) for (tid, lang), p in sorted(seen.items())]

    def render(self, template: ReportTemplate, analysis: BugAnalysis, **extra: Any) -> str:
        ctx = analysis.model_dump(mode="json")
        ctx.update(extra)
        try:
            return _env().from_string(template.body).render(**ctx).strip() + "\n"
        except TemplateError as exc:
            raise TemplateError(f"{template.path}: {exc}") from exc

    # ------------------------------------------------------------------ editing
    def is_user_template(self, template: ReportTemplate) -> bool:
        return self.user_dir is not None and self.user_dir.resolve() in template.path.resolve().parents

    def has_builtin(self, template_id: str) -> bool:
        parts = self._parts(template_id)
        return any(safe_join(self.builtin_dir, *parts[:-1], f"{stem}.md").is_file()
                   for stem in (f"{parts[-1]}_en", parts[-1]))

    def validate_text(self, text: str, template_id: str = "preview") -> tuple[ReportTemplate, str]:
        """Parse and test-render template text against SAMPLE_ANALYSIS.

        Returns (template, rendered_preview); raises TemplateValidationError with a readable message.
        """
        tpl = parse_template(text, template_id, Path(f"<{template_id}>"))
        if not tpl.body.strip():
            raise TemplateValidationError("Template body is empty")
        try:
            rendered = self.render(tpl, SAMPLE_ANALYSIS)
        except Exception as exc:  # syntax errors, unknown fields, sandbox violations
            msg = str(exc).split(": ", 1)[-1] if str(exc).startswith("<") else str(exc)
            raise TemplateValidationError(f"Template error: {msg}") from exc
        return tpl, rendered

    def save_user_template(self, template_id: str, text: str) -> Path:
        """Validate, then atomically write to the user template dir. Built-ins are never modified."""
        if self.user_dir is None:
            raise TemplateValidationError("No user template directory configured")
        parts = self._parts(template_id)
        self.validate_text(text, template_id)
        path = safe_join(self.user_dir, *parts[:-1], f"{parts[-1]}_en.md")
        from bugmind.storage.atomic import atomic_write_text
        atomic_write_text(path, text if text.endswith("\n") else text + "\n", mode=0o644)
        return path

    def delete_user_template(self, template_id: str) -> list[Path]:
        """Remove the user's copy (falls back to the built-in if one exists)."""
        if self.user_dir is None:
            return []
        parts = self._parts(template_id)
        removed = []
        for stem in (f"{parts[-1]}_en", parts[-1]):
            path = safe_join(self.user_dir, *parts[:-1], f"{stem}.md")
            if path.is_file():
                path.unlink()
                removed.append(path)
        return removed
