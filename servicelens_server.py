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
    wants everything about a service."""
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


@mcp.tool()
def get_service_owner(name: str) -> str:
    """Return who owns a service. Uses the repo's CODEOWNERS file when it
    has one, otherwise falls back to the GitHub account that owns the repo."""
    repo = _repo(name)
    code_owners = _codeowners(repo["name"])
    if code_owners:
        return f"{repo['name']} is owned by {code_owners} (from CODEOWNERS)"
    return (
        f"{repo['name']} has no CODEOWNERS file; "
        f"the repository belongs to {repo['owner']['login']}"
    )


@mcp.tool()
def get_service_version(name: str) -> str:
    """Return the version a service is at: its latest GitHub release, or its
    latest git tag if there are no releases."""
    repo = _repo(name)
    version, kind = _version(repo["name"])
    if version:
        return f"{repo['name']} is at version {version} (latest {kind})"
    return f"{repo['name']} has no releases or tags, so no version is known"


@mcp.tool()
def get_service_health(name: str) -> str:
    """Return the status of the most recent CI run (GitHub Actions) on the
    service's default branch, e.g. success, failure, or in_progress."""
    repo = _repo(name)
    return f"{repo['name']}: latest CI run on {repo['default_branch']} = {_health(repo)}"


@mcp.tool()
def unhealthy_services() -> list[dict]:
    """List services whose latest CI run failed, with their owner so the
    user knows who to contact. Checks the 20 most recently active repos."""
    out = []
    for r in [r for r in _repos() if not r.get("archived")][:20]:
        status = _health(r)
        if status in ("failure", "timed_out", "startup_failure"):
            out.append(
                {
                    "name": r["name"],
                    "ci_status": status,
                    "owner": _codeowners(r["name"]) or r["owner"]["login"],
                    "url": r["html_url"],
                }
            )
    return out


if __name__ == "__main__":
    mcp.run()  # stdio transport, which is what Claude Code expects