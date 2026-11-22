"""P10: a probe run reaches no service of the user's and leaves the user's OpenCode config as it was.

``service --help`` is read (never ``service`` itself: the user's own OpenCode service holds a fixed
port and must not be started, stopped or contacted). Every probe run passes ``--standalone``; one
stub run is made and the digest of the user's OpenCode config dirs is taken before and after it, and
the user's OpenCode data, state and cache dirs are scanned for a file naming a probe run root (their
bytes change with the user's own use, so only that leak is judged). pass when ``service --help``
exited 0, the run exited 0, the run's argv carried ``--standalone``, the two digests are equal and no
file names a probe root; fail when the user's config changed, a data/state/cache file names a probe
root or ``--standalone`` was missing; unobserved when ``service --help`` or the run did not complete.
"""
import subprocess

from opencode_probe import harness, record, runner, userconfig

CASE_ID = "P10"
K7 = False
SCENARIO = {"models": {"m1": [{"text": "standalone"}]}}
HELP_LINES = 40


def _service_help(h):
    with h.world(SCENARIO, plugin=False) as w:
        done = subprocess.run([w.binary, "service", "--help"], env=w.env, cwd=w.root, capture_output=True,
                              text=True, timeout=30)
    return done.returncode, (done.stdout or done.stderr).splitlines()[:HELP_LINES]


def run(ctx):
    h = ctx.harness()
    before = userconfig.digest_tree()
    help_exit, help_lines = _service_help(h)
    standalone = h.run(SCENARIO, label="standalone", plugin=False, timeout=60)
    after = userconfig.digest_tree()
    changed = sorted(rel for rel in before.keys() | after.keys() if before.get(rel) != after.get(rel))
    leaked = userconfig.leaked_probe_paths()
    evidence = {"service_help_exit": help_exit, "service_help_head": help_lines,
                "run_exit": standalone.exit_code, "run_argv_flags": [a for a in standalone.argv if a.startswith("--")],
                "cli_base": list(harness.CLI_BASE), "user_config_files": len(before),
                "user_config_changed": changed, "user_data_dirs": [str(d) for d in userconfig.data_dirs()],
                "user_data_files_naming_probe_root": leaked}
    if help_exit != 0 or standalone.exit_code != 0:
        return runner.result(record.UNOBSERVED, evidence)
    ok = "--standalone" in standalone.argv and not changed and not leaked
    return runner.result(record.PASS if ok else record.FAIL, evidence)
