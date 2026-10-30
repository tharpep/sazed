"""Console cards for tool results: ui_blocks.yaml maps a tool to a widget, with no model involved.

Cards reach the console as ui_block SSE events. They're never stored or sent to the model, so
reopening a conversation rebuilds them from its saved tool results (attach_ui_blocks).
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_RULES_PATH = Path(__file__).with_name("ui_blocks.yaml")
_UNTRUSTED_WRAPPER = re.compile(r'^<untrusted_data source="[^"]*">\n(.*)\n</untrusted_data>$', re.S)


@lru_cache
def _rules() -> dict[str, list[dict[str, Any]]]:
    return yaml.safe_load(_RULES_PATH.read_text(encoding="utf-8"))


def build_ui_block(name: str, tool_input: dict[str, Any], content: str) -> dict[str, Any] | None:
    """The card for a successful tool result, or None if the tool has no matching rule."""
    rule = next(
        (
            r
            for r in _rules().get(name, [])
            if all(tool_input.get(k) == v for k, v in r.get("when", {}).items())
        ),
        None,
    )
    if rule is None:
        return None
    wrapped = _UNTRUSTED_WRAPPER.match(content)
    try:
        result = json.loads(wrapped.group(1) if wrapped else content)
    except ValueError:
        return None
    props: dict[str, Any] = {}
    for prop, key in rule["props"].items():
        if key == "$":
            props[prop] = result
        elif isinstance(result, dict) and key in result:
            props[prop] = result[key]
        else:
            return None
    return {"component": rule["component"], "props": props}


def attach_ui_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add each stored tool result's card to the assistant message that called the tool."""
    calls: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                calls[block["id"]] = (message, block)
            elif block.get("type") == "tool_result" and not block.get("is_error"):
                call = calls.get(block.get("tool_use_id"))
                if call is None or not isinstance(block.get("content"), str):
                    continue
                assistant, tool_use = call
                tool_input = tool_use.get("input") or {}
                ui_block = build_ui_block(tool_use["name"], tool_input, block["content"])
                if ui_block:
                    assistant["content"].append({"type": "ui_block", **ui_block})
    return messages
