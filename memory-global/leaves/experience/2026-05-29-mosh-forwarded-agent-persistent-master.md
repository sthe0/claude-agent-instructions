---
name: "mosh: persistent ssh master keeps forwarded agent alive"
description: "Why a mosh session lost its forwarded SSH agent (and ya vault), and the persistent-ControlMaster wrapper that fixes it; plus the macOS Keychain + UseKeychain/AddKeysToAgent pattern for passphrase-free key loading."
type: reference
resolution_confirmed_by_user: "Да, решена (Recommended) — после второго захода на закрытие (mosh влетает без зависания, токены из кэша)"
---

# mosh → forwarded agent dies → ya vault errors

User saw `error: No keys in the SSH Agent. Check output of 'ssh-add -l' command` (×2) on every `mosh the0.klg.yp-c.yandex.net` login; not on plain `ssh`. Source: two `ya vault get version` calls in `~/.bashrc` populating `ROBOT_DEEPAGENT_TOKENS_*`. `ya vault` authenticates via the forwarded SSH agent.

## Final plan as executed

In-thread chain of small changes (no formal plan; carve-out per CLAUDE.md § Classify task weight). Stages, in order:

1. **Identify the source of the message** — `ya vault` itself prints it when `ssh-add -l` fails. Reproduced by `unset SSH_AUTH_SOCK; ya vault ...` on the host.
2. **Naive fallback on host** — `~/.ssh/rc` already created `~/.ssh/ssh_auth_sock → $SSH_AUTH_SOCK` on each ssh login. Added a `.bashrc` block that swaps `SSH_AUTH_SOCK` to that symlink when the inherited one is dead. Rejected the first variant that *also* gated the `ya vault` calls behind `ssh-add -l` — user wanted them to keep failing loudly if the agent is broken, not silently skip.
3. **Found the missing case** — in a mosh session the *symlink target itself* is dead: the ssh that mosh used to start `mosh-server` closes immediately, killing its forwarded socket. `[ -S "$HOME/.ssh/ssh_auth_sock" ]` returns false on a broken symlink → fallback never triggers. Expanded the loop to also try any live `/tmp/ssh-*/agent.*` owned by the user.
4. **Mac side, root cause** — the host-side fallback only helps when *some* parallel forwarded socket exists. For a "mosh in isolation" session, nothing on the host is live. Fix: keep a persistent ssh master from the Mac (`ssh -fN`, picked up by `ControlMaster auto` in `~/.ssh/config`). Wrapped in a `mosh()` zsh function so it only runs when the user mosh-es to this specific host — important because **the host is only reachable through Tunnelblick VPN**; an unconditional `.zshrc` snippet would spam failed background connects when VPN is down.
5. **macOS Keychain passphrase setup** (related side-quest) — `~/.ssh/config` got `UseKeychain yes` + `AddKeysToAgent yes`; `~/.zshrc` got `ssh-add --apple-load-keychain`. User must run `ssh-add --apple-use-keychain ~/.ssh/id_rsa` once for the passphrase to enter the Keychain.
6. **Unrelated**: `compdef: command not found` from yc completion → added `autoload -Uz compinit && compinit` before sourcing yc completion.
7. **Mosh login hung in `.bashrc`** (discovered after the first "resolved"). Ctrl+C unstuck it, and running a parallel ssh made it briefly fine. Diagnosis: `ya vault get version` (×2) does an ssh-agent sign that, through the forwarded socket, reaches Skotty's touch-required key on the Mac → invisible Touch ID prompt → indefinite hang. Parallel ssh "fixed" it only because the recent ssh-auth had warmed Skotty's touch cache. **Real fix**: cache `ya vault` outputs in `~/.cache/ya-vault/<version>-<key>` with a 1-hour TTL via a `_ya_vault_cached` helper in `.bashrc`; fresh fetches wrapped in `timeout 15` so a missed touch can never block the shell. Cache pre-warmed via my own ssh session (sub-second per token).

## Difficulties

- **Broken-symlink mistaken for "stale path"**. First fallback assumed the symlink would be live whenever the user was around — overlooked that mosh's ssh exits within seconds, severing both the original `SSH_AUTH_SOCK` *and* the symlink target. Signal: user reported the error still showing after the fallback was deployed.
- **Wrong tradeoff on first scope** (silent-gate around `ya vault`). User rejected: an error is information; silencing it hides agent breakage in non-mosh paths too.
- **Unconditional `.zshrc` background master would have been broken by VPN gating.** User flagged it explicitly before deploy — surfaced the VPN constraint that wasn't visible in the initial debugging.
- **My own miss of the resolution gate** — after the VPN-aware mosh-wrapper landed I wrote a final summary but didn't put an `AskUserQuestion` in the same reply. User reminded me ("спасибо, почему не спрашиваеш решена ли задача?"). `hook-resolution-reminder.py` didn't catch it because the prompt was 7 words (above the 6-word `MAX_WORDS` cap).
- **Premature resolution declaration.** I asked "resolved?" via `AskUserQuestion`, user said "Да, решена" — but the user hadn't actually tried mosh from a fresh terminal yet; they accepted on the strength of the explanation. The next turn they reported a real hang (the Touch ID one above). The closing protocol asks for textual confirmation but doesn't require an empirical observation from the user. For acceptance-review criteria, "yes" without "I ran it and saw X" is weak evidence.

## Artifacts

- Host `~/.bashrc` (the0.klg.yp-c.yandex.net) — fallback block before the `ya vault` block; iterates over `~/.ssh/ssh_auth_sock` and `/tmp/ssh-*/agent.*` owned by the user, picks the first that answers `ssh-add -l`. Followed by `_ya_vault_cached` helper (1-hour TTL on-disk cache + `timeout 15`) that wraps both `ya vault get version` calls. Original backups: `~/.bashrc.bak.20260529-124308`, `~/.bashrc.bak.predeploy`, and a dated backup before the cache rewrite.
- Host `~/.cache/ya-vault/` — pre-warmed with both tokens (chmod 600); refreshed every hour by the helper.
- Local `~/.ssh/config` — added `UseKeychain yes` + `AddKeysToAgent yes` to the `Host *` block.
- Local `~/.zshrc` — added `autoload -Uz compinit && compinit` before yc completion; `ssh-add --apple-load-keychain`; `mosh()` function that brings up the persistent master only when mosh-ing to `the0.klg` (or full FQDN).
- Self-improvement commit `dbdf071` (this repo): in-thread carve-out in CLAUDE.md § Closing protocol + meta-question trigger in `scripts/hook-resolution-reminder.py`.

## Lessons

- **Forwarded agent socket dies with the ssh that forwarded it.** In `mosh user@host`, the spawning ssh exits as soon as `mosh-server` is up — its `/tmp/ssh-*/agent.*` is gone seconds later. Any "use the same socket" fallback needs an *independent* live ssh holding the forward open.
- **`[ -S link ]` returns false on a broken symlink.** Don't use that test as evidence that a fallback path is reachable; resolve and probe the target (or `ssh-add -l` through it).
- **Persistent forwarded agent on macOS = `ssh -fN` + `ControlMaster auto`.** Wrap in a function around the launcher (`mosh()`) instead of a generic shell-init line when the host is conditionally reachable (VPN, on-call jump host) — failures land in front of the user instead of in `/dev/null`.
- **macOS Keychain pattern is canonical and self-contained**: `~/.ssh/config Host *: UseKeychain yes / AddKeysToAgent yes` + one-time `ssh-add --apple-use-keychain <key>` + `ssh-add --apple-load-keychain` in `.zshrc`. No external `keychain` package needed.
- **Surface external constraints before they bite the design.** User added "but only via VPN" *after* I had proposed the always-on `.zshrc` snippet; should have asked about access topology earlier in option-listing.
- **`ya vault` (ssh-agent-authed) in shell init = invisible-Touch-ID landmine.** Cache the output on disk with a TTL and a `timeout` ceiling on fresh fetches, so a forgotten Touch ID prompt never blocks a remote shell login. The pattern generalizes to any agent-signed credential fetch run unconditionally from `.bashrc` / `.zshrc`.

## Self-critique of the agent system

Concrete friction:

1. **CLAUDE.md § Closing protocol** described the resolution gate as "all stages have passed Final verification", which fits a formal-plan flow. For in-thread substantive work without a plan, the trigger condition never literally fires, so the `AskUserQuestion` close gets dropped. Addressed in commit `dbdf071`: in-thread carve-out in step 1.
2. **`scripts/hook-resolution-reminder.py`** only matched ≤ 6 words + gratitude keyword. The user's reminder ("спасибо, почему не спрашиваеш решена ли задача?") was 7 words, slipped through. Addressed in `dbdf071`: second predicate on gratitude + meta-keyword in ≤ 20 words.
3. **"Resolved?" → "yes" with no empirical check.** I closed the gate, user confirmed, leaf was written and pushed — and then the user actually tried mosh and saw a hang. The `AskUserQuestion` ask for resolution carries no requirement that the user has *observed* the failing scenario stop failing. For acceptance-review criteria especially, the agent should request a specific empirical observation ("please run X and report what you see") before treating the confirmation as resolution. Not yet addressed in code — proposal will go through the `self-improvement` skill below.

**Recurrence note.** The 2026-05-26 leaf `plan-verify-loop.md` already covered the same family — "actual outcome of a stage didn't match the declared `Expected result image:`". Today's item #3 is a different surface of the same root: agent treats user assent as evidence of empirical success. This is the **second** leaf flagging "verification skipped / proxied" — per CLAUDE.md the threshold for an architectural fix (not a rule tweak) is met, but the architectural shape here is narrow (single CLAUDE.md sentence + maybe a hook), so I'm running `self-improvement` directly rather than `overcome-difficulty` first.

## Cost, effort, and tool usage

- Wall-clock: ~3 hours (first turn → final resolution confirmation), interleaved with other work and one premature "resolution" mid-way through.
- `claude -p` spawns: 0 — fully in-thread, small-change carve-out throughout.
- User interventions: ~12 turns; 3 were rejections / scope additions (silent-gate refusal, "let it complain", VPN constraint), 1 was the resolution-gate reminder, 1 was the empirical hang report after my first "resolved" gate.

| Tool / skill | Count | Purpose |
|---|---|---|
| `Skill(self-improvement)` | 2 | (1) closing-protocol miss → CLAUDE.md + hook edits; (2) premature-resolution pattern → empirical-check requirement for acceptance-review. |
| `Bash` (ssh / scp / grep / diff) | ~45 | Diagnose host state across three `.bashrc` rewrites; verify behaviour across ssh / mosh / no-VPN / Touch-ID scenarios; cache pre-warm. |
| `Edit` | ~15 | `.bashrc` v1 / v2 / v3, `.ssh/config`, `.zshrc` (multiple steps), instruction files, experience leaf amendments. |
| `Read` | ~12 | `.bashrc`, `.zshrc`, hook script, CLAUDE.md, cursor mirror, yc completion, current leaf. |
| `AskUserQuestion` | 5 | Keychain target; persistent-ssh approach; self-improvement apply + resolution v1; cache-vs-timeout choice; final resolution. |

Resources that drove cost: the host's `~/.bashrc` (three distinct rewrites — symlink-only fallback, broader socket-search fallback, cache+timeout); the late VPN constraint (rewrote the Mac-side approach); and the post-"resolved" hang report (cost an extra iteration plus this self-improvement turn).
