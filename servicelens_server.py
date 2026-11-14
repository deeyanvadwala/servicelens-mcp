"""ServiceLens MCP server (GitHub-backed).

Treats every public repo under a GitHub user/org as a "service" and lets an
AI agent ask who owns it, what version it runs, and whether its CI is healthy.

Config (environment variables, both optional):
  GITHUB_OWNER  user or org to use as the catalog (default: deeyanvadwala)
  GITHUB_TOKEN  raises the API rate limit from 60 to 5000 requests/hour
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("servicelens")

OWNER = os.environ.get("GITHUB_OWNER", "deeyanvadwala")
TOKEN = os.environ.get("GITHUB_TOKEN")
API = "https://api.github.com"
CACHE_TTL = 60  # seconds; saves API calls when the agent asks follow-ups
_cache: dict = {}


class GitHubError(Exception):
    """Raised with a clear message the agent can relay to the user."""


# --- Data layer -------------------------------------------------------------


def _gh(path: str, params: dict | None = None):
    """GET from the GitHub API. Returns parsed JSON, or None on 404."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "servicelens-mcp",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            data = None
        elif e.code in (403, 429):
            raise GitHubError(
                "GitHub rate limit reached or access denied. "
                "Set GITHUB_TOKEN to raise the limit, or try again later."
            )
        else:
            raise GitHubError(f"GitHub API returned an error ({e.code}).")
    except urllib.error.URLError as e:
        raise GitHubError(f"Could not reach GitHub: {e.reason}")

    _cache[url] = (time.time(), data)
    return data


def _repos() -> list[dict]:
    return _gh(f"/users/{OWNER}/repos", {"per_page": 100, "sort": "pushed"}) or []


def _repo(name: str) -> dict:
    data = _gh(f"/repos/{OWNER}/{urllib.parse.quote(name.strip())}")
    if data is None:
        raise GitHubError(
            f"No service named '{name}' under {OWNER}. "
            "Use list_services to see valid names."
        )
    return data


def _codeowners(name: str) -> str | None:
    """Owners from the first rule of a CODEOWNERS file, if the repo has one."""
    for path in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
        f = _gh(f"/repos/{OWNER}/{name}/contents/{path}")
        if isinstance(f, dict) and f.get("content"):
            text = base64.b64decode(f["content"]).decode("utf-8", "replace")
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return " ".join(parts[1:])
    return None


def _version(name: str) -> tuple[str | None, str | None]:
    rel = _gh(f"/repos/{OWNER}/{name}/releases/latest")
    if isinstance(rel, dict) and rel.get("tag_name"):
        return rel["tag_name"], "release"
    tags = _gh(f"/repos/{OWNER}/{name}/tags", {"per_page": 1})
    if tags:
        return tags[0]["name"], "tag"
    return None, None


def _health(repo: dict) -> str:
    runs = _gh(
        f"/repos/{OWNER}/{repo['name']}/actions/runs",
        {"per_page": 1, "branch": repo["default_branch"]},
    )
    items = (runs or {}).get("workflow_runs") or []
    if not items:
        return "unknown (no CI runs)"
    run = items[0]
    return run["conclusion"] or run["status"]


FAILING = ("failure", "timed_out", "startup_failure")


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).days


def _active_repos(limit: int = 30) -> list[dict]:
    """Non-archived repos, most recently pushed first (capped to save API calls)."""
    return [r for r in _repos() if not r.get("archived")][:limit]


# --- Tools ------------------------------------------------------------------
# The docstrings matter: the model reads them to decide when to call each tool.


@mcp.tool()
def list_services() -> list[str]:
    """List the names of all services (repositories) in the catalog."""
    return [r["name"] for r in _repos()]


@mcp.tool()
def search_services(query: str) -> list[dict]:
    """Find services whose name, description, language or topics contain the
    query text (case-insensitive). Use this when the user describes a service
    without knowing its exact name."""
    q = query.lower().strip()
    out = []
    for r in _repos():
        haystack = " ".join(
            [r["name"], r.get("description") or "", r.get("language") or ""]
            + (r.get("topics") or [])
        ).lower()
        if q in haystack:
            out.append(
                {
                    "name": r["name"],
                    "description": r.get("description"),
                    "language": r.get("language"),
                }
            )
    return out


@mcp.tool()
def get_service(name: str) -> dict:
    """Get a full overview of one service: description, owner, version,
    language, open issues, last activity and URL. Use this when the user
    wants everything about a service. If the service does not exist, the
    result has an 'error' field explaining what to do."""
    try:
        repo = _repo(name)
        version, kind = _version(repo["name"])
        return {
            "name": repo["name"],
            "description": repo.get("description"),
            "owner": _codeowners(repo["name"]) or repo["owner"]["login"],
            "version": version or "no releases or tags",
            "version_source": kind,
            "language": repo.get("language"),
            "open_issues": repo.get("open_issues_count"),
            "last_push": repo.get("pushed_at"),
            "archived": repo.get("archived"),
            "url": repo["html_url"],
        }
    except GitHubError as e:
        return {"error": str(e)}


@mcp.tool()
def get_service_owner(name: str) -> str:
    """Return who owns a service. Uses the repo's CODEOWNERS file when it
    has one, otherwise falls back to the GitHub account that owns the repo.
    If the service does not exist, the result explains how to find valid names."""
    try:
        repo = _repo(name)
        code_owners = _codeowners(repo["name"])
        if code_owners:
            return f"{repo['name']} is owned by {code_owners} (from CODEOWNERS)"
        return (
            f"{repo['name']} has no CODEOWNERS file; "
            f"the repository belongs to {repo['owner']['login']}"
        )
    except GitHubError as e:
        return str(e)


@mcp.tool()
def get_service_version(name: str) -> str:
    """Return the version a service is at: its latest GitHub release, or its
    latest git tag if there are no releases. If the service does not exist,
    the result explains how to find valid names."""
    try:
        repo = _repo(name)
        version, kind = _version(repo["name"])
        if version:
            return f"{repo['name']} is at version {version} (latest {kind})"
        return f"{repo['name']} has no releases or tags, so no version is known"
    except GitHubError as e:
        return str(e)


@mcp.tool()
def get_service_health(name: str) -> str:
    """Return the status of the most recent CI run (GitHub Actions) on the
    service's default branch, e.g. success, failure, or in_progress. If the
    service does not exist, the result explains how to find valid names."""
    try:
        repo = _repo(name)
        return (
            f"{repo['name']}: latest CI run on {repo['default_branch']} "
            f"= {_health(repo)}"
        )
    except GitHubError as e:
        return str(e)


@mcp.tool()
def unhealthy_services() -> dict:
    """List services whose latest CI run failed, with owner and URL so the
    user knows who to contact. Also reports how many services have no CI at
    all, because those are not monitored. Checks the 20 most active repos."""
    failing, no_ci, checked = [], 0, 0
    for r in _active_repos(20):
        status = _health(r)
        checked += 1
        if status.startswith("unknown"):
            no_ci += 1
        elif status in FAILING:
            failing.append(
                {
                    "name": r["name"],
                    "ci_status": status,
                    "owner": _codeowners(r["name"]) or r["owner"]["login"],
                    "url": r["html_url"],
                }
            )
    result = {"checked": checked, "failing_ci": failing, "no_ci_configured": no_ci}
    if no_ci:
        result["note"] = (
            "Services with no CI are not monitored, so an empty failing list "
            "does not mean everything is healthy."
        )
    return result


@mcp.tool()
def stale_services(days: int = 90) -> list[dict]:
    """List services with no pushes in the given number of days (default 90),
    oldest first. Use this to find abandoned or neglected services."""
    out = []
    for r in _active_repos(100):
        d = _days_since(r.get("pushed_at"))
        if d is not None and d >= days:
            out.append(
                {
                    "name": r["name"],
                    "days_since_last_push": d,
                    "last_push": r.get("pushed_at"),
                    "url": r["html_url"],
                }
            )
    return sorted(out, key=lambda x: -x["days_since_last_push"])


@mcp.tool()
def services_without_owner() -> list[dict]:
    """List services that have no CODEOWNERS file, meaning nobody is
    explicitly responsible for them. Checks the 30 most active repos."""
    return [
        {
            "name": r["name"],
            "repo_account": r["owner"]["login"],
            "url": r["html_url"],
        }
        for r in _active_repos(30)
        if not _codeowners(r["name"])
    ]


@mcp.tool()
def services_without_ci() -> list[str]:
    """List services with no CI runs (no GitHub Actions history), so their
    health is unknown. Checks the 30 most active repos."""
    return [
        r["name"]
        for r in _active_repos(30)
        if _health(r).startswith("unknown")
    ]


@mcp.tool()
def attention_report(stale_days: int = 90) -> dict:
    """One combined report of services that need attention. Flags failing CI,
    missing CI, missing CODEOWNERS, and staleness, worst first. Use this for
    broad questions like 'what needs attention?' or 'what should I fix first?'.
    Checks the 30 most active repos."""
    repos = _active_repos(30)
    report, checked, note = [], 0, None
    try:
        for r in repos:
            flags = []
            status = _health(r)
            if status in FAILING:
                flags.append(f"CI {status}")
            elif status.startswith("unknown"):
                flags.append("no CI")
            if not _codeowners(r["name"]):
                flags.append("no CODEOWNERS")
            d = _days_since(r.get("pushed_at"))
            if d is not None and d >= stale_days:
                flags.append(f"stale ({d} days since last push)")
            checked += 1
            if flags:
                report.append(
                    {
                        "name": r["name"],
                        "flags": flags,
                        "open_issues": r.get("open_issues_count"),
                        "url": r["html_url"],
                    }
                )
    except GitHubError as e:
        note = f"Report is incomplete: {e}"
    report.sort(key=lambda x: -len(x["flags"]))
    return {
        "checked": checked,
        "total_active_services": len(repos),
        "needs_attention": report,
        "incomplete": note is not None,
        "note": note,
    }


if __name__ == "__main__":
    mcp.run()  # stdio transport, which is what Claude Code expects