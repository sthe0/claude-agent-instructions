# Labelling brief — result images: echo or genuine

This brief is committed alongside the labels it produced, so that what it did and did not
say can be read afterwards. It is the whole of what the labeller was told about the task.

## The distinction you are labelling

A plan in this repository is a sequence of stages. Each stage declares an
`expected_result_image`: a statement of **what the stage's result IS** — what exists, or is
true, or has changed, once the stage is done.

Each stage separately declares a **control**: a check that decides whether the stage
succeeded (a command to run, a test to pass, an observation to make).

These are two different things, and the defect being labelled is the collapse of the first
into the second.

- **genuine** — the image says what the result is. A reader who has not run the check can
  learn from it what now exists or holds that did not before.
- **echo** — the image instead answers *whether the check came out well*. It reports the
  verdict of the stage's own control in place of describing the result the control was
  meant to be evidence for. A reader learns that something succeeded, but not what it is.

The test that separates them: **read the image and ask what a reader now knows.** If the
answer is "that the stage's check produced a favourable verdict", it is an echo. If the
answer names a state of the world that the check happens to be evidence for, it is genuine.

Two clarifications, because they are the cases where the distinction is easy to misapply:

- **An image is not an echo merely because it mentions a check, a test, a command or a
  status.** An image may legitimately name the check as part of describing what now exists
  — for instance where the thing produced by the stage *is* a check. Ask what the reader
  learns, not which words appear.
- **An image is not genuine merely because it is long, specific or well written.** A
  detailed restatement of a verdict is still a verdict. Conversely a short image that names
  a state of the world is genuine.

Where the image does both — states a result *and* restates the verdict — judge it by
whether the result-statement would stand on its own if the verdict clause were deleted. If
it would, label it **genuine**; if deleting the verdict clause leaves nothing that says what
the result is, label it **echo**.

## What you produce

For **every** stage in the corpus that carries an `expected_result_image`, one entry:

- the key (see the format below),
- `label`: exactly `"echo"` or `"genuine"`,
- `reason`: one line, your own words, why.

Judge each image on its own. There is no target proportion, no expected rate, and no
quota — a corpus in which almost all images are genuine and a corpus in which almost all
are echoes are both possible outcomes of this task, and either is an acceptable result.

## What this brief deliberately does not contain

It does not describe any mechanism that will later be built to detect echoes, any property
such a mechanism would look at, or any part of the plan stage this labelling serves. The
labelling exists to score that mechanism, so a labeller who knew what the mechanism looks
for would be scoring it against itself.

---

# Mechanics

## Your domain

Four files in your sandbox hold the images to label, 50 each, 200 in total:

`.review/images_part1.json` … `.review/images_part4.json`

Each entry carries:

- `key` — the identifier you must use verbatim, of the form `<plan-filename>:<stage-index>`;
- `stage_title` — the stage's title;
- `expected_result_image` — **the text you are labelling**;
- `done_criterion` and `verify_command` — the stage's control, given so you can see what
  verdict the image might be echoing. These are context, not the object of the label.

The entries were extracted mechanically from `scripts/tests/fixtures/plan_corpus/` — 55
plan files, every stage that carries a non-empty `expected_result_image`. The extraction is
recorded here so that what you were shown is auditable. The full plan files are also in your
sandbox and you may open any of them if a stage's context is unclear.

## What to emit

Read part 1, label its 50, and `Write` your labels to `.review/labels_part1.json`. Then part
2 → `.review/labels_part2.json`, and so on through part 4. Four files, 50 entries each.

Each file is a JSON array of objects with exactly three keys:

```json
[
  {"key": "some-plan.toml:1", "label": "echo", "reason": "one line, your own words"},
  {"key": "some-plan.toml:2", "label": "genuine", "reason": "one line, your own words"}
]
```

Rules that make the result usable:

- `key` **verbatim** from the input. Every key in a part appears exactly once in its labels
  file — no additions, no omissions, no renaming.
- `label` is exactly `"echo"` or `"genuine"`. There is no third value and no abstention: an
  image you find genuinely borderline still gets one of the two, and your `reason` says it
  was borderline and which way you went.
- `reason` is one line. It is read by people auditing your judgement later, so make it say
  *why this image*, not restate the definition.

Work through them in order and do not skip. If you run short of room, finish and write the
part you are on before starting the next, so that a partial result is still a complete set
of parts rather than a truncated file.
