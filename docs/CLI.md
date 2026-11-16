# CLI reference

Every command accepts `--help`. Exit codes: `0` success, `1` failure, `2` configuration error,
`3` exact duplicate refused, `4` not saved because the agent asked for more information.

## `bugmind`
Launches the TUI. On first run with no provider configured, the provider picker opens.

## `bugmind init [--provider claude|opencode|custom]`
Creates the config file, template folder and storage, and renders the empty memory files.

## `bugmind add`
```
bugmind add "description"            # inline
bugmind add -f bug.txt               # from a file
cat log.txt | bugmind add            # from stdin
bugmind add                          # opens $EDITOR
bugmind add -i shot.png -i 2.jpg "Pay button shows this error"   # with screenshots
```
| Option | Meaning |
|---|---|
| `-p, --provider` | Use another provider for this run only |
| `-i, --image PATH` | Attach a screenshot (PNG/JPEG/GIF/WebP, ≤ 5 MB, up to 5; repeatable). Checked by content, copied to `<project>/attachments/<BUG-ID>/` on save |
| `-y, --yes` | Save even if the agent asked clarification questions (status `needs_info`) |
| `--force` | Save even if the text is an exact duplicate of an archived bug |

Output: the rendered report, redaction/duplicate notices, open questions, then the saved ID and
assigned pattern. Exact duplicates are refused *before* the agent is called.

## `bugmind list [--severity] [--category] [--status] [-n LIMIT]`
Table of the latest revision of each bug. Warns if corrupted archive lines were skipped.

## `bugmind show BUG-0001 [--original]`
Report, metadata, open questions and hypotheses. `--original` also prints the original input.

## `bugmind search "terms"`
All terms must match (id, title, input, description, endpoints, category, status, pattern ids).

## `bugmind status BUG-0001 <status>`
`confirmed`, `unconfirmed`, `needs_info` or `invalid`. Appends a revision and rebuilds memory.
`confirmed` turns a candidate rule into an active one; `invalid` removes the bug from knowledge
(the record itself is kept).

## `bugmind memory [--rebuild]` · `bugmind patterns` · `bugmind stats`
Print `BACKEND_PREVENTION.md`, `BUG_PATTERNS.md`, `STATISTICS.md`. `--rebuild` regenerates all
memory files (and any missing report files) from `bugs.jsonl`.

## `bugmind export [prevention|patterns|stats|all]`
| Option | Meaning |
|---|---|
| `-o, --output` | Destination directory (or a file path for single-file exports). Default: `.` |
| `--zip` | For `all`: produce `bugmind-knowledge-<project>-<timestamp>.zip` |
| `--force` | Overwrite existing files |
| `-d, --downloads` | Save to your Downloads folder (`XDG_DOWNLOAD_DIR`, else `~/Downloads`). Never overwrites: a second export becomes `BACKEND_PREVENTION (1).md` |

Example — the bug-prevention instructions for your coding agent: `bugmind export prevention --downloads`

`all` contains the three memory files, `bugs.jsonl`, `reports/` and `MANIFEST.json`.
Export is always manual; BugMind has no scheduled or automatic reporting.

## `bugmind templates` · `bugmind providers`
List template ids (● = active) / check agent availability and versions.

## `bugmind config`
```
bugmind config                         # print config
bugmind config path                    # config + data locations
bugmind config get provider.timeout_seconds
bugmind config set agent claude        # aliases: agent, provider, timeout, template, project, data_dir, auto_save
bugmind config set provider.custom.command "['/usr/local/bin/my-agent', '--json']"
bugmind config set provider.claude.model sonnet
bugmind config set provider.claude.effort low   # low|medium|high|xhigh|max, or null for Claude's default
bugmind config set analysis.auto_save never
```
Values are parsed as YAML scalars/lists and validated; invalid values are rejected.

| Key | Default | Notes |
|---|---|---|
| `provider.default` | – | `claude`, `opencode`, `custom` |
| `provider.timeout_seconds` | 300 | 10–3600 |
| `provider.claude.executable` / `.model` / `.effort` / `.extra_args` | `claude` / `sonnet` / `low` / `[]` | `--model` and `--effort` keep analysis fast; `null` = Claude default |
| `provider.opencode.executable` / `.model` / `.agent` / `.extra_args` | `opencode` | model is `provider/model` |
| `provider.custom.name` / `.command` / `.input_mode` / `.env` | | see PROVIDERS.md |
| `output.template` | `default/professional` | |
| `storage.data_dir` | XDG data dir | |
| `storage.project` | `default` | lowercase slug; one archive per project |
| `storage.backups_to_keep` | 20 | memory backups |
| `analysis.duplicate_threshold` | 0.88 | near-duplicate similarity |
| `analysis.redact_secrets` | true | |
| `analysis.auto_save` | `complete` | TUI: `complete`, `always`, `never` |
