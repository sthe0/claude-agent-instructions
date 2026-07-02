---
name: 2026-07-01-ssh-agent-forward-mosh-orphan-loop-flapping
description: Keys 'fall off' inside a month-old mosh+tmux pane because agent forwarding needs a live origin connection mosh cannot keep; the fix is a persistent autossh forward + a single host-side writer of the stable ~/.ssh/ssh_auth_sock symlink. The non-obvious trap: a remote 'while :; do ln -sf ...; done' repoint loop launched over -T (no PTY) does NOT die on connection drop (sshd can't deliver SIGHUP without a controlling tty -> reparented to init), so every autossh reconnect accumulates one orphan; a dozen unguarded orphans then blindly repoint the symlink at their own dead sockets every 60s -> LIVE/DEAD flapping. Cure: give the daemon a forced PTY (autossh -tt -A host 'exec sleep infinity') so a hangup on disconnect kills the holder cleanly (no orphan) and it only holds a live forwarded socket without touching the symlink; make ONE guarded host-side writer (a systemd --user healer repointing to any live owned /tmp/ssh-*/agent.* every 10s) the sole symlink owner. Note -N -A creates NO forwarded socket (no session channel opened).
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com (ssh-add -l in real ccgram pane shows keys, no manual ssh the0)"
tier: 1
created: 2026-07-01
last_verified: 2026-07-02
---

# Persistent ssh-agent forward into long-lived mosh/tmux: orphan remote loops flap the stable symlink

## Difficulty
Forwarded ssh-agent keys keep falling off inside a long-lived mosh+tmux pane on a corp host; manual 'ssh host' heals them but the user wants zero manual intervention. Deeper: successive fix attempts made it WORSE via accumulating orphan remote repoint-loops that flap the symlink onto dead sockets.

## Order & criterion
1. Confirm forwarding lifecycle: mosh's launcher ssh closes after mosh-server starts so mosh keeps no live forwarded socket; a long-lived pane holds a FROZEN SSH_AUTH_SOCK/PROMPT_COMMAND (editing dotfiles never re-enters a months-old bash). 2. A stable symlink ~/.ssh/ssh_auth_sock that panes hold, plus a persistent origin-side connection (autossh) to guarantee a live forwarded socket exists. 3. CRUCIAL: enumerate host processes (ps -u) before adding writers — orphan 'ln -sf ...; while :; do sleep 60' loops (PPID=1) from prior reconnects are the flapping source. 4. Kill all accumulated loops. 5. Daemon = autossh -M 0 -tt -A host 'exec sleep infinity' (PTY -> clean SIGHUP on drop, no orphan; verified CLEAN_NO_ORPHAN). 6. Exactly ONE guarded writer of the symlink (systemd healer, repoint-if-dead every 10s). 7. Verify: reconnect (kickstart -k) keeps symlink LIVE on one socket; force symlink to a dead path -> healer restores within 10s; real pane ssh-add -l shows keys.

**Acceptance check:** measurable

## Contexts

### 2026-07-01 — initial
- Where it arose: the0.klg.yp-c.yandex.net (Yandex corp host, VPN-only), Mac launchd LaunchAgent com.the0.autossh-klg, Skotty secure-enclave agent with a no-touch insecure@secure-enclave cert for non-interactive daemon connects
- Working plan: Diagnose orphan-loop accumulation as the true root cause (not the forwarding architecture); kill all orphan loops; switch the Mac autossh daemon to -tt -A + 'exec sleep infinity' so remote holders die cleanly on disconnect; make a single systemd --user healer the sole symlink writer; verify reconnect-stability + dead-path recovery + keys live in the real frozen pane.

### 2026-07-02 — healer picked a LIVE-but-wrong socket (`Invalid RSA signature. Make sure to use skotty`)
- Symptom: interactive `ssh the0.klg` prints `error: Invalid RSA signature. Make sure to use skotty` **twice**, right after `Last login`, before the prompt. Non-interactive `ssh host 'cmd'` and even `bash -lc` are CLEAN — only an INTERACTIVE PTY shell (`ssh -tt host 'bash -ic ...'`) reproduces it, because the message is emitted from `~/.bashrc`, not from auth/login banner. The doubling = exactly the two `_ya_vault_cached` → `ya vault get` calls in `~/.bashrc`; each signs a vault challenge through `$SSH_AUTH_SOCK` (= the `~/.ssh/ssh_auth_sock` symlink), and Yandex vault REJECTS any non-Skotty signature with that (misleading — says "RSA" regardless of actual key type) message.
- Root cause: the single healer's selection criterion was too weak. The old healer repointed the symlink only when its target was **dead**, to **any live owned** `/tmp/ssh-*/agent.*`. A forward from ANOTHER origin (here a VPS session forwarding a plain ED25519 `the0@claude-agent-instructions-vm`, no Skotty cert) is alive but useless for vault; once it won the symlink the healer never migrated off it. So "single guarded writer" (2026-07-01) was necessary but not sufficient — the writer must also pick the RIGHT socket.
- Fix: change the healer criterion from "ssh-add -l succeeds" to "ssh-add -l lists a `secure-enclave` cert", AND migrate even off a live-but-non-Skotty target (drop the "only when dead" guard). `ssh-add -l` only lists (no signing) → no Touch ID. Restarted the Mac autossh daemon (`launchctl kickstart -k`) to publish a fresh Skotty forward; the patched healer repointed within one 10s loop.
- Red herrings that cost time: (a) a local Mac `~/.ssh/id_rsa` (2048 SHA-1 RSA) present in the Apple/Skotty agent looked like THE cause — it is NOT: vault signs with the Skotty cert and ignores id_rsa (proven: a socket with rsa=1 AND skotty=3 gave `ya vault get` exit=0). id_rsa also regenerates via `UseKeychain yes`+`AddKeysToAgent yes` and Skotty proxies it, so chasing it is futile. (b) `Error: remote port forwarding failed for listen path .../agent-forward.sock` in the daemon log is STALE output from a retired `-R` fixed-path design (current daemon is `-A` only; `ssh -G` shows no RemoteForward) — not a live failure.
- Bonus hygiene (not the root fix): aligned Mac `~/.ssh/config` with `skotty ssh conf` — added `Host *.yandex.net` with `ForwardAgent`/`IdentityAgent` → `~/.skotty/sock/default.sock` before `Host *` (first-value-wins) so interactive ssh forwards the Skotty socket instead of the blanket `ForwardAgent Yes` on `Host *` (which forwarded the whole Apple agent).
- Verify: `ssh -tt host 'bash -ic ...'` clean; `~/.cache/ya-vault/*` freshly (re)written = vault signed OK; symlink target lists 3 secure-enclave keys; `~/.ssh/rc` confirmed a no-op (comment only) so the healer remains the sole writer.

## Cost
Medium — one session; the reproduction insight (interactive-PTY-only, from `~/.bashrc` vault calls, not from auth) and separating the id_rsa red herring from the real "healer picks a non-Skotty live socket" root cause were the load-bearing steps.

## Cost (2026-07-01)
High — spanned multiple sessions of failed designs (bashrc PROMPT_COMMAND heal, ~/.ssh/rc repoint, -R fixed-path) before a `ps -u` process enumeration exposed the accumulating orphan loops as the real root cause. The winning fix itself was ~1 session of diagnosis + a single-file daemon rewrite.

## Self-critique of the agent system
Weeks were lost adding MORE symlink writers (bashrc PROMPT_COMMAND heal, ~/.ssh/rc repoint, -R fixed-path) before enumerating host processes revealed the accumulating orphans. Lesson: on a 'my fix made it worse / it flaps' symptom, FIRST enumerate every process/writer touching the contended resource (ps -u), before designing another writer. Multiple uncoordinated writers of one resource is the anti-pattern; converge on a single guarded writer.
