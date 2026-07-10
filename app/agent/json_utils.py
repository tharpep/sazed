"""Shared helpers for parsing JSON out of LLM text output."""


def strip_json_fence(text: str) -> str:
    """Strip a markdown code fence (```...```) wrapping LLM JSON output, if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1])
    return text
