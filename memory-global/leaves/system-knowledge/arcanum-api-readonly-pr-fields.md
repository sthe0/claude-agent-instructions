---
name: arcanum-api-readonly-pr-fields
description: "Arcanum API: the review-request resource is read-only, BUT its sub-resources /summary and /description accept PUT — edit a published PR's title/body programmatically, no web UI or force-push needed."
type: reference
schema: leaf/v1
---

# Arcanum public API: edit PR title/description via the /summary and /description sub-resources

## Difficulty

Desired — update an already-published PR's title (`summary`) or `description` programmatically (e.g. a stale verification line in the body); actual — the obvious `PATCH`/`PUT` on the review-request object returns 405, and the earlier (wrong) conclusion was "no API write path → use web UI / amend", which also fails when the stale text lives in a custom description block not derived from the commit.

## Guidance

**Fact (verified 2026-06-18 against PR 13964799; supersedes the 2026-06-15 read-only claim):**

- **Read:** `GET https://a.yandex-team.ru/api/v1/review-requests/{id}?fields=summary,description`
  — `fields=` is required (without it the response is only `{"id": …}`). `summary` = PR
  title (first line); `description` = PR body **below** the title. The body field also
  contains the commit-message block appended at create time.
- **The top-level object is read-only:** `OPTIONS /review-requests/{id}` →
  `Allow: HEAD,GET,OPTIONS`; `PATCH`/`PUT`/`POST` on it → `405`. **This is what misled
  the earlier note — it never probed the sub-resources.**
- **Writes live on dedicated sub-resources:**
  - `OPTIONS /review-requests/{id}/description` → `Allow: OPTIONS,PUT`
  - `OPTIONS /review-requests/{id}/summary` → `Allow: OPTIONS,PUT`
  - `PUT https://a.yandex-team.ru/api/v1/review-requests/{id}/description`
    with header `Authorization: OAuth <token>`, `Content-Type: application/json`,
    body `{"description": "<full new body>"}` → **`HTTP 204`** on success.
    (`summary` analogously expects `{"summary": "..."}`.)
  - `$OAUTH_TOKEN` (general internal OAuth) and `~/.tracker-token` both authorized the
    GET and the PUT here (204). PUT replaces the whole field — read it first and do a
    minimal string replace to preserve the rest.
  - A description-only PUT does **not** create a new diff-set and does **not** revert the
    PR to draft — publication state is untouched (`arc pr status` stays `(open)`).
- `arc pr create` does **not** update an existing PR — it errors `PR … already exists`
  even with `-f/--force`. So the API sub-resource is the only clean programmatic path;
  arc CLI has no PR-description edit.
- Amend + force-push only changes the commit-message-derived portion; it will **not**
  touch a custom description block set via `arc pr create -m`. Use the PUT for that.

> verified by: OPTIONS `Allow: OPTIONS,PUT` on `/description` + `PUT … 204` + read-back
> (stale line gone), conversation 2026-06-18 (DEEPAGENT-340 / PR 13964799)

## See also
