---
name: DeepSeek v4 API specifics
description: Difficulty it removes — you need to call DeepSeek v4 but don't know the model ids, how to toggle thinking-mode, or the reasoning_effort levels. Fact — model identifiers, thinking-mode parameter shape (extra_body), and reasoning_effort levels for DeepSeek's OpenAI-compatible API as of 2026-05.
type: reference
schema: leaf/v1
---

# DeepSeek v4 API specifics

## Difficulty

You need to call DeepSeek v4 but don't know the current model IDs, how to toggle thinking mode, or what values `reasoning_effort` accepts — documentation is sparse and the legacy model aliases (`deepseek-chat`, `deepseek-reasoner`) are misleading.

## Guidance

Verified 2026-05-29 against https://api-docs.deepseek.com/quick_start/pricing/ and https://api-docs.deepseek.com/guides/thinking_mode.

**Current models** (preferred):
- `deepseek-v4-pro` — higher-capability
- `deepseek-v4-flash` — faster

**Legacy** (to be deprecated): `deepseek-chat` → maps to v4-flash non-thinking; `deepseek-reasoner` → maps to v4-flash thinking.

**API base URL**: `https://api.deepseek.com/v1` (OpenAI-compatible chat completions).

**Thinking mode**:
- Default-on for both v4-pro and v4-flash.
- Explicit toggle via OpenAI SDK: `extra_body={"thinking": {"type": "enabled"}}` (or `"disabled"`).
- **Not** controlled by the standard OpenAI `reasoning` param — that's the Responses API; DeepSeek uses chat-completions with the extra_body switch.

**Reasoning effort**:
- `reasoning_effort="high"` (default for regular requests) or `"max"` (highest).
- Pass via `extra_body` for ChatCompletions-based SDK callers, or top-level `reasoning_effort=` if the SDK forwards it.

**`openai-agents` SDK integration pattern**:
```python
from openai import AsyncOpenAI
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents import ModelSettings

client = AsyncOpenAI(api_key=..., base_url="https://api.deepseek.com/v1")
model = OpenAIChatCompletionsModel(model="deepseek-v4-pro", openai_client=client)
settings = ModelSettings(
    extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
)
```

> verified by: docs fetch on 2026-05-29 and live smoke-test against `deepseek-v4-pro` (response payload included `provider_data={'model': 'deepseek-v4-pro', ...}` and a `ResponseReasoningItem` summary, confirming thinking mode active).

## See also
