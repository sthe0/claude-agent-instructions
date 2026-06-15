---
name: arcanum-api-readonly-pr-fields
description: "Arcanum public HTTP API /api/v1/review-requests/{id} is read-only — PR summary/description cannot be edited via API; use web UI or amend the lead commit."
type: reference
---

# Arcanum public API: PR summary/description is not API-editable

> **Difficulty (functional ground):** desired — update an already-published PR's title
> (`summary`) or `description` programmatically (stale "PR-A" title after scope grew);
> actual — the obvious `PATCH`/`PUT` on the review-request fails, and you burn a spawn +
> several probes discovering there is no API write path.

**Fact (verified 2026-06-15 against PR 13870840):**

- `GET https://a.yandex-team.ru/api/v1/review-requests/{id}?fields=summary,description,...`
  reads fields fine (without `fields=` it returns only `{"id": …}`).
- **Writes are not supported on this endpoint.** `PATCH` and `PUT` both return
  `HTTP 405 Method Not Allowed`. `OPTIONS` confirms it definitively:
  `Allow: HEAD,GET,OPTIONS`.
- The Arcanum "Public API" doc (`docs.yandex-team.ru/arcanum/communication/public-api`,
  passport-gated) covers only specific actions (commit-suggestions etc.), not
  summary/description edits.
- `arc pr create` does **not** update an existing PR either — it errors
  `PR … already exists`, even with `-f/--force`.

**How to actually change a PR title/description:**
1. **Arcanum web UI** — edit the PR overview; fastest, no history rewrite.
2. **Amend the lead commit message + force-push the branch** — the PR title/description
   derive from the commit message; clean-looking but rewrites published-branch history.

There is no clean CLI/API path; pick UI (default) or amend by whether a history rewrite is acceptable.

> verified by: OPTIONS `Allow: HEAD,GET,OPTIONS` + 405 on PATCH/PUT, conversation 2026-06-15 (DEEPAGENT-426 / PR 13870840)
