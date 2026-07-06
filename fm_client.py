"""
FileMaker Data API client for the "IRD Subscribing Contacts" database.

Read-only. Talks to two layouts only:
  - "Data Entry Screen"     (contacts, incl. related SUBCO/company lookup fields)
  - "Subscriber Dialogues"  (interaction history)

There is no standalone Companies layout in this database (verified against the
live API 2026-07-06) — company-level data only exists as lookup fields on the
Contact record ("Company Station", and "subct_SUBCO by name::*" fields pulled
from the related SUBCO table). So `get_company` below is implemented as a
filtered contact search, not a separate table query.

Credentials come from environment variables (see .env.example) so the same
code runs locally or on the VPS without touching files:
  FM_HOST      - default https://fms14.filemakerstudio.com.au
  FM_DATABASE  - default "IRD Subscribing Contacts"
  FM_USER      - FileMaker username
  FM_PASSWORD  - FileMaker password

Session token is cached in memory and transparently refreshed on expiry
(FileMaker error codes 952/951), matching the behaviour of the existing
get_token.sh / pull_dialogue.sh scripts in MeetingPrepTool.
"""

from __future__ import annotations

import asyncio
import base64
import os
import urllib.parse
from typing import Any

import httpx

FM_HOST = os.environ.get("FM_HOST", "https://fms14.filemakerstudio.com.au")
FM_DATABASE = os.environ.get("FM_DATABASE", "IRD Subscribing Contacts")
FM_USER = os.environ.get("FM_USER", "")
FM_PASSWORD = os.environ.get("FM_PASSWORD", "")

LAYOUT_CONTACTS = "Data Entry Screen"
LAYOUT_DIALOGUES = "Subscriber Dialogues"

_ENC_DB = urllib.parse.quote(FM_DATABASE)

# --- Field projections -----------------------------------------------------
# We deliberately return a small, named subset of fields instead of FileMaker's
# raw fieldData (100+ fields per record) so tool responses stay compact.

CONTACT_PROJECTION = {
    "id": "ID",
    "first_name": "First Name",
    "surname": "Surname",
    "email": "Email",
    "phone": "Phone",
    "mobile": "Mobile",
    "position": "Position",
    "linkedin": "LinkedIn",
    "active": "Active",
    "account_manager": "subct_SUBCO by name::Account Manager",
    "client_status": "subct_SUBCO by name::Client Status",
    "tier": "subct_SUBCO by name::Tier",
    "sales_rep": "subct_SUBCO by name::Sales Rep",
}

DIALOGUE_PROJECTION = {
    "date": "Contact Date",
    "account_manager": "Account Manager",
    "contact_method": "Contact Method",
    "contact_success": "Contact Success",
    "note": "Dialogue",
    "meeting_completed": "Meeting Completed",
    "action_item": "Action_Item",
}


def _project(field_data: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    out = {key: field_data.get(source) for key, source in mapping.items()}
    return out


def _project_company(field_data: dict[str, Any]) -> str:
    return (
        field_data.get("Company Station")
        or field_data.get("Company")
        or "Unknown"
    )


class FileMakerError(RuntimeError):
    """Raised for FileMaker Data API errors that aren't a plain 'no records found'."""


class FileMakerClient:
    """Holds one cached session token, refreshing it as needed. Not thread-safe
    across processes — designed for a single long-running server instance."""

    def __init__(self) -> None:
        if not FM_USER or not FM_PASSWORD:
            raise RuntimeError(
                "FM_USER / FM_PASSWORD are not set. Copy .env.example to .env "
                "and fill in the shared FileMaker login."
            )
        self._token: str | None = None
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=20.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self) -> str:
        creds = base64.b64encode(f"{FM_USER}:{FM_PASSWORD}".encode()).decode()
        url = f"{FM_HOST}/fmi/data/v1/databases/{_ENC_DB}/sessions"
        resp = await self._client.post(
            url,
            json={},
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {creds}",
            },
        )
        data = resp.json()
        messages = data.get("messages", [{}])
        if messages[0].get("code") != "0":
            raise FileMakerError(f"FileMaker login failed: {messages}")
        token = data["response"]["token"]
        self._token = token
        return token

    async def _get_token(self) -> str:
        if self._token is None:
            async with self._lock:
                if self._token is None:
                    await self._login()
        return self._token  # type: ignore[return-value]

    async def _find(
        self,
        layout: str,
        query: list[dict[str, Any]],
        limit: int = 50,
        sort: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """POST to a layout's _find endpoint. Returns the raw list of
        {fieldData: {...}} records (empty list if no records found).
        Automatically refreshes the session token once on expiry."""
        enc_layout = urllib.parse.quote(layout)
        url = f"{FM_HOST}/fmi/data/v1/databases/{_ENC_DB}/layouts/{enc_layout}/_find"
        body: dict[str, Any] = {"query": query, "limit": limit}
        if sort:
            body["sort"] = sort

        token = await self._get_token()
        resp = await self._client.post(
            url,
            json=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        data = resp.json()
        code = data.get("messages", [{}])[0].get("code")

        if code in ("952", "951"):
            # Session expired — re-login once and retry.
            async with self._lock:
                self._token = None
            token = await self._get_token()
            resp = await self._client.post(
                url,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
            data = resp.json()
            code = data.get("messages", [{}])[0].get("code")

        if code == "401":
            # FileMaker's "no records match the request" — not an error for us.
            return []
        if code != "0":
            raise FileMakerError(f"FileMaker query failed ({layout}): {data.get('messages')}")

        return [rec["fieldData"] for rec in data.get("response", {}).get("data", [])]

    # --- Public, read-only operations --------------------------------------

    async def find_contact(self, query: str) -> dict[str, Any] | None:
        """Look up a single contact by email or 'First Last' name."""
        query = query.strip()
        if "@" in query:
            records = await self._find(LAYOUT_CONTACTS, [{"Email": f"=={query}"}], limit=1)
            if not records:
                records = await self._find(LAYOUT_CONTACTS, [{"Email": f"*{query}*"}], limit=1)
        else:
            parts = query.split(" ", 1)
            if len(parts) == 2:
                first, surname = parts
                records = await self._find(
                    LAYOUT_CONTACTS,
                    [{"First Name": first, "Surname": surname}],
                    limit=1,
                )
            else:
                records = await self._find(LAYOUT_CONTACTS, [{"First Name": parts[0]}], limit=1)

        if not records:
            return None

        field_data = records[0]
        result = _project(field_data, CONTACT_PROJECTION)
        result["company"] = _project_company(field_data)
        return result

    async def get_dialogue(
        self, subscriber_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Interaction history for a contact, newest first. `limit` defaults
        small on purpose — ask for more explicitly rather than dumping
        everything by default."""
        records = await self._find(
            LAYOUT_DIALOGUES,
            [{"Subscriber ID": f"={subscriber_id}"}],
            limit=limit,
            sort=[{"fieldName": "Contact Date", "sortOrder": "descend"}],
        )
        return [_project(r, DIALOGUE_PROJECTION) for r in records]

    async def get_company(self, company_name: str, limit: int = 50) -> list[dict[str, Any]]:
        """All contacts at a given company (matched against 'Company Station').
        Company data has no standalone layout in this database — this is a
        filtered contact search, not a separate table query."""
        records = await self._find(
            LAYOUT_CONTACTS, [{"Company Station": f"*{company_name}*"}], limit=limit
        )
        results = []
        for field_data in records:
            result = _project(field_data, CONTACT_PROJECTION)
            result["company"] = _project_company(field_data)
            results.append(result)
        return results


_client: FileMakerClient | None = None


def get_client() -> FileMakerClient:
    global _client
    if _client is None:
        _client = FileMakerClient()
    return _client
