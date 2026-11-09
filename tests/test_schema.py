"""Locks the --json schema that reconcile-tasks consumes.

If this test fails, the output schema changed. Removing or renaming a field, or
changing its type or meaning, needs SCHEMA_VERSION bumped; any change needs the
README "JSON schema" section updated. Adding a field only needs the README and
the sets below updated.
"""

import json
import re
from pathlib import Path

from conftest import FakeRunner, commit, git
from test_cli import gh_replies, point_origin_at_github
from test_github import pr, repo_line, status_payload

from syncghrepo import cli, save
from syncghrepo.run import Result

SCHEMA_VERSION = 1
TOP = {"schema_version", "tool", "version", "generated_at", "user", "dry_run", "roots",
       "ghq_clones", "gh_dash", "summary", "items", "repos", "errors",
       "mode", "cloned", "nested"}  # fmt: skip
MODES = {"local", "api"}
CLONE_RECORD = {"name", "path", "archived", "fork", "status", "note"}
CLONE_RECORD_STATUS = {"would-clone", "cloned", "skipped", "failed"}
NESTED = {"path", "parent", "repo", "submodule", "status"}
REMOTE_BRANCH = {"name", "age_days", "committed_at", "url"}
FORK_SYNC = {"cmd", "ok", "note"}
GH_DASH = {"path", "written", "existing_config"}
SUMMARY = {
    "repos", "on_github", "cloned", "not_cloned", "clones", "clone_status", "saved_files",
    "held_back_files", "auto_resolved", "conflicts", "open_prs", "draft_prs",
    "prs_needing_review", "ci_failing", "assigned_issues", "stashes", "unpushed_branches",
    "stale_branches", "needs_attention", "items",
    "remote_stale_branches", "forks_synced", "new_clones", "clones_skipped", "nested",
}  # fmt: skip
ITEM = {"id", "kind", "repo", "title", "url", "path", "branch", "flags", "updated_at", "detail"}
KINDS = {"pr", "issue", "ci_failing", "github_error", "conflict", "error", "skipped",
         "held_back", "not_pushed", "unpushed_branch", "gone_branch", "stale_branch", "stash",
         "worktree", "remote_stale_branch", "fork_sync_failed", "clone_skipped",
         "save_skipped"}  # fmt: skip
REPO = {"name", "url", "github", "fork", "archived", "default_branch", "sources", "clones",
        "remote", "remote_error", "attention", "fork_sync"}  # fmt: skip
REMOTE = {"name", "url", "fork", "archived", "parent", "can_push", "default_branch", "ci",
          "ci_commit", "open_prs", "prs", "assigned_issues", "assigned_issues_total",
          "stale_branches"}  # fmt: skip
PR = {"number", "title", "url", "author", "mine", "draft", "review_decision",
      "review_requested", "needs_review", "updated_at"}  # fmt: skip
ISSUE = {"number", "title", "url", "updated_at"}
CLONE = {
    "path", "origin", "branch", "upstream", "upstream_gone", "default_branch", "head",
    "changes", "untracked", "busy", "ahead", "behind", "unpushed", "branches", "stashes",
    "worktrees", "saved", "held_back", "save_skipped", "resolved", "conflicts", "backup_refs",
    "actions", "status", "note", "fork", "can_push", "git_sync", "nested_in", "submodule",
}  # fmt: skip
BRANCH = {"name", "upstream", "unpushed", "age_days", "gone", "stale"}
STASH = {"commit", "ref", "message", "created"}
ACTION = {"cmd", "ok", "note"}
GIT_SYNC_RUN = {"code", "log"}
SAVED = {"branch", "files", "commit"}
HELD = {"path", "reason"}
CONFLICT = {"path", "how"}  # plus "error" when a step failed
CLONE_STATUS = {"up-to-date", "updated", "would-update", "held-back", "conflict", "error",
                "skipped"}  # fmt: skip


def busy_report(tmp_path, origin, clone, capsys) -> dict:
    """Run syncghrepo --json (a real run) on a clone with most kinds of unresolved work."""
    point_origin_at_github(clone)
    old = "2020-01-01T00:00:00"
    git(clone, "switch", "-q", "-c", "old")
    commit(clone, "o.txt", "old\n", GIT_COMMITTER_DATE=old, GIT_AUTHOR_DATE=old)
    git(clone, "switch", "-q", "-c", "local-idea", "main")
    commit(clone, "i.txt", "idea\n")
    git(clone, "switch", "-q", "main")
    (clone / "a.txt").write_text("stashed\n")
    git(clone, "stash", "push", "-q", "-m", "half done")
    git(clone, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "side")
    git(clone, "switch", "-q", "-c", "feat")
    git(clone, "push", "-q", "-u", "origin", "feat")
    origin.advance("a.txt", "theirs\n", branch="feat")
    (clone / "a.txt").write_text("mine\n")
    # A second clone with a secret: skipped as held-back.
    other = tmp_path / "code" / "GitHub" / "proj-copy"
    git(tmp_path, "clone", "-q", str(origin.bare), str(other))
    point_origin_at_github(other)
    (other / ".env").write_text("SECRET=1\n")

    issue = {"number": 4, "title": "Fix it", "url": "https://x/4", "updatedAt": "2026-10-01"}
    payload = status_payload("benpshore/proj", ci="FAILURE", prs=[pr(7, "dependabot"),
                             pr(8, draft=True)], issues=[issue])  # fmt: skip
    replies = {
        ("api", "user"): Result(0, "benpshore\n"),
        ("api", "--paginate"): Result(0, repo_line("benpshore/proj") + "\n"),
        ("search", "prs"): Result(0, ""),
        ("api", "graphql"): Result(0, payload),
    }
    args = ["--json"]
    assert cli.main(args, runner=FakeRunner(replies)) == 1
    first = json.loads(capsys.readouterr().out)
    cli.main(args, runner=FakeRunner(replies))
    second = json.loads(capsys.readouterr().out)
    return first, second


def test_json_schema_is_stable(tmp_path, origin, clone, capsys):
    report, again = busy_report(tmp_path, origin, clone, capsys)

    assert report["schema_version"] == cli.SCHEMA_VERSION == SCHEMA_VERSION
    assert set(report) == TOP
    assert report["mode"] in MODES and report["mode"] == "local"
    assert report["cloned"] == [] and report["nested"] == []  # $SYNCGHREPO_ROOTS: no cloning
    assert set(report["summary"]) == SUMMARY
    assert set(report["gh_dash"]) == GH_DASH
    (repo,) = report["repos"]
    assert set(repo) == REPO
    assert set(repo["remote"]) == REMOTE
    assert all(set(p) == PR for p in repo["remote"]["prs"])
    assert all(set(i) == ISSUE for i in repo["remote"]["assigned_issues"])
    by_status = {c["status"]: c for c in repo["clones"]}
    assert set(by_status) == {"conflict", "held-back"}
    assert all(set(c) == CLONE for c in repo["clones"])
    clone_out = by_status["conflict"]
    held_out = by_status["held-back"]
    assert clone_out["status"] in CLONE_STATUS
    assert clone_out["branches"] and all(set(b) == BRANCH for b in clone_out["branches"])
    assert clone_out["stashes"] and all(set(s) == STASH for s in clone_out["stashes"])
    assert all(set(a) == ACTION for a in clone_out["actions"])
    assert set(clone_out["git_sync"]) == GIT_SYNC_RUN and held_out["git_sync"] is None
    assert set(clone_out["saved"]) == SAVED
    assert held_out["held_back"] and all(set(h) == HELD for h in held_out["held_back"])
    assert clone_out["conflicts"] and all(set(c) >= CONFLICT for c in clone_out["conflicts"])

    items = report["items"]
    assert all(set(entry) == ITEM for entry in items)
    kinds = {entry["kind"] for entry in items}
    assert kinds <= KINDS
    assert kinds >= {"pr", "issue", "ci_failing", "conflict", "held_back", "unpushed_branch",
                     "stale_branch", "stash", "worktree"}  # fmt: skip
    assert all(entry["id"].startswith(entry["kind"] + ":") for entry in items)
    ids = [entry["id"] for entry in items]
    assert ids == sorted(set(ids))  # unique and sorted
    assert report["summary"]["items"] == {k: sum(e["kind"] == k for e in items) for k in kinds}
    by_id = {entry["id"]: entry for entry in items}
    assert by_id["pr:benpshore/proj#8"]["flags"] == ["mine", "draft"]
    assert by_id["pr:benpshore/proj#7"]["flags"] == ["needs_review"]
    assert by_id["issue:benpshore/proj#4"]["flags"] == ["assigned"]
    # The same work gives the same ids on the next run.
    assert [entry["id"] for entry in again["items"]] == ids


def test_readme_documents_every_field_and_kind():
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    section = readme[readme.index("## JSON schema") :]
    section = section[: section.find("\n## ", 1)]
    assert f"schema_version` is `{SCHEMA_VERSION}`" in section
    documented = set(re.findall(r"`([a-z_]+)`", section))
    fields = (TOP | SUMMARY | ITEM | KINDS | REPO | REMOTE | PR | ISSUE | CLONE | BRANCH
              | STASH | ACTION | GIT_SYNC_RUN | SAVED | HELD | CONFLICT | GH_DASH | MODES
              | CLONE_RECORD | NESTED | REMOTE_BRANCH | FORK_SYNC)  # fmt: skip
    assert sorted(fields - documented) == []


def test_a_clone_skipped_for_too_many_files_is_an_item(origin, clone, capsys):
    # Real work in a.txt, plus build output that a missing .gitignore lets through.
    git(clone, "switch", "-q", "-c", "feat")
    git(clone, "push", "-q", "-u", "origin", "feat")
    point_origin_at_github(clone)
    out = clone / "out"
    out.mkdir()
    for n in range(save.MAX_FILES + 1):
        (out / f"f{n}.txt").write_text(f"{n}\n")
    (clone / "a.txt").write_text("real work\n")

    code = cli.main(["--json"], runner=FakeRunner(gh_replies()))

    report = json.loads(capsys.readouterr().out)
    (entry,) = [i for i in report["items"] if i["kind"] == "save_skipped"]
    assert set(entry) == ITEM and entry["kind"] in KINDS
    assert entry["id"] == f"save_skipped:{clone.resolve()}:feat"
    assert (entry["path"], entry["branch"]) == (str(clone.resolve()), "feat")
    assert entry["detail"].startswith(f"{save.MAX_FILES + 2} files changed")
    assert report["summary"]["items"]["save_skipped"] == 1
    assert code == 1
