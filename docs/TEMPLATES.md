# Report templates

Templates are Markdown files with YAML front matter, rendered by a **sandboxed** Jinja2
environment (no attribute tricks, no file access, no code execution).

## Where templates live

Search order (first match wins):

1. `~/.config/bugmind/templates/` — your templates and overrides
2. built-ins shipped with BugMind (`src/bugmind/builtin_templates/`)

A template id maps to a file: `default/professional` → `default/professional_en.md`
(falling back to `default/professional.md`). Project templates use
`projects/<project>/<name>`, e.g. `projects/example_backend/custom_bug_report`.

Select one with `bugmind config set template projects/example_backend/custom_bug_report`
or in the TUI (Templates → Enter). `bugmind templates` lists them.

## Format

```markdown
---
name: My compact report
guidance: |
  - Rules for the AI about how to write each field.
  - These are added to the system prompt verbatim.
---
### {{ title | na }}

**Endpoint:** {{ api_endpoints | join(", ") | na }}

{{ description | na }}

**Steps**
{{ steps_to_reproduce | numbered }}
{% if evidence %}
**Evidence**
{% for e in evidence %}
- `{{ e }}`
{% endfor %}
{% endif %}
```

- Section order, optional sections (`{% if field %}`) and formatting are entirely up to the file.
- `guidance` tells the agent how to fill the fields; keep the field *meaning* consistent so the
  structured data stays comparable across templates.
- Output is always English; templates should be written in English.

## Available fields

| Field | Type |
|---|---|
| `title`, `description`, `impact`, `severity_justification` | text |
| `component`, `page_url`, `http_method`, `actual_status_code`, `expected_status_code` | text or null |
| `category` | kebab-case text |
| `severity` | `critical`/`high`/`medium`/`low`/`unknown` |
| `api_endpoints`, `steps_to_reproduce`, `actual_result`, `expected_result`, `evidence`, `confirmed_facts`, `hypotheses`, `missing_information`, `clarification_questions` | lists of text |
| `pattern.relation`, `pattern.pattern_name`, … | pattern assessment |

## Filters

| Filter | Effect |
|---|---|
| `na` / `na("Unknown")` | value, or `N/A` when empty/null |
| `bullets` | indented `- item` list, `- N/A` when empty |
| `numbered` | indented `1. item` list, `1. N/A` when empty |
| standard Jinja filters | `join`, `indent`, `upper`, `default`, … |

## Editing templates in the TUI

Open **Templates** in the sidebar (or type `/templates`):

| Action | How |
|---|---|
| Activate | highlight a template, press `Enter` |
| Edit | `✎ Edit` button or `e` — editor on the left, live preview on the right |
| Save | `Ctrl+S` — only valid templates are saved; errors (bad YAML, unknown field, unclosed `{% if %}`) are shown under the editor |
| Cancel | `Esc` — asks before discarding unsaved changes |
| New | `＋ New` — choose an id (e.g. `projects/<project>/report`); starts as a copy of the highlighted template |
| Reset / delete | `↺ Reset` — removes your copy (built-in comes back) or deletes a template you created, after confirmation |

Built-in templates are never modified: saving a built-in writes your copy to
`~/.config/bugmind/templates/<id>_en.md`, which then takes precedence. Custom copies are tagged
`custom` in the list. Templates are validated by test-rendering them against a sample bug, so a
saved template can always render a report.

The default `default/professional` template implements the BugMind QA report format exactly
(Bug Title → Environment & Endpoint Details → Description → Steps to Reproduce → Actual Result
→ Expected Result) with its formatting rules as AI guidance.
