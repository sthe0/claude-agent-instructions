# Pre-land instruction smoke test

An instruction change (a rule, a skill, an import, a hook) can pass every unit test and still break the next session that loads it. This recipe loads the committed candidate revision into a throw-away sandbox, runs the repo's own static checks against it, and starts one tool-less `claude -p` session inside it to prove the instructions still load end to end. It runs before the change lands, and it touches nothing outside the sandbox.

## Invocation

Build a sandbox of the committed candidate, then verify it:

```
scripts/instruction-sandbox.sh --core-ref HEAD --root /tmp/isb-demo
scripts/instruction-sandbox-verify.sh /tmp/isb-demo
```

`--no-live` skips the live launches (static checks only, no marker written); `--timeout <seconds>` bounds each launch. The verify script prints one line per check and a final verdict:

```
CHECK <name> PASS|FAIL|UNAVAILABLE <detail>
RESULT: PASS|FAIL|UNAVAILABLE
```

It exits 0 for PASS, 1 for FAIL, 3 for UNAVAILABLE and 2 for a usage error. Every check runs even after an earlier FAIL, so one run lists every problem.

With a project layer, build the sandbox with `--project-mount <dir>` and a project composer installed through the sandbox plugin seam (`docs/operations/org-portability.md`). Core ships no composer; without one the sandbox is Core-only.

## Sandbox layout

```
<root>/core/         private clone of the candidate revision (detached, no remote)
<root>/home/         fake HOME; .claude-agent and .cursor composed by the candidate's own setup-symlinks.sh
<root>/project/      only with --project-mount: the project's .claude/ and CLAUDE.md
<root>/sandbox.env   what was built (source, revision, project mount, composer)
```

The candidate must be committed: the sandbox clones a ref, never the working tree. The static checks are the clone's own copies, run with HOME, `CLAUDE_AGENT_HOME`, `CLAUDE_CONFIG_DIR` and `CLAUDE_INSTRUCTIONS_REPO` pointing into the sandbox, so they judge the candidate and not the checkout you are standing in. Session-identity, plugin-directory and canon-path variables from the caller are scrubbed (the list lives in `instruction-sandbox-verify.sh`, `instruction-sandbox-live.py` and `instruction-sandbox.sh`; a test keeps the three equal).

## Checks

| Check | What it proves |
|---|---|
| `static:lint-prose-length` | the candidate's prose ceilings hold |
| `static:verify-layout-contract` | the files, links and hook registry the layout promises exist |
| `static:verify-instructions-sync` | the candidate's instruction tree is internally consistent (it runs the layout check first) |
| `static:lint-hooks-executable` | every hook script is executable in git |
| `project:structure` | the composed project tree has the expected shape (project only) |
| `live:core-marker` | a real session loads `CLAUDE.md` and follows its `@~/.claude-agent/config.md` import |
| `live:project-marker` | a session started inside the composed project also sees the project layer (project only) |
| `canon:unchanged` | the canonical checkout and the real config root are byte-identical before and after the run |

A live check appends a plain-text `SANDBOX-MARKER: <random token>` line to a sandbox-owned file (`<root>/core/config.md`, and `<root>/project/CLAUDE.md` for the project check) and asks a session with all tools disabled to repeat it. The token is reachable only through the import, so seeing it proves the import resolved inside the sandbox. A previous run's marker is replaced, not stacked. Credentials are lent to the launch for its duration through the existing host-LLM helper; the credentials file is never copied into the sandbox.

## The three outcomes

- **PASS** — every check passed.
- **FAIL** — a check ran to completion and found a defect: a static check exited nonzero, the canon changed, or a live session finished and its reply lacked the token (`FAIL marker-missing <token>`).
- **UNAVAILABLE** — a live launch could not complete: no usable credential (`auth`), no answer within the bound (`timeout`), or the launch itself failed (`launch-exit=<n>`). The check is neither passed nor failed.

UNAVAILABLE is not FAIL, because a check that could not run says nothing about the candidate; reading it as FAIL would send the author hunting for a defect in a change that is fine, and reading it as PASS would be worse. Rerun, or fix the credential or the network, and look at `RESULT`: if a static check also failed, `RESULT` is FAIL. The default launch bound is 480 s, taken from the measured latency in `docs/operations/advisor-timeout-calibration.md`; raise it with `--timeout` on a slow host.

## What this catches

- Broken instruction, skill or agent links in the candidate tree.
- Prose-ceiling regressions.
- A broken or mis-resolved `@~` import, which would leave a session without its coordination constants.
- Hooks wired to the wrong checkout, or not executable in git.
- A SessionStart- or UserPromptSubmit-class hook that breaks session load, since the live launch goes through session start and a prompt submission.
- Project-layer composition breakage (a link that resolves outside the sandbox, a missing project file).

## What this does NOT catch

- **The concurrency class.** A scope-conflict hook once worked for about 22 hours after landing, then false-blocked parallel sessions through a multi-session interaction, and was reverted. One sandboxed session cannot reproduce an interaction between several live ones, so the smoke test does not catch it, and a change to anything that coordinates parallel sessions needs its own multi-session exercise.
- **Any PreToolUse- or PostToolUse-class defect.** The smoke prompt runs with tools disabled, so those hooks never fire.
- **Real-HOME-dependent behaviour.** The session runs in a fake HOME; anything that reads state from the real home behaves differently there.
- **Caller-environment path overrides that are not on the scrub list.** A variable the list does not name passes through to the launch.
- **Semantic correctness of rule content.** The recipe shows the rules load, not that they are right.
- **Data- or timing-dependent behaviour.**

## Cost

A run makes one live `claude -p` call, or two with a project, on a small model. It is meant for manual use before landing an instruction change, not for CI.

## Relation to the other gates

The smoke test is a complement to, and does not replace, the mandatory thinker plan-review or the mandatory `agentctl normalize` step. Those judge whether the change is the right one; this checks that the candidate still loads.
