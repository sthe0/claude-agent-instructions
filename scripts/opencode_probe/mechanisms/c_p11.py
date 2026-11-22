"""P11: a binary whose version is not the pin refuses launch.

A stand-in binary (a wrapper script fixing the fake's version and marker path, so the process
environment is not touched) reports a different version; the harness must refuse before any run is
launched (the stand-in's ``run`` would leave a marker). The real binary under test must report
exactly the pin. pass when both hold; fail when the stand-in was launched or the harness did not
refuse; unobserved when the real binary's version cannot be read.
"""
import shlex
import shutil
import stat

from opencode_probe import core, harness, record, runner

CASE_ID = "P11"
K7 = False
FAKE = core.REPO_ROOT / "scripts" / "tests" / "fixtures" / "opencode-fake" / "opencode"


def run(ctx):
    pinned = core.read_pin()["version"]
    real_version = harness.binary_version(ctx.binary or core.opencode_bin())
    fake_dir = ctx.events_dir.parent / "p11-fake"
    fake_dir.mkdir(parents=True, exist_ok=True)
    fake_copy = fake_dir / "opencode-fake"
    shutil.copyfile(FAKE, fake_copy)
    fake_copy.chmod(fake_copy.stat().st_mode | stat.S_IXUSR)
    marker = fake_dir / "launched"
    fake = fake_dir / "opencode"
    fake.write_text(f"#!/bin/sh\nFAKE_OPENCODE_VERSION=9.9.9 FAKE_OPENCODE_MARKER={shlex.quote(str(marker))} "
                    f"exec {shlex.quote(str(fake_copy))} \"$@\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    refused, message = False, ""
    try:
        ctx.harness_for(str(fake)).run({"models": {"m1": [{"text": "x"}]}}, label="wrong-version")
    except harness.VersionMismatch as exc:
        refused, message = True, str(exc)
    evidence = {"pin": pinned, "real_binary_version": real_version, "refused": refused,
                "refusal": message, "stand_in_launched": marker.exists()}
    if not real_version:
        return runner.result(record.UNOBSERVED, evidence)
    ok = refused and not marker.exists() and real_version == pinned
    return runner.result(record.PASS if ok else record.FAIL, evidence)
