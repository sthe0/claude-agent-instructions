"""check-org-neutral.py: org-internal markers must fail, neutral text must pass."""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "check-org-neutral.py"


def run(text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "-"],
        input=text, capture_output=True, text=True,
    )


def test_neutral_text_passes():
    r = run("A transport-neutral pending-gate seam with pluggable notifier.d executables. "
            "Generic ticket-marker poller, messenger bridge, autonomous-spend ledger.")
    assert r.returncode == 0, r.stdout


def test_each_marker_class_caught_case_insensitively():
    for leak in ["GENA", "gena", "T-Run", "Theya", "auto-solve", "ccgram", "Telegram",
                 "startrek", "Yandex", "yandex-guru", "junk/the0", "OOSEVEN",
                 "Arcadia", "arcanum", "Nirvana", "DEEPAGENT"]:
        r = run(f"harmless prefix {leak} harmless suffix")
        assert r.returncode == 1, f"{leak!r} slipped through"


def test_word_boundary_no_false_positive():
    # 'general' contains 'gena' as a substring; \b guard must not fire.
    r = run("a general-purpose agent; short-running jobs; nirvanaesque is not checked")
    assert r.returncode == 0, r.stdout


def test_file_argument(tmp_path):
    f = tmp_path / "body.txt"
    f.write_text("mentions ccgram once", encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), str(f)], capture_output=True, text=True)
    assert r.returncode == 1
