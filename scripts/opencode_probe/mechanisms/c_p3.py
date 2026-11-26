"""P3: which instruction files and entry points reach the model.

Characterization: unique tokens are planted in every location OpenCode may read instructions
from (project AGENTS.md and CLAUDE.md, a global config-dir AGENTS.md, the HOME-level Claude
stand-ins, ``instructions`` in the config, the integration config dir, skills and agents
directories) and the stub reports which tokens appear in the request bodies (messages and tool
descriptions). pass when a request was seen and every token has a recorded answer.
"""
from opencode_probe import record, runner

CASE_ID = "P3"
K7 = False

SKILL = "---\nname: {name}\ndescription: {token}\n---\n{token}\n"
FILES = {
    "project/AGENTS.md": "p3-project-agents",
    "project/CLAUDE.md": "p3-project-claude",
    "xdg/config/opencode/AGENTS.md": "p3-xdg-config-agents",
    "opencode-config/AGENTS.md": "p3-integration-dir-agents",
    "p3-extra.md": "p3-instructions-key",
    "opencode-config/skills/p3a/SKILL.md": SKILL.format(name="p3a", token="p3-integration-dir-skill"),
    "project/.opencode/skills/p3b/SKILL.md": SKILL.format(name="p3b", token="p3-project-opencode-skill"),
    "project/.claude/skills/p3c/SKILL.md": SKILL.format(name="p3c", token="p3-project-claude-skill"),
    "project/.agents/skills/p3d/SKILL.md": SKILL.format(name="p3d", token="p3-project-agents-skill"),
    "opencode-config/agents/p3agent.md": "---\ndescription: p3-integration-dir-agent\nmode: subagent\n---\np3-agent-body\n",
    "project/.opencode/agents/p3agent2.md": "---\ndescription: p3-project-agent\nmode: subagent\n---\np3-agent2-body\n",
}
TOKENS = ["p3-project-agents", "p3-project-claude", "p3-xdg-config-agents", "p3-integration-dir-agents",
          "p3-instructions-key", "p3-integration-dir-skill", "p3-project-opencode-skill",
          "p3-project-claude-skill", "p3-project-agents-skill", "p3-integration-dir-agent",
          "p3-project-agent", "stand-in global instructions", "stand-in-agents", "p3-agent-body",
          "You are the **root coordinator**"]


def run(ctx):
    h = ctx.harness()
    config = {"instructions": ["{root}/p3-extra.md"]}
    run = h.run({"models": {"m1": [{"text": "p3 done"}]}}, label="characterize", files=FILES,
                config=config, cwd="project", stand_ins=True)
    chat = [r for r in run.requests if r.get("tools")]
    if not chat:
        return runner.result(record.UNOBSERVED, {"error": "the stub saw no chat request",
                                                 "exit_code": run.exit_code})
    first = chat[0]
    messages, descriptions = str(first.get("messages")), str(first.get("tool_descriptions"))
    seen = {t: {"in_messages": t in messages, "in_tool_descriptions": t in descriptions} for t in TOKENS}
    evidence = {"requests_with_tools": len(chat), "tokens": seen, "tool_names": first.get("tools"),
                "system_message_count": sum(1 for m in first.get("messages") or []
                                            if m.get("role") == "system")}
    return runner.result(record.PASS, evidence)
