"""Automation shapes: a trigger (when), an action (what), and outputs (where results go)."""

from datetime import datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger as _ApsCron
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_TIMEZONE = "America/New_York"

# Direct gateway calls skip Sazed's approval gate, so writes are limited to harmless endpoints.
# Anything else (email, deletes, container power) must go through a sazed_prompt action, where
# sensitive tools become pending approvals instead of running unattended.
GATEWAY_WRITE_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/notify"),
    }
)


# Index = standard cron weekday number; cron allows both 0 and 7 for Sunday.
_DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _cron_dow_to_names(field: str) -> str:
    """Standard cron numbers weekdays 0=Sunday, but APScheduler 3 numbers them 0=Monday (and
    from_crontab doesn't convert), so '1-5' would mean Tue-Sat. Day names mean the same to both,
    so rewrite numbers as names: '1-5' -> 'mon-fri', '0,6' -> 'sun,sat'."""

    def name(token: str) -> str:
        return _DOW_NAMES[int(token)] if token.isdigit() else token

    parts = []
    for part in field.split(","):
        base, slash, step = part.partition("/")
        if base != "*":
            base = "-".join(name(t) for t in base.split("-"))
        parts.append(base + slash + step)
    return ",".join(parts)


class CronSchedule(BaseModel):
    type: Literal["cron"] = "cron"
    cron: str = Field(..., description="5-field crontab, e.g. '0 8 * * 1-5'")
    timezone: str = DEFAULT_TIMEZONE

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone '{v}'") from e
        return v

    @model_validator(mode="after")
    def _valid_cron(self):
        self._trigger()  # raises ValueError on a bad expression
        return self

    def _trigger(self) -> _ApsCron:
        fields = self.cron.split()
        if len(fields) != 5:
            raise ValueError(f"invalid cron '{self.cron}': expected 5 fields")
        minute, hour, day, month, dow = fields
        try:
            return _ApsCron(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=_cron_dow_to_names(dow),
                timezone=ZoneInfo(self.timezone),
            )
        except (ValueError, IndexError) as e:
            raise ValueError(f"invalid cron '{self.cron}': {e}") from e

    def next_after(self, moment: datetime) -> datetime:
        """First fire time strictly after `moment`."""
        nxt = self._trigger().get_next_fire_time(None, moment + timedelta(seconds=1))
        if nxt is None:
            raise ValueError(f"cron '{self.cron}' never fires")
        return nxt

    def upcoming(self, start: datetime, count: int = 3) -> list[datetime]:
        times, moment = [], start
        for _ in range(count):
            moment = self.next_after(moment)
            times.append(moment)
        return times


class PromptAction(BaseModel):
    type: Literal["sazed_prompt"] = "sazed_prompt"
    prompt: str = Field(..., min_length=1, max_length=4000)


class GatewayAction(BaseModel):
    type: Literal["gateway_call"] = "gateway_call"
    method: Literal["GET", "POST"] = "GET"
    path: str = Field(..., min_length=1, max_length=300)
    body: dict | None = None

    @model_validator(mode="after")
    def _safe_path(self):
        if not self.path.startswith("/") or "://" in self.path or ".." in self.path:
            raise ValueError("path must be a gateway path like '/homelab/services'")
        if self.method != "GET" and (self.method, self.path) not in GATEWAY_WRITE_ALLOWLIST:
            allowed = ", ".join(f"{m} {p}" for m, p in sorted(GATEWAY_WRITE_ALLOWLIST))
            raise ValueError(
                f"direct gateway writes are limited to: {allowed}. "
                "Use a sazed_prompt action for anything else (it goes through approvals)."
            )
        return self


class JobAction(BaseModel):
    """Run a worker job directly (plans/07): research, a second opinion, or a code task.

    Deterministic: no Sazed turn decides anything; the job runs with this exact input. Change
    tasks still wait for Pryce's approval in the console before they start.
    """

    type: Literal["job"] = "job"
    kind: Literal["research", "second_opinion", "code_task"]
    # The question (research), the thing to judge (second_opinion) or the instruction (code_task)
    input: str = Field(..., min_length=3, max_length=8000)
    depth: Literal["quick", "deep"] = "quick"
    save_to_kb: bool = False
    repo: str | None = Field(None, max_length=200)
    mode: Literal["review", "change"] = "review"

    @model_validator(mode="after")
    def _repo_for_code(self):
        if self.kind == "code_task" and not self.repo:
            raise ValueError("code_task automations need a repo")
        return self


class PipelineAction(BaseModel):
    """A built-in multi-step pipeline (app/backlog/pipelines.py): the daily scout that files new
    ideas, or the weekly triage that ranks them. Code drives the steps; models only write text."""

    type: Literal["pipeline"] = "pipeline"
    name: Literal["scout", "triage"]
    max_ideas: int = Field(3, ge=1, le=5)  # scout only


Action = Annotated[
    PromptAction | GatewayAction | JobAction | PipelineAction, Field(discriminator="type")
]


class PushoverOutput(BaseModel):
    title: str | None = Field(None, max_length=100, description="Defaults to the automation name")
    priority: int = Field(0, ge=-2, le=1)


class JournalOutput(BaseModel):
    category: Literal["career", "personal"] = "personal"
    subcategory: str = Field("Automations", min_length=1, max_length=100)


class Outputs(BaseModel):
    pushover: PushoverOutput | None = None
    journal: JournalOutput | None = None
    # Sazed may answer NOTHING on a quiet day; the run is then 'skipped' and nothing is sent.
    skip_if_nothing: bool = False


class AutomationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=1000)
    enabled: bool = True
    trigger: CronSchedule
    action: Action
    outputs: Outputs = Outputs()
    created_by: Literal["console", "sazed", "claude"] = "console"


class AutomationUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=1000)
    enabled: bool | None = None
    trigger: CronSchedule | None = None
    action: Action | None = None
    outputs: Outputs | None = None


class SchedulePreview(BaseModel):
    cron: str
    timezone: str = DEFAULT_TIMEZONE
