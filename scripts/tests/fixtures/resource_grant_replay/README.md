# Resource-grant replay fixture

`denials.jsonl` is the answer key the resource-grant hook and its per-segment
resolver are replayed against. Each line is one tool call a spawned specialist
made (or, where named below, a call constructed by analogy). Each line also
records what the new hook must decide for that call and which resources the
call changes.

Validate with `python3 scripts/tests/check_replay_fixture.py <file | /dev/stdin>`.
It exits 0 and prints per-class counts, or exits 1 and names the failing rule.

## Provenance

The cases come from the permission-stop scan run in session
`4e2d6f6e-bc60-4590-b1d8-dae58ab925df`. That scan classified 8042 permission
stops from spawned specialists by denial kind (A1 needs-approval with no prompt
available, A3 permission denied, A5 directory denied by settings, A6 path
outside the allowed directories). Each case's `source` field names the denial
kind and the 8-character session prefix it was taken from.

Selection rule: for each class below, take the clearest real denials of that
shape, preferring different sessions. Long heredoc bodies and commit messages
are shortened. Compound commands that were unrelated to the class are trimmed
to the relevant segment. The `source` field says so whenever a case was trimmed.

Grants are anonymized. The `improvement-scan-batch2` cases carry that plan's
real grant list. The others carry the standard developer grant shape (`git -C`
add/commit/status, pytest), because the scan did not keep each session's
grant list.

## Schema

One JSON object per line, written with `json.dumps` default separators.

| Field | Type | Meaning |
|---|---|---|
| `id` | str | Unique case id, `<class>-NN`. |
| `class` | str | One of the classes below. |
| `kind` | str | Specialist kind that made the call. |
| `tool` | str | `Bash`, `Read`, `Edit` or `Write`. |
| `command` / `path` | str | `command` for Bash, `path` for the other tools; never both. |
| `venue` | str | Always `{VENUE}`. |
| `branch` | str | The stage's own branch. |
| `grants` | list[str] | The stage's permission grants. |
| `expect` | str | `allow`, `deny`, or `none` (the hook must not act). |
| `expected_effects` | list | Resources the call changes; empty for read-only calls. |
| `opaque` | bool | True when the effect cannot be read from the command text. |
| `opaque_kind` | str | `python-c`, `stdin-body` or `script-file`; present iff `opaque`. |
| `allowed_by` | str | `allow-entry` or `mode`; present iff class is `allowed-today`. |
| `resolver` | str | `fall-through`, on a non-opaque Bash case only: the per-segment resolver deliberately leaves the command unresolved (here, git in a repository other than the venue's), so the hook falls through to the user prompt instead of acting. `expect` still states what the answer key says the effect is; the hook-level replay expects `none` for these cases. |
| `source` | str | Where the case came from. |

Path placeholders: `{VENUE}` is the stage's worktree root, `{SCRATCH}` is the
session scratchpad, and `{OUTSIDE}` is any directory outside both. Every path
in a case starts with one of them.

## Effect form (git answer key)

Effects mirror `scripts/agentctl/resources.py`:

- `{"kind": "file", "path": P, "op": "write"}`. This is a `FileResource` in
  write mode. Reads need no grant, so they are never listed.
- `{"kind": "vcs_ref", "remote": R, "ref": F, "op": O}`. This is a
  `VcsRefResource`, with `O` one of `commit`, `push` or `land`.

Git commands resolve as follows:

- A git write subcommand (add, commit, checkout, restore, ...) writes the
  worktree: a `file` write on the worktree root.
- A local commit also moves the local ref: `vcs_ref` with remote `"."`,
  the current branch as `ref`, and op `commit`. A commit effect is always
  paired with a file write on the worktree root (`{VENUE}`, or one segment
  under `{OUTSIDE}` for a foreign checkout).
- A push is `vcs_ref` with the named remote and ref and op `push`. It is never
  given remote `"."`.

## Coverage model

The checker tests every `expect` against an implied stage resource set:

- `developer` and `tech-writer` may write files at or under `{VENUE}` and
  commit to the stage's own branch.
- `developer` may also push the stage's own branch to `origin`, unless that
  branch is `main`, `master` or `release-*`.
- Every other kind may write nothing.

`allow` requires every listed effect to be covered. `deny` requires at least
one uncovered effect. `none` cases are not checked against the model.

## Classes

| Class | Min | Expect | What it pins |
|---|---|---|---|
| `prefix-mismatch` | 3 | allow | A covered write spelled differently from the grant prefix. |
| `git-C` | 3 | allow | `git -C {VENUE} ...` writes inside the venue. |
| `python-c` | 3 | allow | Opaque `python3 -c`. |
| `python-stdin` | 3 | allow | Opaque `python3 -` heredoc body. |
| `compound` | 3 | allow | Chained add/commit/push in one call. |
| `push-own-branch` | 3 | allow | Push of the stage's own branch. |
| `scratchpad-read` | 3 | allow | Reads of `{SCRATCH}`; no effects. |
| `uncovered-write` | 6 | deny | Bash writes outside the stage's resources. |
| `edit-write-outside` | 1 | none | Edit/Write outside the venue; the harness owns these. |
| `allowed-today` | 3 | none | Calls the harness already allows; the hook must not narrow them. |

The deny set must include all of these: a redirect outside the venue, an
opaque `python3 -c` writing outside, a push to a protected branch, a push to
another branch, and a `git -C` commit in a foreign checkout.

## Observed cases that fall through

`prefix-mismatch-02..04` are observed denials of `git push origin x:x`. Their
`resolver` is `fall-through`: after a colon git matches a bare destination
against the remote's existing refs first (an existing `refs/remotes/x` is
updated instead of a branch), which cannot be seen offline, so the per-segment
resolver resolves only `x:refs/heads/x` and the colon-less `git push origin x`
(the latter only while no `remote.<r>.push` entry exists and `push.default` is
not `upstream`/`tracking`).
The hook shows the user prompt for the `x:x` spelling.

## Cases constructed by analogy

The scan has no observed denial for these. Their `source` says so.

- `uncovered-write-05`: push to `main`.
- `uncovered-write-06`: push to another plan's branch.
- `uncovered-write-07`: `git -C` commit in the canonical checkout. Its
  `resolver` is `fall-through`: the per-segment resolver never resolves git in
  another repository (its refs, hooks and config are not the venue's), so the
  hook shows the user prompt instead of denying. Accepted at plan level.
- `uncovered-write-05` pushes with the full destination `refs/heads/main`: a bare
  `HEAD:main` is not resolved (see `prefix-mismatch-02..04`).
- `allowed-today-01..04`: calls the harness allows today, not denials. Two come
  from ambient allow entries (git status, git log). Two come from the
  acceptEdits behaviour recorded in
  `docs/operations/resource-grant-hook-harness-probe.md` Q6 (mkdir, touch).

## Leak rule

The checker scans every fixture line and this README for user-home paths in
any spelling. That covers the absolute Linux and macOS home prefixes, the
superuser home, the dash-encoded project-directory form, the tilde-slash
shorthand, and the HOME environment variable in both brace forms. It also
scans for token-shaped strings: GitHub, Slack, AWS and OpenAI-style key
prefixes, OAuth and Bearer tokens, JWTs, long hex runs, and long mixed-case
base64 runs. One match fails the whole file.
