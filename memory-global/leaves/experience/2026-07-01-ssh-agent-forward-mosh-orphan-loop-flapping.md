---
name: 2026-07-01-ssh-agent-forward-mosh-orphan-loop-flapping
description: Keys 'fall off' inside a month-old mosh+tmux pane because agent forwarding needs a live origin connection mosh cannot keep; the fix is a persistent autossh forward + a single host-side writer of the stable ~/.ssh/ssh_auth_sock symlink. The non-obvious trap: a remote 'while :; do ln -sf ...; done' repoint loop launched over -T (no PTY) does NOT die on connection drop (sshd can't deliver SIGHUP without a controlling tty -> reparented to init), so every autossh reconnect accumulates one orphan; a dozen unguarded orphans then blindly repoint the symlink at their own dead sockets every 60s -> LIVE/DEAD flapping. Cure: give the daemon a forced PTY (autossh -tt -A host 'exec sleep infinity') so a hangup on disconnect kills the holder cleanly (no orphan) and it only holds a live forwarded socket without touching the symlink; make ONE guarded host-side writer (a systemd --user healer repointing to any live owned /tmp/ssh-*/agent.* every 10s) the sole symlink owner. Note -N -A creates NO forwarded socket (no session channel opened).
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com (ssh-add -l in real ccgram pane shows keys, no manual ssh the0)"
tier: 1
created: 2026-07-01
last_verified: 2026-07-01
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

## Cost
High — spanned multiple sessions of failed designs (bashrc PROMPT_COMMAND heal, ~/.ssh/rc repoint, -R fixed-path) before a `ps -u` process enumeration exposed the accumulating orphan loops as the real root cause. The winning fix itself was ~1 session of diagnosis + a single-file daemon rewrite.

## Self-critique of the agent system
Weeks were lost adding MORE symlink writers (bashrc PROMPT_COMMAND heal, ~/.ssh/rc repoint, -R fixed-path) before enumerating host processes revealed the accumulating orphans. Lesson: on a 'my fix made it worse / it flaps' symptom, FIRST enumerate every process/writer touching the contended resource (ps -u), before designing another writer. Multiple uncoordinated writers of one resource is the anti-pattern; converge on a single guarded writer.
