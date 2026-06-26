"""Back-compat alias: ExternalChannel -> GitHubChannel.

The concrete GitHub Issues adapter lives in github.py. This module keeps the
``ExternalChannel`` name importable for any code that referenced the old stub and
registers the channel under the legacy ``"external"`` key so existing configs
that specify channel=external continue to work.
"""
from __future__ import annotations

from .github import GitHubChannel
from ..port import register_channel

# Public alias preserved for backward compatibility.
ExternalChannel = GitHubChannel

register_channel("external", GitHubChannel)
