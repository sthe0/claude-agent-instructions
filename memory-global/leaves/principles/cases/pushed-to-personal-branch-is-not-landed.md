---
name: pushed-to-personal-branch-is-not-landed
description: Approved production commits had been pushed to a personal branch only.
type: reference
schema: case/v1
origin_tier: project
source_digest: sha256:49babd80936c02509c98a3d541494042d81e174e72092ae403a92df4ad5c4895
generalized_into: [a-coordinate-shifts-while-the-content-it-addressed-stays]
created: 2026-10-21
last_verified: 2026-10-21
---

# Case: pushed-to-personal-branch-is-not-landed

## Aspect

A task counted as delivered while its commits were not present on the shared mainline.

## Expected

Delivered identity is established by content or ancestry on the target branch.

## Actual

The commits had to be cherry-picked onto a fresh branch and a review request opened afterwards.

## Mismatch

Delivery was judged by where the commits were pushed, not by whether they reached the target.
