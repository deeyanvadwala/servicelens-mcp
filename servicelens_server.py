"""Service Catalog MCP server.

Lets an AI agent (Claude Code, Claude Desktop, etc.) answer questions like
"who owns the payments service?" by querying the catalog live.

Run:  python service_catalog_server.py
"""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("servicelens")

# --- Data layer -------------------------------------------------------------
# Mock data for now. To make this "real", replace these helpers with calls to
# your actual catalog API (e.g. requests.get(...)) and keep the tools unchanged.
SERVICES = {
    "payments": {
        "owner": "alice@example.com",
        "team": "Payments",
        "version": "2.4.1",
        "status": "healthy",
        "repo": "https://github.com/example/payments",
        "tier": 1,
    },
    "auth": {
        "owner": "bob@example.com",
        "team": "Identity",
        "version": "5.0.3",
        "status": "healthy",
        "repo": "https://github.com/example/auth",
        "tier": 1,
    },
    "notifications": {
        "owner": "carol@example.com",
        "team": "Growth",
        "version": "1.9.0",
        "status": "degraded",
        "repo": "https://github.com/example/notifications",
        "tier": 2,
    },
    "search": {
        "owner": "dave@example.com",
        "team": "Discovery",
        "version": "3.2.7",
        "status": "healthy",
        "repo": "https://github.com/example/search",
        "tier": 2,
    },
}


def _lookup(name: str) -> dict | None:
    return SERVICES.get(name.strip().lower())


def _not_found(name: str) -> str:
    known = ", ".join(sorted(SERVICES))
    return f"No service named '{name}'. Known services: {known}"


# --- Tools ------------------------------------------------------------------
# The docstrings matter: the model reads them to decide when to call each tool.


@mcp.tool()
def list_services() -> list[str]:
    """List the names of all services in the catalog."""
    return sorted(SERVICES)


@mcp.tool()
def get_service(name: str) -> dict | str:
    """Get all known details for one service: owner, team, version, status,
    repo URL and tier. Use this when the user wants a full overview."""
    svc = _lookup(name)
    return {"name": name.lower(), **svc} if svc else _not_found(name)


@mcp.tool()
def get_service_owner(name: str) -> str:
    """Return who owns a service (owner email and team)."""
    svc = _lookup(name)
    if not svc:
        return _not_found(name)
    return f"{name} is owned by {svc['owner']} (team: {svc['team']})"


@mcp.tool()
def get_service_version(name: str) -> str:
    """Return the version a service is currently running."""
    svc = _lookup(name)
    return f"{name} is running version {svc['version']}" if svc else _not_found(name)


@mcp.tool()
def services_by_team(team: str) -> list[str]:
    """List all services owned by a given team (case-insensitive)."""
    return sorted(n for n, s in SERVICES.items() if s["team"].lower() == team.lower())


@mcp.tool()
def unhealthy_services() -> list[dict]:
    """List services whose status is not 'healthy', with owner for escalation."""
    return [
        {"name": n, "status": s["status"], "owner": s["owner"]}
        for n, s in SERVICES.items()
        if s["status"] != "healthy"
    ]


if __name__ == "__main__":
    mcp.run()  # stdio transport, which is what Claude Code / Desktop expect