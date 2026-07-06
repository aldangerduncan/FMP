"""
Read-only MCP server exposing the FileMaker "IRD Subscribing Contacts" CRM
over Streamable HTTP, protected by a static bearer token.

Three tools:
  - find_contact   search contacts by name or email
  - get_dialogue   interaction history for a contact (paginated, small by default)
  - get_company    all contacts at a given company

Auth: a plain shared-secret bearer token, checked by a small ASGI middleware
in front of the MCP app. We deliberately don't use the MCP SDK's built-in
OAuth support (FastMCP's `auth=` / `token_verifier=`) — that machinery is
built for multi-tenant OAuth resource servers (issuer URLs, protected
resource metadata, client registration, etc.), which is overkill for a
single-user personal tool. A static bearer check is simpler to run and
reason about here.

Run with:
    python server.py
Configured via environment variables — see .env.example.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()  # must run before importing fm_client, which reads env at import time

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from fm_client import get_client

MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8420"))
# Public hostname(s) this server is reached through (e.g. via Caddy/nginx in
# front of it). The MCP SDK's DNS-rebinding protection validates the Host/
# Origin headers against this list — without it, every request coming
# through a reverse proxy gets rejected with "Invalid Host header", since
# the proxy's hostname won't match 127.0.0.1/localhost by default.
PUBLIC_HOSTNAMES = [h.strip() for h in os.environ.get("MCP_PUBLIC_HOSTNAMES", "").split(",") if h.strip()]

mcp = FastMCP(
    "filemaker-crm",
    instructions=(
        "Read-only access to the IRD / Prospector FileMaker CRM. "
        "Use find_contact to look up a person by name or email and get their "
        "Subscriber ID, then get_dialogue with that ID for interaction history. "
        "Use get_company to list everyone at a given company."
    ),
    host=HOST,
    port=PORT,
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1", "localhost", *PUBLIC_HOSTNAMES],
        allowed_origins=[f"https://{h}" for h in PUBLIC_HOSTNAMES] + ["http://127.0.0.1", "http://localhost"],
    ),
)


@mcp.tool()
async def find_contact(query: str) -> dict:
    """Look up a single CRM contact by email address or 'First Last' name.

    Returns contact details (id, name, email, phone, position, company,
    LinkedIn, account manager, client status, tier, sales rep) or an
    explicit not-found result. The returned `id` is the Subscriber ID
    needed by get_dialogue.
    """
    client = get_client()
    contact = await client.find_contact(query)
    if contact is None:
        return {"found": False, "query": query}
    return {"found": True, "contact": contact}


@mcp.tool()
async def get_dialogue(subscriber_id: str, limit: int = 10) -> dict:
    """Get interaction/dialogue history for a contact, newest first.

    `subscriber_id` is the `id` field returned by find_contact.
    `limit` defaults to 10 — raise it explicitly if you need more history,
    rather than pulling everything by default.
    """
    client = get_client()
    records = await client.get_dialogue(subscriber_id, limit=limit)
    return {"subscriber_id": subscriber_id, "count": len(records), "dialogue": records}


@mcp.tool()
async def get_company(company_name: str, limit: int = 50) -> dict:
    """List all contacts at a given company (matched against the company
    field on their contact record — there is no separate company table
    exposed by this database, so this is a filtered contact search).
    """
    client = get_client()
    contacts = await client.get_company(company_name, limit=limit)
    return {"company_query": company_name, "count": len(contacts), "contacts": contacts}


class BearerAuthMiddleware:
    """Rejects any HTTP request that doesn't present the configured token,
    either as `Authorization: Bearer <token>` (for curl / testing) or as a
    `?token=<token>` query parameter (for Claude's custom connector UI,
    which as of 2026-07 only supports OAuth or no-auth for remote MCP
    servers — there's no field to paste a static bearer token into. Putting
    the token in the URL itself is the pragmatic workaround: paste
    `https://<host>/mcp?token=<token>` as the connector URL and nothing
    else needs configuring). Applied in front of the whole MCP ASGI app."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        auth_header = request.headers.get("authorization", "")
        query_token = request.query_params.get("token", "")
        expected = f"Bearer {self.token}"

        authorized = self.token and (auth_header == expected or query_token == self.token)

        if not authorized:
            response: Response = JSONResponse(
                {"error": "unauthorized"}, status_code=401
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def build_app() -> Starlette:
    inner_app = mcp.streamable_http_app()
    if not MCP_BEARER_TOKEN:
        raise RuntimeError(
            "MCP_BEARER_TOKEN is not set. Copy .env.example to .env and set a token."
        )
    return BearerAuthMiddleware(inner_app, MCP_BEARER_TOKEN)  # type: ignore[return-value]


app = build_app()

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
