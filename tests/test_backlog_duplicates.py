"""Filing a card: only open items block it, the 409 names the match, allow_similar overrides."""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.backlog import store
from app.routers import backlog

TITLE = "Rules as data for the router"
BODY = {"type": "idea", "title": TITLE}


def _row(status: str, title: str = TITLE) -> dict:
    return {"id": uuid.uuid4(), "title": title, "status": status}


class FakePool:
    """Answers the duplicate query by honouring the status filter it is given."""

    def __init__(self, rows):
        self.rows = rows
        self.inserted = None

    async def fetch(self, sql, statuses=None):
        return [r for r in self.rows if statuses is None or r["status"] in statuses]

    async def fetchrow(self, sql, *args):
        self.inserted = args
        return {"id": uuid.uuid4(), "title": args[1], "status": "new", "note": args[-1]}


@pytest.fixture
def make_client(monkeypatch):
    def make(rows):
        pool = FakePool(rows)
        app = FastAPI()
        app.include_router(backlog.router, prefix="/backlog")
        monkeypatch.setattr(store, "get_pool", lambda: pool)
        return TestClient(app), pool

    return make


@pytest.mark.parametrize("status", ["done", "dismissed", "stale", "approved"])
def test_closed_items_do_not_block_a_new_card(make_client, status):
    client, pool = make_client([_row(status)])
    assert client.post("/backlog", json=BODY).status_code == 201
    assert pool.inserted is not None


def test_open_near_duplicate_blocks_and_names_the_match(make_client):
    existing = _row("shortlisted", "Rules as data for router")
    client, pool = make_client([existing])
    resp = client.post("/backlog", json=BODY)
    assert resp.status_code == 409
    body = resp.json()
    assert "Already in the backlog: Rules as data for router" in body["detail"]
    assert body["existing"] == {
        "id": str(existing["id"]),
        "title": existing["title"],
        "status": "shortlisted",
    }
    assert pool.inserted is None


def test_parked_item_is_reported_but_does_not_block(make_client):
    parked = _row("parked", "Rules as data for router")
    client, _ = make_client([parked])
    resp = client.post("/backlog", json=BODY)
    assert resp.status_code == 201
    assert resp.json()["similar_parked"]["id"] == str(parked["id"])


def test_allow_similar_with_a_reason_files_the_card_and_records_it(make_client):
    client, pool = make_client([_row("new")])
    resp = client.post(
        "/backlog", json=BODY | {"allow_similar": True, "similar_reason": "Different layer"}
    )
    assert resp.status_code == 201
    assert pool.inserted[-1] == "Filed despite a similar item: Different layer"
    assert "similar_parked" not in resp.json()


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_allow_similar_without_a_reason_is_rejected(make_client, reason):
    client, pool = make_client([_row("new")])
    resp = client.post("/backlog", json=BODY | {"allow_similar": True, "similar_reason": reason})
    assert resp.status_code == 422
    assert pool.inserted is None


def test_item_create_requires_reason_only_when_overriding():
    backlog.ItemCreate(type="idea", title=TITLE)
    with pytest.raises(ValidationError):
        backlog.ItemCreate(type="idea", title=TITLE, allow_similar=True)
