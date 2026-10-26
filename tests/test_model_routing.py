"""Model routing (plans/11 phase 1): Sonnet front door, Opus on escalation, Haiku background."""

from app.agent.client import model_params
from app.agent.loop import _select_model
from app.config import settings


def test_front_door_is_sonnet():
    assert _select_model(escalated=False) == (settings.sonnet_model, "front_door")


def test_escalation_moves_to_opus():
    assert _select_model(escalated=True) == (settings.opus_model, "escalation_requested")


def test_haiku_runs_without_thinking():
    assert model_params(settings.haiku_model) == {"thinking": {"type": "disabled"}}


def test_sonnet_and_opus_share_thinking_params():
    for model in (settings.sonnet_model, settings.opus_model):
        params = model_params(model)
        body = params["extra_body"]
        # Opus 5.5 rejects disabled thinking at every effort level, so it must be adaptive.
        assert body["thinking"]["type"] == "adaptive"
        assert body["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
        assert body["output_config"] == {"effort": "medium"}
        assert body["fallbacks"] == "default"
        betas = params["extra_headers"]["anthropic-beta"].split(",")
        assert "thinking-binding-controls-2026-08-01" in betas
        assert "server-side-fallback-2026-07-01" in betas


def test_unknown_model_gets_no_extra_params():
    assert model_params("claude-something-else") == {}
