"""Policy core (plans/12 phase 1): today's rules moved into app.policy with behavior unchanged."""

import pytest
from pydantic import ValidationError

from app.automations.models import GatewayAction
from app.config import settings
from app.jobs import service
from app.policy import GatewayWrite, StartJob, ToolCall, counts_toward_cap, decide


def test_sensitive_tool_pauses_only_when_someone_can_answer(monkeypatch):
    monkeypatch.setattr(settings, "confirmation_required", True)
    assert decide(ToolCall("send_email", interactive=True)).verdict == "pause"
    assert decide(ToolCall("send_email", interactive=False)).verdict == "allow"
    assert decide(ToolCall("get_events", interactive=True)).verdict == "allow"


def test_confirmation_switch_turns_the_pause_off(monkeypatch):
    monkeypatch.setattr(settings, "confirmation_required", False)
    assert decide(ToolCall("send_email", interactive=True)).verdict == "allow"


def test_model_allowlist_per_agent():
    assert decide(StartJob("claude", "console", model="claude-sonnet-5-5")).verdict == "allow"
    blocked = decide(StartJob("claude", "console", model="claude-fable-5-1"))
    assert blocked.verdict == "block"
    assert blocked.reason.startswith("model 'claude-fable-5-1' is not allowed for claude.")
    assert decide(StartJob("agy", "console", model="claude-sonnet-5-5")).verdict == "block"


def test_excluded_repos_match_case_insensitively(monkeypatch):
    monkeypatch.setattr(settings, "code_task_excluded_repos", ["tharpep/Trusted-Model-Registry"])
    blocked = decide(StartJob("claude", "console", repo="tharpep/trusted-model-registry"))
    assert decide(StartJob("claude", "mcp", repo="tharpep/TRUSTED-model-registry")).verdict == (
        "block"
    )
    assert blocked.verdict == "block"
    assert blocked.reason.endswith("is excluded from code tasks")
    assert decide(StartJob("claude", "console", repo="tharpep/sazed")).verdict == "allow"


def test_daily_cap_applies_only_to_jobs_sazed_starts_itself(monkeypatch):
    monkeypatch.setattr(settings, "jobs_daily_cap_claude", 5)
    for requested_by in ("automation-run:1", "think:x", "ci-fix:1", "sazed"):
        assert counts_toward_cap(requested_by)
        assert decide(StartJob("claude", requested_by, used_today=5)).verdict == "block"
        assert decide(StartJob("claude", requested_by, used_today=4)).verdict == "allow"
    for requested_by in ("console", "mcp", "session:abc"):
        assert not counts_toward_cap(requested_by)
        assert decide(StartJob("claude", requested_by, used_today=99)).verdict == "allow"


def test_cap_message_is_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "jobs_daily_cap_agy", 20)
    reason = decide(StartJob("agy", "automation-run:1", used_today=20)).reason
    assert reason == (
        "daily cap reached for agy (20/20 jobs in the last 24 hours). "
        "It frees up as older jobs age out."
    )


def test_gateway_writes_limited_to_the_allowlist():
    assert decide(GatewayWrite("GET", "/homelab/services")).verdict == "allow"
    assert decide(GatewayWrite("POST", "/notify")).verdict == "allow"
    blocked = decide(GatewayWrite("POST", "/email/send"))
    assert blocked.verdict == "block"
    assert "direct gateway writes are limited to: POST /notify." in blocked.reason


def test_gateway_action_model_still_rejects_writes():
    with pytest.raises(ValidationError, match="direct gateway writes are limited to"):
        GatewayAction(method="POST", path="/email/send")
    assert GatewayAction(method="POST", path="/notify", body={"message": "hi"}).path == "/notify"


def test_job_service_raises_the_policy_reason():
    with pytest.raises(service.JobError, match="is not allowed for agy"):
        service._check_model("agy", "claude-fable-5-1")


async def test_check_cap_counts_only_capped_requesters(monkeypatch):
    calls = []

    async def count_recent(agent):
        calls.append(agent)
        return 99

    monkeypatch.setattr(service.store, "count_recent", count_recent)
    await service._check_cap("claude", "console")  # uncapped: no count, no error
    assert calls == []
    with pytest.raises(service.JobError, match="daily cap reached for claude"):
        await service._check_cap("claude", "automation-run:1")
    assert calls == ["claude"]
