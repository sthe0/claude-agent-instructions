---
name: tests-cover-resolved-path-not-raw-readers
description: Slimming a configuration record broke consumers that read its raw form.
type: reference
schema: case/v1
origin_tier: project
source_digest: sha256:4e4c40a1a6d6df43cb29e29b960a7087bed72390a808f2d85f94f8091c890e4e
generalized_into: [verdict-covers-the-evidence-domain-it-claims]
created: 2026-10-21
last_verified: 2026-10-21
---

# Case: tests-cover-resolved-path-not-raw-readers

## Aspect

A green suite exercised only the resolved view of the record and was read as covering every reader.

## Expected

Verification covers every reader of the changed record.

## Actual

Raw readers broke after the change while the suite stayed green.

## Mismatch

The passing verdict covered the resolved path only, not the whole set of readers it implied.
