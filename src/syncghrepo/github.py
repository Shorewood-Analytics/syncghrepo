"""GitHub side: which repos you have, and the open work in each. Read-only."""

import json
import time
from datetime import datetime

from .run import Runner

USER_REPOS = "user/repos?affiliation=owner,collaborator,organization_member&per_page=100"
REPO_FIELDS = (
    ".[] | {name: .full_name, url: .html_url, fork, archived, default_branch,"
    " owner: .owner.login, owner_type: .owner.type}"
)
STATUS_QUERY = """
query($owner: String!, $name: String!, $me: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner url isFork isArchived viewerPermission
    parent { nameWithOwner }
    defaultBranchRef { name target { ... on Commit { oid statusCheckRollup { state } } } }
    pullRequests(states: OPEN, first: 50, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      nodes {
        number title url isDraft updatedAt reviewDecision author { login }
        reviewRequests(first: 20) { nodes { requestedReviewer { ... on User { login } } } }
      }
    }
    issues(states: OPEN, first: 50, filterBy: {assignee: $me}) {
      totalCount
      nodes { number title url updatedAt }
    }
  }
}
"""


# Every branch on GitHub (--api mode only), all pages: one JSON line per branch.
BRANCHES_QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", first: 100, after: $endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        target { ... on Commit { committedDate } }
        associatedPullRequests(states: OPEN) { totalCount }
      }
    }
  }
}
"""
BRANCHES_JQ = (
    ".data.repository.refs.nodes[] | {name, committed_at: .target.committedDate,"
    " open_prs: .associatedPullRequests.totalCount}"
)


PUSH_PERMISSIONS = frozenset({"WRITE", "MAINTAIN", "ADMIN"})


class GhError(RuntimeError):
    pass


def not_found(error: str) -> bool:
    """Whether a status query failed only because GitHub has no such repo for you
    (deleted, renamed away or no longer visible), not because the query failed."""
    return "Could not resolve to a Repository" in error or error.endswith(": not found")


def gh(runner: Runner, *args: str) -> str:
    result = runner(["gh", *args], None)
    if not result.ok:
        raise GhError(f"gh {' '.join(args[:2])}: {result.last_line()}")
    return result.out


def whoami(runner: Runner) -> str:
    return gh(runner, "api", "user", "--jq", ".login").strip()


def new_repo(name: str, source: str | None = None) -> dict:
    return {
        "name": name,
        "url": f"https://github.com/{name}",
        "github": True,
        "fork": None,
        "archived": None,
        "default_branch": None,
        "sources": [source] if source else [],
    }


def discover(runner: Runner, me: str) -> tuple[dict[str, dict], list[str]]:
    """Repos you own, your orgs' repos, repos you collaborate on, and repos you
    opened PRs in. Keyed by lowercase owner/name."""
    repos: dict[str, dict] = {}
    errors: list[str] = []
    try:
        for line in gh(runner, "api", "--paginate", USER_REPOS, "--jq", REPO_FIELDS).splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            if data["owner"].lower() == me.lower():
                source = "owner"
            elif data["owner_type"] == "Organization":
                source = "org"
            else:
                source = "collaborator"
            entry = new_repo(data["name"], source)
            entry.update(
                url=data["url"],
                fork=data["fork"],
                archived=data["archived"],
                default_branch=data["default_branch"],
            )
            repos[data["name"].lower()] = entry
    except (GhError, ValueError, KeyError) as err:
        errors.append(f"listing repos: {err}")
    try:
        found = gh(
            runner,
            "search",
            "prs",
            "--author=@me",
            "--limit=1000",
            "--json=repository",
            "--jq=.[].repository.nameWithOwner",
        )
        for name in sorted(set(found.split())):
            entry = repos.setdefault(name.lower(), new_repo(name))
            if "contributed" not in entry["sources"]:
                entry["sources"].append("contributed")
    except GhError as err:
        errors.append(f"searching PRs: {err}")
    return repos, errors


def stale_branches(
    runner: Runner, name: str, default: str | None, stale_days: int, now: float
) -> list[dict]:
    """Branches on GitHub with no commits for stale_days, other than the default
    branch and branches with an open PR."""
    owner, repo = name.split("/", 1)
    raw = gh(runner, "api", "graphql", "--paginate", "-f", f"query={BRANCHES_QUERY}",
             "-f", f"owner={owner}", "-f", f"name={repo}", "--jq", BRANCHES_JQ)  # fmt: skip
    found = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        node = json.loads(line)
        date = node.get("committed_at") or ""
        if node["name"] == default or not date or node.get("open_prs"):
            continue
        age = int((now - datetime.fromisoformat(date).timestamp()) // 86400)
        if age >= stale_days:
            found.append(
                {
                    "name": node["name"],
                    "age_days": age,
                    "committed_at": date,
                    "url": f"https://github.com/{name}/tree/{node['name']}",
                }
            )
    return sorted(found, key=lambda branch: branch["name"])


def repo_status(
    runner: Runner,
    name: str,
    me: str,
    *,
    all_prs: bool = True,
    branches: bool = False,
    stale_days: int = 30,
    now: float | None = None,
) -> dict:
    """Open PRs, CI on the default branch, and issues assigned to me.

    With all_prs False (repos I only contributed to), keep just my PRs and PRs
    that request my review. With branches (--api mode), also the remote branches
    that are stale (stale_branches; null otherwise).
    """
    owner, repo = name.split("/", 1)
    raw = gh(
        runner,
        "api",
        "graphql",
        "-f",
        f"query={STATUS_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={repo}",
        "-f",
        f"me={me}",
    )
    data = (json.loads(raw).get("data") or {}).get("repository")
    if not data:
        raise GhError(f"{name}: not found")
    ref = data.get("defaultBranchRef") or {}
    commit = ref.get("target") or {}
    rollup = commit.get("statusCheckRollup") or {}

    prs = []
    for node in data["pullRequests"]["nodes"]:
        author = (node.get("author") or {}).get("login") or "ghost"
        requested = any(
            ((req.get("requestedReviewer") or {}).get("login") or "").lower() == me.lower()
            for req in node["reviewRequests"]["nodes"]
        )
        mine = author.lower() == me.lower()
        draft = bool(node["isDraft"])
        if not (all_prs or mine or requested):
            continue
        prs.append(
            {
                "number": node["number"],
                "title": node["title"],
                "url": node["url"],
                "author": author,
                "mine": mine,
                "draft": draft,
                "review_decision": node.get("reviewDecision"),
                "review_requested": requested,
                "needs_review": not draft
                and (requested or (not mine and node.get("reviewDecision") != "APPROVED")),
                "updated_at": node["updatedAt"],
            }
        )

    return {
        "name": data["nameWithOwner"],
        "url": data["url"],
        "fork": data["isFork"],
        "archived": data["isArchived"],
        "parent": (data.get("parent") or {}).get("nameWithOwner"),
        "can_push": data.get("viewerPermission") in PUSH_PERMISSIONS,
        "default_branch": ref.get("name"),
        "ci": (rollup.get("state") or "").lower() or None,
        "ci_commit": (commit.get("oid") or "")[:7] or None,
        "open_prs": data["pullRequests"]["totalCount"],
        "prs": prs,
        "assigned_issues": [
            {k: node[k] for k in ("number", "title", "url")} | {"updated_at": node["updatedAt"]}
            for node in data["issues"]["nodes"]
        ],
        "assigned_issues_total": data["issues"]["totalCount"],
        "stale_branches": stale_branches(
            runner,
            data["nameWithOwner"],
            ref.get("name"),
            stale_days,
            time.time() if now is None else now,
        )
        if branches
        else None,
    }
