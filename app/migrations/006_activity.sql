-- Activity timeline (console Activity page): one row per thing Sazed did, written by triggers on
-- the tables that already record each action, so every code path shows up without the agent
-- loop having to remember to log. Gateway events (Pushovers, outages, MCP calls) are copied in
-- by app/activity/gateway_feed.py.
--
-- trace groups events into flows: session:<id> (a chat or an automation's prompt session),
-- run:<id> (an automation run), job:<id> (a job nobody's conversation started: console, MCP),
-- item:<id> (a backlog item). app/activity/service.py links related traces for the flow view.
--
-- Every trigger body swallows its own errors: the timeline must never break a chat or a job.
-- New rows NOTIFY 'activity' (a small JSON summary); job steps NOTIFY 'activity_step' only.

CREATE TABLE IF NOT EXISTS activity_events (
    id           BIGSERIAL PRIMARY KEY,
    at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind         TEXT NOT NULL,     -- chat.user | chat.reply | llm | tool | approval | automation
                                    -- | job | job.pr | job.ci | backlog | gw.<kind>
    title        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'info',  -- ok | error | running | pending | info
    trace        TEXT,
    refs         JSONB NOT NULL DEFAULT '{}'::jsonb,   -- ids: session, run, job, item, ...
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb,   -- kind-specific, fetched on demand
    duration_ms  INT
);

CREATE INDEX IF NOT EXISTS idx_activity_at ON activity_events (at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_trace ON activity_events (trace, at);
CREATE INDEX IF NOT EXISTS idx_activity_kind ON activity_events (kind, at DESC);

-- ---------------------------------------------------------------- helpers

CREATE OR REPLACE FUNCTION activity_emit(
    p_kind TEXT, p_title TEXT, p_status TEXT, p_trace TEXT,
    p_refs JSONB, p_detail JSONB, p_duration INT, p_at TIMESTAMPTZ DEFAULT NOW()
) RETURNS VOID AS $$
BEGIN
    INSERT INTO activity_events (at, kind, title, status, trace, refs, detail, duration_ms)
    VALUES (p_at, p_kind, left(coalesce(p_title, p_kind), 300), coalesce(p_status, 'info'),
            p_trace, coalesce(p_refs, '{}'::jsonb), coalesce(p_detail, '{}'::jsonb), p_duration);
END $$ LANGUAGE plpgsql;

-- The visible text of a stored message, or NULL for tool plumbing (tool_use / tool_result).
CREATE OR REPLACE FUNCTION activity_message_text(p_content TEXT) RETURNS TEXT AS $$
DECLARE
    j JSONB;
    b JSONB;
BEGIN
    BEGIN
        j := p_content::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN p_content;
    END;
    IF jsonb_typeof(j) = 'string' THEN
        RETURN j #>> '{}';
    END IF;
    IF jsonb_typeof(j) <> 'array' THEN
        RETURN NULL;
    END IF;
    FOR b IN SELECT value FROM jsonb_array_elements(j) LOOP
        IF b->>'type' IN ('tool_use', 'tool_result') THEN
            RETURN NULL;
        END IF;
    END LOOP;
    FOR b IN SELECT value FROM jsonb_array_elements(j) LOOP
        IF b->>'type' = 'text' THEN
            RETURN b->>'text';
        END IF;
    END LOOP;
    RETURN NULL;
END $$ LANGUAGE plpgsql IMMUTABLE;

-- A job's flow: follow parent_id to the first job, then map who asked for it.
CREATE OR REPLACE FUNCTION activity_job_trace(p_job UUID) RETURNS TEXT AS $$
DECLARE
    cur UUID := p_job;
    par UUID;
    rb TEXT;
    i INT := 0;
BEGIN
    LOOP
        SELECT parent_id, requested_by INTO par, rb FROM jobs WHERE id = cur;
        EXIT WHEN par IS NULL OR i >= 20;
        cur := par;
        i := i + 1;
    END LOOP;
    IF rb ~ '^(session|automation|think|claude):[0-9a-f-]{36}$' THEN
        RETURN 'session:' || split_part(rb, ':', 2);
    ELSIF rb LIKE 'automation-run:%' THEN
        RETURN 'run:' || split_part(rb, ':', 2);
    END IF;
    RETURN 'job:' || cur;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION activity_job_label(p_kind TEXT) RETURNS TEXT AS $$
    SELECT CASE p_kind
        WHEN 'research' THEN 'Research'
        WHEN 'second_opinion' THEN 'Second opinion'
        WHEN 'code_task' THEN 'Code task'
        ELSE initcap(replace(p_kind, '_', ' ')) END
$$ LANGUAGE sql IMMUTABLE;

-- ---------------------------------------------------------------- live notify

CREATE OR REPLACE FUNCTION activity_notify() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('activity', json_build_object(
        'id', NEW.id, 'at', NEW.at, 'kind', NEW.kind, 'title', left(NEW.title, 200),
        'status', NEW.status, 'trace', NEW.trace, 'refs', NEW.refs,
        'duration_ms', NEW.duration_ms)::text);
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RETURN NEW;
END $$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------- backfill (last 14 days)

INSERT INTO activity_events (at, kind, title, status, trace, refs, detail, duration_ms)
SELECT m.timestamp,
       CASE WHEN m.role = 'user' THEN 'chat.user' ELSE 'chat.reply' END,
       left(activity_message_text(m.content), 300), 'ok', 'session:' || m.session_id,
       jsonb_build_object('session', m.session_id, 'session_type', s.session_type),
       jsonb_build_object('text', left(activity_message_text(m.content), 4000)), NULL
FROM messages m JOIN sessions s ON s.id = m.session_id
WHERE m.timestamp > NOW() - INTERVAL '14 days'
  AND m.role IN ('user', 'assistant')
  AND activity_message_text(m.content) IS NOT NULL;

INSERT INTO activity_events (at, kind, title, status, trace, refs, detail, duration_ms)
SELECT l.timestamp, 'llm', l.model || coalesce(' · ' || l.purpose, ''), 'ok',
       'session:' || l.session_id, jsonb_build_object('session', l.session_id),
       jsonb_build_object('model', l.model, 'purpose', l.purpose, 'turn', l.turn,
                          'input_tokens', l.input_tokens, 'output_tokens', l.output_tokens,
                          'cache_read_tokens', l.cache_read_tokens,
                          'cache_write_tokens', l.cache_write_tokens),
       l.duration_ms
FROM llm_calls l
WHERE l.timestamp > NOW() - INTERVAL '14 days';

INSERT INTO activity_events (at, kind, title, status, trace, refs, detail, duration_ms)
SELECT a.timestamp, 'tool', a.tool_name,
       CASE WHEN a.status = 'success' THEN 'ok' ELSE 'error' END,
       'session:' || a.session_id, jsonb_build_object('session', a.session_id),
       jsonb_build_object('input', a.input, 'output', left(a.output, 4000),
                          'error', a.error_message),
       a.duration_ms
FROM action_logs a
WHERE a.timestamp > NOW() - INTERVAL '14 days';

INSERT INTO activity_events (at, kind, title, status, trace, refs, detail, duration_ms)
SELECT coalesce(r.finished_at, r.started_at), 'automation', a.name || ': ' || r.status,
       CASE r.status WHEN 'success' THEN 'ok' WHEN 'skipped' THEN 'info'
                     WHEN 'needs_approval' THEN 'pending' WHEN 'running' THEN 'running'
                     ELSE 'error' END,
       'run:' || r.id,
       jsonb_build_object('run', r.id, 'automation', r.automation_id, 'session', r.session_id,
                          'triggered_by', r.triggered_by),
       jsonb_build_object('output', left(r.output, 2000), 'error', r.error,
                          'delivered', r.delivered),
       (extract(epoch FROM (r.finished_at - r.started_at)) * 1000)::int
FROM automation_runs r JOIN automations a ON a.id = r.automation_id
WHERE r.started_at > NOW() - INTERVAL '14 days';

INSERT INTO activity_events (at, kind, title, status, trace, refs, detail, duration_ms)
SELECT coalesce(j.finished_at, j.created_at), 'job',
       activity_job_label(j.kind) || ' ' || replace(j.status, '_', ' ') || ': ' || j.title,
       CASE j.status WHEN 'succeeded' THEN 'ok' WHEN 'running' THEN 'running'
                     WHEN 'queued' THEN 'info' WHEN 'needs_approval' THEN 'pending'
                     WHEN 'cancelled' THEN 'info' ELSE 'error' END,
       activity_job_trace(j.id),
       jsonb_build_object('job', j.id, 'parent', j.parent_id, 'agent', j.agent,
                          'model', j.model, 'requested_by', j.requested_by),
       jsonb_build_object('error', j.error, 'pr_url', j.meta->>'pr_url'),
       (extract(epoch FROM (j.finished_at - j.started_at)) * 1000)::int
FROM jobs j
WHERE j.created_at > NOW() - INTERVAL '14 days';

INSERT INTO activity_events (at, kind, title, status, trace, refs, detail, duration_ms)
SELECT b.created_at, 'backlog', 'New ' || replace(b.type, '_', ' ') || ': ' || b.title, 'info',
       'item:' || b.id, jsonb_build_object('item', b.id, 'source', b.source), '{}'::jsonb, NULL
FROM backlog_items b;

-- ---------------------------------------------------------------- triggers

CREATE OR REPLACE FUNCTION activity_on_message() RETURNS trigger AS $$
DECLARE
    txt TEXT;
    stype TEXT;
BEGIN
    IF NEW.role NOT IN ('user', 'assistant') THEN
        RETURN NEW;
    END IF;
    txt := activity_message_text(NEW.content);
    IF txt IS NULL OR btrim(txt) = '' THEN
        RETURN NEW;
    END IF;
    SELECT session_type INTO stype FROM sessions WHERE id = NEW.session_id;
    PERFORM activity_emit(
        CASE WHEN NEW.role = 'user' THEN 'chat.user' ELSE 'chat.reply' END,
        txt, 'ok', 'session:' || NEW.session_id,
        jsonb_build_object('session', NEW.session_id, 'session_type', coalesce(stype, 'chat')),
        jsonb_build_object('text', left(txt, 4000)), NULL);
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'activity_on_message: %', SQLERRM;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION activity_on_llm_call() RETURNS trigger AS $$
BEGIN
    PERFORM activity_emit(
        'llm', NEW.model || coalesce(' · ' || NEW.purpose, ''), 'ok',
        'session:' || NEW.session_id, jsonb_build_object('session', NEW.session_id),
        jsonb_build_object('model', NEW.model, 'purpose', NEW.purpose, 'turn', NEW.turn,
                           'input_tokens', NEW.input_tokens, 'output_tokens', NEW.output_tokens,
                           'cache_read_tokens', NEW.cache_read_tokens,
                           'cache_write_tokens', NEW.cache_write_tokens),
        NEW.duration_ms);
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'activity_on_llm_call: %', SQLERRM;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION activity_on_action_log() RETURNS trigger AS $$
BEGIN
    PERFORM activity_emit(
        'tool', NEW.tool_name, CASE WHEN NEW.status = 'success' THEN 'ok' ELSE 'error' END,
        'session:' || NEW.session_id, jsonb_build_object('session', NEW.session_id),
        jsonb_build_object('input', NEW.input, 'output', left(NEW.output, 4000),
                           'error', NEW.error_message),
        NEW.duration_ms);
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'activity_on_action_log: %', SQLERRM;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION activity_on_pending_action() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM activity_emit(
            'approval', 'Waiting for approval: ' || NEW.tool_name, 'pending',
            'session:' || NEW.session_id,
            jsonb_build_object('session', NEW.session_id, 'pending_action', NEW.id),
            jsonb_build_object('input', NEW.tool_input), NULL);
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM activity_emit(
            'approval', NEW.tool_name || ' ' || NEW.status,
            CASE WHEN NEW.status = 'confirmed' THEN 'ok' ELSE 'info' END,
            'session:' || NEW.session_id,
            jsonb_build_object('session', NEW.session_id, 'pending_action', NEW.id),
            '{}'::jsonb, NULL);
    END IF;
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'activity_on_pending_action: %', SQLERRM;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION activity_on_automation_run() RETURNS trigger AS $$
DECLARE
    nm TEXT;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NEW;
    END IF;
    SELECT name INTO nm FROM automations WHERE id = NEW.automation_id;
    PERFORM activity_emit(
        'automation',
        coalesce(nm, 'Automation') || CASE WHEN NEW.status = 'running' THEN ' started'
                                           ELSE ': ' || replace(NEW.status, '_', ' ') END,
        CASE NEW.status WHEN 'success' THEN 'ok' WHEN 'skipped' THEN 'info'
                        WHEN 'needs_approval' THEN 'pending' WHEN 'running' THEN 'running'
                        ELSE 'error' END,
        'run:' || NEW.id,
        jsonb_build_object('run', NEW.id, 'automation', NEW.automation_id,
                           'session', NEW.session_id, 'triggered_by', NEW.triggered_by),
        jsonb_build_object('output', left(NEW.output, 2000), 'error', NEW.error,
                           'delivered', NEW.delivered),
        CASE WHEN NEW.finished_at IS NOT NULL
             THEN (extract(epoch FROM (NEW.finished_at - NEW.started_at)) * 1000)::int END);
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'activity_on_automation_run: %', SQLERRM;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION activity_on_job() RETURNS trigger AS $$
DECLARE
    tr TEXT;
    refs JSONB;
    label TEXT;
BEGIN
    -- Assigned here, not in DECLARE: initializer errors would escape the EXCEPTION clause.
    tr := activity_job_trace(NEW.id);
    refs := jsonb_build_object('job', NEW.id, 'parent', NEW.parent_id, 'agent', NEW.agent,
                               'model', NEW.model, 'requested_by', NEW.requested_by,
                               'repo', NEW.params->>'repo');
    label := activity_job_label(NEW.kind);
    IF TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM activity_emit(
            'job',
            label || CASE NEW.status
                WHEN 'queued' THEN ' queued'
                WHEN 'needs_approval' THEN ' waiting for approval'
                WHEN 'running' THEN ' started'
                WHEN 'succeeded' THEN ' done'
                ELSE ' ' || replace(NEW.status, '_', ' ') END || ': ' || NEW.title,
            CASE NEW.status WHEN 'succeeded' THEN 'ok' WHEN 'running' THEN 'running'
                            WHEN 'queued' THEN 'info' WHEN 'needs_approval' THEN 'pending'
                            WHEN 'cancelled' THEN 'info' ELSE 'error' END,
            tr, refs,
            jsonb_build_object('error', NEW.error, 'result', left(NEW.result, 1500),
                               'meta', NEW.meta - 'usage'),
            CASE WHEN NEW.finished_at IS NOT NULL AND NEW.started_at IS NOT NULL
                 THEN (extract(epoch FROM (NEW.finished_at - NEW.started_at)) * 1000)::int END);
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.meta->>'pr_url' IS DISTINCT FROM OLD.meta->>'pr_url'
       AND NEW.meta->>'pr_url' IS NOT NULL THEN
        PERFORM activity_emit(
            'job.pr',
            'PR #' || (NEW.meta->>'pr_number') ||
                CASE WHEN (NEW.meta->>'pr_updated')::boolean THEN ' updated' ELSE ' opened' END ||
                ' · ' || coalesce(split_part(NEW.params->>'repo', '/', 2), ''),
            'ok', tr, refs || jsonb_build_object('pr_url', NEW.meta->>'pr_url'),
            jsonb_build_object('pr_url', NEW.meta->>'pr_url'), NULL);
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.meta->>'ci_state' IS DISTINCT FROM OLD.meta->>'ci_state'
       AND NEW.meta->>'ci_state' IS NOT NULL THEN
        PERFORM activity_emit(
            'job.ci',
            'CI ' || replace(NEW.meta->>'ci_state', '_', ' ') || ' on PR #' ||
                coalesce(NEW.meta->>'pr_number', '?'),
            CASE NEW.meta->>'ci_state' WHEN 'success' THEN 'ok' WHEN 'failure' THEN 'error'
                                       WHEN 'fixing' THEN 'running' ELSE 'info' END,
            tr, refs || jsonb_build_object('pr_url', NEW.meta->>'pr_url'),
            jsonb_build_object('ci_runs', NEW.meta->'ci_runs'), NULL);
    END IF;
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'activity_on_job: %', SQLERRM;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

-- Job steps can number in the hundreds per job, and job_events already keeps them, so they're
-- only announced live (the flow view reads job_events for the full list).
CREATE OR REPLACE FUNCTION activity_on_job_event() RETURNS trigger AS $$
DECLARE
    payload TEXT;
BEGIN
    payload := json_build_object('job', NEW.job_id, 'at', NEW.at, 'event', NEW.event)::text;
    IF octet_length(payload) > 7500 THEN
        payload := json_build_object('job', NEW.job_id, 'at', NEW.at,
                                     'event', json_build_object('type', NEW.event->>'type',
                                                                'truncated', true))::text;
    END IF;
    PERFORM pg_notify('activity_step', payload);
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION activity_on_backlog() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM activity_emit(
            'backlog', 'New ' || replace(NEW.type, '_', ' ') || ': ' || NEW.title, 'info',
            'item:' || NEW.id, jsonb_build_object('item', NEW.id, 'source', NEW.source),
            jsonb_build_object('summary', left(NEW.summary, 1500)), NULL);
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM activity_emit(
            'backlog', NEW.title || ': ' || NEW.status,
            CASE NEW.status WHEN 'done' THEN 'ok' WHEN 'approved' THEN 'running' ELSE 'info' END,
            'item:' || NEW.id,
            jsonb_build_object('item', NEW.id, 'job', NEW.job_id, 'source', NEW.source),
            jsonb_build_object('note', NEW.note), NULL);
    END IF;
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'activity_on_backlog: %', SQLERRM;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS activity_notify_trg ON activity_events;
CREATE TRIGGER activity_notify_trg AFTER INSERT ON activity_events
    FOR EACH ROW EXECUTE FUNCTION activity_notify();

DROP TRIGGER IF EXISTS activity_message_trg ON messages;
CREATE TRIGGER activity_message_trg AFTER INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION activity_on_message();

DROP TRIGGER IF EXISTS activity_llm_trg ON llm_calls;
CREATE TRIGGER activity_llm_trg AFTER INSERT ON llm_calls
    FOR EACH ROW EXECUTE FUNCTION activity_on_llm_call();

DROP TRIGGER IF EXISTS activity_action_trg ON action_logs;
CREATE TRIGGER activity_action_trg AFTER INSERT ON action_logs
    FOR EACH ROW EXECUTE FUNCTION activity_on_action_log();

DROP TRIGGER IF EXISTS activity_pending_trg ON pending_actions;
CREATE TRIGGER activity_pending_trg AFTER INSERT OR UPDATE OF status ON pending_actions
    FOR EACH ROW EXECUTE FUNCTION activity_on_pending_action();

DROP TRIGGER IF EXISTS activity_run_trg ON automation_runs;
CREATE TRIGGER activity_run_trg AFTER INSERT OR UPDATE OF status ON automation_runs
    FOR EACH ROW EXECUTE FUNCTION activity_on_automation_run();

DROP TRIGGER IF EXISTS activity_job_ins_trg ON jobs;
CREATE TRIGGER activity_job_ins_trg AFTER INSERT ON jobs
    FOR EACH ROW EXECUTE FUNCTION activity_on_job();

-- Heartbeats update jobs every few seconds; only real changes reach the function.
DROP TRIGGER IF EXISTS activity_job_upd_trg ON jobs;
CREATE TRIGGER activity_job_upd_trg AFTER UPDATE ON jobs
    FOR EACH ROW
    WHEN (OLD.status IS DISTINCT FROM NEW.status
          OR OLD.meta->>'pr_url' IS DISTINCT FROM NEW.meta->>'pr_url'
          OR OLD.meta->>'ci_state' IS DISTINCT FROM NEW.meta->>'ci_state')
    EXECUTE FUNCTION activity_on_job();

DROP TRIGGER IF EXISTS activity_job_event_trg ON job_events;
CREATE TRIGGER activity_job_event_trg AFTER INSERT ON job_events
    FOR EACH ROW EXECUTE FUNCTION activity_on_job_event();

DROP TRIGGER IF EXISTS activity_backlog_trg ON backlog_items;
CREATE TRIGGER activity_backlog_trg AFTER INSERT OR UPDATE OF status ON backlog_items
    FOR EACH ROW EXECUTE FUNCTION activity_on_backlog();
