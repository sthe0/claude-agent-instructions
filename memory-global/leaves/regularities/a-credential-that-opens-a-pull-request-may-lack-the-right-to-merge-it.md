---
name: a-credential-that-opens-a-pull-request-may-lack-the-right-to-merge-it
description: A fine-grained forge token that can create PRs and issues can still be refused merge, repository creation and branch deletion, so the landing path depends on the rights held, not on the flow chosen.
type: reference
schema: regularity/v1
generality: 1
induced_from: [2026-07-17-2026-07-17-core-pr-landing-with-fine-grained-pat]
domain: landing
created: 2026-11-03
last_verified: 2026-11-03
---

# A credential that opens a pull request may lack the right to merge it

## Regularity

The rights needed to open a pull request and the rights needed to merge it are separate grants: a token that creates PRs and issues can be refused the merge, repository creation and branch deletion with a 403, so which landing flow (PR merge or a direct fast-forward push of the branch to trunk) is available is fixed by the credential held.

## Generality

Level 1 — landing work on a git forge with fine-grained personal access tokens. It is not claimed for classic tokens or for forges whose merge right is implied by PR creation.

## Induced from

- [[2026-07-17-2026-07-17-core-pr-landing-with-fine-grained-pat]] — a token that created PRs and issues was refused merge, repository creation and branch deletion (403), and the branch landed by a direct fast-forward push instead.

## Refutation

Held-out check: the single source is the only context, so the claim is held at level 1 on it alone. A token shown to create a PR yet not be refusable the merge on the same forge, or a forge where PR creation implies merge, refutes it or narrows it to the forge class where the grants differ.
