"""Tests for lint-prose-length.py WARN threshold and fail-at-ceiling behavior.

Builds a throwaway repo root with a config.md and governed files, then points
the module's REPO_ROOT/CONFIG_MD globals at it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))


def _load_mod():
    path = _SCRIPTS / "lint-prose-length.py"
    spec = importlib.util.spec_from_file_location("lint_prose_length", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CONFIG_TEMPLATE = """\
| Key | Value | Meaning |
|---|---|---|
| `claude-md-max-lines` | `100` | . |
| `claude-md-max-chars` | `1000` | . |
| `readme-max-lines` | `50` | . |
| `cursor-mirror-max-lines` | `50` | . |
| `skill-md-max-lines` | `50` | . |
| `policy-md-max-lines` | `50` | . |
| `skill-description-max-chars` | `850` | . |
| `always-loaded-surface-advisory-chars` | `100000` | . |
| `memory-index-max-bytes` | `1000` | . |
"""


def _make_repo(tmp: Path, claude_lines: int, claude_line_width: int = 5) -> None:
    (tmp / "config.md").write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    body = "\n".join("x" * claude_line_width for _ in range(claude_lines)) + "\n"
    (tmp / "CLAUDE.md").write_text(body, encoding="utf-8")
    (tmp / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp / "cursor" / "rules").mkdir(parents=True)
    (tmp / "cursor" / "rules" / "claude-code-sync.mdc").write_text("m\n", encoding="utf-8")


def _write_memory_index(tmp: Path, body: str) -> None:
    (tmp / "memory-global").mkdir(parents=True, exist_ok=True)
    (tmp / "memory-global" / "MEMORY.md").write_text(body, encoding="utf-8")


def _write_skill(tmp: Path, name: str, description: str) -> None:
    skill_dir = tmp / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody\n",
        encoding="utf-8",
    )


def _run(tmp: Path, capsys):
    mod = _load_mod()
    mod.REPO_ROOT = tmp
    mod.CONFIG_MD = tmp / "config.md"
    rc = mod.main([])
    return rc, capsys.readouterr().out


def test_clean_tree_no_warn(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)  # 50% of lines, 300/1000 chars
    rc, out = _run(tmp_path, capsys)
    assert rc == 0
    assert "WARN" not in out
    assert "OK" in out


def test_warn_at_90_percent_exits_zero(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=92)  # 92% of the 100-line ceiling
    rc, out = _run(tmp_path, capsys)
    assert rc == 0
    assert "lint-prose-length: WARN — CLAUDE.md: 92 lines, 92% of limit 100" in out
    assert "OK" in out


def test_char_warn_at_90_percent(tmp_path, capsys):
    # 50 lines x 18 chars + newline = 950 chars -> 95% of the 1000-char ceiling.
    _make_repo(tmp_path, claude_lines=50, claude_line_width=18)
    rc, out = _run(tmp_path, capsys)
    assert rc == 0
    assert "WARN — CLAUDE.md: 950 chars, 95% of limit 1000" in out


def test_char_unit_not_byte_unit_cyrillic(tmp_path, capsys):
    # 950 Cyrillic chars = 951 chars total (95% of the 1000-char ceiling,
    # WARN) but 1901 UTF-8 bytes -- over the 1000-byte ceiling the OLD
    # byte-based model would have FAILED on. Proves the linter measures
    # chars, not bytes.
    (tmp_path / "config.md").write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    body = "б" * 950 + "\n"
    (tmp_path / "CLAUDE.md").write_text(body, encoding="utf-8")
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp_path / "cursor" / "rules").mkdir(parents=True)
    (tmp_path / "cursor" / "rules" / "claude-code-sync.mdc").write_text("m\n", encoding="utf-8")
    assert len(body.encode("utf-8")) > 1000  # sanity: would FAIL as bytes
    rc, out = _run(tmp_path, capsys)
    assert rc == 0
    assert "WARN — CLAUDE.md: 951 chars, 95% of limit 1000" in out


def test_fail_above_ceiling_still_fatal(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=101)
    rc, out = _run(tmp_path, capsys)
    assert rc == 1
    assert "FAIL" in out
    assert "CLAUDE.md: 101 lines, limit 100" in out


def test_memory_index_over_byte_ceiling_fails(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)
    _write_memory_index(tmp_path, "m" * 1000 + "\n")  # 1001 bytes vs the 1000 ceiling
    rc, out = _run(tmp_path, capsys)
    assert rc == 1
    assert "FAIL" in out
    assert "memory-global/MEMORY.md: 1001 bytes, limit 1000 (memory-index-max-bytes)" in out


def test_memory_index_warn_at_90_percent_exits_zero(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)
    _write_memory_index(tmp_path, "m" * 949 + "\n")  # 950 bytes = 95% of the ceiling
    rc, out = _run(tmp_path, capsys)
    assert rc == 0
    assert (
        "WARN — memory-global/MEMORY.md: 950 bytes, 95% of limit 1000 "
        "(memory-index-max-bytes)" in out
    )
    assert "OK" in out


def test_memory_index_under_ceiling_silent(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)
    _write_memory_index(tmp_path, "m" * 99 + "\n")  # 100 bytes = 10% of the ceiling
    rc, out = _run(tmp_path, capsys)
    assert rc == 0
    assert "memory-global/MEMORY.md" not in out


def test_memory_index_byte_unit_not_char_unit_cyrillic(tmp_path, capsys):
    # The discriminating case, and the reason this check exists at all: 600
    # Cyrillic characters plus a newline are 601 CHARACTERS — comfortably under
    # the 1000 ceiling, so a len(read_text()) implementation reports OK — but
    # 1201 UTF-8 BYTES, which is the axis the harness truncates on.
    _make_repo(tmp_path, claude_lines=50)
    body = "б" * 600 + "\n"
    _write_memory_index(tmp_path, body)
    assert len(body) < 1000  # sanity: a char-measured check would pass this
    assert len(body.encode("utf-8")) > 1000
    rc, out = _run(tmp_path, capsys)
    assert rc == 1
    assert "memory-global/MEMORY.md: 1201 bytes, limit 1000 (memory-index-max-bytes)" in out


def test_skill_description_over_cap_fails(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)
    _write_skill(tmp_path, "toolong", "x" * 900)
    rc, out = _run(tmp_path, capsys)
    assert rc == 1
    assert "FAIL" in out
    assert "skills/toolong/SKILL.md: 900 chars description, limit 850" in out


def test_skill_description_under_cap_passes(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)
    _write_skill(tmp_path, "fine", "x" * 800)
    rc, out = _run(tmp_path, capsys)
    assert rc == 0


def test_surface_report_consistency(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)
    (tmp_path / "memory-global").mkdir()
    (tmp_path / "memory-global" / "MEMORY.md").write_text("m" * 40 + "\n", encoding="utf-8")
    _write_skill(tmp_path, "a", "d" * 100)
    _write_skill(tmp_path, "b", "e" * 200)

    mod = _load_mod()
    mod.REPO_ROOT = tmp_path
    mod.CONFIG_MD = tmp_path / "config.md"
    rc = mod.main(["--surface-report"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "DYNAMIC" not in out

    lines = [l.strip() for l in out.splitlines() if l.strip().endswith("chars")]
    breakdown_total = sum(int(l.rsplit(" ", 2)[-2]) for l in lines if "TOTAL" not in l)
    total_line = next(l for l in lines if l.startswith("TOTAL"))
    reported_total = int(total_line.rsplit(" ", 2)[-2])
    assert reported_total == breakdown_total


def test_surface_report_no_dynamic_scan_without_include_dynamic(tmp_path, capsys):
    # scan_dynamic_injection() is gated by --include-dynamic; the separate
    # PRICE block (price_window_stats()) is NOT gated by that flag and reads
    # transcripts unconditionally — see the module docstring.
    _make_repo(tmp_path, claude_lines=50)

    mod = _load_mod()
    mod.REPO_ROOT = tmp_path
    mod.CONFIG_MD = tmp_path / "config.md"

    def _boom(*args, **kwargs):
        raise AssertionError("scan_dynamic_injection must not run without --include-dynamic")

    mod.scan_dynamic_injection = _boom
    mod.price_window_stats = lambda n_days=mod.PRICE_WINDOW_DAYS: None
    rc = mod.main(["--surface-report"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DYNAMIC" not in out


def test_surface_report_price_block_renders(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)

    mod = _load_mod()
    mod.REPO_ROOT = tmp_path
    mod.CONFIG_MD = tmp_path / "config.md"
    mod.price_window_stats = lambda n_days=mod.PRICE_WINDOW_DAYS: {
        "n_days": 14,
        "n_steps": 1000,
        "total_tokens": 1_000_000,
    }
    rc = mod.main(["--surface-report"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "tokens per step" in out
    assert "charsPerToken" in out
    assert "14 days" in out
    assert "1000 steps" in out
    assert "per 1000 chars" in out
    assert "% of the measured 14-day window" in out


def test_surface_report_price_no_transcript_data_degrades(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)

    mod = _load_mod()
    mod.REPO_ROOT = tmp_path
    mod.CONFIG_MD = tmp_path / "config.md"
    mod.price_window_stats = lambda n_days=mod.PRICE_WINDOW_DAYS: None
    rc = mod.main(["--surface-report"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "no transcript data — cannot price" in out


def test_compute_price_margin_scales_linearly():
    mod = _load_mod()
    p1 = mod.compute_price(
        90_000, n_days=14, n_steps=1000, total_tokens=1_000_000, margin_chars=1000
    )
    p2 = mod.compute_price(
        90_000, n_days=14, n_steps=1000, total_tokens=1_000_000, margin_chars=2000
    )
    assert p2["margin_tokens_per_step"] == 2 * p1["margin_tokens_per_step"]
    assert p2["margin_share_pct"] == 2 * p1["margin_share_pct"]


def test_compute_price_pins_absolute_values():
    # Hand-computed from the inputs: 90000 chars / charsPerToken 3 = 30000 tokens,
    # which is also the per-step cost (the surface rides every step); 30000 * 1000
    # steps / 1000000 window tokens = 3000%. Pinned as literals because a scaling
    # test alone cannot catch a factor that is held constant within it.
    mod = _load_mod()
    price = mod.compute_price(
        90_000, n_days=14, n_steps=1000, total_tokens=1_000_000, margin_chars=1000
    )
    margin_tokens = 1000 / 3
    assert price["surface_tokens"] == 30_000.0
    assert price["tokens_per_step"] == 30_000.0
    assert price["share_pct"] == 3000.0
    assert price["margin_tokens_per_step"] == margin_tokens
    assert price["margin_share_pct"] == margin_tokens * 1000 / 1_000_000 * 100


def test_compute_price_share_tracks_step_count():
    # Doubling the step count doubles both shares: n_steps enters nowhere else, so
    # dropping it from the numerator would leave every other assertion green.
    mod = _load_mod()
    one = mod.compute_price(90_000, n_days=14, n_steps=1000, total_tokens=1_000_000)
    two = mod.compute_price(90_000, n_days=14, n_steps=2000, total_tokens=1_000_000)
    assert two["share_pct"] == 2 * one["share_pct"]
    assert two["margin_share_pct"] == 2 * one["margin_share_pct"]
    assert two["tokens_per_step"] == one["tokens_per_step"]


def test_compute_price_zero_total_tokens_no_zerodiv():
    mod = _load_mod()
    price = mod.compute_price(90_000, n_days=14, n_steps=0, total_tokens=0)
    assert price["share_pct"] == 0.0
    assert price["margin_share_pct"] == 0.0
