# Sazed Development Roadmap
---

## Next (Tier 1 — High Impact)

### 1. Async Tool Execution
- Parallelize tool calls within a single agent turn using `asyncio.gather`
- Claude regularly batches multiple independent `tool_use` blocks in one response (e.g., calendar + tasks + email simultaneously)
- Currently these execute sequentially — parallelizing cuts multi-tool turn latency by 3-4x
- Change is contained to `execute_tool` dispatch in `loop.py`

### 2. Memory Relevance Filtering
- Instead of injecting all memory facts into every system prompt, retrieve only facts relevant to the current conversation/query
- As fact count grows over months, the all-or-nothing approach becomes token-heavy and noisy
- Options: embedding similarity against the user message, or topic tagging on facts at upsert time

### 3. Memory Cleanup & Expiration
- Facts currently live forever with no expiration or review
- Add TTL or "last confirmed" timestamps to facts; flag stale facts for review
- Agent-initiated fact review: periodically surface facts that may be outdated ("You mentioned project X in 2024 — is this still active?")
- Manual review endpoint: expose fact list with confidence + source + age so facts can be pruned

### 4. Conversation Search & Naming
- Sessions are UUID-only; no way to find a past conversation by topic
- Auto-generate a short title for each session after it closes (Haiku, 5-7 words)
- Add full-text search over session summaries and titles
- DB: add `title` column to `sessions`; search endpoint in `conversations.py`

### 5. Weekly Portfolio Update Automation
- Scheduled job (Cloud Scheduler or cron) that runs sazed against a curated system prompt weekly
- Prompt instructs the agent to: review recent GitHub activity (PRs merged, issues closed, repos updated), cross-reference with projects in KB, and generate or update portfolio content accordingly
- Output: draft PR to portfolio site repo, or write a Drive doc summarizing changes for manual review
- Requires: curated prompt stored in config or Drive, portfolio site GitHub repo accessible via existing `github` tools
- Automations endpoint: `POST /agent/run` with `prompt` + `session_id` body (headless, no user in the loop)

### 6. Google Contacts
- Resolve names to email addresses in conversations
- Use cases: "Email John about X", "What's Sarah's phone number?"
- Gateway endpoints: search, get, create, update contact

### 7. KB Write Operations (remaining)
- `PUT /kb/chunks/{id}` — update and re-embed a specific chunk
- `POST /kb/ingest/text` — ingest raw text directly without a file backing
- Note: `delete_kb_source` and `sync_kb` are already done

### 8. Gmail Advanced
- Auto-process, label, archive emails
- Draft smart reply suggestions
- Extract and ingest attachments to KB
- Use cases: "Summarize my unread emails", "Find the contract from Sarah"
- Requires: `gmail.modify` scope

### 9. Google Calendar Advanced
- Smart time-blocking based on task deadlines
- Suggest optimal meeting times given availability
- Use cases: auto-reserve deep work blocks

### 10. Improved Indexing on KB
- Build a fast index system on top of RAG DB (synced with Drive)
- Agent chooses between: direct index lookup (faster, fewer tokens) vs. full RAG search (more thorough)
- Benefits: faster access for hot data, reduced token usage

### 11. Readwise Reader Integration
- Search saved articles and highlights
- Ingest highlights to KB
- Use cases: research workflows, pull insights from saved articles

### 12. Anthropic Files API
- Upload large files (PDFs, code repos, datasets) directly to Claude
- No chunking needed — agent reasons over full files
- Use cases: analyze long documents, understand large codebases

### 13. Weather API
- Daily weather integrated into context
- Use cases: activity planning, commute suggestions
- Provider: Open-Meteo (free) or WeatherAPI

### 14. Twitter/X API (Read-Only)
- Search tweets and view timelines
- Use cases: stay aware of tech discussions, find relevant takes on topics

### 15. Social Feed APIs (Read-Only)
- Other read-only feeds: Hacker News, Reddit, Product Hunt
- Agent can pull relevant posts when asked about a topic

---

## Future (Tier 2 — Medium Priority)

### 16. Feedback & Correction Loop
- After each session, surface extracted facts for review before committing them permanently
- In-chat correction: "That's wrong, forget that" triggers immediate fact deletion or confidence downgrade
- Response rating: thumbs up/down logged per response, used to identify patterns in bad outputs
- Bad fact report: tool or endpoint to flag a specific memory key as incorrect

### 17. Session KB Ingestion Cleanup
- Every conversation currently generates a Drive markdown file and a KB entry indefinitely
- Add retention policy: archive or delete session files older than N days
- Deduplication: detect near-identical session summaries (light topic days) and merge or skip
- Tag session KB entries distinctly so they can be filtered out of search results when searching "real" knowledge documents
- Review endpoint: list all session-derived KB entries with option to delete

### 18. Documentation API
- Fetch and parse API docs (OpenAPI/Swagger specs)
- Agent can look up library/service docs on-demand
- Use cases: "How does X library work?", auto-generate integration examples

### 19. News Feed
- Real-time headlines + search
- Provider: NewsAPI (free tier)

### 20. Location Context
- iOS Shortcuts integration to track current location
- Injected into system prompt for contextual awareness

### 21. Notification Enhancements
- Schedule notifications for specific times
- Recurring reminders
- Gateway endpoints: schedule, list, cancel scheduled notifications

### 22. Proactive Agent (Scheduled Pings)
- Agent runs on a schedule (morning, midday, evening)
- Evaluates context and decides what to surface
- Cloud Scheduler + `/agent/think` endpoint
- Shares infrastructure with the Portfolio Update automation (#5)

### 23. GCP Observability
- Agent can view Cloud Run service health, logs, and metrics
- Use cases: "Is the knowledge base running?", "Show me recent errors"

### 24. Web Bookmarking
- `POST /kb/ingest/url` — save any URL to KB
- iOS Share Sheet + chat integration

### 25. Google Drive Comments & Suggestions
- Agent comments on documents with questions/ideas
- Use cases: collaborative notes, document review workflows

### 26. Analytics & Self-Healing
- Tool call logging and success/failure tracking
- Cost and token usage stats
- Self-healing retries and fallbacks

---

## Later (Tier 3 — Nice to Have)

- **Spotify Integration** — play music, queue tracks, control playback
- **Smart Home** — control lights, thermostat, scenes (depends on hardware)
- **Maps Integration** — geocoding, directions, distance calculations

---

## Cost Impact
Most expansions are **zero incremental cost** at personal scale. Gmail, Calendar, Drive, Twitter, and markdown files use existing APIs. Readwise requires subscription. Anthropic Files API is free. Weather via Open-Meteo is free. Documentation API parsing is free. Web search (Tavily) has a free tier.
