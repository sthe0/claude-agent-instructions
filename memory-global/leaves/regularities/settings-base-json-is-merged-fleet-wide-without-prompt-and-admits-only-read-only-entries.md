---
name: settings-base-json-is-merged-fleet-wide-without-prompt-and-admits-only-read-only-entries
description: settings/base.json merges into every machine's allow list without a prompt, so it may hold only read-only entries; the grant validator checks rule shape, not what a rule admits
type: reference
schema: regularity/v1
generality: 1
induced_from: [settings-permission-tiers]
rule_derived: "lifted from settings-permission-tiers; no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# settings-base-json-is-merged-fleet-wide-without-prompt-and-admits-only-read-only-entries

## Regularity

settings/base.json is merged into every machine's live ~/.claude-agent/settings.json permissions.allow on git pull through apply-settings.sh (a union merge, base first, local-only entries preserved) without a prompt, and by a security invariant it may hold only side-effect-free entries; scripts/lint-settings-base.py, a verify-all check, fails with "non-read-only entry in base.json" on anything that could mutate state, and pytest is not read-only. Machine-local entries survive the union merge but are not versioned or shared. Launching a child with --permission-mode bypassPermissions does not lift the Bash command-safety classifier on a bare code-executing command; an allow entry does. The grant validator (grants.validate_rule) is a shape check on rule text at declaration time: it cannot see what a script named in a validated rule contains at materialization time, or what a :* wildcard admits (find -exec, rg --pre, git log --output), and its never-grantable lists are hand-maintained enumerations, so a dangerous program absent from them validates. A grant-set change is bound into approval by grants_sha256 alongside plan_sha256.

## Generality

Level 1 — ranges over agent tool permissions placed in a fleet-merged settings file or declared through the grant validator. It is deliberately not claimed for permission mechanisms of other tools, nor for settings files that are not merged into the fleet without a prompt.

## Induced from

- [[settings-permission-tiers]] — contributes the observed practice behind the statement: the placement tiers of agent tool permissions, the no-prompt fleet-wide merge of base.json and the limits of the grant validator.

## Refutation

lint-settings-base.py accepts a code-executing entry such as Bash(python3 -m pytest:*) in settings/base.json, or a child launched with bypassPermissions alone runs a bare code-executing Bash command without an approval prompt, or validate_rule rejects a rule solely because the script it names contains unsafe code.
