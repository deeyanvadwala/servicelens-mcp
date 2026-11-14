"""Offline tests for ServiceLens. GitHub is replaced with fake data, so these
make no network calls and use none of your API rate limit.

Run from the project root:  python -m unittest -v
"""

import base64
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import servicelens_server as s  # noqa: E402

OWNER = s.OWNER


def repo(name, days_ago=1, archived=False):
    pushed = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "name": name,
        "default_branch": "main",
        "archived": archived,
        "pushed_at": pushed.isoformat().replace("+00:00", "Z"),
        "open_issues_count": 0,
        "html_url": f"https://github.com/{OWNER}/{name}",
        "owner": {"login": OWNER},
        "description": None,
        "language": None,
    }


def codeowners_file(text):
    return {"content": base64.b64encode(text.encode()).decode()}


def runs(conclusion):
    return {"workflow_runs": [{"conclusion": conclusion, "status": "completed"}]}


def fake_github(routes):
    """Build a stand-in for s._gh. Unknown paths return None (like a 404);
    an Exception value is raised, e.g. to simulate a rate limit."""

    def fake(path, params=None):
        value = routes.get(path)
        if isinstance(value, Exception):
            raise value
        return value

    return fake


def repos_route(*repos):
    return {f"/users/{OWNER}/repos": list(repos)}


def ci_route(name, conclusion):
    return {f"/repos/{OWNER}/{name}/actions/runs": runs(conclusion)}


def owners_route(name, text="* @alice"):
    return {f"/repos/{OWNER}/{name}/contents/CODEOWNERS": codeowners_file(text)}


class HelperTests(unittest.TestCase):
    def setUp(self):
        s._cache.clear()

    def test_days_since(self):
        ten_days = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        self.assertEqual(s._days_since(ten_days.replace("+00:00", "Z")), 10)

    def test_days_since_handles_missing_date(self):
        self.assertIsNone(s._days_since(None))

    def test_codeowners_uses_first_rule_and_skips_comments(self):
        routes = owners_route("api", "# who owns what\n\n* @alice @org/platform\n")
        with patch.object(s, "_gh", fake_github(routes)):
            self.assertEqual(s._codeowners("api"), "@alice @org/platform")

    def test_codeowners_found_in_github_folder(self):
        routes = {
            f"/repos/{OWNER}/api/contents/.github/CODEOWNERS": codeowners_file("* @bob")
        }
        with patch.object(s, "_gh", fake_github(routes)):
            self.assertEqual(s._codeowners("api"), "@bob")

    def test_codeowners_none_when_missing(self):
        with patch.object(s, "_gh", fake_github({})):
            self.assertIsNone(s._codeowners("api"))

    def test_version_prefers_release_over_tag(self):
        routes = {
            f"/repos/{OWNER}/api/releases/latest": {"tag_name": "v2.0.0"},
            f"/repos/{OWNER}/api/tags": [{"name": "v1.0.0"}],
        }
        with patch.object(s, "_gh", fake_github(routes)):
            self.assertEqual(s._version("api"), ("v2.0.0", "release"))

    def test_version_falls_back_to_tag(self):
        routes = {f"/repos/{OWNER}/api/tags": [{"name": "v1.0.0"}]}
        with patch.object(s, "_gh", fake_github(routes)):
            self.assertEqual(s._version("api"), ("v1.0.0", "tag"))

    def test_version_none_without_release_or_tag(self):
        with patch.object(s, "_gh", fake_github({})):
            self.assertEqual(s._version("api"), (None, None))

    def test_health_reports_conclusion(self):
        with patch.object(s, "_gh", fake_github(ci_route("api", "failure"))):
            self.assertEqual(s._health(repo("api")), "failure")

    def test_health_unknown_without_ci_runs(self):
        with patch.object(s, "_gh", fake_github({})):
            self.assertTrue(s._health(repo("api")).startswith("unknown"))


class ToolTests(unittest.TestCase):
    def setUp(self):
        s._cache.clear()

    def run_with(self, routes, fn, *args):
        with patch.object(s, "_gh", fake_github(routes)):
            return fn(*args)

    def test_list_services(self):
        routes = repos_route(repo("api"), repo("web"))
        self.assertEqual(self.run_with(routes, s.list_services), ["api", "web"])

    def test_unknown_service_returns_helpful_message_for_each_tool(self):
        for tool in (
            s.get_service_owner,
            s.get_service_version,
            s.get_service_health,
        ):
            result = self.run_with({}, tool, "nope")
            self.assertIsInstance(result, str)
            self.assertIn("list_services", result)

    def test_get_service_unknown_returns_error_field(self):
        result = self.run_with({}, s.get_service, "nope")
        self.assertIn("list_services", result["error"])

    def test_rate_limit_message_reaches_the_agent(self):
        routes = {f"/repos/{OWNER}/api": s.GitHubError("rate limit hit")}
        result = self.run_with(routes, s.get_service_owner, "api")
        self.assertEqual(result, "rate limit hit")

    def test_owner_falls_back_to_repo_account(self):
        routes = {f"/repos/{OWNER}/api": repo("api")}
        result = self.run_with(routes, s.get_service_owner, "api")
        self.assertIn("no CODEOWNERS", result)
        self.assertIn(OWNER, result)

    def test_stale_services_oldest_first_and_respects_threshold(self):
        routes = repos_route(repo("old", 400), repo("mid", 100), repo("fresh", 5))
        result = self.run_with(routes, s.stale_services, 90)
        self.assertEqual([r["name"] for r in result], ["old", "mid"])

    def test_stale_services_ignores_archived_repos(self):
        routes = repos_route(repo("dead", 900, archived=True))
        self.assertEqual(self.run_with(routes, s.stale_services, 90), [])

    def test_unhealthy_lists_failures_with_owner(self):
        routes = {}
        routes.update(repos_route(repo("api"), repo("web")))
        routes.update(ci_route("api", "failure"))
        routes.update(ci_route("web", "success"))
        routes.update(owners_route("api", "* @alice"))
        result = self.run_with(routes, s.unhealthy_services)
        self.assertEqual([r["name"] for r in result["failing_ci"]], ["api"])
        self.assertEqual(result["failing_ci"][0]["owner"], "@alice")
        self.assertNotIn("note", result)

    def test_unhealthy_warns_when_services_have_no_ci(self):
        routes = repos_route(repo("api"))  # no CI runs configured
        result = self.run_with(routes, s.unhealthy_services)
        self.assertEqual(result["failing_ci"], [])
        self.assertEqual(result["no_ci_configured"], 1)
        self.assertIn("not monitored", result["note"])

    def test_attention_report_flags_and_ranks(self):
        routes = {}
        routes.update(repos_route(repo("bad", 200), repo("good", 2)))
        routes.update(ci_route("bad", "failure"))
        routes.update(ci_route("good", "success"))
        routes.update(owners_route("good"))
        result = self.run_with(routes, s.attention_report, 90)
        names = [r["name"] for r in result["needs_attention"]]
        self.assertEqual(names, ["bad"])  # "good" is clean, so it is left out
        flags = " ".join(result["needs_attention"][0]["flags"])
        self.assertIn("CI failure", flags)
        self.assertIn("no CODEOWNERS", flags)
        self.assertIn("stale", flags)
        self.assertFalse(result["incomplete"])

    def test_attention_report_sorts_worst_first(self):
        # one-flag: healthy CI and recent, but no CODEOWNERS (1 flag)
        # three-flags: failing CI, no CODEOWNERS, stale (3 flags)
        routes = {}
        routes.update(repos_route(repo("one-flag", 1), repo("three-flags", 300)))
        routes.update(ci_route("one-flag", "success"))
        routes.update(ci_route("three-flags", "failure"))
        result = self.run_with(routes, s.attention_report, 90)
        self.assertEqual(
            [r["name"] for r in result["needs_attention"]],
            ["three-flags", "one-flag"],
        )

    def test_attention_report_survives_rate_limit(self):
        routes = repos_route(repo("api"))
        routes[f"/repos/{OWNER}/api/actions/runs"] = s.GitHubError("rate limit")
        result = self.run_with(routes, s.attention_report)
        self.assertTrue(result["incomplete"])
        self.assertIn("rate limit", result["note"])


if __name__ == "__main__":
    unittest.main()