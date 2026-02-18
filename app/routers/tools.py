"""Tools registry endpoint — exposes the agent's available tools as JSON."""

from fastapi import APIRouter
from pydantic import BaseModel

from app.agent.tools import TOOLS, ToolDef

router = APIRouter()


class ToolParameter(BaseModel):
    name: str
    type: str
    description: str
    required: bool


class ToolInfo(BaseModel):
    name: str
    description: str
    category: str
    method: str
    endpoint: str
    parameters: list[ToolParameter]


def _category(tool: ToolDef) -> str:
    if tool.method == "INTERNAL":
        return "memory"
    prefix = tool.endpoint.lstrip("/").split("/")[0]
    return prefix or "other"


def _parameters(tool: ToolDef) -> list[ToolParameter]:
    props = tool.input_schema.get("properties", {})
    required = set(tool.input_schema.get("required", []))
    return [
        ToolParameter(
            name=k,
            type=v.get("type", "string"),
            description=v.get("description", ""),
            required=k in required,
        )
        for k, v in props.items()
    ]


@router.get("", response_model=list[ToolInfo])
def list_tools():
    """Return all tools available to the agent, grouped with parameter details."""
    return [
        ToolInfo(
            name=t.name,
            description=t.description,
            category=_category(t),
            method=t.method,
            endpoint=t.endpoint,
            parameters=_parameters(t),
        )
        for t in TOOLS
    ]
