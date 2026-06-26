# Verification guards

> The suite of `verify-*.py` / `lint-*.py` scripts that keep the repo consistent. Run after any structural change; the pre-commit hook runs the staged-file subset automatically.

## Running all checks

```bash
python3 scripts/verify-all.py          # full check — all tracked files
python3 scripts/verify-all.py --staged # pre-commit mode — staged files only
bash scripts/verify-layout-contract.sh # layout check — run separately (not in verify-all)
```

`verify-all.py` runs every check in the table below (except `verify-layout-contract.sh`, which must be invoked directly). Exit code 0 means all pass.

## Guard inventory

| Script | What it checks | In `verify-all`? |
|---|---|---|
| [scripts/verify-cross-refs.py](../../scripts/verify-cross-refs.py) | Every Markdown link, and every inline-code path that begins with a known top-level directory, resolves to an actual file. The pervasive guard — any file move must repoint its inbound refs before this passes. | Yes |
| [scripts/verify-readme.py](../../scripts/verify-readme.py) | The HTML-comment inventory sentinel tables (`<!-- inventory:skills:begin/end -->`, `<!-- inventory:specializations:begin/end -->`) exist in their registered file (`REGION_FILES` dict), match the filesystem, and have no stale rows. | Yes |
| [scripts/verify-doc-concepts.py](../../scripts/verify-doc-concepts.py) | Every binding in `scripts/doc-bindings.json` resolves: the target file exists and contains the named heading. | Yes |
| [scripts/verify-agentctl.py](../../scripts/verify-agentctl.py) | The coordination engine's schema, state-machine transitions, cognitive leaves, and gate guardian hooks stay consistent. | Yes |
| [scripts/verify-leaf-structure.py](../../scripts/verify-leaf-structure.py) | Memory leaves that opt into the `leaf/v1` schema have the required sections (`## Difficulty`, `## Guidance`, `## See also`). | Yes |
| [scripts/verify-memory-index.py](../../scripts/verify-memory-index.py) | Every leaf pointer in `MEMORY.md` index files resolves to an existing file. | Yes |
| [scripts/verify-experience-leaf.py](../../scripts/verify-experience-leaf.py) | Experience leaves (`difficulty/v1`) have required frontmatter fields including `resolution_confirmed_by_user`. | Yes |
| [scripts/verify-language.py](../../scripts/verify-language.py) | Files in the repo that must stay English do not contain significant Russian text (and vice-versa for user-facing reply surfaces). | Yes |
| [scripts/lint-prose-length.py](../../scripts/lint-prose-length.py) | Governed prose files (`CLAUDE.md`, `cursor/rules/claude-code-sync.mdc`, skill `SKILL.md`/`policy.md`, `README.md`) stay within their line/byte ceilings defined in `config.md`. | Yes |
| [scripts/lint-permissions.py](../../scripts/lint-permissions.py) | `permissions/global.json` and any project permission files are valid JSON and match the expected schema. | Yes |
| [scripts/lint-settings-base.py](../../scripts/lint-settings-base.py) | `settings/base.json` is valid and the action taxonomy is consistent. | Yes |
| [scripts/lint-hooks-executable.py](../../scripts/lint-hooks-executable.py) | Every `scripts/hook-*.py` file is executable. | Yes |
| [cursor/scripts/lint-cursor-mirror.py](../../cursor/scripts/lint-cursor-mirror.py) | The Cursor mirror (`cursor/rules/claude-code-sync.mdc`) stays within its line ceiling. | Yes |
| [scripts/verify-layout-contract.sh](../../scripts/verify-layout-contract.sh) | On-disk layout matches `skills/self-improvement/policy.md` § File structure; every `scripts/hook-*.py` is registered in both this contract and `scripts/README.md` (bidirectional guard). **Not in `verify-all`** — run separately. | No |

## Adding a new guard

1. Add the script under `scripts/` (or `cursor/scripts/` for Cursor-specific checks).
2. Add it to `CHECKS` in `scripts/verify-all.py` to run it with the suite.
3. Register it in `scripts/verify-layout-contract.sh` with a `require_file` line.
4. Add it to the `scripts/README.md` hook/script inventory.
5. Add a row to the table above.

Steps 3–5 are enforced bidirectionally by `verify-layout-contract.sh`'s hook-registration section — a missing registration is a hard pre-commit failure.

## See also

- [guards.md](guards.md) (this file) — the guard inventory.
- [setup.md](setup.md) — running the guards after initial setup.
- [scripts/README.md](../../scripts/README.md) — the machine-checked inventory of all scripts and hooks.
