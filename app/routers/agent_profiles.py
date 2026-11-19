"""Agent profiles for dispatch and worker jobs (migrations 028 and 035), and MCP servers (029)."""

import re
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from app.board import mcp_servers, profiles
from app.dependencies import ShapeV2

router = APIRouter()

AUTHOR = r"^(pryce|agent:[\w.:-]{1,60}|dispatch:[\w.:-]{1,60})$"
Name = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{1,30}$")]
ServerName = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{1,30}$")]
SERVER_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
TOOL = re.compile(r"^([A-Za-z0-9_-]{1,64}|\*)$")
ENV_REF = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")


class ProfileIn(BaseModel):
    """engine, capability and max_tool_calls are optional so a client from before migration 035
    (read_only, no capability) can still save; profiles.save fills them from the row."""

    engine: Literal["claude", "agy"] | None = None
    capability: Literal["research", "review", "change", "browse"] | None = None
    model: str = Field(..., pattern=r"^[\w.\-\[\]]{1,100}$")
    effort: str | None = Field(None, pattern=r"^(low|medium|high|xhigh|max)$")
    budget_usd: float | None = Field(None, ge=0.05, le=50)
    timeout_s: int = Field(..., ge=60, le=7200)
    max_tool_calls: int | None = Field(None, ge=1, le=1000)
    read_only: bool | None = None
    evidence_required: bool = False
    description: str = Field("", max_length=300)
    mcp: dict[str, list[str]] = Field(default_factory=dict)
    author: str = Field(..., pattern=AUTHOR)
    reason: str = Field(..., min_length=3, max_length=300)
    expected_version: int | None = Field(None, ge=1)

    @field_validator("mcp")
    @classmethod
    def _grants(cls, mcp: dict[str, list[str]]) -> dict[str, list[str]]:
        for server, tools in mcp.items():
            if not SERVER_NAME.match(server):
                raise ValueError(f"bad server name {server!r}")
            if not tools or not all(TOOL.match(t) for t in tools):
                raise ValueError(f"bad tool names for {server!r}")
            if "*" in tools and tools != ["*"]:
                raise ValueError(f"'*' stands alone for {server!r}")
        return mcp


class ServerIn(BaseModel):
    transport: str = Field(..., pattern=r"^(http|stdio)$")
    url: str | None = Field(None, max_length=500)
    command: str | None = Field(None, min_length=1, max_length=300)
    args: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    agent_safe: bool
    description: str = Field("", max_length=300)
    author: str = Field(..., pattern=AUTHOR)
    reason: str = Field(..., min_length=3, max_length=300)
    expected_version: int | None = Field(None, ge=1)

    @field_validator("headers")
    @classmethod
    def _env_refs(cls, headers: dict[str, str]) -> dict[str, str]:
        if not all(ENV_REF.match(v) for v in headers.values()):
            raise ValueError("header values must be ${ENV_VAR} references")
        return headers

    @model_validator(mode="after")
    def _transport_fields(self):
        if self.transport == "http":
            if not self.url or not self.url.startswith(("https://", "http://")):
                raise ValueError("http servers need an http(s) url")
            self.command, self.args = None, []
        else:
            if not self.command:
                raise ValueError("stdio servers need a command")
            self.url, self.headers = None, {}
        return self


def _http(e: profiles.ProfileError) -> HTTPException:
    return HTTPException(e.status, str(e))


@router.get("/profiles")
async def list_profiles(v2: ShapeV2):
    rows = await profiles.list_profiles()
    return [profiles.shaped(p, v2) for p in rows if v2 or profiles.old_client_can_show(p)]


@router.get("/profiles/{name}")
async def get_profile(name: Name, v2: ShapeV2):
    try:
        found = await profiles.get(name)
    except profiles.ProfileError as e:
        raise _http(e) from e
    return {**profiles.shaped(found, v2), "history": found["history"]}


@router.put("/profiles/{name}")
async def put_profile(name: Name, body: ProfileIn, v2: ShapeV2):
    unset = {"max_tool_calls"} - body.model_fields_set
    try:
        saved = await profiles.save(name, **body.model_dump(exclude=unset))
    except profiles.ProfileError as e:
        raise _http(e) from e
    return profiles.shaped(saved, v2) if saved else saved


@router.delete("/profiles/{name}")
async def delete_profile(
    name: Name,
    author: str = Query(..., pattern=AUTHOR),
    reason: str = Query(..., min_length=3, max_length=300),
):
    try:
        return await profiles.delete(name, author=author, reason=reason)
    except profiles.ProfileError as e:
        raise _http(e) from e


def _server_http(e: mcp_servers.ServerError) -> HTTPException:
    return HTTPException(e.status, str(e))


@router.get("/mcp-servers")
async def list_mcp_servers():
    return await mcp_servers.list_servers()


@router.get("/mcp-servers/{name}")
async def get_mcp_server(name: ServerName):
    try:
        return await mcp_servers.get(name)
    except mcp_servers.ServerError as e:
        raise _server_http(e) from e


@router.put("/mcp-servers/{name}")
async def put_mcp_server(name: ServerName, body: ServerIn):
    try:
        return await mcp_servers.save(name, **body.model_dump())
    except mcp_servers.ServerError as e:
        raise _server_http(e) from e


@router.delete("/mcp-servers/{name}")
async def delete_mcp_server(
    name: ServerName,
    author: str = Query(..., pattern=AUTHOR),
    reason: str = Query(..., min_length=3, max_length=300),
):
    try:
        return await mcp_servers.delete(name, author=author, reason=reason)
    except mcp_servers.ServerError as e:
        raise _server_http(e) from e
