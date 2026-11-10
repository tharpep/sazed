from unittest.mock import AsyncMock, MagicMock, patch

from app.backlog import store
from app.board import store as board_store
from app.routers.backlog import ItemUpdate


def test_parked_is_off_the_board_and_out_of_the_open_views():
    """A parked card leaves the inbox and next without being dismissed or done."""
    assert "parked" in board_store.OFF_BOARD
    assert "parked" not in store.OPEN
    assert ItemUpdate(status="parked").status == "parked"


async def test_parked_view_lists_only_parked_items():
    """GET /backlog?view=parked asks the database for parked items and nothing else."""
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    with patch.object(store, "get_pool", return_value=pool):
        await store.list_items(None, "parked")
    assert pool.fetch.call_args.args[2] == ["parked"]


async def test_mark_stale_never_touches_parked_items():
    """Parked ideas are kept on purpose, so the weekly stale sweep only ages new ones."""
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 0")
    with patch.object(store, "get_pool", return_value=pool):
        await store.mark_stale(30)
    sql = pool.execute.call_args.args[0]
    assert "status = 'new'" in sql
    assert "parked" not in sql
