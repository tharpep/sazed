-- MCP server registry for dispatch agents, with a history row for every save.
-- Agent profiles grant servers (and tools on them) through agent_profiles.mcp.
CREATE TABLE IF NOT EXISTS mcp_servers (
    name        TEXT PRIMARY KEY,
    transport   TEXT NOT NULL,
    url         TEXT,
    command     TEXT,
    args        JSONB NOT NULL DEFAULT '[]'::jsonb,
    headers     JSONB NOT NULL DEFAULT '{}'::jsonb,
    agent_safe  BOOLEAN NOT NULL DEFAULT false,
    description TEXT NOT NULL DEFAULT '',
    version     INT NOT NULL,
    updated_by  TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mcp_server_versions (
    id      BIGSERIAL PRIMARY KEY,
    name    TEXT NOT NULL,
    version INT NOT NULL,
    author  TEXT NOT NULL,
    reason  TEXT NOT NULL,
    server  JSONB NOT NULL,
    at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (name, version)
);

ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS mcp JSONB NOT NULL DEFAULT '{}'::jsonb;

INSERT INTO mcp_servers
    (name, transport, url, command, args, headers, agent_safe, description, version, updated_by)
VALUES
    ('context7', 'http', 'https://mcp.context7.com/mcp', NULL, '[]'::jsonb, '{}'::jsonb, true,
     'Library and framework docs (Context7)', 1, 'pryce')
ON CONFLICT (name) DO NOTHING;

INSERT INTO mcp_server_versions (name, version, author, reason, server)
VALUES
    ('context7', 1, 'pryce', 'seeded: public docs server, verified headless on 2026-11-07',
     jsonb_build_object(
         'transport', 'http', 'url', 'https://mcp.context7.com/mcp', 'command', NULL,
         'args', '[]'::jsonb, 'headers', '{}'::jsonb, 'agent_safe', true,
         'description', 'Library and framework docs (Context7)'))
ON CONFLICT (name, version) DO NOTHING;
