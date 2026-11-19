"""Tests of scripts/disk-usage-report.py and scripts/lib/disk_report.py, each on a planted tree in tmp_path."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lib import disk_report

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "scripts" / "disk-usage-report.py"
KIB = 1024
MIB = 1 << 20
BASE_MOUNTINFO = "1 0 8:1 / / rw - ext4 /dev/planted rw\n"
REAPER_SRC = 'NAME = "{name}"\n\n\ndef scan(ctx):\n    return []\n\n\ndef remove(path, ctx):\n    return False\n'


class Planted:
    """A planted root with its home, naming table, mountinfo, temp root, plugin dir and agent home."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.root = base / "root"
        self.home = self.root / "home"
        self.out = base / "out"
        self.ttmp = base / "ttmp"
        self.agent = base / "agent"
        self.plugins = base / "plugins"
        self.names = base / "names.toml"
        self.mountinfo = base / "mountinfo"
        for d in (self.root, self.home, self.ttmp, self.agent, self.plugins):
            d.mkdir(parents=True, exist_ok=True)
        self.names.write_text("")
        self.mountinfo.write_text(BASE_MOUNTINFO)

    def blob(self, rel: str, size: int) -> Path:
        directory = self.root / rel
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "blob").write_bytes(b"x" * size)
        return directory

    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        env.update(CLAUDE_AGENT_HOME=str(self.agent), CLAUDE_REAPER_PLUGIN_DIR=str(self.plugins),
                   TMPDIR=str(self.ttmp), **extra)
        return env

    def args(self, *extra: str) -> list:
        return [sys.executable, str(TOOL), "--root", str(self.root), "--names", str(self.names),
                "--out-dir", str(self.out), "--deadline-min", "5", "--min-bytes", "1",
                "--temp-root", str(self.ttmp), "--home", str(self.home),
                "--mountinfo", str(self.mountinfo), *extra]

    def run(self, *extra: str, env: "dict | None" = None) -> subprocess.CompletedProcess:
        return subprocess.run(self.args(*extra), capture_output=True, text=True, timeout=300,
                              env=env or self.env())

    def report(self, *extra: str) -> dict:
        done = self.run(*extra)
        assert done.returncode == 0, done.stderr
        return json.loads(sorted(self.out.glob("disk-usage-*.json"))[-1].read_text())


@pytest.fixture
def planted(tmp_path: Path) -> Planted:
    return Planted(tmp_path)


def snapshot(root: Path) -> dict:
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath, name)
            st = p.lstat()
            data = p.read_bytes() if p.is_file() and not p.is_symlink() else b""
            snap[str(p)] = (st.st_mode, st.st_size, st.st_mtime_ns, data)
    return snap


def test_entries_descend_and_split_named_from_unnamed(planted: Planted) -> None:
    planted.blob("small", 1 * MIB)
    planted.blob("big", 4 * MIB)
    planted.blob("mid", 2 * MIB)
    planted.names.write_text(
        f'[[entry]]\npath = "{planted.root}/big"\nlabel = "big thing"\nkind = "cache"\n\n'
        f'[[entry]]\npath = "{planted.root}/small"\nlabel = "small thing"\nkind = "tool-data"\n')
    report = planted.report()
    named = [e["path"] for e in report["entries"] if e["label"] in ("big thing", "small thing")]
    assert named == [f"{planted.root}/big", f"{planted.root}/small"]
    sizes = [e["bytes"] for e in report["entries"]]
    assert sizes == sorted(sizes, reverse=True)
    assert [c["path"] for c in report["unknown"]["c_entries"] if c["path"].endswith("/mid")] == [f"{planted.root}/mid"]


def test_unnamed_directories_below_min_bytes_fold_per_parent(planted: Planted) -> None:
    for name in ("p1", "p2", "p3"):
        planted.blob(name, 1 * KIB)
    planted.blob("large", 3 * MIB)
    report = planted.report("--min-bytes", str(MIB))
    unknown = report["unknown"]
    folded = [c for c in unknown["c_entries"] if c["folded"]]
    shown = [c for c in unknown["c_entries"] if not c["folded"]]
    assert [c["path"] for c in shown] == [f"{planted.root}/large"]
    assert len(folded) == 1 and folded[0]["count"] >= 3
    assert report["walk_total_bytes"] == sum(e["bytes"] for e in report["entries"]) + unknown["c_bytes"]


def test_arithmetic_of_gap_c_and_bound(planted: Planted) -> None:
    planted.blob("named", 2 * MIB)
    planted.blob("other", 1 * MIB)
    planted.names.write_text(f'[[entry]]\npath = "{planted.root}/named"\nlabel = "n"\nkind = "cache"\n')
    report = planted.report()
    fs, unknown = report["fs"], report["unknown"]
    used = (fs["blocks"] - fs["bfree"]) * fs["frsize"]
    assert fs["used_bytes"] == used
    walk = sum(e["bytes"] for e in report["entries"]) + sum(c["bytes"] for c in unknown["c_entries"])
    assert report["walk_total_bytes"] == walk
    assert unknown["b_gap_bytes"] == used - walk
    assert unknown["c_bytes"] == sum(c["bytes"] for c in unknown["c_entries"]) > 0
    assert unknown["bound_pct"] == pytest.approx((max(unknown["b_gap_bytes"], 0) + unknown["c_bytes"]) / used * 100)
    assert unknown["bound_limit_pct"] == 10.0
    assert report["units"] == "bytes; GiB=2^30 bytes"


@pytest.mark.parametrize("entry,registry,expected", [
    ({}, {"discovered": {}, "opt_in": {}, "params": {}}, "none"),
    ({"reaper": "r"}, {"discovered": {}, "opt_in": {"r": True}, "params": {}}, "not-registered"),
    ({"reaper": "r", "reaper_mode": "report-only", "opt_in_key": "r"},
     {"discovered": {"r": "plugin"}, "opt_in": {}, "params": {}}, "report-only"),
    ({"reaper": "r", "opt_in_key": "r"}, {"discovered": {"r": "plugin"}, "opt_in": {"r": False}, "params": {}},
     "registered-opt-in-off"),
    ({"reaper": "r", "opt_in_key": "r"}, {"discovered": {"r": "plugin"}, "opt_in": {}, "params": {}},
     "registered-opt-in-off"),
    ({"reaper": "r", "opt_in_key": "k", "needs_params": ["limit"]},
     {"discovered": {"r": "plugin"}, "opt_in": {"k": True}, "params": {"r": {"limit": 1}}}, "registered-unconfigured"),
    ({"reaper": "r", "opt_in_key": "none", "needs_params": ["limit"]},
     {"discovered": {"r": "plugin"}, "opt_in": {}, "params": {"r": {}}}, "registered-unconfigured"),
    ({"reaper": "r", "opt_in_key": "none", "needs_params": ["limit"]},
     {"discovered": {"r": "plugin"}, "opt_in": {}, "params": {"r": {"limit": 1}}}, "reclaimed"),
    ({"reaper": "r", "opt_in_key": "k", "needs_params": ["limit"]},
     {"discovered": {"r": "plugin"}, "opt_in": {"k": True}, "params": {"k": {"limit": 1}}}, "reclaimed"),
    ({"reaper": "r"}, {"discovered": {"r": "plugin"}, "opt_in": {}, "params": {}}, "reclaimed"),
])
def test_reaper_status_rule_first_match_wins(entry: dict, registry: dict, expected: str) -> None:
    assert disk_report.reaper_status(entry, registry) == expected


def test_report_derives_every_status_from_a_planted_registry(planted: Planted) -> None:
    (planted.plugins / "reapers").mkdir()
    for name in ("rp-on", "rp-off", "rp-cfg", "rp-rep", "rp-none"):
        (planted.plugins / "reapers" / f"{name.replace('-', '_')}.py").write_text(REAPER_SRC.format(name=name))
    (planted.agent / "reaper").mkdir()
    (planted.agent / "reaper" / "opt-in.toml").write_text('rp-on = true\nrp-cfg = true\nrp-off = false\n')
    rows = [("a", 'reaper = "rp-on"\ncovers = "whole"\nreaper_mode = "reclaims"\nopt_in_key = "rp-on"'),
            ("b", 'reaper = "rp-off"\ncovers = "whole"\nreaper_mode = "reclaims"\nopt_in_key = "rp-off"'),
            ("c", 'reaper = "rp-cfg"\ncovers = "whole"\nreaper_mode = "reclaims"\nopt_in_key = "rp-cfg"\nneeds_params = ["limit"]'),
            ("d", 'reaper = "ghost"\ncovers = "whole"\nreaper_mode = "reclaims"\nopt_in_key = "ghost"'),
            ("e", 'reaper = "rp-rep"\ncovers = "whole"\nreaper_mode = "report-only"\nopt_in_key = "none"'),
            ("f", 'reaper = "rp-none"\ncovers = "whole"\nreaper_mode = "reclaims"\nopt_in_key = "none"'),
            ("g", "")]
    table = ""
    for name, extra in rows:
        planted.blob(name, 64 * KIB)
        table += f'[[entry]]\npath = "{planted.root}/{name}"\nlabel = "row {name}"\nkind = "cache"\n{extra}\n\n'
    planted.names.write_text(table)
    report = planted.report()
    status = {Path(e["path"]).name: e["reaper_status"] for e in report["entries"] if e["label"].startswith("row ")}
    assert status == {"a": "reclaimed", "b": "registered-opt-in-off", "c": "registered-unconfigured",
                      "d": "not-registered", "e": "report-only", "f": "reclaimed", "g": "none"}
    registry = report["reaper_registry"]
    assert registry["discovered"]["rp-on"] == "plugin" and registry["discovered"]["temp-residue"] == "builtin"
    assert "ghost" not in registry["discovered"]
    assert registry["opt_in"]["rp-on"] is True and registry["opt_in"]["rp-off"] is False
    for entry in report["entries"]:
        assert entry["reaper_status"] == disk_report.reaper_status(entry, registry)


def test_symlink_to_outside_directory_is_neither_followed_nor_measured(planted: Planted, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "blob").write_bytes(b"x" * (5 * MIB))
    planted.blob("real", 1 * MIB)
    os.symlink(outside, planted.root / "link-out")
    report = planted.report()
    seen = {e["path"] for e in report["entries"]} | {c["path"] for c in report["unknown"]["c_entries"]} | set(report["du_roots"])
    assert not any(p.startswith(str(outside)) for p in seen)
    assert report["walk_total_bytes"] < 5 * MIB


def test_excluded_fuse_and_bind_mounts_are_listed_never_measured(planted: Planted) -> None:
    for rel in ("excluded", "bind_dir", "fuse_dir", "home/fuse_home", "home/kept", "kept"):
        planted.blob(rel, 1 * MIB)
    planted.names.write_text(f'[[exclude]]\npath = "{planted.root}/excluded"\nwhy = "planted"\n')
    planted.mountinfo.write_text(
        BASE_MOUNTINFO
        + f"2 1 8:1 /bindsrc {planted.root}/bind_dir rw - ext4 /dev/planted rw\n"
        + f"3 1 0:99 / {planted.root}/fuse_dir rw - fuse.planted planted rw\n"
        + f"4 1 0:98 / {planted.home}/fuse_home rw - fuse.planted planted rw\n")
    report = planted.report()
    skipped = [f"{planted.root}/excluded", f"{planted.root}/bind_dir", f"{planted.root}/fuse_dir", f"{planted.home}/fuse_home"]
    assert all(p in report["excluded"] for p in skipped)
    assert report["fuse_mounts"] == sorted([f"{planted.root}/fuse_dir", f"{planted.home}/fuse_home"])
    assert f"{planted.root}/bind_dir" not in report["fuse_mounts"]
    assert all(f"--exclude={p}" in report["launch"]["du_argv"] for p in skipped)
    assert not any(r == p or r.startswith(p + "/") for r in report["du_roots"] for p in skipped)
    assert f"{planted.root}/kept" in report["du_roots"] and f"{planted.home}/kept" in report["du_roots"]


def test_a_mount_inside_a_root_is_excluded_from_that_roots_total(planted: Planted) -> None:
    planted.blob("tree/mounted", 3 * MIB)
    planted.blob("tree/plain", 1 * MIB)
    planted.mountinfo.write_text(BASE_MOUNTINFO + f"2 1 8:1 /src {planted.root}/tree/mounted rw - ext4 /dev/planted rw\n")
    report = planted.report()
    assert f"--exclude={planted.root}/tree/mounted" in report["launch"]["du_argv"]
    assert report["walk_total_bytes"] < 3 * MIB


def test_hard_link_under_two_measured_subtrees_is_counted_once(planted: Planted) -> None:
    planted.blob("hl_a", 4 * MIB)
    (planted.root / "hl_b").mkdir()
    os.link(planted.root / "hl_a" / "blob", planted.root / "hl_b" / "blob")
    report = planted.report()
    assert 4 * MIB <= report["walk_total_bytes"] < 5 * MIB


def test_entries_are_disjoint(planted: Planted) -> None:
    planted.blob("outer/inner", 1 * MIB)
    planted.blob("outer/sibling", 1 * MIB)
    planted.names.write_text(f'[[entry]]\npath = "{planted.root}/outer/inner"\nlabel = "inner"\nkind = "cache"\n')
    report = planted.report()
    paths = [e["path"] for e in report["entries"] if e["label"] == "inner"] + [
        c["path"] for c in report["unknown"]["c_entries"] if not c["folded"]]
    for a in paths:
        assert not any(b != a and a.startswith(b + "/") for b in paths)
    assert f"{planted.root}/outer/inner" in paths
    assert report["walk_total_bytes"] == sum(e["bytes"] for e in report["entries"]) + report["unknown"]["c_bytes"]


def test_home_and_its_ancestors_are_never_du_roots_but_their_children_are(planted: Planted) -> None:
    planted.blob("home/work", 1 * MIB)
    planted.blob("top", 1 * MIB)
    report = planted.report()
    assert str(planted.home) not in report["du_roots"] and str(planted.root) not in report["du_roots"]
    assert f"{planted.home}/work" in report["du_roots"] and f"{planted.root}/top" in report["du_roots"]
    own = [e for e in report["entries"] if e["path"] == str(planted.home)]
    assert own and own[0]["bytes"] == planted.home.lstat().st_blocks * 512


@pytest.mark.skipif(os.getuid() == 0, reason="root reads mode-000 directories")
def test_unreadable_directory_goes_to_part_a_as_a_path_without_size(planted: Planted) -> None:
    locked = planted.blob("locked", 1 * MIB)
    inner = locked / "inner"
    inner.mkdir()
    (inner / "f").write_bytes(b"x" * MIB)
    planted.blob("open", 1 * MIB)
    inner.chmod(0)
    try:
        report = planted.report()
    finally:
        inner.chmod(0o755)
    assert str(inner) in report["unknown"]["a_paths"]
    assert all(isinstance(p, str) for p in report["unknown"]["a_paths"])


def test_deadline_keeps_partial_output_and_lists_unreached_roots(planted: Planted, monkeypatch: pytest.MonkeyPatch) -> None:
    planted.blob("a", 1 * MIB)
    planted.blob("b", 1 * MIB)
    for key, value in planted.env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(disk_report, "apply_self_priority", lambda: None)
    seen = {}

    def slow_du(argv: list, timeout_s: float) -> "tuple[bytes, str, bool]":
        seen["argv"], seen["timeout"] = argv, timeout_s
        return f"{MIB}\t{planted.root}/a\0".encode(), "", True

    report = disk_report.build_report(
        root=str(planted.root), home=str(planted.home), names_paths=[disk_report.DEFAULT_TABLE, planted.names],
        mountinfo=str(planted.mountinfo), deadline_min=1, min_bytes=1, max_depth=4, temp_roots=[], argv=["t"], run=slow_du)
    assert 0 < seen["timeout"] <= 60
    assert report["deadline_exceeded"] is True
    assert f"{planted.root}/b" in report["unknown"]["a_paths"] and f"{planted.root}/a" not in report["unknown"]["a_paths"]
    assert all(not e["path"].startswith(f"{planted.root}/b") for e in report["entries"])


def test_du_runs_at_idle_priority_and_the_report_records_it(planted: Planted) -> None:
    planted.blob("x", 1 * MIB)
    report = planted.report()
    launch = report["launch"]
    assert launch["du_argv"][:7] == ["ionice", "-c3", "nice", "-n19", "du", "-x", "-B1"]
    assert launch["priority"] == {"ionice_class": 3, "nice": 19}
    assert launch["du_argv"][:len(launch["du_argv_prefix"])] == launch["du_argv_prefix"]
    assert launch["max_depth"] == 4 and launch["deadline_minutes"] == 5
    assert launch["tool_sha256"] == disk_report.tool_sha256()
    assert set(launch["tool_sha256"]) == {"scripts/disk-usage-report.py", "scripts/lib/disk_report.py"}
    assert [Path(s["path"]).name for s in launch["names_sources"]] == ["disk_report_names.default.toml", "names.toml"]


def test_refuses_without_ionice(planted: Planted, tmp_path: Path) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    done = planted.run(env=planted.env(PATH=str(empty)))
    assert done.returncode == 2 and "ionice" in done.stderr
    assert not list(planted.out.glob("*")) if planted.out.exists() else True


@pytest.mark.parametrize("minutes", ["61", "0", "-1"])
def test_deadline_outside_one_to_sixty_minutes_is_refused(planted: Planted, minutes: str) -> None:
    done = planted.run("--deadline-min", minutes)
    assert done.returncode == 2 and "--deadline-min" in done.stderr


def test_outputs_are_created_exclusively_and_a_second_run_writes_a_suffixed_pair(planted: Planted) -> None:
    planted.blob("x", 1 * MIB)
    assert planted.run().returncode == 0
    first = sorted(p.name for p in planted.out.iterdir())
    kept = {p.name: p.read_bytes() for p in planted.out.iterdir()}
    assert planted.run().returncode == 0
    second = sorted(p.name for p in planted.out.iterdir())
    assert len(first) == 2 and len(second) == 4
    assert any(name.endswith("-2.json") for name in second) and any(name.endswith("-2.md") for name in second)
    assert all((planted.out / name).read_bytes() == data for name, data in kept.items())


def test_temp_root_total_splits_into_residue_kinds_and_the_rest(planted: Planted) -> None:
    (planted.ttmp / "instruction-sandbox.abc").mkdir()
    (planted.ttmp / "instruction-sandbox.abc" / "f").write_bytes(b"x" * MIB)
    (planted.ttmp / "tmp.xyz").mkdir()
    (planted.ttmp / "tmp.xyz" / "f").write_bytes(b"x" * (2 * MIB))
    (planted.ttmp / "other").mkdir()
    (planted.ttmp / "other" / "f").write_bytes(b"x" * (3 * MIB))
    (planted.ttmp / "sbx.q").mkdir()
    (planted.ttmp / "sbx.q" / "f").write_bytes(b"x" * (4 * MIB))
    report = planted.report()
    root = next(r for r in report["temp"]["roots"] if r["realpath"] == str(planted.ttmp.resolve()))
    assert MIB * 3 <= root["residue_kinds_bytes"] < MIB * 3 + 64 * KIB
    assert root["outside_kinds_bytes"] >= 7 * MIB
    assert root["bytes"] == root["residue_kinds_bytes"] + root["outside_kinds_bytes"]
    assert root["top_outside"][0]["path"].endswith("/sbx.q")
    assert report["temp"]["session_root"] == str(planted.ttmp.resolve())


def test_machine_table_overrides_a_default_row_and_adds_its_own(planted: Planted, tmp_path: Path) -> None:
    default = tmp_path / "default.toml"
    default.write_text(f'[[entry]]\npath = "{planted.root}/x"\nlabel = "default x"\nkind = "os"\n')
    planted.names.write_text(
        f'[[entry]]\npath = "{planted.root}/x"\nlabel = "machine x"\nkind = "cache"\n\n'
        f'[[entry]]\npath = "~/h"\nlabel = "home thing"\nkind = "user-files"\n')
    rows, _, sources = disk_report.load_names([default, planted.names], str(planted.home))
    by_path = {r["path"]: r for r in rows}
    assert by_path[f"{planted.root}/x"]["label"] == "machine x"
    assert by_path[f"{planted.home}/h"]["label"] == "home thing"
    assert len(sources) == 2 and all(len(s["sha256"]) == 64 for s in sources)


def test_a_row_above_the_root_does_not_name_entries_below_it(planted: Planted) -> None:
    planted.blob("below", 2 * MIB)
    planted.blob("unnamed", 2 * MIB)
    planted.names.write_text(
        f'[[entry]]\npath = "{planted.root.parent}"\nlabel = "ancestor"\nkind = "os"\n\n'
        f'[[entry]]\npath = "{planted.root}/below"\nlabel = "below thing"\nkind = "cache"\n')
    report = planted.report()
    assert "ancestor" not in {e["label"] for e in report["entries"]}
    assert [e["path"] for e in report["entries"] if e["label"] == "below thing"] == [f"{planted.root}/below"]


def test_default_table_rows_are_valid_and_neutral() -> None:
    rows, excludes, _ = disk_report.load_names([disk_report.DEFAULT_TABLE], "/home/x")
    assert rows and not excludes
    assert all(r["path"].startswith("/") and r["label"] and r["kind"] in disk_report.KINDS for r in rows)


def test_run_changes_nothing_in_the_planted_tree_and_writes_only_to_out_dir(planted: Planted) -> None:
    planted.blob("x", 1 * MIB)
    planted.blob("home/y", 1 * MIB)
    os.symlink(planted.root / "x", planted.root / "link")
    before = snapshot(planted.base)
    assert planted.run().returncode == 0
    after = snapshot(planted.base)
    created = sorted(set(after) - set(before))
    assert all(p.startswith(str(planted.out)) for p in created) and len(created) == 3  # out dir, .json, .md
    assert {p: v for p, v in after.items() if p in before} == before


def test_mountinfo_octal_escapes_decode() -> None:
    text = "5 1 0:9 / /a\\040b rw - fuse.x src rw\n6 1 0:9 / /plain rw shared:1 - ext4 /dev/x rw\n"
    assert disk_report.parse_mountinfo(text) == [("/a b", "fuse.x"), ("/plain", "ext4")]
