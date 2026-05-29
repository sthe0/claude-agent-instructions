---
name: "mosh: persistent ssh master keeps forwarded agent alive"
description: "Why a mosh session lost its forwarded SSH agent (and ya vault), and the persistent-ControlMaster wrapper that fixes it; plus the macOS Keychain + UseKeychain/AddKeysToAgent pattern for passphrase-free key loading."
type: reference
resolution_confirmed_by_user: "Да, решена (Recommended)"
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

## Difficulties

- **Broken-symlink mistaken for "stale path"**. First fallback assumed the symlink would be live whenever the user was around — overlooked that mosh's ssh exits within seconds, severing both the original `SSH_AUTH_SOCK` *and* the symlink target. Signal: user reported the error still showing after the fallback was deployed.
- **Wrong tradeoff on first scope** (silent-gate around `ya vault`). User rejected: an error is information; silencing it hides agent breakage in non-mosh paths too.
- **Unconditional `.zshrc` background master would have been broken by VPN gating.** User flagged it explicitly before deploy — surfaced the VPN constraint that wasn't visible in the initial debugging.
- **My own miss of the resolution gate** — after the VPN-aware mosh-wrapper landed I wrote a final summary but didn't put an `AskUserQuestion` in the same reply. User reminded me ("спасибо, почему не спрашиваеш решена ли задача?"). `hook-resolution-reminder.py` didn't catch it because the prompt was 7 words (above the 6-word `MAX_WORDS` cap).

## Artifacts

- Host `~/.bashrc` (the0.klg.yp-c.yandex.net) — fallback block before the `ya vault` exports; iterates over `~/.ssh/ssh_auth_sock` and `/tmp/ssh-*/agent.*` owned by the user, picks the first that answers `ssh-add -l`. Original kept at `~/.bashrc.bak.20260529-124308` and `~/.bashrc.bak.predeploy`.
- Local `~/.ssh/config` — added `UseKeychain yes` + `AddKeysToAgent yes` to the `Host *` block.
- Local `~/.zshrc` — added `autoload -Uz compinit && compinit` before yc completion; `ssh-add --apple-load-keychain`; `mosh()` function that brings up the persistent master only when mosh-ing to `the0.klg` (or full FQDN).
- Self-improvement commit `dbdf071` (this repo): in-thread carve-out in CLAUDE.md § Closing protocol + meta-question trigger in `scripts/hook-resolution-reminder.py`.

## Lessons

- **Forwarded agent socket dies with the ssh that forwarded it.** In `mosh user@host`, the spawning ssh exits as soon as `mosh-server` is up — its `/tmp/ssh-*/agent.*` is gone seconds later. Any "use the same socket" fallback needs an *independent* live ssh holding the forward open.
- **`[ -S link ]` returns false on a broken symlink.** Don't use that test as evidence that a fallback path is reachable; resolve and probe the target (or `ssh-add -l` through it).
- **Persistent forwarded agent on macOS = `ssh -fN` + `ControlMaster auto`.** Wrap in a function around the launcher (`mosh()`) instead of a generic shell-init line when the host is conditionally reachable (VPN, on-call jump host) — failures land in front of the user instead of in `/dev/null`.
- **macOS Keychain pattern is canonical and self-contained**: `~/.ssh/config Host *: UseKeychain yes / AddKeysToAgent yes` + one-time `ssh-add --apple-use-keychain <key>` + `ssh-add --apple-load-keychain` in `.zshrc`. No external `keychain` package needed.
- **Surface external constraints before they bite the design.** User added "but only via VPN" *after* I had proposed the always-on `.zshrc` snippet; should have asked about access topology earlier in option-listing.

## Self-critique of the agent system

Concrete friction (already addressed in commit `dbdf071`):

1. **CLAUDE.md § Closing protocol** described the resolution gate as "all stages have passed Final verification", which fits a formal-plan flow. For in-thread substantive work without a plan, the trigger condition never literally fires, so the `AskUserQuestion` close gets dropped. Added an in-thread carve-out to step 1: when you're about to write the final summary, that *is* the gate, ask in the same reply.
2. **`scripts/hook-resolution-reminder.py`** only matched ≤ 6 words + gratitude keyword. The user's reminder ("спасибо, почему не спрашиваеш решена ли задача?") was 7 words, slipped through. Added a second predicate: gratitude + meta-keyword (`спрашиваеш / почему не / решена / закрыта / ask(ed|ing) / why didn't / resolved / done …`) in ≤ 20 words.

Did **not** find a recurring pattern (≥ 2 prior leaves with the same friction). The closest related leaf is `2026-05-26-soft-control-hooks-arc.md` (which introduced these hooks); this entry refines that work rather than calling for an architectural rethink.

## Cost, effort, and tool usage

- Wall-clock: ~2 hours (first turn → resolution confirmation), interleaved with other work.
- `claude -p` spawns: 0 — fully in-thread, small-change carve-out throughout.
- User interventions: ~9 turns; 3 were rejections / scope additions (silent-gate refusal, "let it complain", VPN constraint) and 1 was the resolution-gate reminder.

| Tool / skill | Count | Purpose |
|---|---|---|
| `Skill(self-improvement)` | 1 | Diagnose the closing-protocol miss; produce CLAUDE.md + hook edits. |
| `Bash` (ssh / scp / grep / diff) | ~30 | Diagnose host state, deploy edits, verify behaviour across scenarios. |
| `Edit` | ~10 | `.bashrc` v1 + v2, `.ssh/config`, `.zshrc` (multiple steps), instruction files. |
| `Read` | ~10 | `.bashrc`, `.zshrc`, hook script, CLAUDE.md, cursor mirror, yc completion. |
| `AskUserQuestion` | 3 | Choice of where to set up keychain; persistent-ssh approach; resolution confirmation bundled with self-improvement apply. |

Resources that drove cost: the host's `~/.bashrc` (two distinct rewrites), the iterative discovery that the symlink-only fallback was insufficient (cost a re-deploy), and the late VPN constraint (forced a second rewrite of the Mac-side approach).
