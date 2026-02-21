# Sazed Development Roadmap
---

## Next (Tier 1 — High Impact)

### 1. Improved Indexing on KB
- Build a fast index system on top of RAG DB (synced with Drive)
- Agent chooses between: direct index lookup (faster, fewer tokens) vs. full RAG search (more thorough)
- Use cases: quick context retrieval, cost optimization, hybrid retrieval strategy
- Benefits: faster access for hot data, reduced token usage, maintains RAG capability as fallback

### 2. Google Contacts
- Resolve names to email addresses in conversations
- Use cases: "Email John about X", "What's Sarah's phone number?"
- Gateway endpoints: search, get, create, update contact

### 3. Google Sheets Integration
- Create and manipulate spreadsheets directly
- Use cases: expense tracking, budget, habit tracking, shopping lists
- Gateway endpoints: create sheet, read/write ranges, append rows

### 4. File Copy Endpoint
- `POST /storage/files/{id}/copy` — duplicate a file
- Needed for common "save a copy" workflows

### 5. KB Write Operations
- `DELETE /kb/sources/{id}` — remove source + chunks
- `PUT /kb/chunks/{id}` — update and re-embed chunks
- `POST /kb/ingest/text` — ingest raw text directly (no file backing)

### 6. Gmail Advanced
- Auto-process, label, archive emails
- Draft smart reply suggestions
- Extract and ingest attachments to KB
- Use cases: "Summarize my unread emails", "Find the contract from Sarah"
- Requires: `gmail.modify` scope

### 7. Google Calendar Advanced
- Smart time-blocking based on task deadlines
- Auto-adjust events with travel time estimates
- Suggest optimal meeting times
- Use cases: auto-reserve deep work blocks, integrate with Maps for travel

### 8. Google Drive Comments & Suggestions
- Agent comments on documents with questions/ideas
- Track document feedback loops
- Use cases: collaborative notes, document review workflows

### 9. Readwise Reader Integration
- Search saved articles and highlights
- Ingest highlights to KB
- Use cases: research workflows, pull insights from saved articles

### 10. Anthropic Files API
- Upload large files (PDFs, code repos, datasets) directly to Claude
- No chunking needed — agent reasons over full files
- Use cases: analyze long documents, understand large codebases

### 11. Documentation API
- Fetch and parse API docs (OpenAPI/Swagger specs)
- Agent can look up library/service docs on-demand
- Use cases: "How does the X library work?", auto-generate integration examples

### 12. Weather API
- Daily weather integrated into context
- Use cases: activity planning, commute suggestions
- Note: Google Cloud doesn't have native weather — use Open-Meteo (free) or WeatherAPI

### 13. Markdown File Management
- Read/write/update markdown files for course notes, project specs, etc.
- Enables KB sync from markdown in Drive
- Use cases: keep Purdue notes, project documentation synchronized with KB

### 14. Twitter/X API (Read-Only)
- Search tweets and view timelines
- Track mentions/discussions
- Use cases: stay aware of tech discussions, find relevant takes on topics

### 15. Social Feed APIs (Read-Only)
- Other read-only feeds: Hacker News, Reddit, Product Hunt
- Agent can pull relevant posts when you ask about a topic
- Extends beyond Twitter to broader tech awareness

---

## Future (Tier 2 — Medium Priority)

### 16. News Feed
- Real-time headlines + search
- Use cases: morning digest, current events queries
- Provider: NewsAPI (free tier)

### 17. Location Context
- iOS Shortcuts integration to track current location
- Injected into system prompt for contextual awareness
- Use cases: campus-aware tasks, location-triggered actions

### 18. Notification Enhancements
- Schedule notifications for specific times
- Recurring reminders
- Gateway endpoints: schedule, list, cancel scheduled notifications

### 19. Proactive Agent (Scheduled Pings)
- Agent runs on a schedule (morning, midday, evening)
- Evaluates context and decides what to surface
- Use cases: morning digest, task reminders, goal check-ins
- Cloud Scheduler + `/agent/think` endpoint

### 20. GCP Observability
- Agent can view Cloud Run service health, logs, and metrics
- Use cases: "Is the knowledge base running?", "Show me recent errors"
- Gateway endpoints: list services, get logs, get metrics, aggregate health

### 21. Web Bookmarking
- `POST /kb/ingest/url` — save any URL to KB
- iOS Share Sheet + chat integration
- Use cases: research sessions, archive articles

### 22. Analytics & Self-Healing
- Tool call logging and success/failure tracking
- Cost and token usage stats
- Self-healing retries and fallbacks
- Gateway endpoints: tool analytics, usage, error logs, manual retry

---

## Later (Tier 3 — Nice to Have)

- **Spotify Integration** — play music, queue tracks, control playback
- **Smart Home** — control lights, thermostat, scenes (depends on hardware)
- **Agent Self-Management** — expose tool inventory, system health, usage metrics
- **Maps Integration** — geocoding, directions, distance calculations

---

## Cost Impact
Most expansions are **zero incremental cost** at personal scale. Gmail, Calendar, Drive, Twitter, and markdown files use existing APIs. Readwise requires subscription. Anthropic Files API is free. Weather via Open-Meteo is free. Documentation API parsing is free (just parsing public specs).
