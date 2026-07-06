# fm-crm-mcp

Read-only MCP server exposing the FileMaker "IRD Subscribing Contacts" CRM
(the same database `MeetingPrepTool` uses) so Claude can look up contacts,
their interaction history, and company contact lists from anywhere — no
laptop required.

## Tools

| Tool | Input | Returns |
|---|---|---|
| `find_contact` | `query` (name or email) | One contact: id, name, email, phone, position, company, LinkedIn, account manager, client status, tier, sales rep |
| `get_dialogue` | `subscriber_id` (from `find_contact`), `limit` (default 10) | Interaction history, newest first: date, account manager, contact method/outcome, note text |
| `get_company` | `company_name`, `limit` (default 50) | All contacts whose company field matches — same projection as `find_contact` |

All three are read-only `_find` queries against FileMaker. Nothing writes
back. `get_company` is a filtered contact search, not a separate table
query — this database has no standalone Companies layout exposed via the
Data API (verified live 2026-07-06). Company data only exists as fields on
the contact record.

## How it's built

- `fm_client.py` — FileMaker Data API client. Logs in with Basic auth once,
  caches the session Bearer token in memory, and transparently re-logs-in
  on FileMaker's expiry codes (952/951). Talks to two layouts only:
  `Data Entry Screen` (contacts) and `Subscriber Dialogues` (interaction
  history). Projects FileMaker's ~100-field raw records down to a small
  named set of fields per tool, so responses stay compact.
- `server.py` — a `FastMCP` server (Python MCP SDK) exposing the three
  tools above over **Streamable HTTP**, wrapped in a small ASGI middleware
  that requires a static `Authorization: Bearer <token>` header on every
  request. This is a deliberately simple auth model (one shared secret, no
  OAuth server) — appropriate for a single-user personal tool, not a
  multi-tenant service.

## Local setup

```bash
cd fm-crm-mcp
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in FM_USER / FM_PASSWORD (same shared login as MeetingPrepTool's
# .fm_creds — decode it with: python3 -c "import base64; print(base64.b64decode(open('.fm_creds').read()).decode())")
# generate MCP_BEARER_TOKEN with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
python3 server.py
```

Runs on `http://127.0.0.1:8420/mcp` by default.

## VPS deployment

Deployed at `74.208.125.60` (SSH on port 2222 — note this is a different
box than the one in MeetingPrepTool's own README, which is stale). It runs
as a systemd service behind **Caddy** (already installed on this VPS,
fronting other services), not as a cron job — MCP servers are long-running.

No owned domain was available, so this uses **sslip.io**, a free wildcard
DNS service where `<anything>.<ip>.sslip.io` resolves to `<ip>` with zero
setup. Same pattern already used for the "prospector" service on this box
(`prospector.74.208.125.60.sslip.io`). Caddy issues and renews the Let's
Encrypt cert for it automatically.

1. **Get the code onto the VPS:**
   ```bash
   ssh -p 2222 root@74.208.125.60
   cd /root && git clone https://github.com/aldangerduncan/FMP.git fm-crm-mcp
   cd fm-crm-mcp
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```

2. **Configure secrets** — create `/root/fm-crm-mcp/.env` from
   `.env.example`, filling in `FM_USER`/`FM_PASSWORD` (same shared FileMaker
   login already used by MeetingPrepTool) and a freshly generated
   `MCP_BEARER_TOKEN`. Leave `MCP_HOST=127.0.0.1` — Caddy is the only thing
   that should reach this process directly. Also set
   `MCP_PUBLIC_HOSTNAMES=fm-crm-mcp.74.208.125.60.sslip.io` — the MCP SDK's
   DNS-rebinding protection rejects every request with "Invalid Host header"
   unless the proxy's hostname is explicitly allowlisted here.

3. **Install the systemd service:**
   ```bash
   cp deploy/fm-crm-mcp.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now fm-crm-mcp
   systemctl status fm-crm-mcp   # confirm it's running
   ```

4. **Add the Caddy site block** (see `deploy/Caddyfile.snippet`):
   ```bash
   cat deploy/Caddyfile.snippet >> /etc/caddy/Caddyfile
   systemctl reload caddy
   ```
   `deploy/nginx-fm-crm-mcp.conf` is kept only as a reference for a future
   move off Caddy — it isn't used in this deployment.

5. **Firewall** — 80/443/2222 are already open (ufw); port 8420 isn't
   exposed externally since it's bound to 127.0.0.1.

6. **Verify:**
   ```bash
   curl -i https://fm-crm-mcp.74.208.125.60.sslip.io/mcp   # expect 401, no bearer sent
   ```

## Connecting Claude to it

Add it as a custom remote MCP connector using:
- URL: `https://fm-crm-mcp.74.208.125.60.sslip.io/mcp`
- Auth: Bearer token — the `MCP_BEARER_TOKEN` value from `.env`

## Troubleshooting

- **"Invalid Host header" on every request:** `MCP_PUBLIC_HOSTNAMES` in
  `.env` doesn't include the hostname you're reaching the server through.
  The MCP SDK validates the `Host`/`Origin` headers against an allowlist
  (DNS-rebinding protection) — add the hostname, restart the service.

## Operational notes

- **Logs:** `journalctl -u fm-crm-mcp -f`
- **Restart after a code update:** `systemctl restart fm-crm-mcp`
- **Rotating the bearer token:** generate a new one, update `.env`, restart
  the service, update the token wherever it's configured as a connector.
- **If the FileMaker password changes:** update `FM_USER`/`FM_PASSWORD` in
  `.env` and restart — the server logs in fresh on next request.
- **Session limits:** FileMaker Data API sessions have a server-side
  timeout; the client re-logs-in automatically on expiry, so this needs no
  manual token refresh.
