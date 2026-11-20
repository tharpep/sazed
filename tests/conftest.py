"""Tests run without Postgres, so the guard proposal's advisory lock is a no-op here."""

from contextlib import asynccontextmanager

import pytest

from app.learning import store

REAL_CLASS_LOCK = store.class_lock


@asynccontextmanager
async def _no_lock(cls):
    yield


@pytest.fixture(autouse=True)
def no_guard_lock(monkeypatch):
    monkeypatch.setattr(store, "class_lock", _no_lock)
