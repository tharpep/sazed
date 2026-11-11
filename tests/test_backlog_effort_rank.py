"""Effort and impact change on an existing card; a new card can take a rank on creation."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.routers import backlog as router

NEW_ID = uuid.uuid4()


async def _patch(body):
    with patch.object(router.store, "update_item", AsyncMock(return_value={"id": "x"})) as upd:
        await router.update_item(uuid.uuid4(), body)
    return upd.call_args.kwargs


async def test_patch_sets_effort_and_impact():
    assert await _patch(router.ItemUpdate(effort="M", impact="high")) == {
        "effort": "M",
        "impact": "high",
    }


@pytest.mark.parametrize("bad", [{"effort": "XL"}, {"impact": "huge"}])
def test_patch_refuses_values_the_create_path_refuses(bad):
    with pytest.raises(ValidationError):
        router.ItemUpdate(**bad)


def _create_patches(created):
    return (
        patch.object(router.service, "add_item", AsyncMock(return_value=(created, None))),
        patch.object(router.store, "find_duplicate", AsyncMock(return_value=None)),
        patch.object(router.store, "move_item", AsyncMock(return_value=created | {"rank": 2})),
    )


async def test_create_with_rank_moves_the_new_card_to_that_position():
    created = {"id": NEW_ID, "title": "A card", "rank": None}
    add, dup, move = _create_patches(created)
    with add as add_item, dup, move as move_item:
        item = await router.create_item(router.ItemCreate(type="planned", title="A card", rank=2))
    assert "rank" not in add_item.call_args.kwargs
    move_item.assert_awaited_once_with(NEW_ID, 2)
    assert item["rank"] == 2


async def test_create_without_rank_leaves_the_card_unranked():
    created = {"id": NEW_ID, "title": "A card", "rank": None}
    add, dup, move = _create_patches(created)
    with add, dup, move as move_item:
        item = await router.create_item(router.ItemCreate(type="planned", title="A card"))
    move_item.assert_not_awaited()
    assert item["rank"] is None
