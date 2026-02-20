# Expansion Plan â€“ Tool Breadth & Capabilities

**Prerequisite:** Core development plan (Phases 1â€“6) complete.
**Purpose:** Scale the agent's usefulness by expanding what it can actually *do*.
**Principle:** Every expansion = new gateway endpoints. Agent + MCP + frontend
get them for free. No architectural changes needed.

---

## 1. File & Document Management

Upgrade the agent from read-only to full file lifecycle management.

### 1A. Gateway Endpoints

```
POST   /storage/files              â€“ create file in Drive (text, doc, sheet)
PUT    /storage/files/{id}         â€“ update file content
DELETE /storage/files/{id}         â€“ delete/trash file
GET    /storage/files/{id}/content â€“ fetch full file content (not chunks)
POST   /storage/files/{id}/copy    â€“ duplicate a file
POST   /storage/folders            â€“ create folder in Drive
```

### 1B. KB Write Operations

```
DELETE /kb/sources/{id}            â€“ remove source + all its chunks
DELETE /kb/chunks/{id}             â€“ remove specific chunk(s)
POST   /kb/ingest/text             â€“ ingest raw text (no file backing)
PUT    /kb/chunks/{id}             â€“ update chunk content + re-embed
```

### 1C. Use Cases

- "Create a shopping list" â†’ create Google Sheet in Drive, populate rows
- "Save these meeting notes" â†’ create markdown in Drive â†’ auto-ingest to KB
- "Delete my old resume from the knowledge base" â†’ remove source + chunks
- "Remember that the deploy password is X" â†’ ingest text directly to KB
- Agent writes its own documentation, test plans, runbooks

### 1D. Decisions

- Simple lists â†’ Google Tasks (already have CRUD). Use task lists, not Sheets.
- Structured/tabular data â†’ Google Sheets via Sheets API
- Prose documents â†’ Google Docs or markdown files in Drive
- Quick notes / agent-generated knowledge â†’ direct KB text ingestion (no file)
- File creation auto-triggers KB sync for that file (optional, configurable)

---

## 2. Task List Management âœ… DONE

Expand beyond fixed task lists to full task list lifecycle.

### 2A. Gateway Endpoints

```
POST   /tasks/lists                â€“ create new task list
PATCH  /tasks/lists/{id}           â€“ rename task list
DELETE /tasks/lists/{id}           â€“ delete task list
POST   /tasks/{id}/complete        â€“ mark task complete
POST   /tasks/{id}/uncomplete      â€“ mark task incomplete
PATCH  /tasks/{id}/move            â€“ move task to different list
```

### 2B. Use Cases

- "Make me a grocery list" â†’ create task list â†’ add items
- "Move that task to my work list" â†’ move between lists
- "Mark everything on my errands list as done" â†’ batch complete
- Dynamic project-specific lists created and destroyed as needed

---

## 3. Web Search âœ… DONE

Give the agent access to real-time information beyond the knowledge base.

### 3A. Gateway Endpoints

```
POST   /search/web                 â€“ web search (returns snippets + URLs)
POST   /search/web/fetch           â€“ fetch full page content from URL
```

### 3B. Provider Options

| Provider | Cost | Notes |
|----------|------|-------|
| SerpAPI | $50/5000 searches | Google results, reliable |
| Brave Search API | Free tier (2000/mo) | Good quality, privacy-focused |
| Tavily | Free tier (1000/mo) | Built for AI agents, returns clean text |
| Google Custom Search | Free tier (100/day) | Official, limited |

**Recommendation:** Tavily for v1. Purpose-built for agents â€“ returns
pre-extracted text rather than raw HTML, supports search depth control,
and the free tier is generous for personal use. Brave as fallback.

### 3C. Use Cases

- "What's the weather tomorrow?" â†’ web search
- "Find me the best price on a 4090" â†’ search + compare
- "What are the side effects of X medication?" â†’ search, summarize
- "Look up the API docs for this library" â†’ fetch specific URL
- Agent self-serves when KB doesn't have the answer

### 3D. Agent Integration

The agent's reasoning loop should attempt KB search first, then fall back
to web search when KB doesn't have relevant results. This preserves the
principle that personal knowledge is the primary source of truth.

---

## 4. Google Contacts (People API)

Enable contact resolution so the agent can work with names, not just
email addresses.

### 4A. Gateway Endpoints

```
GET    /contacts/search            â€“ search contacts by name/email/phone
GET    /contacts/{id}              â€“ get contact details
POST   /contacts                   â€“ create contact
PATCH  /contacts/{id}              â€“ update contact
GET    /contacts/groups            â€“ list contact groups
```

### 4B. Use Cases

- "Email John about the meeting" â†’ resolve "John" to john@email.com â†’ send
- "What's Sarah's phone number?" â†’ contact lookup
- "Schedule a call with my dentist" â†’ resolve contact â†’ create event
- Agent auto-resolves names in calendar invites, emails, tasks

### 4C. OAuth Scope

Requires `https://www.googleapis.com/auth/contacts.readonly` at minimum,
`https://www.googleapis.com/auth/contacts` for write. Add to existing
OAuth consent screen.

---

## 5. Google Sheets Integration

Distinct from basic Drive file creation â€“ this is structured data manipulation.

### 5A. Gateway Endpoints

```
POST   /sheets                     â€“ create new spreadsheet
GET    /sheets/{id}                â€“ get spreadsheet metadata
GET    /sheets/{id}/values/{range} â€“ read cell range
PUT    /sheets/{id}/values/{range} â€“ write cell range
POST   /sheets/{id}/values/append  â€“ append rows
DELETE /sheets/{id}/sheets/{sheetId} â€“ delete a tab
```

### 5B. Use Cases

- "Track my expenses this month" â†’ create sheet, append rows over time
- "Add this receipt to my expense tracker" â†’ append row to existing sheet
- "What did I spend on food last week?" â†’ read range, summarize
- Budget tracking, habit tracking, any structured personal data
- Shopping lists with quantities, prices, categories

---

## 6. Notification Enhancements

Expand beyond push notifications to scheduled and conditional alerts.

### 6A. Gateway Endpoints

```
POST   /notify/schedule            â€“ schedule notification for future time
GET    /notify/scheduled           â€“ list pending scheduled notifications
DELETE /notify/scheduled/{id}      â€“ cancel scheduled notification
POST   /notify/recurring           â€“ set up recurring notification
```

### 6B. Implementation

Use Cloud Scheduler + Cloud Tasks for deferred delivery. Notifications
stored in Postgres with delivery state tracking.

### 6C. Use Cases

- "Remind me to call mom at 5pm" â†’ scheduled notification
- "Remind me every Monday to review my budget" â†’ recurring
- Agent proactively notifies: "You have a meeting in 15 minutes"
- Daily digest notification with agenda + tasks due

---

## 7. Location & Maps

### 7A. Gateway Endpoints

```
POST   /maps/geocode               â€“ address â†’ coordinates
POST   /maps/directions            â€“ routing between points
POST   /maps/places                â€“ search nearby places
GET    /maps/distance              â€“ distance/time between locations
```

### 7B. Provider Options

Google Maps API (already in GCP ecosystem, $200/mo free credit covers
personal use easily).

### 7C. Use Cases

- "How long will it take to get to my dentist appointment?"
- "Find a coffee shop near campus"
- "What's the best route to the airport for my 6am flight?"
- Agent enriches calendar events with travel time estimates

---

## 8. GitHub Integration

Since you're a developer and your projects live on GitHub.

### 8A. Gateway Endpoints

```
GET    /github/repos               â€“ list repos
GET    /github/repos/{repo}/issues â€“ list issues
POST   /github/repos/{repo}/issues â€“ create issue
PATCH  /github/repos/{repo}/issues/{num} â€“ update issue
GET    /github/repos/{repo}/pulls  â€“ list PRs
GET    /github/repos/{repo}/actions â€“ workflow run status
POST   /github/repos/{repo}/actions/{id}/dispatch â€“ trigger workflow
```

### 8B. Use Cases

- "Create an issue on api-gateway for the auth bug I found"
- "What's the status of my last deploy?"
- "List open PRs across my repos"
- "Trigger a deploy of the gateway"
- Agent creates issues from conversation context automatically
- KB ingests README, docs, and code from repos

### 8C. Auth

GitHub Personal Access Token (fine-grained), stored as gateway secret.

---

## 9. Spotify / Music Control

Lower priority but high quality-of-life.

### 9A. Gateway Endpoints

```
GET    /spotify/playing             â€“ currently playing track
POST   /spotify/play                â€“ play track/playlist/album
POST   /spotify/pause               â€“ pause playback
POST   /spotify/next                â€“ skip track
POST   /spotify/queue               â€“ add to queue
GET    /spotify/playlists           â€“ list playlists
```

### 9B. Use Cases

- "Play my focus playlist"
- "What song is this?"
- "Queue up some jazz"
- Especially valuable once voice interface exists (Phase 6)

### 9C. Auth

Spotify OAuth, separate from Google OAuth flow. Refresh token stored
in gateway secrets.

---

## 10. Smart Home (Future)

### 10A. Integration Options

| Platform | Protocol | Notes |
|----------|----------|-------|
| Home Assistant | REST API | If running HA, most flexible |
| Google Home | Google Home API | Limited but fits ecosystem |
| MQTT direct | MQTT | For custom devices |

### 10B. Gateway Endpoints

```
GET    /home/devices               â€“ list devices + state
POST   /home/devices/{id}/command  â€“ send command (on/off/set)
GET    /home/scenes                â€“ list scenes
POST   /home/scenes/{id}/activate  â€“ activate scene
```

### 10C. Use Cases

- "Turn off the lights"
- "Set the thermostat to 72"
- "Activate my bedtime scene"
- Agent-driven automations: "When I have an early meeting, set alarm + lights"

---

## 11. Agent Self-Management

Meta-capabilities for the agent to manage itself.

### 11A. Capabilities

```
GET    /agent/tools                â€“ list available tools + status
GET    /agent/health               â€“ system health across all services
GET    /agent/usage                â€“ token usage, API call counts, costs
POST   /agent/feedback             â€“ log user feedback on responses
GET    /agent/sessions/stats       â€“ conversation analytics
```

### 11B. Use Cases

- "How much have I spent on API calls this month?"
- "Which tools do I use most?"
- "Run a health check on all services"
- Agent surfaces its own issues: "KB sync hasn't run in 3 days"
- Self-monitoring for degraded performance

---

## 12. GCP Observability

Give the agent visibility into the infrastructure it runs on â€” pull logs,
check service health, and monitor metrics without leaving the conversation.

### 12A. Gateway Endpoints

```
GET    /gcp/services                        â€“ list Cloud Run services + current status/conditions
GET    /gcp/services/{name}/logs            â€“ pull recent Cloud Logging entries for a service
GET    /gcp/services/{name}/metrics         â€“ request count, latency p50/p99, error rate
GET    /gcp/services/{name}/revisions       â€“ list revisions + traffic splits
GET    /gcp/health                          â€“ aggregate health check across all Sazed services
```

### 12B. Implementation

Use Google Cloud Python client libraries from within the api-gateway Cloud Run
instance. The gateway's service account already has a GCP identity â€” just grant
it the necessary read-only IAM roles. No separate credentials needed.

**Required IAM roles on the gateway's service account:**

```
roles/logging.viewer       â€“ Cloud Logging read access
roles/run.viewer           â€“ Cloud Run service/revision metadata
roles/monitoring.viewer    â€“ Cloud Monitoring metrics
```

Grant via gcloud:

```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT \
  --member="serviceAccount:YOUR_GATEWAY_SA@YOUR_PROJECT.iam.gserviceaccount.com" \
  --role="roles/logging.viewer"

# Repeat for roles/run.viewer and roles/monitoring.viewer
```

**Dependencies to add to gateway requirements.txt:**

```
google-cloud-logging
google-cloud-run
google-cloud-monitoring
```

**Logs endpoint sketch:**

```python
from google.cloud import logging as gcp_logging

@router.get("/gcp/services/{service_name}/logs")
async def get_service_logs(service_name: str, limit: int = 50, severity: str = None):
    client = gcp_logging.Client()
    filter_str = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{service_name}"'
    )
    if severity:
        filter_str += f" severity>={severity}"

    entries = client.list_entries(
        filter_=filter_str,
        max_results=limit,
        order_by=gcp_logging.DESCENDING
    )
    return [
        {"timestamp": e.timestamp, "severity": e.severity, "message": e.payload}
        for e in entries
    ]
```

**Service list endpoint sketch:**

```python
from google.cloud import run_v2

@router.get("/gcp/services")
async def list_services():
    client = run_v2.ServicesClient()
    parent = f"projects/{PROJECT_ID}/locations/{REGION}"
    services = client.list_services(parent=parent)
    return [
        {
            "name": s.name.split("/")[-1],
            "uri": s.uri,
            "conditions": [c.type_ for c in s.conditions],
        }
        for s in services
    ]
```

### 12C. Agent Tools

```
get_gcp_services      â€“ list all Cloud Run services + health conditions
get_gcp_logs          â€“ pull recent logs for a named service, optionally filtered by severity
get_gcp_metrics       â€“ request/error/latency stats for a service over a time window
get_gcp_health        â€“ aggregate health across all Sazed services (api-gateway, knowledge-base, agent)
```

### 12D. Use Cases

- "Why is the knowledge base slow?" â†’ pull logs, surface errors
- "Is everything running?" â†’ aggregate health check
- "What's the error rate on the gateway today?" â†’ metrics query
- "Show me the last 20 error logs from the agent service" â†’ filtered log pull
- Agent proactively surfaces degraded services during health check tool calls
- Post-deploy sanity check: "Did the new revision deploy cleanly?"

### 12E. Notes

- No extra auth complexity â€” gateway service account is already trusted by GCP
- Keep log payloads summarized before returning to agent; raw stacktraces can
  be large. Truncate to ~500 chars and let agent request full entry by ID if needed.
- For metrics, Cloud Monitoring's `list_time_series` API returns data points â€”
  aggregate to a single summary value (avg, p99) before passing to the LLM.

---

## 13. File Ingest via Chat / MCP

Upload a file directly in the chat or MCP client and ingest it to the KB
and/or Drive without any manual pipeline step.

### 13A. Gateway Endpoints

```
POST   /kb/ingest/file             â€“ upload file bytes â†’ chunk + embed â†’ KB
POST   /storage/files/upload       â€“ upload file bytes â†’ Drive (with category/folder)
```

Both endpoints accept `multipart/form-data` with the file and metadata
(category, folder, title). The ingest endpoint reuses the existing KB
chunking pipeline â€” no new infrastructure.

### 13B. UI / MCP Integration

- **Chat UI:** file attachment input â†’ on send, POST to gateway before the
  message is processed. Agent receives confirmation + source ID in context.
- **MCP:** `ingest_file` tool accepts base64-encoded file content + metadata.
  Claude Desktop can call it directly when a file is dragged in.

### 13C. Use Cases

- Drag a PDF lecture note into chat â†’ "ingest this to KB under school"
- Upload a resume â†’ "save to Drive in /career and ingest to KB"
- Drop a text file â†’ agent auto-detects category, asks to confirm before ingesting
- Any file â†’ KB pipeline without touching the Drive UI

### 13D. Notes

- Auto-detect category from filename/content using a cheap Haiku call, then
  confirm with user before committing.
- Supported types mirror existing KB ingest: PDF, txt, md, docx, html.
- File size limit should match Cloud Run request limit (32MB default, configurable).

---

## 14. Web Bookmarking

Capture any URL â†’ fetch content â†’ ingest to KB. Works from iOS share sheet,
browser extension, or directly in chat.

### 14A. Gateway Endpoints

```
POST   /kb/ingest/url              â€“ fetch URL content â†’ chunk + embed â†’ KB
GET    /kb/bookmarks               â€“ list bookmarked URLs + metadata
DELETE /kb/bookmarks/{id}          â€“ remove bookmark + chunks
```

### 14B. Ingestion Flow

```
URL submitted
    â”‚
    â–¼
Gateway fetches page (trafilatura or readability for clean text extraction)
    â”‚
    â–¼
Chunks + embeds â†’ pgvector (category: "reference" or user-specified)
    â”‚
    â–¼
Bookmark record saved (url, title, ingested_at, chunk_ids)
```

Use `trafilatura` for extraction â€” better than raw BeautifulSoup for
article content, handles paywalls gracefully (returns what's publicly visible).

### 14C. Integration Points

- **iOS Share Sheet:** iOS Shortcut with URL input â†’ POST to `/kb/ingest/url`
- **Chat:** paste URL in message â†’ agent detects URL, offers to bookmark
- **MCP:** `bookmark_url` tool

### 14D. Use Cases

- Share an article from Safari â†’ auto-ingested to KB
- "Save this docs page" â†’ pasted URL â†’ ingested under "reference"
- "What did that article say about X?" â†’ KB search returns it
- Research sessions: bookmark 10 pages, query across all of them later

---

## 15. News Feed

Real-time news access, parameterized by topic. Primary driver for daily
digest and on-demand current events queries.

### 15A. Gateway Endpoints

```
GET    /news/headlines             â€“ top headlines (filterable by category/query)
GET    /news/search                â€“ search news articles by keyword
POST   /news/digest                â€“ generate a summarized digest from headlines
```

### 15B. Provider

NewsAPI (newsapi.org) â€” free tier is 100 req/day, plenty for personal use.
Returns title, description, source, URL, published_at. Paid tier ($449/mo)
not needed; free covers all personal use cases.

Fallback: Tavily with `search_depth: basic` and a news-focused query â€”
already integrated, zero additional cost.

### 15C. Agent Tools

```
get_news_headlines    â€“ top headlines, optional category (technology/science/sports/etc)
search_news           â€“ keyword search across recent articles
generate_news_digest  â€“ summarize top N headlines into a brief
```

### 15D. Use Cases

- Morning digest: agent pulls headlines, summarizes into 5-bullet brief
- "What's happening in AI today?" â†’ news search
- "Any news about Anthropic?" â†’ keyword search
- Proactive ping (section 18) triggers digest at configured time each morning
- "Catch me up on the news" â†’ digest on demand

---

## 16. Location Context (iOS Shortcuts)

Give the agent awareness of where you are without building a native app.

### 16A. Gateway Endpoints

```
POST   /context/location           â€“ update current location
GET    /context/location           â€“ get last known location + timestamp
```

### 16B. Implementation

iOS Shortcuts automation fires on arrive/leave for saved locations (home,
campus, gym, etc.) and POSTs to the gateway. Also supports a manual
"update location" shortcut for arbitrary locations.

Location stored in a lightweight `user_context` table in Postgres alongside
other ephemeral context (not KB â€” this is operational state, not knowledge).

```python
# user_context table
# key: "location"
# value: { "label": "Purdue campus", "lat": ..., "lng": ..., "updated_at": ... }
```

### 16C. Agent Integration

Location injected into system prompt alongside time-of-day:

```
Current context: Thursday 9:14 AM | Location: Purdue campus (updated 8:47 AM)
```

### 16D. Use Cases

- Agent knows you're on campus â†’ surfaces class-relevant tasks
- "Arriving home" trigger â†’ agent sends end-of-day recap notification
- "Leaving gym" â†’ agent logs workout prompt or water reminder
- Location-aware calendar enrichment: "you'll need to leave by 2:30 to make it"

### 16E. Notes

- Shortcuts automation requires the gateway URL to be accessible â€” already
  the case since api-gateway is on Cloud Run.
- Add a simple API key header for Shortcuts requests (not full OAuth â€” these
  are automated, not user-initiated browser flows).

---

## 17. Analytics & Self-Healing

Operational intelligence: track what the agent does, fix what breaks.

### 17A. Gateway Endpoints

```
GET    /analytics/tools            â€“ tool call frequency, success/failure rates
GET    /analytics/usage            â€“ token usage, cost estimates over time
GET    /analytics/errors           â€“ recent tool failures + error types
POST   /agent/retry                â€“ manual retry trigger for a failed tool call
```

### 17B. Tool Call Logging

Add a `tool_call_log` table to the agent DB:

```sql
CREATE TABLE tool_call_log (
    id          SERIAL PRIMARY KEY,
    session_id  UUID REFERENCES sessions(id),
    tool_name   TEXT NOT NULL,
    input       JSONB,
    output      JSONB,
    success     BOOLEAN NOT NULL,
    error_msg   TEXT,
    duration_ms INTEGER,
    called_at   TIMESTAMPTZ DEFAULT NOW()
);
```

### 17C. Self-Healing

Wrap all tool executor calls with tenancy retry logic (already planned).
Beyond retries, the agent should:

- Detect repeated failures on the same tool within a session and surface
  a warning rather than silently failing
- On gateway 5xx, attempt the fallback path if one exists (e.g. Tavily
  fallback for news if NewsAPI is down)
- Log all failures to `tool_call_log` regardless of retry outcome

### 17D. Use Cases

- "Which tools have failed most this week?" â†’ analytics query
- "How much have I spent on tokens this month?" â†’ usage endpoint
- Agent self-reports: "I've had 3 failed KB searches today, might be a
  connectivity issue"
- Post-deploy: verify tool success rates haven't degraded
- Identify underused tools as candidates for removal/simplification

---

## 18. Proactive Agent (Scheduled Pings)

The agent runs on a schedule, evaluates context, and decides whether to act
â€” without any user message.

### 18A. Gateway Endpoints

```
POST   /agent/think                â€“ trigger a proactive reasoning cycle
GET    /agent/think/history        â€“ log of past proactive actions taken
```

### 18B. How It Works

Cloud Scheduler fires `POST /agent/think` N times per day (configurable â€”
start with 3: morning, midday, evening). The endpoint:

1. Loads full context: current time, location, upcoming calendar events,
   overdue tasks, recent activity, stored goals, last proactive action timestamp
2. Sends to Sonnet with a system prompt focused on: "Given this context,
   is there anything genuinely useful to surface to the user right now?"
3. Agent responds with either `action: none` or a specific action
   (send notification, create a task reminder, flag something)
4. If action: fires Pushover notification or creates a task
5. Logs decision + reasoning to `think_log` table

### 18C. Noise Prevention

The hardest problem here is not being annoying. Guardrails:

- Minimum 2-hour gap between proactive notifications (enforced in DB)
- Agent must justify the notification with a specific reason tied to context
- "Should I say something?" is a explicit reasoning step before acting
- User can set quiet hours via a preference (stored in `agent_memory`)
- Start conservative: morning digest only, expand from there

### 18D. Use Cases

- 7 AM: "Good morning â€” you have a 10 AM lecture, 2 tasks due today, and
  Anthropic dropped a new model release overnight"
- Midday: detects an overdue task with a soft deadline â†’ sends a nudge
- Evening: "You mentioned wanting to review your budget this week â€”
  still on your list"
- Detects a meeting tomorrow with no prep notes â†’ suggests creating them
- Long-term goal check-in: "You set a goal to finish Sazed's UI by March â€”
  no commits to that repo in 5 days"

### 18E. Notes

- This is the feature that makes Sazed feel like an assistant rather than
  a tool. Low noise is critical to not training yourself to ignore it.
- Cloud Scheduler cron syntax: `0 7,12,18 * * *` for 7am/noon/6pm.
- The `/agent/think` endpoint should be auth-protected with a scheduler
  service account, not the user OAuth token.

---

## Priority Order

Roughly ordered by impact-to-effort ratio for daily usefulness:

```
âœ… Done:
  2. Task List Management           â€“ completes the task story
  3. Web Search                     â€“ breaks the closed-world limitation

Tier 1 (High impact, moderate effort):
  1. File & Document Management     â€“ unlocks agent as creator, not just reader
  13. File Ingest via Chat/MCP      â€“ daily friction removal for KB building
  14. Web Bookmarking               â€“ passive KB growth from browsing

Tier 2 (High impact, more effort):
  15. News Feed                     â€“ real-time awareness + digest foundation
  16. Location Context              â€“ contextual intelligence via Shortcuts
  18. Proactive Agent               â€“ transforms Sazed from reactive to proactive
  4. Contacts                       â€“ enables natural language for people
  5. Sheets Integration             â€“ structured personal data tracking
  6. GitHub Integration             â€“ dev workflow automation
  7. GCP Observability              â€“ infrastructure visibility

Tier 3 (Quality of life):
  17. Analytics & Self-Healing      â€“ reliability + operational visibility
  8. Notification Enhancements      â€“ proactive agent, reminders
  9. Agent Self-Management          â€“ cost tracking
  10. Location & Maps               â€“ travel intelligence

Tier 4 (Nice to have):
  11. Spotify                       â€“ voice-first experience enhancer
  12. Smart Home                    â€“ depends on hardware setup
```

---

## Cost Impact Estimate

| Expansion | Additional Cost |
|-----------|----------------|
| File/Doc/Task/KB writes | $0 (existing APIs) |
| Web Search (Tavily) | $0 (free tier) |
| Contacts | $0 (existing GCP project) |
| Sheets | $0 (existing GCP project) |
| GitHub | $0 (PAT, free tier) |
| Notifications (Cloud Scheduler) | $0 (free tier covers it) |
| Maps | $0 ($200/mo GCP credit) |
| Spotify | $0 (free API tier) |
| Smart Home | $0â€“5/mo (depends on platform) |
| GCP Observability | $0 (Logging/Monitoring free tier covers personal scale) |
| File Ingest via Chat | $0 (existing KB pipeline) |
| Web Bookmarking | $0 (trafilatura + existing KB pipeline) |
| News Feed | $0 (NewsAPI free tier) |
| Location Context | $0 (iOS Shortcuts + existing gateway) |
| Analytics & Self-Healing | $0 (Postgres log table + existing infra) |
| Proactive Agent | $0â€“2/mo (Cloud Scheduler free tier + ~3 Sonnet calls/day) |

Most expansions are **zero incremental cost** at personal scale. The
architecture pays for itself â€“ one gateway, one auth model, one DB.

---

## Notes

- Every expansion follows the same pattern: add gateway endpoints â†’
  add tool schemas to agent â†’ all consumers get it.
- No expansion requires architectural changes to the core system.
- Prioritize based on what you actually find yourself wanting the
  agent to do day-to-day. The list will reorder itself naturally.
- Some expansions (Sheets, Contacts) require adding OAuth scopes â€“
  do these together to minimize re-consent flows.