"""Invariance check for the `--review-topo` feature: the pre-existing surface
(`plan_content_digest`/`plan_meta_digest`/`stage_element_keys`/
`render_stage_brief`) must render byte-identical output on the delivered tree
and on `origin/main`, for a fixture plan neither this change nor any other
stage of this task is allowed to alter.

This is the ONLY test file in this task that imports symbols against a
checked-out `origin/main` tree (via `git archive` into a temp dir, run as a
subprocess so `agentctl`'s relative imports resolve normally) -- every other
test file here imports only the delivered tree's `agentctl` package directly.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from agentctl.plan import load_plan, plan_content_digest, plan_meta_digest, stage_element_keys
from agentctl.render import render_stage_brief

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_two_stage.toml"

_SNAPSHOT_SCRIPT = """
import json
import sys

sys.path.insert(0, "scripts")
from agentctl.plan import load_plan, plan_content_digest, plan_meta_digest, stage_element_keys
from agentctl.render import render_stage_brief

doc = load_plan(sys.argv[1])
out = {
    "plan_content_digest": plan_content_digest(doc),
    "plan_meta_digest": plan_meta_digest(doc),
    "stage_element_keys": {str(s.index): stage_element_keys(s) for s in doc.stages},
    "stage2_brief": render_stage_brief(doc, 2),
}
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def origin_main_tree(tmp_path_factory):
    dest = tmp_path_factory.mktemp("origin-main-tree")
    result = subprocess.run(
        ["git", "archive", "origin/main"], cwd=REPO_ROOT, capture_output=True, check=True,
    )
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tf:
        tf.extractall(dest, filter="data")
    return dest


def _origin_snapshot(tree: Path, fixture: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _SNAPSHOT_SCRIPT, str(fixture)],
        cwd=tree, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _delivered_snapshot(fixture: Path) -> dict:
    doc = load_plan(str(fixture))
    return {
        "plan_content_digest": plan_content_digest(doc),
        "plan_meta_digest": plan_meta_digest(doc),
        "stage_element_keys": {str(s.index): stage_element_keys(s) for s in doc.stages},
        "stage2_brief": render_stage_brief(doc, 2),
    }


def test_origin_main_and_delivered_fixture_are_byte_identical(origin_main_tree):
    # The comparison below is only meaningful if origin/main's own copy of
    # this fixture is unchanged from the delivered tree's -- confirm that
    # first, or a divergence there would silently mask as a digest mismatch
    # (or a false pass) below.
    origin_fixture = origin_main_tree / "scripts" / "tests" / "fixtures" / "plan_two_stage.toml"
    assert origin_fixture.read_bytes() == FIXTURE.read_bytes()


def test_digests_and_stage_brief_byte_identical_to_origin_main(origin_main_tree):
    origin = _origin_snapshot(origin_main_tree, FIXTURE)
    delivered = _delivered_snapshot(FIXTURE)
    assert delivered["plan_content_digest"] == origin["plan_content_digest"]
    assert delivered["plan_meta_digest"] == origin["plan_meta_digest"]
    assert delivered["stage_element_keys"] == origin["stage_element_keys"]
    assert delivered["stage2_brief"] == origin["stage2_brief"]
