"""E8: a ``{env:NAME}`` reference in the config file's provider settings is replaced by the environment value.

The provider's ``apiKey`` in ``opencode.json`` is ``{env:PROBE_KEY}`` and the process environment
carries a distinctive value; the stub records the ``Authorization`` header of the model request.
Record fields: ``env_substitution`` (the header carries the value) and ``provider_block_shape`` (the
provider block as written, with the reference in place). The ``OPENCODE_CONFIG_CONTENT`` channel is
not driven: its substitution is recorded ``not observed``. pass when the substitution happened;
absent when the header carries the literal reference or no key; unobserved when the stub saw no
model request.
"""
from opencode_probe import harness, record, runner
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E8"
K7 = False

FIELDS = ("env_substitution", "provider_block_shape")
VALUE = "env-sub-VALUE-123"
REFERENCE = "{env:PROBE_KEY}"
SCENARIO = {"models": {"m1": [{"text": "hello"}]}}
CONFIG = {"providers": {"stub": {"settings": {"apiKey": REFERENCE}}}}


def run(ctx):
    h = ctx.harness()
    with sh.captured_post_headers() as posts:
        result = h.run(SCENARIO, label="env-substitution", config=CONFIG, env={"PROBE_KEY": VALUE}, timeout=60)
    sent = [p["headers"].get("Authorization") or p["headers"].get("authorization") for p in posts]
    sent = [value for value in sent if value]
    block = harness.deep_merge(h.base_config(plugin=False), CONFIG)["providers"]["stub"]
    evidence = {"env_substitution": f"Bearer {VALUE}" in sent, "authorization_values_seen": sorted(set(sent)),
                "provider_block_shape": block, "exit_code": result.exit_code,
                "config_content_channel": "not observed"}
    if not sent:
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(record.PASS if evidence["env_substitution"] else record.ABSENT, evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS)
