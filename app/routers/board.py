"""The board (plans/13): every card in one read, for the console and the gateway MCP."""

from fastapi import APIRouter, HTTPException

from app.board import service

router = APIRouter()


@router.get("")
async def get_board():
    return service.board(await service.cards())


@router.get("/{card_id}")
async def get_card(card_id: str):
    card = await service.card(card_id)
    if card is None:
        raise HTTPException(404, "Card not found")
    return card
