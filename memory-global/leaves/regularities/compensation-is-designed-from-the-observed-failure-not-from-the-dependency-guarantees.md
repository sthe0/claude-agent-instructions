---
name: compensation-is-designed-from-the-observed-failure-not-from-the-dependency-guarantees
description: Code that compensates for a dependency's misbehaviour (a parser, normalizer, retry, fallback) is written from the failure seen at the call site; the dependency's own current guarantees are not consulted, and once a template canonizes the compensation, later failures of the same kind are patched inside it.
type: reference
schema: regularity/v1
generality: 2
induced_from: [regex-not-for-semantic-classification]
rule_derived: "Audit behind Core issue #595: about 20 model judges each grew a hand-written format parser, normalizer or re-ask around claude -p free text, copied from the YES/NO template this rule leaf canonizes, while the CLI's --json-schema (a parsed structured_output) was never consulted; no experience leaf records it."
created: 2026-11-17
last_verified: 2026-11-17
---

# Compensation is designed from the observed failure, not from what the dependency guarantees

## Regularity

Compensating code is designed from the failure observed at the call site; the dependency's own current guarantees are not consulted, and a canonized template carries the compensation forward unexamined.

The failure is what the author sees, so the remedy is shaped to it: a reply in the wrong shape gets a parser, a dropped call gets a retry, a slow call gets a local timeout. Whether the tool, CLI, library or service can rule the failure out itself (a structured-output mode, an idempotency key, a native deadline, a notification instead of polling) is a question about the dependency, and nothing at the failure site asks it. Once the first compensation is written into a norm as the template to mirror, every later site copies it, and each new failure of the same kind is treated as a defect of that one site and patched inside the template. The template itself is not re-checked when the dependency gains a guarantee, nor when the patches pile up.

## Generality

Level 2 — ranges over code that wraps an external dependency whose output or behaviour the author does not control: a model CLI, a third-party API, a library, a platform service. It is not claimed for compensation of the author's own code, where the remedy is a fix at the source, nor for a dependency known to offer no such guarantee.

## Induced from

- [[regex-not-for-semantic-classification]] — the rule that named `judge_binary_ask` the template every judge mirrors, "YES/NO protocol" included; about 20 judges followed it with per-judge parsers and re-asks while the CLI's `--json-schema` went unconsulted (Core issue #595).

## Refutation

A body of wrappers around external dependencies in which the compensations were chosen after the dependency's native guarantees were checked, and in which a template was revised when the dependency gained a guarantee, would refute it; so would finding that the compensations here persisted because the native guarantee was checked and judged unfit.
