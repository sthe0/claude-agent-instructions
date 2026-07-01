---
name: 2026-07-01-non-interactive-external-deploy-from-agent-bash
description: Deploying a generated artifact to a third-party host (Cloudflare Pages) + pushing to a brand-new GitHub repo, fully non-interactively from the Claude Bash tool, hit three non-obvious traps that compound: (1) secrets set by the user via a '! ...' prompt command do NOT reach my Bash-tool shells (separate process) — env-var handoff silently yields empty; the working pattern is file-drop (user writes to /tmp/.creds with umask 077, I read/use/DELETE). (2) git push to a new repo failed 'Password authentication is not supported' because osxkeychain matches by HOST only and returned a STALE x-access-token entry belonging to the user's OTHER github repos — erasing it would break their other access, so the fix is a repo-local credential.helper reading a dedicated token file (~/.config/travel-agent/token) WITHOUT touching the keychain. (3) wrangler 'pages deploy' fails 'Project not found [8000007]' because it will NOT auto-create the Pages project non-interactively — must POST /accounts/{id}/pages/projects first, then deploy.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "the0"
refs: [2026-05-25-resolution-gate-confirm-before-record.md]
created: 2026-07-01
last_verified: 2026-07-01
---

# Shipping to an external service non-interactively from the agent's Bash tool: three coupled traps

## Difficulty
Each trap presents as a generic auth/deploy failure (empty var, 'password auth not supported', 'project not found') that invites the wrong destructive fix — retry with the same env, git credential erase (collateral: breaks user's other repos), or re-login. The correct fixes are all non-obvious and non-destructive: file-drop for secrets, repo-local helper instead of keychain surgery, API pre-create instead of interactive project creation.

## Order & criterion
self: verify the actual failure cause (git credential fill; curl the deployed URL) before applying a fix; never erase a shared keychain credential to fix a single-repo push

**Acceptance check:** empirical: git push lands on origin/main AND all deployed URLs return HTTP 200 (curl --retry) — not 'command exited 0'

## Contexts

### 2026-07-01 — trips / non-interactive deploy
- Where it arose: trips workspace: push to private sthe0/travel-agent + deploy generated site/ to Cloudflare Pages trips-greece-europe-2026-09.pages.dev, driven by agentctl stage 8
- Working plan: 1) Secrets: user file-drops to /tmp/.creds (umask 077); read, use, delete — do NOT rely on env from '!'. 2) New-repo push on a machine with existing github creds: set repo-local credential.helper that echoes username=x-access-token + password=$(cat token-file); leave osxkeychain untouched. 3) Cloudflare: POST /accounts/{id}/pages/projects {name, production_branch} BEFORE wrangler pages deploy; auth via CLOUDFLARE_API_TOKEN+ACCOUNT_ID. 4) zsh cleanup gotcha: an unmatched glob (nomatch) aborts the WHOLE 'rm -f a b *.json' line — list literals separately or the earlier files survive undeleted. 5) Verify empirically (curl 200 + git status clean) before recording.


### 2026-07-01 — redeploy + engine live-200 verify
- Where it arose: greece-europe-2026-09 CSS redesign: re-render, wrangler pages deploy, agentctl verify-final probing public URLs
- Working plan: Persist the third-party host token to ~/.config/travel-agent/ (600), not just /tmp, so future redeploys need no re-drop. After 'wrangler pages deploy', the apex *.pages.dev edge lags a couple minutes; a verify step that curls the live URL for HTTP 200 fails transiently right after deploy even though the deployment 'complete' line printed. Do NOT treat the first failure as real — the deploy and the exact command both pass out-of-band via the same python->bash->curl path. Re-run the live-200 verify after ~1-2 min propagation; it then passes.

## Common core & variations
**Common:** Any engine/CI gate that asserts HTTP 200 on a freshly deployed CDN URL races edge propagation.

**Variations:** Cloudflare Pages: apex pages.dev took ~2 min / a couple retries to serve the newest deploy for a green verify-final.

## Cost
No meaningful per-task figure: this ran in-thread (main session, spawn_count 0), so `agentctl resolve` returned null cost, `~/.local/log/claude-spawn-costs.jsonl` has no rows for it, and `cost-report.py` found no transcript for the session id. Per-stage main-session tokens aren't split out. The real cost here was engineering iterations against the three traps, not spawn dollars.

## Self-critique of the agent system
I prematurely deleted the token temp file before confirming the push succeeded (recovered from the persisted ~/.config copy); and my cleanup rm silently no-op'd on a zsh nomatch, leaving the CF creds file present for an extra step — both argue for: persist-then-verify-then-delete, and never batch a literal-file rm with a glob.
