Correction to the session-distribution claim in this issue's body.

The issue currently states: "All 5 belong to the same source session,
`7514dd40-b947-4cc5-84aa-983476c2515c`... No other approval_ask@30 session
has a single timeout." That is incorrect. An independent re-query of
`judge-usage-ledger.jsonl` (`judge==approval_ask`, `kind==decided`,
`ceiling==30`, `timed_out==true`) confirms the same 5 rows, durations, and
dates, but their `source` fields split as follows:

- 4 rows (2026-08-20, 2026-08-22 ×2, 2026-08-24) → session
  `7514dd40-b947-4cc5-84aa-983476c2515c`
- 1 row (2026-08-27T12:32:41, 29.72s) → a **different** session,
  `ed1e2dd0-8dec-4a05-a802-710612808849`

The kills are not confined to a single session. This weakens rather than
supports a "session/environment-specific" reading: a distinct-population
wedge recurring across at least two independent sessions over roughly a
week is more consistent with a generic judge-latency/wedge problem than with
one session's local artifact. The underlying finding — that the 5 timed-out
calls (28.8–29.8s) form a duration-shape-distinct population from the 203
completed calls (max 22.05s, p90 ≈15.0s), independent of which session they
ran in — is unaffected and still stands as the basis for this issue.

See `docs/operations/approval-ask-residuals.md` (judge-ceiling-drift plan,
stage 5) for the corrected write-up.
