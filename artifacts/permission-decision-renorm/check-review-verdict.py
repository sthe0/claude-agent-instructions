"""Final-check helper for permission-decision-renorm: the independent code review must stand at
`pass` and be bound to the delivery worktree's current head commit.

Lives beside the plan rather than in the product tree: it is plan machinery, not a repo artifact.

Binding detail that a first draft of this file got wrong: `agentctl` does NOT persist the
`--code-ref` string. `cmd_code_review` stores `code_sha256 = _digest(code_ref)`, i.e.
`sha256(ref.encode())[:12]`, so the only way to check the binding is to hash the ref the same way.
The ref this plan records is the full 40-char commit sha.

Which head: the delivery worktree while it exists, canon afterwards. Stage 4 lands by fast-forward
and then REMOVES the worktree, so a check pinned to the worktree would break at exactly the point
the plan finishes; after a fast-forward the two shas are equal, so reading canon is the same
assertion with a longer life.

ANCESTRY, not equality. A first draft demanded the review name the CURRENT head, which a correct
execution fails: stage 4 commits the unrelated experience leaves and the sync pull may bring other
commits, so head moves past the reviewed sha by design. The check therefore accepts a passing
review bound to any commit in `BASE..HEAD` (plus BASE itself). Digests are one-way, so ancestry is
tested by hashing each candidate commit the same way `agentctl` does and looking for the recorded
digest among them — an exact test over a bounded set, not a heuristic.

Usage: `check-review-verdict.py [session-id]` — defaults to $CLAUDE_CODE_SESSION_ID, then to the
session this plan was authored in. Exits 0 when a passing review names a commit in range, 1
otherwise.
"""
import hashlib
import json
import os
import subprocess
import sys

AUTHORING_SESSION = "779c1cca-a90c-4094-aef8-2168822e4610"
WORKTREE = "/tmp/cc-permguard"
CANON = "/Users/the0/claude-agent-instructions"
BASE = "21ba7805a13215e026aa80c5c33b077a22efde33"

if len(sys.argv) > 1:
    session = sys.argv[1]
else:
    session = os.environ.get("CLAUDE_CODE_SESSION_ID") or AUTHORING_SESSION
state_path = f"/Users/the0/.claude-agent/agentctl/state/{session}.json"
if not os.path.isfile(state_path):
    print(f"no agentctl state for session {session} at {state_path}")
    sys.exit(1)

repo = WORKTREE if os.path.isdir(WORKTREE) else CANON
rng = subprocess.run(["git", "-C", repo, "rev-list", f"{BASE}..HEAD"],
                     capture_output=True, text=True)
if rng.returncode:
    print(f"cannot list {BASE}..HEAD in {repo}:", rng.stderr.strip())
    sys.exit(1)
candidates = [c for c in rng.stdout.split() if c] + [BASE]
want = {hashlib.sha256(c.encode("utf-8")).hexdigest()[:12]: c for c in candidates}

reviews = json.load(open(state_path)).get("code_reviews") or []
passing = [r for r in reviews
           if r.get("verdict") == "pass" and r.get("code_sha256") in want]

if not passing:
    print(f"no `pass` review bound to any commit in {BASE[:12]}..HEAD of {repo} "
          f"({len(candidates)} candidates); recorded: "
          f"{[(r.get('verdict'), r.get('code_sha256')) for r in reviews]}")
    sys.exit(1)

named = want[passing[-1]["code_sha256"]]
print(f"review pass bound to {named[:12]} (in range, {repo}) "
      f"by {passing[-1].get('reviewer')}")
