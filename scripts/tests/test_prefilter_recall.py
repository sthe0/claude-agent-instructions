"""Calibration of the result-image echo prefilter against the labelled corpus.

The prefilter (agentctl.result_image.echo_prefilter) is a high-recall structural
net in front of a model judge. Two numbers bound it, and BOTH were fixed before
the labels in scripts/tests/fixtures/result_image_echo_labels.json were readable
by anyone writing the prefilter:

    recall over the labelled echoes   >= 0.70
    false positives over the genuine  <= 0.25

They only mean something as a pair. A net that flags every image clears the floor
and blows the ceiling; a net that flags nothing does the reverse. Retuning either
in response to what the labels turned out to say would make the labelling
ceremony worthless, so this file measures against literals and the plan forbids
moving them.

Three guards sit in front of the measurement, because a score is only as good as
the ground truth it is scored against:

* the labels file's keys must be exactly the set the fixture glob yields -- a
  measurement over a subset of the domain is not a measurement of the domain;
* the labels must be byte-identical to the blob committed when they landed --
  a label edited after the fact is a score edited after the fact;
* the labels must have been committed BEFORE the prefilter existed, read from
  this repository's own history rather than taken on anyone's word.
"""
from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from agentctl.result_image import echo_prefilter

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORPUS = FIXTURES / "plan_corpus"
LABELS_PATH = FIXTURES / "result_image_echo_labels.json"

LABELS_REPO_PATH = "scripts/tests/fixtures/result_image_echo_labels.json"
PREFILTER_REPO_PATH = "scripts/agentctl/result_image.py"

RECALL_FLOOR = 0.70
FALSE_POSITIVE_CEILING = 0.25


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _adding_commit(repo_path: str) -> str:
    """The commit that ADDED ``repo_path`` (the oldest one, if it was ever re-added)."""
    out = _git("log", "--diff-filter=A", "--format=%H", "--", repo_path).split()
    assert out, f"no commit in this history adds {repo_path}"
    return out[-1]


def _corpus_images() -> dict[str, tuple[str, str]]:
    """Every stage of every corpus plan carrying a non-empty expected_result_image.

    Returns key -> (image, verify_command), keyed exactly as the labels file's
    documented key_format, "<plan-filename>:<stage index>".
    """
    images: dict[str, tuple[str, str]] = {}
    for path in sorted(CORPUS.glob("*.toml")):
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
        for stage in doc.get("stage", []):
            image = stage.get("expected_result_image", "")
            if image and image.strip():
                images[f"{path.name}:{stage['index']}"] = (
                    image,
                    stage.get("verify_command", "") or "",
                )
    return images


@pytest.fixture(scope="module")
def labels() -> dict[str, str]:
    doc = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    return {row["key"]: row["label"] for row in doc["labels"]}


@pytest.fixture(scope="module")
def images() -> dict[str, tuple[str, str]]:
    return _corpus_images()


def test_labels_cover_exactly_the_enumerated_domain(labels, images):
    """The ground truth spans the domain -- no missing image, no invented key."""
    assert set(labels) == set(images), {
        "unlabelled": sorted(set(images) - set(labels)),
        "not_in_corpus": sorted(set(labels) - set(images)),
    }
    assert set(labels.values()) == {"echo", "genuine"}


def test_labels_unchanged_since_the_commit_that_landed_them():
    """The working copy of the labels is the blob that was committed with them.

    Without this, "the prefilter clears 0.70" degrades into "the prefilter clears
    whatever the labels were quietly moved to".
    """
    landed = _adding_commit(LABELS_REPO_PATH)
    committed = _git("show", f"{landed}:{LABELS_REPO_PATH}")
    assert committed == LABELS_PATH.read_text(encoding="utf-8"), (
        f"{LABELS_REPO_PATH} differs from the blob committed at {landed}"
    )


def test_labels_were_committed_before_the_prefilter_existed():
    """Read from git, not asserted by the author: the labelling is independent.

    A prefilter written first and labels written to fit it would score perfectly
    and mean nothing.
    """
    labels_commit = _adding_commit(LABELS_REPO_PATH)
    prefilter_commit = _adding_commit(PREFILTER_REPO_PATH)
    assert labels_commit != prefilter_commit, (
        "labels and prefilter landed in the same commit -- the labelling cannot be "
        "shown to have preceded the mechanism it scores"
    )
    ancestor = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor",
         labels_commit, prefilter_commit],
        capture_output=True,
        text=True,
    )
    assert ancestor.returncode == 0, (
        f"the commit adding {LABELS_REPO_PATH} ({labels_commit}) is not an ancestor of "
        f"the commit adding {PREFILTER_REPO_PATH} ({prefilter_commit})"
    )


def _score(labels, images):
    hits = {key for key, (image, command) in images.items()
            if echo_prefilter(image, verify_command=command)}
    echoes = {key for key, label in labels.items() if label == "echo"}
    genuine = set(labels) - echoes
    return hits, echoes, genuine


def test_prefilter_recall_meets_the_frozen_floor(labels, images):
    assert set(labels) == set(images), "domain guard must pass before the score means anything"
    hits, echoes, genuine = _score(labels, images)
    recall = len(hits & echoes) / len(echoes)
    assert recall >= RECALL_FLOOR, (
        f"recall {recall:.3f} < {RECALL_FLOOR}; missed: {sorted(echoes - hits)}"
    )


def test_prefilter_false_positives_meet_the_frozen_ceiling(labels, images):
    assert set(labels) == set(images), "domain guard must pass before the score means anything"
    hits, echoes, genuine = _score(labels, images)
    rate = len(hits & genuine) / len(genuine)
    assert rate <= FALSE_POSITIVE_CEILING, (
        f"false-positive rate {rate:.3f} > {FALSE_POSITIVE_CEILING}; "
        f"flagged: {sorted(hits & genuine)}"
    )


def test_prefilter_is_not_a_constant(labels, images):
    """Both numbers are cleared by DISCRIMINATING, not by flagging everything or nothing."""
    hits, echoes, genuine = _score(labels, images)
    assert hits, "prefilter flags nothing"
    assert hits != set(images), "prefilter flags everything"
