# Architecture & storage

```
            ┌──────────── TUI (Textual) ────────────┐   ┌──── CLI (Typer) ────┐
            │ chat · history · knowledge · settings │   │ add list show …     │
            └───────────────────┬───────────────────┘   └─────────┬───────────┘
                                └──────────── service.py ─────────┘
                 analyze ─────────────┐                 save ──────────────────────┐
   security.redact ─ analysis.find_duplicates          lock ─ dedupe ─ assign pattern IDs
   analysis.build_*_prompt ─ providers.run             BugStore.create ─ write report
   analysis.parse_response (Pydantic) ─ templates      knowledge.build ─ write_memory (+backup)
```

| Module | Responsibility |
|---|---|
| `models.py` | Pydantic schemas: AI output (lenient shape, strict values), `BugRecord`, derived `Pattern`/`PreventionRule` |
| `config.py` | YAML config, validation, `config set` key handling |
| `security.py` | secret redaction, untrusted-input fencing, slug/path safety, terminal-escape stripping |
| `storage/` | atomic writes, `fcntl` locks, backups, the JSONL `BugStore` |
| `providers/` | adapter contract, safe subprocess runner, Claude Code / OpenCode / custom adapters |
| `analysis.py` | prompts, JSON extraction/validation, duplicate detection |
| `templates.py` | sandboxed template engine |
| `knowledge.py` | pattern/rule/statistics fold over the archive and Markdown rendering |
| `service.py` | the analyze → save workflow shared by CLI and TUI |
| `export.py` | on-demand export |

## Storage layout

```
<data_dir>/<project>/
├── .bugmind.lock                 # advisory lock (fcntl) for all writes
├── bugs/bugs.jsonl               # canonical archive (append-only revision log)
├── reports/BUG-0001.md           # rendered report per bug
└── memory/
    ├── BACKEND_PREVENTION.md
    ├── BUG_PATTERNS.md
    ├── STATISTICS.md
    └── .backups/                 # timestamped copies before every rewrite (rotated)
```

### bugs.jsonl

One JSON object per line; each line is a **complete revision** of a bug:

```json
{"schema_version": 1, "id": "BUG-0001", "revision": 1, "original_input": "…", "language": "ar",
 "template": "default/professional", "provider": "claude", "project": "default",
 "analysis": {"title": "…", "severity": "medium", "category": "input-validation", "…": "…",
              "pattern": {"relation": "new", "pattern_name": "…", "rule": {"title": "…", "…": "…"}}},
 "formatted_report": "…", "pattern_ids": ["PAT-001"], "status": "unconfirmed",
 "possible_duplicate_of": [], "redactions": 1, "created_at": "…", "updated_at": "…", "note": ""}
```

- The latest revision of an id is current; older revisions are never rewritten.
- `original_input`, `created_at` and `evidence` are immutable across revisions (evidence can only grow).
- IDs are allocated under the lock as max + 1 (also counting IDs seen on corrupted lines, so a
  damaged record's ID is never reused). Width grows past 4 digits automatically.
- Appends are `O_APPEND` + `fsync`. If a crash left a partial last line, a newline is inserted
  first so it stays isolated; unreadable lines are reported (`bugmind list` warns) and skipped,
  never deleted.
- Every other file is written atomically (temp file + `fsync` + `rename`) with mode 0600.

## Knowledge is a fold over the archive

When a bug is saved, Python validates the agent's pattern decision and assigns concrete IDs:

| Agent says | Python does |
|---|---|
| `recurrence` + existing `PAT-xxx` | link to that pattern |
| any relation whose pattern name matches an existing pattern | link to it (prevents duplicate rules) |
| `new` / `related` / unknown id with a name | allocate the next `PAT-xxx` (`related` keeps a link) |
| `insufficient` | leave the bug unclassified |

The pattern IDs and the agent's rule proposal are stored **in the bug record**. Patterns, rules
and statistics are then recomputed deterministically from all records (`knowledge.build_knowledge`):

- pattern name is fixed at first observation; description and signature are refined;
- rule `RULE-NNN` belongs to `PAT-NNN`; its title/problem take the latest wording and its
  instruction lists are merged (latest first, near-duplicates removed, nothing distinct dropped);
- every pattern and rule lists the bug IDs it derives from;
- confidence: `suspected` (1 unconfirmed bug) → *candidate rule* only; `single-confirmed`,
  `recurring`, `confirmed-recurring` → active rule;
- bugs marked `invalid` are excluded; statistics are counted from records, never from the AI.

Consequences: memory can be deleted and rebuilt (`bugmind memory --rebuild`) with identical
content; no AI call can overwrite or drop knowledge; traceability is structural.

## Security model

- Bug text, logs and AI output are untrusted. Input is fenced with nonce tags the content cannot
  forge; the system prompt forbids following embedded instructions; agents run with no tools.
- AI output is only data: parsed as JSON, validated, length-limited, rendered as Markdown.
  Nothing from it is executed; terminal escape sequences are stripped before display.
- Secrets (bearer tokens, API keys, passwords, JWTs, AWS keys, PEM keys, URL credentials) are
  redacted before the agent sees the text and before storage (`analysis.redact_secrets`).
- Project and template names are validated slugs; template paths are confined to their roots.
- Subprocesses: argv lists only (`shell=False`), own process group, timeouts, kill on cancel.
