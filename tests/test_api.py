"""--api: GitHub only. No git, no clone read or written, no gh-dash config."""

import json
import os

from conftest import FakeRunner, git
from test_cli import point_origin_at_github
from test_github import (
    assigned_beyond_50,
    branch_lines,
    busy_upstream,
    issue,
    pr,
    ref,
    repo_line,
    status_payload,
)
from test_schema import FORK_SYNC, ITEM, KINDS, REMOTE_BRANCH

from syncghrepo import cli, dash
from syncghrepo.run import Result

SYNCED = Result(0, "", '✓ Synced the "main" branch from up/fork')


def api_github(sync=SYNCED):
    """Ben owns proj (an old branch on GitHub) and fork (a fork); friend/shared is a
    collaboration. gh repo sync answers with sync."""
    refs = [ref("main", "2026-10-19T00:00:00Z"), ref("old", "2020-01-01T00:00:00Z")]
    payloads = {
        "benpshore/proj": status_payload("benpshore/proj", prs=[pr(3, draft=True)]),
        "benpshore/fork": status_payload("benpshore/fork", fork=True),
        "friend/shared": status_payload("friend/shared", permission="WRITE"),
    }
    branches = {"benpshore/proj": refs}

    def graphql(cmd):
        owner = next(a for a in cmd if a.startswith("owner="))[6:]
        name = next(a for a in cmd if a.startswith("name="))[5:]
        if "--paginate" in cmd:
            return branch_lines(branches.get(f"{owner}/{name}", []))
        return Result(0, payloads[f"{owner}/{name}"])

    lines = [repo_line("benpshore/proj"), repo_line("benpshore/fork", fork=True),
             repo_line("friend/shared")]  # fmt: skip
    return {
        ("api", "user"): Result(0, "benpshore\n"),
        ("api", "--paginate"): Result(0, "\n".join(lines) + "\n"),
        ("search", "prs"): Result(0, ""),
        ("api", "graphql"): graphql,
        ("repo", "sync"): sync,
    }


def snapshot(root):
    """Every path under root with its size and mtime."""
    found = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(folder, name)
            info = os.lstat(path)
            found[path] = (info.st_size, info.st_mtime_ns)
    return found


def test_api_uses_only_gh_and_writes_nothing(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    (clone / "a.txt").write_text("edited\n")  # local work: --api must not see or touch it
    before = snapshot(tmp_path)
    runner = FakeRunner(api_github())

    code = cli.main(["--api", "--json"], runner=runner)

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert {c[0] for c in runner.calls} == {"gh"}  # no git, git-sync or ghq at all
    assert [c for c in runner.calls if c[1:3] == ["repo", "sync"]] == [
        ["gh", "repo", "sync", "benpshore/fork"]
    ]
    assert not any(c[1:3] == ["repo", "clone"] for c in runner.calls)
    assert snapshot(tmp_path) == before  # nothing written, not even the gh-dash config
    assert not dash.config_dir().exists()
    assert git(clone, "status", "--porcelain") == "M a.txt"
    assert report["mode"] == "api" and report["roots"] == [] and report["gh_dash"] is None
    assert report["cloned"] == [] and report["nested"] == []
    repos = {r["name"]: r for r in report["repos"]}
    assert all(r["clones"] == [] for r in report["repos"])
    fork = repos["benpshore/fork"]["fork_sync"]
    assert set(fork) == FORK_SYNC and fork["ok"] is True and "Synced" in fork["note"]
    assert repos["benpshore/proj"]["fork_sync"] is None
    (stale,) = repos["benpshore/proj"]["remote"]["stale_branches"]
    assert set(stale) == REMOTE_BRANCH and stale["name"] == "old"
    ids = [i["id"] for i in report["items"]]
    assert "remote_stale_branch:benpshore/proj:old" in ids and "pr:benpshore/proj#3" in ids
    assert report["summary"]["forks_synced"] == 1
    assert report["summary"]["remote_stale_branches"] == 1


def test_api_dry_run_does_not_sync_forks(tmp_path, capsys):
    runner = FakeRunner(api_github())
    assert cli.main(["--api", "--dry-run"], runner=runner) == 0
    out = capsys.readouterr().out
    assert not any(c[1:3] == ["repo", "sync"] for c in runner.calls)
    assert {c[0] for c in runner.calls} == {"gh"}
    assert "API only, DRY RUN: forks not synced" in out
    assert "  - gh repo sync benpshore/fork  # dry run: not synced" in out
    assert "branch old on GitHub: no commits for" in out
    assert "--api cannot save or sync local work" in out


def test_api_fork_sync_failure_exits_2(tmp_path, capsys):
    failing = Result(1, "", "can't sync because there are diverging changes")
    assert cli.main(["--api", "--json"], runner=FakeRunner(api_github(failing))) == 2
    report = json.loads(capsys.readouterr().out)
    (item,) = [i for i in report["items"] if i["kind"] == "fork_sync_failed"]
    assert item["id"] == "fork_sync_failed:benpshore/fork"
    assert "diverging" in item["detail"]


RATE_LIMITED = Result(1, "", "gh: API rate limit exceeded for user ID 1. (HTTP 403)")


def test_api_failed_github_queries_exit_2_and_forks_are_still_synced(capsys):
    github = api_github()
    github[("api", "graphql")] = RATE_LIMITED
    runner = FakeRunner(github)

    code = cli.main(["--api", "--json"], runner=runner)

    report = json.loads(capsys.readouterr().out)
    assert code == 2  # not "all fine": nothing could be queried
    kinds = {i["id"]: i["kind"] for i in report["items"]}
    assert set(kinds.values()) == {"github_error"} and len(kinds) == 3
    # benpshore/fork is yours and a fork per the repo listing: synced all the same.
    assert [c for c in runner.calls if c[1:3] == ["repo", "sync"]] == [
        ["gh", "repo", "sync", "benpshore/fork"]
    ]
    repos = {r["name"]: r for r in report["repos"]}
    assert repos["benpshore/fork"]["fork_sync"]["ok"] is True
    assert repos["benpshore/proj"]["fork_sync"] is None


def test_api_repo_github_cannot_resolve_is_an_item_not_an_error(capsys):
    github = api_github()
    answer = github[("api", "graphql")]
    gone = "gh: Could not resolve to a Repository with the name 'friend/shared'. (repository)"

    def graphql(cmd):
        return Result(1, "", gone) if "name=shared" in cmd else answer(cmd)

    github[("api", "graphql")] = graphql
    assert cli.main(["--api", "--json"], runner=FakeRunner(github)) == 0
    report = json.loads(capsys.readouterr().out)
    (item,) = [i for i in report["items"] if i["kind"] == "github_error"]
    assert item["id"] == "github_error:friend/shared" and "Could not resolve" in item["detail"]


def test_api_lists_my_pr_in_a_busy_repo_i_contributed_to(capsys):
    # gh search prs found a PR of mine in up/big, older than its 50 latest open PRs.
    github = {
        ("api", "user"): Result(0, "benpshore\n"),
        ("api", "--paginate"): Result(0, ""),
        ("search", "prs"): Result(0, "up/big\n"),
        ("api", "graphql"): busy_upstream(([pr(7)], [])),
    }
    cli.main(["--api", "--json"], runner=FakeRunner(github))
    report = json.loads(capsys.readouterr().out)
    assert [i["id"] for i in report["items"]] == ["pr:up/big#7"]
    assert report["summary"]["open_prs"] == 1


def test_api_items_cover_prs_and_issues_beyond_the_first_50(capsys):
    # A repo of mine with 300 open PRs and 80 issues assigned to me: GitHub returns
    # 50 of each in the status query.
    github = {
        ("api", "user"): Result(0, "benpshore\n"),
        ("api", "--paginate"): Result(0, repo_line("benpshore/proj") + "\n"),
        ("search", "prs"): Result(0, ""),
        ("api", "graphql"): assigned_beyond_50([issue(n) for n in range(1, 81)]),
    }

    code = cli.main(["--api", "--json"], runner=FakeRunner(github))

    report = json.loads(capsys.readouterr().out)
    assert code == 0  # open PRs and issues are work, not a failure
    kinds = [i["kind"] for i in report["items"]]
    assert {k: kinds.count(k) for k in set(kinds)} == {"pr": 50, "issue": 80, "prs_not_listed": 1}
    assert report["summary"]["assigned_issues"] == 80
    (more,) = [i for i in report["items"] if i["kind"] == "prs_not_listed"]
    assert set(more) == ITEM and more["kind"] in KINDS
    assert more["id"] == "prs_not_listed:benpshore/proj"
    assert more["title"] == "250 more open PR(s) not listed"
    assert more["url"] == "https://github.com/benpshore/proj/pulls"
    assert more["detail"] == "300 open, 50 listed"
    (repo,) = report["repos"]
    assert repo["remote"]["prs_not_listed"] == 250
    assert "250 more open PR(s) not listed" in repo["attention"]
