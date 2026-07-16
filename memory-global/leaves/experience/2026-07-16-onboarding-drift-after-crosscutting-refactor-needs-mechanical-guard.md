---
name: 2026-07-16-onboarding-drift-after-crosscutting-refactor-needs-mechanical-guard
description: A cross-cutting refactor (isolated-root migration: bare `claude` = personal install, `claude-agent`/`claude-task` = system) updated the top-of-README switch + doctor.sh but left § Getting started's fenced launch command teaching the stale bare-`claude` first-task launch. A universally-quantified done criterion ('all onboarding surfaces teach the system entry') was verified only on the surfaces the author touched, and no verify-* guard mechanized it — so it silently regressed and confused a new user at install (the most trivial, most important part: the first task). Fixing the prose is necessary but insufficient; the durable remedy is a mechanical guard wired into the pre-commit gate.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com"
refs: [https://github.com/sthe0/claude-agent-instructions/commit/378b7d1]
created: 2026-07-16
last_verified: 2026-07-16
---

# Onboarding drift after a cross-cutting refactor needs a mechanical guard, not just a doc fix

## Difficulty
After the isolated-root migration, README § Getting started (and setup.md:107) still taught bare `claude` as the disciplined-system entry, contradicting the README top switch + doctor.sh. Two prose fixes had already been shipped (README:88/106) yet a twin at setup.md:107 survived — a doc-only fix cannot prove the class is closed. User asked 'why did the problem arise AT ALL in installation?', reframing the task from 'fix the line' to 'mechanize the invariant so it cannot regress'.

## Order & criterion
root-cause the drift (stale onboarding surface after a cross-cutting refactor, no mechanical guard) → mechanize: a verify-* check forbidding bare `claude` as a fenced-block launch command in onboarding docs → wire into verify-all CHECKS (pre-commit gate) + pytest + inventory → fix the live setup.md:107 twin by hand (inline prose the guard by design excludes)

**Acceptance check:** measurable: verify-onboarding-entrypoint.py FAILs on an injected bare-`claude` launch line and passes on the fixed tree; verify-all --staged green; the guard runs in the pre-commit gate

## Contexts

### 2026-07-16 — isolated-root onboarding entry-point drift
- Where it arose: instructions-repo onboarding docs (README.md, docs/operations/setup.md); the verify-suite (scripts/verify-all.py CHECKS + githooks/pre-commit)
- Working plan: onboarding-entrypoint-guard (2 stages: build guard+pytest; fix setup.md:107 + wire into verify-all + inventory). Determinize the rule part (fenced launch-command detection) as a gate; leave the perception part (inline-prose contrast) to review — the CLAUDE.md rule/perception split.

## Cost
1 developer spawn (stage 1, $0.85) + 3 thinker plan-reviews ($0.11+$0.06+$0.07) + 2 engine difficulties worked through (transport-drop MALFORMED, verify-scope --staged)

## Self-critique of the agent system
The original README fix (87d8c10) treated the symptom on the surfaces I looked at; only the user's 'why at all' pushed to the functional place (missing guard). A universally-quantified done criterion should trigger a mechanical enumeration + guard by default, not a spot fix — this is the determinize-the-decidable-rule principle applied to onboarding docs.
