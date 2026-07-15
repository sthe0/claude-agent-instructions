---
name: 2026-07-11-detect-compiled-claude-exe-via-ps-comm-not-pgrep-argv
description: macOS hides the argv of a hardened compiled binary (the shipped @anthropic-ai/claude-code/bin/claude.exe) from NON-root processes, so argv-based detection (pgrep -f 'path', pgrep -x by argv[0]) returns nothing under a normal user while the same check as root matches. Detect the process via its comm (executable basename) with ps -axo comm= | grep -qxE 'claude(\.exe)?' — comm comes from kinfo_proc and is readable without root, so it works identically for a root daemon and a non-root status command.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
created: 2026-07-11
last_verified: 2026-07-11
---

# Detecting the compiled claude.exe process: use ps -o comm, not pgrep argv (hardened binary hides argv from non-root)

## Difficulty
claude-keepawake (auto-sleep manager) reported 'claude running: no' with SleepDisabled=1 in auto mode when run as a normal user, looking like the machine would drain battery. Root cause: its claude_running() used pgrep, which reads argv; the compiled claude.exe is hardened and non-root cannot read its argv, so pgrep -x/-f missed the real running claude — a FALSE NEGATIVE only in the non-root status path (the root daemon read argv fine and kept disablesleep correct).

## Order & criterion
1) reproduce status false-negative; 2) trace real process (lsof txt -> .../claude-code/bin/claude.exe, comm=claude); 3) recognize argv-hidden-for-hardened-binary; 4) switch detection to ps -o comm; 5) verify match as non-root; 6) apply + verify status shows 'yes'.

**Acceptance check:** measurable: after fix, non-root 'claude-keepawake status' reports 'claude running: yes' while claude.exe runs; ps-comm detect matches live pid

## Contexts

### 2026-07-11 — claude-keepawake claude_running() fix
- Where it arose: macOS power management; process detection of Claude Code shipped as a compiled binary
- Working plan: Replace pgrep-argv detection in /usr/local/bin/claude-keepawake claude_running() with ps -axo comm= basename match 'claude(.exe)?', keeping pgrep as root-only fallback; user applies via sudo cp (backup .bak); verify via status.

## Cost
~1 session, in-thread; no specialist spawn; user-applied sudo

## Self-critique of the agent system
Answered the battery question twice with premature verdicts ('all fine', then 'stuck/unhealthy') before checking the governing observable (pmset SleepDisabled / disablesleep) and before understanding the root-vs-user argv visibility. Lesson: for a verify-a-property task, identify and read the SINGLE governing flag first, and don't declare healthy/unhealthy until the mechanism (here: root daemon vs non-root reporter) is understood.
