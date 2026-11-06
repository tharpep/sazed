from app.agent.memory import format_for_prompt
from app.agent.procedures import format_procedures_for_prompt


def _facts():
    return [
        {"fact_type": "preference", "key": "tone", "value": "brief"},
        {"fact_type": "identity", "key": "name", "value": "Pryce"},
        {"fact_type": "preference", "key": "editor", "value": "vim"},
        {"fact_type": "identity", "key": "city", "value": "Austin"},
    ]


def test_memory_text_ignores_load_order():
    facts = _facts()
    assert format_for_prompt(facts) == format_for_prompt(list(reversed(facts)))
    lines = format_for_prompt(facts).splitlines()
    assert lines[0] == "**Identity**"
    assert lines[1] == "- city: Austin"


def _procs():
    step = {"tool": "get_events", "purpose": "look"}
    return [
        {"name": "b-proc", "trigger_desc": "second", "steps": [step]},
        {"name": "a-proc", "trigger_desc": "first", "steps": [step]},
    ]


def test_procedures_text_ignores_load_order():
    procs = _procs()
    assert format_procedures_for_prompt(procs) == format_procedures_for_prompt(procs[::-1])
    assert format_procedures_for_prompt(procs).index("a-proc") < format_procedures_for_prompt(
        procs
    ).index("b-proc")
