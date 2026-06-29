---
name: mirror-working-caller-before-bypass
description: When code fails for missing ambient context inside a constrained environment, mirror the existing working caller that establishes that context before inventing an env/quota/path bypass.
type: feedback
created: 2026-06-14
last_verified: 2026-06-24
---

# Mirror the working caller before inventing a bypass

**Difficulty (functional ground):** code fails inside a constrained environment (a porto/YT job, a sandbox, a frozen base layer) because some *ambient* it needs is absent — a vh3 graph-context, a session/profile context, secrets, a quota, or a path that exists only on the launcher. The tempting fix is a local shim: an env-var, an explicit-quota argument, a hardcoded path. That shim reimplements — usually incorrectly — what a **working caller elsewhere already does correctly**, and it tends to fix the symptom one layer up while the real cause (wrong context, wrong path) stays.

**Rule:** before reaching for an env / quota / path workaround, find the existing *working* caller that performs the **same operation** in a working environment — the standalone CLI, the client-side publish path, the reference launcher — and replicate **how it establishes the missing context**. Mirror first; bypass only if no working caller exists.

**How to apply**
- Name the missing ambient precisely (e.g. "`find_or_create_stored_data` reads `context.quota` from a vh3 graph-context that does not exist inside a porto-job").
- Grep for a function/command that does the same thing and *works* (e.g. the client publish path, a `run_*_only` reference). Read how it sets up context — usually a `with <profile>.context()`, an explicit client object, or a baked path.
- Replicate that setup at the failing site. It is almost always shorter and more correct than the shim.
- Distinguish this from a genuine missing capability: if **no** caller establishes the context (the operation has never worked in this environment), a new mechanism may be justified — but say so explicitly.

**Contexts**
- **DEEPAGENT-403 val-eval in-job publish** (2026-06-14): a developer added a `DEEPAGENT_VH3_EXECUTABLE_QUOTA` env shim so an in-job executable upload could get a quota without the graph-context. The working client publish path simply ran the register/start under `with profile.context()`; mirroring that (drafting the executable in-job + `profile.context()`) replaced the shim and was correct. Separately, the real secret-resolution unblocker was a **baked `/arcadia` profile path** valid inside the job's FS, not the launcher's `/wt` mount — another "use what the working path uses" instance. Full saga: project `experience/2026-06-14-deepagent-403-val-eval-in-job-publish.md`.

Related: shared-entry-point default rule in `skills/specializations/developer/SKILL.md` § While developing; verify-the-load-bearing-axis (don't trust a green static check for a runtime-context failure).
