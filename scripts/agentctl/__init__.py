"""agentctl — deterministic coordination state-machine engine.

The CLAUDE.md coordination spine (classify -> route -> plan-approval gate ->
dispatch -> verify -> resolution gate, plus difficulty/replan) expressed as code.
The LLM is invoked only on cognitive leaves; the engine owns the control flow.

Layering (the only filesystem seam is store.py, the future Variant-3/MCP boundary):
  config.py    parse thresholds from config.md
  state.py     SessionState / Stage / GateRecord dataclasses + invariants + JSON
  store.py     StateStore Protocol + FileStateStore (durable state persistence)
  plan.py      read the author-written TOML plan -> Stage[]; diff for replan
  classify.py  pure weight/criterion/route classification
  gates.py     gate registry: predicate + guardian
  machine.py   Node enum, transition table, transition()
  directive.py Directive dataclass (CLI-now / MCP-later contract)
  dispatch.py  subprocess wrappers over spawn-specialist.py
  cli.py       subcommands; each returns a Directive
"""
