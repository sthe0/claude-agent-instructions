# Resource-grant hook: what the harness lets an `allow` hook do

The resource-grant hook (a later stage of the `resource-grants-executor-request` plan) answers `allow` for a
child's tool call when the stage declared the resources that call changes. That design only works if the harness
lets a PreToolUse `allow` pass a call it would otherwise refuse, and keeps refusing what must stay refused.
This note records what the harness actually does, measured on live children, so the hook is built on observed
behaviour rather than on documentation.

Every answer below comes from one run of

```
python3 scripts/probe-hook-decision-semantics.py --only allow --raw-dump-dir /tmp/probe-allow-raw
```

on Claude Code **2.1.310**, 2026-10-27. Each cell is a real `claude -p` child (model haiku) launched by the script
through a Python subprocess. The probe hook exists only in that child's `--settings`; no live settings file is
read or changed. Isolated cells run with `lib.host_llm.isolated_run_kwargs()` (a fresh `CLAUDE_CONFIG_DIR`, so no
fleet hooks); ambient cells run on the live settings chain at `AGENT_RECURSION_DEPTH=1`, the shape of a spawned
specialist. A cell's outcome is read from the side effect on disk (or a marker that only a real read can return),
never from the child's own report.

Hook timeout: 10s

The value is provisional. The hook script itself answered in 0.001–0.017 s per call (Q2, Q3); the 10 s budget
leaves room for the stage 7 judge, whose latency stage 7 measures. The final value is fixed after the live
end-to-end run. Q5 shows that an overrun is safe: it never turns into an allow.

## Q1 — Do hooks given through `claude -p --settings` run alongside the user-level hooks?

Yes. In the ambient depth-1 child of Q4, the probe hook (present only in `--settings`) logged its call to its
witness file, and the user-level `hook-guard-permission-surface.py` (registered in `~/.claude-agent/settings.json`)
returned its G2 refusal for the same Bash call. Both ran on one tool call; the `--settings` hooks are added to the
user-level ones, not substituted for them.

> verified by: cell `allow:q4_g2_depth1:hook` — `probe hook fired=True` and the tool_result
> `PreToolUse:Bash hook error: Bash invokes `claude` with a widening flag outside the plan's own grant channel…`;
> Claude Code 2.1.310.

Consequence for stage 8: register the grant hook in the child's `--settings` (the spawn payload). Every existing
user-level guard keeps running beside it, so the grant hook does not need to reimplement any of them.

## Q2 — Is `git -C <dir> commit` denied without a hook and passed by an allow hook?

Yes, both. In an isolated acceptEdits child, `git -C <repo outside cwd> commit -q -m probe` was refused with
`This command requires approval` and no commit appeared. With the probe hook answering `allow`, the same command
ran and the repository gained a commit.

> verified by: cells `allow:q2_git_commit_outside:baseline` (sentinel ran: no; refusal `This command requires
> approval`) and `allow:q2_git_commit_outside:hook` (sentinel ran: yes, `rev-parse HEAD` succeeds); Claude Code
> 2.1.310.

Consequence for stage 8: the core mechanism works — an `allow` from the grant hook removes exactly the
"requires approval" stops this plan exists to remove. The result is positive, so the replan branch the plan kept
for a negative Q2 is not needed.

## Q3 — Do Read and Edit outside the allowed directories pass with an allow hook, and does a settings deny rule still hold?

Read and Write outside the working directories were refused without a hook (`Claude requested permissions to
read from …` / `… to write to …, but you haven't granted it yet`) and passed with the allow hook: the marker in
the file came back, and the new file appeared. A Read of a path under a `permissions.deny` rule in the child's
settings stayed refused (`File is in a directory that is denied by your permission settings.`) even with the
allow hook present — and the hook was **not called at all** for that Read (0 witness entries): the deny rule is
checked before hooks run.

> verified by: cells `allow:q3_read_outside:baseline` / `:hook` (no / yes), `allow:q3_write_outside:baseline` /
> `:hook` (no / yes), `allow:q3_read_deny_rule:hook` (no; hook calls=0); Claude Code 2.1.310.

Consequence for stage 8: the hook can widen Read and Edit/Write to any declared path, and secret-protecting deny
rules keep working without any help from the hook. The flip side: a deny rule cannot be overridden by the hook.
A stage that declares a write into a directory must not also receive an `Edit` deny over it (see Q6), or the
grant is dead on arrival.

## Q4 — At depth 1, does the G2 deny of `hook-guard-permission-surface` win over the allow hook?

Yes. In the ambient depth-1 acceptEdits child, `claude --permission-mode bypassPermissions --version` was refused
by G2 while the probe hook answered `allow` for the same call; the command did not run.

> verified by: cell `allow:q4_g2_depth1:hook` — sentinel ran: no; tool_result `PreToolUse:Bash hook error: Bash
> invokes `claude` with a widening flag…`; Claude Code 2.1.310.

Consequence for stage 8: a deny from any hook beats the grant hook's allow, so existing guards stay authoritative
and the grant hook cannot become a way around them. The hook may answer `allow` without first checking the
guards' rules.

## Q5 — What does a hook that sleeps past its timeout do?

It falls through to the normal flow and never becomes an allow. The probe hook, registered with `timeout: 2`,
slept 8 s before answering `allow`; the harness stopped waiting (the witness shows one call that started and never
finished), and the Read outside the working directories was refused with the ordinary `Claude requested
permissions to read …` message. The whole child took 8 s. A completed hook call cost 0.001–0.017 s of script time.

> verified by: cell `allow:q5_hook_timeout:hook_sleeps` — sentinel ran: no; `hook calls=1, unfinished=1`;
> refusal `Claude requested permissions to read from …`; Claude Code 2.1.310.

Consequence for stage 8: a slow or hung grant hook fails closed to today's behaviour (the call is refused as it
would be without the hook), so a timeout cannot widen anything. Its cost is only a lost allow, which shows up as
a refusal the decision log must record as "no decision (timeout)".

## Q6 — Which filesystem Bash commands does acceptEdits already let through?

Measured in the real child launch shape: ambient settings chain, depth 1, `--permission-mode acceptEdits`, one
read-only `--add-dir` carrying an `Edit` deny over it (as `spawn-specialist.py` emits for a read grant), no probe
hook. Each of `mkdir`, `touch`, `cp`, `mv`, `sed -i`, `rm` was tried with absolute paths in three places:

| Location | mkdir | touch | cp | mv | sed -i | rm | Refusal text |
|---|---|---|---|---|---|---|---|
| child's cwd | ran | ran | ran | ran | ran | ran | — |
| read-only add-dir | refused | refused | refused | refused | refused | refused | `Permission to use Bash with command … has been denied.` |
| outside both | refused | refused | refused | refused | refused | refused | `… needs approval. The path is outside the working directories for this session (…)` |

In the add-dir the refusal is a deny (the `Edit` deny rule applied to file-changing Bash), not an approval stop;
outside, it is an approval stop that an allow could lift.

> verified by: cells `allow:q6_accept_edits_fs:cwd` (all six ran), `allow:q6_accept_edits_fs:add_dir` (all six
> refused, "has been denied"), `allow:q6_accept_edits_fs:outside` (all six refused, "needs approval … outside the
> working directories"); Claude Code 2.1.310.

Consequence for stage 8: in the cwd these six commands need no grant decision — acceptEdits already runs them.
Outside the working directories the hook can lift the approval stop for a declared path. Inside a read-only
add-dir it cannot, because the deny rule wins (Q3); a declared write there must be granted by not emitting the
`Edit` deny, not by the hook. Commands not in this table (for example `ln`, `chmod`, `install`, `tar -x`,
`git` subcommands in the cwd) were not tested and must be treated as "may already be auto-allowed": stage 8 makes
no decision about them on this evidence.
