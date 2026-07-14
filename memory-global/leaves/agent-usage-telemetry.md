---
name: agent-usage-telemetry
description: The opt-in cross-installation usage rollup — how a machine emits an anonymized counts-only aggregate to a per-channel tracking sink and how the aggregator pulls them; the consent contract, the exact emitted schema, what is never emitted, the two sink ids, and the private-sink reachability constraint.
type: reference
schema: leaf/v1
created: 2026-07-13
last_verified: 2026-07-13
---

## Difficulty

The local usage report (`agent-stats.py`, see [[solved-by-007-marker-and-usage-stats]]) answers "how much is this system used *on this machine*", but a single machine's usage was invisible to any other — there was no way to see usage across a fleet of installations "in one place", including non-Yandex installations that never touch Startrek. A naive cross-machine rollup would either ship other installations' data without consent or leak task ids / tracker keys / paths into a shared surface. The mechanism must collect a fleet-wide rollup while (1) never emitting anything from a machine that did not opt in and (2) never emitting anything but anonymized counts.

## Guidance

**The two commands** (`scripts/usage-digest.py`, mirroring the channel-pull shape of `core-difficulty-digest.py`):

- `emit` — computes a compact aggregate over this machine's last **complete ISO week** and, **only when opt-in is ON**, posts it as one fenced-JSON comment on this machine's channel sink. Reuses `agent-stats.aggregate` for the counts and the `difficulty_channel` adapters' `add_comment` for the append.
- `pull` (alias `agent-stats.py --cross-machine`) — read-only; lists every configured sink's comments, extracts the well-formed aggregates (ignoring human chatter), dedups re-emitted periods per `(installation_id, period)`, and sums the disjoint ISO-week rows into one rollup, segmented `non-yandex` (GitHub) vs `yandex` (Startrek).

**Opt-in — default OFF.** A machine emits nothing unless `usage_telemetry=on` is set in `agent-identity.local` (the machine-local, never-git-tracked identity file). With the key absent or ≠ `on`, `emit` prints `opt-in OFF; nothing emitted` and posts zero bytes. This is load-bearing, not cosmetic: the rollup ships *other* installations' data, so consent defaults to off (standard distributed-telemetry data-minimization). Turn it on per machine by adding that one line.

**Exactly what a machine emits when ON** — the whitelisted, counts-only payload (`usage-digest.py::WHITELIST` refuses to post anything outside it):

```json
{
  "schema": "usage/v1",
  "period": "2026-W28",
  "installation_id": "<16-hex salted sha256>",
  "channel": "github" | "startrek",
  "n_invocations": 0, "n_resolved": 0, "n_quality_rated": 0,
  "n_marked_precedents": 0, "mean_quality": null,
  "total_cost_usd": 0.0, "n_spawns": 0
}
```

- `period` is a **disjoint ISO-week bucket** (`YYYY-Www`), never a rolling window, so `pull` can sum distinct periods without double-counting overlapping days.
- `installation_id` is a **salted sha256** of a machine-stable id (`socket.getfqdn()` + a per-machine `usage_salt` persisted in `agent-identity.local`) — stable across emits so the aggregator's dedup key holds, but the raw hostname/login never leaves the machine.
- `n_quality_rated` travels alongside `mean_quality` so `pull` can compute a **rated-row-weighted** fleet mean — Σ(mean_qᵢ·n_ratedᵢ)/Σ(n_ratedᵢ), never invocation-weighted, so a high-traffic low-quality installation cannot dominate.

**What a machine NEVER emits:** task ids, tracker keys, ticket/issue references, filesystem paths, prompts, ticket bodies, commit messages — any of them present in the payload makes `assert_counts_only` raise before the post. Only counts + the anonymized id leave a machine.

**The two sinks** (a dedicated tracking issue/ticket per channel; each emit is one append-only comment, so cross-machine emits never race on a shared blob and history is preserved):

- `USAGE_SINK_GITHUB` — a tracking issue in a **PRIVATE** GitHub repo (honoring the "closed" requirement), for the non-Yandex channel. **Default empty (unprovisioned):** the maintainer's fine-grained PAT cannot create a repo (`403 Resource not accessible by personal access token`), so the private repo + issue is created **manually** when a non-Yandex installation first opts into telemetry (default OFF — nothing emits there until then), then wired via `usage_sink_github`. An empty sink fail-open-skips.
- `USAGE_SINK_STARTREK` — a tracking ticket in Startrek, for the Yandex channel. **Provisioned:** `OOSEVEN-16` (do-not-close aggregate ticket), wired as the in-code default.

A fork/operator overrides both without editing code via `agent-identity.local` keys `usage_sink_github=owner/repo#N` and `usage_sink_startrek=QUEUE-N`; the in-code constants are only defaults.

**Cadence.** Run `emit` once per closed ISO week (a cron / scheduled step, never on the `resolve` hot path — emit is a separate opt-in periodic action). Re-emitting the same week is safe: `pull` keeps the latest comment per `(installation_id, period)`.

**Reachability constraint (honest scope, not overclaimed).** A **private** GitHub sink is reachable only by the operator's own fleet plus accounts explicitly granted repo access — an arbitrary external fork cannot contribute to it without a granted sink credential. So "all installations" means, in practice, **the operator's fleet + granted collaborators** on the GitHub (non-Yandex) side and **all internally-reachable Yandex installations** (which all reach the internal ticket) on the Startrek side. A truly public global rollup would require a **public** sink, which the "closed/private" requirement excludes — that tension is real and documented here rather than silently papered over.

## See also

- [[solved-by-007-marker-and-usage-stats]] — the local usage report + the `solved_by_007` marker this telemetry sits atop; `emit` reuses its `aggregate`.
- [[tracker-write-token]] — the `~/.tracker-token` write-auth the Startrek `add_comment` reuses.
- `scripts/core-difficulty-digest.py` — the existing channel-pull aggregator whose shape `usage-digest.py` mirrors.
