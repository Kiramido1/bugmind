# Example workflow

## 1. Report a bug (Arabic input, English output)

In the TUI, type and press Enter:

> لما ببعت باراميتر ordering فاضي في GET /api/v1/products?ordering= السيرفر بيرجع 500
> بدل validation error. نفس المشكلة في /api/v1/orders?ordering= كمان.

BugMind streams the agent's progress, then shows the English report in your template, the
severity (only if justified), category, pattern assessment, hypotheses, and any open questions:

```
- **Bug Title:** [API / List Endpoints] Empty `ordering` query parameter returns 500 …
- **Environment & Endpoint Details:**
  - **API Endpoint:** /api/v1/products?ordering=, /api/v1/orders?ordering=
  - **Request Method & Status Code:** GET — 500
…
Missing information — reply with details, or /save to keep it as needs_info:
- What is the response body returned with the 500?
```

Reply with the details (they're merged into the same draft), or `/save`. Without open
questions the bug is saved automatically (`analysis.auto_save`).

```
✓ Saved BUG-0001 · patterns: PAT-001 · status needs_info · prevention memory updated.
```

Because it is a single unconfirmed bug, `RULE-001` appears under **Candidate Rules**.

## 2. A recurrence

```bash
bugmind add "GET /api/v1/customers?ordering=-nonexistent_field returns HTTP 500 instead of 400."
```

The agent sees the known patterns and their rules, recognises the same engineering problem, and
BugMind links it: `Saved BUG-0002 · patterns: PAT-001`. `PAT-001` now has 2 occurrences, so
`RULE-001` becomes an **active** rule with both bug IDs as evidence, and its instructions merge
what the new bug taught.

## 3. Confirm and review

```bash
bugmind status BUG-0001 confirmed    # confidence → confirmed-recurring
bugmind memory                       # BACKEND_PREVENTION.md
bugmind patterns                     # BUG_PATTERNS.md
bugmind stats                        # STATISTICS.md
```

## 4. Share the knowledge when you decide to

```bash
bugmind export prevention -o ./AGENTS-backend-rules.md    # one file
bugmind export all -o ~/exports --zip                      # full package
```

Hand `BACKEND_PREVENTION.md` to backend developers, or drop it into a repository as instructions
for coding agents (e.g. reference it from `AGENTS.md` / `CLAUDE.md`).
