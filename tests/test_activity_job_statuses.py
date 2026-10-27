"""Activity's job trigger must classify every job status on purpose. Statuses it doesn't list
fall through to 'error', which is how needs_input (a job waiting on Pryce) first showed up as an
error (2026-10-27, fixed in migration 008)."""

import re

from app.jobs import store
from app.migrations import _MIGRATIONS_DIR, _discover

# Every status a job row can have (store.py and migration 004).
ALL_JOB_STATUSES = {"queued", "needs_approval", "running", *store.SETTLED_STATUSES}
# Only these may fall through to 'error'.
ERROR_STATUSES = {"failed", "timed_out"}


def _latest_job_trigger() -> str:
    """The newest migration's definition of activity_on_job()."""
    for _, _, path in reversed(_discover()):
        sql = path.read_text(encoding="utf-8")
        if "FUNCTION activity_on_job()" in sql:
            return sql.split("FUNCTION activity_on_job()", 1)[1].split("END $$", 1)[0]
    raise AssertionError("no migration defines activity_on_job()")


def _status_map(trigger: str) -> dict[str, str]:
    """status -> activity status, from the CASE that ends in ELSE 'error'."""
    case = re.search(r"CASE NEW\.status (WHEN '[^']+' THEN '[^']+'\s*)+ELSE 'error' END", trigger)
    assert case, "the job trigger's status CASE wasn't found"
    return dict(re.findall(r"WHEN '([^']+)' THEN '([^']+)'", case.group(0)))


def test_every_non_error_status_is_mapped_explicitly():
    mapped = _status_map(_latest_job_trigger())
    missing = ALL_JOB_STATUSES - ERROR_STATUSES - set(mapped)
    assert not missing, f"these job statuses would show as errors in Activity: {missing}"
    assert not ERROR_STATUSES & {s for s, v in mapped.items() if v != "error"}


def test_waiting_statuses_are_pending():
    mapped = _status_map(_latest_job_trigger())
    assert mapped["needs_approval"] == "pending"
    assert mapped["needs_input"] == "pending"


def test_a_job_waiting_on_an_answer_says_so():
    assert "WHEN 'needs_input' THEN ' waiting for your answer'" in _latest_job_trigger()


def test_migration_008_is_discovered_in_order():
    versions = [v for v, _, _ in _discover()]
    assert versions == sorted(versions)
    assert 8 in versions
    assert (_MIGRATIONS_DIR / "008_activity_needs_input.sql").exists()
