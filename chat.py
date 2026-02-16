"""Minimal CLI for chatting with Sazed locally."""

import httpx

BASE = "http://localhost:8000"
session_id = None

print("Sazed — ctrl+c to exit")
print(f"Connecting to {BASE}\n")

while True:
    try:
        msg = input("You: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nBye.")
        break

    if not msg:
        continue

    try:
        resp = httpx.post(
            f"{BASE}/chat",
            json={"session_id": session_id, "message": msg},
            timeout=60.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(f"[error {e.response.status_code}] {e.response.text}\n")
        continue
    except httpx.RequestError as e:
        print(f"[connection error] {e}\n")
        continue

    data = resp.json()
    session_id = data["session_id"]
    print(f"\nSazed: {data['response']}")
    print(f"[session: {session_id[:8]}...]\n")
