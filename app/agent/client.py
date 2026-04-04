"""Shared Anthropic client singleton and common agent utilities."""

import anthropic

from app.config import settings

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def tool_sig(name: str, args: dict) -> tuple:
    """Stable hashable signature for a tool call — used for stuck loop detection."""
    return (name, tuple(sorted((k, str(v)) for k, v in args.items())))
