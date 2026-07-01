# Resolver — source this file from any setup/tooling script that targets the
# agent config root.  Never hardcode $HOME/.claude in install targets.
#
# Override for tests or the Yandex overlay:  export CLAUDE_AGENT_HOME=/path
# Default: ~/.claude-agent  (isolated from the user's personal ~/.claude)
export CLAUDE_AGENT_HOME="${CLAUDE_AGENT_HOME:-$HOME/.claude-agent}"
