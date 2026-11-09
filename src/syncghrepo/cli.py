"""syncghrepo: keep all work in your local clones saved and in sync, and list
what is still open. One command does it all:

  syncghrepo              discover, clone, save and sync, refresh gh-dash, report
  syncghrepo --dry-run    the same, as a preview: nothing is changed or written
  syncghrepo --json       the report as JSON (schema_version 1), e.g. for reconcile-tasks
  syncghrepo --api        GitHub API only: no local clone is read or touched

Every run:
  1. discovers repos (gh) and clones (ghq list -p plus ~/code, or only
     $SYNCGHREPO_ROOTS, colon-separated, when set), plus git repos nested in
     clones and submodules;
  2. clones each repo of yours that has no clone anywhere under the roots
     (matched by origin URL) into ~/code/GitHub/<name> with gh repo clone,
     archived ones too; an existing folder is never overwritten (only with the
     default roots, not with $SYNCGHREPO_ROOTS);
  3. saves and syncs each clone, nested repos too, with git-sync (simonthum/git-sync): a
     secret-looking, .env or huge changed file skips the clone; work on the
     default branch moves to wip/<YYYYMMDD>-<host> (the default branch is only
     fast-forwarded); a backup ref is made before any rebase; trivial conflicts
     are resolved, anything else aborted; git submodule update --init --recursive
     where a .gitmodules exists (never moving a submodule off local work);
  4. runs gh repo sync for forks you can push to;
  5. writes or refreshes the gh-dash config (never launches gh dash);
  6. prints the open-work report: PRs, review requests, failing CI, assigned
     issues, unpushed/stale branches, stashes.

--api works purely through the GitHub API: no git, no clone, no fetch, no
gh-dash config; it only prints. It runs gh repo sync (server-side) for your
forks and reports open PRs, drafts, review requests, failing default-branch CI,
assigned issues and stale branches on GitHub. It cannot save or sync local
work. With --dry-run it does not sync forks either.

Exit status: 0 all fine; 1 needs attention (uncommitted work, a conflict, a
held-back file, a clone mid-merge/rebase, failing CI); 2 an error.

Environment: SYNCGHREPO_ROOTS (folders of clones, instead of ghq + ~/code),
SYNCGHREPO_GIT_SYNC (the git-sync to run; else PATH, then ~/.local/bin)."""

import argparse
import json
import os
import time
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import clone as cloner
from . import dash, github, gitsync, local, nested, sync
from .run import Runner, make_runner

# Bump when a field is removed or renamed, or its type or meaning changes; new fields
# may be added without a bump (consumers ignore unknown keys). See README "JSON schema".
SCHEMA_VERSION = 1
PROBLEMS = ("conflict", "error", "held-back")  # clone statuses listed in attention
# Fixed settings (formerly flags).
DEPTH = 3  # how deep to search a root for clones
STALE_DAYS = 30  # branch age that counts as stale
TIMEOUT = 120.0  # seconds per command
JOBS = 8  # clones processed in parallel
FAILING_CI = ("failure", "error")
INVOLVED = {"owner", "org", "collaborator"}  # else: only contributed to, or just cloned
SHOWN_PER_REPO = 12


def involved(entry: dict) -> bool:
    """Whether all of the repo's PRs and its CI are your business, not just your own PRs."""
    return bool(INVOLVED & set(entry["sources"]))


def tool_version() -> str:
    try:
        return version("syncghrepo")
    except PackageNotFoundError:
        return "0.0.0"


def env_roots() -> list[str]:
    """$SYNCGHREPO_ROOTS, colon-separated; empty when unset."""
    env = os.environ.get("SYNCGHREPO_ROOTS", "")
    return [root for root in env.split(os.pathsep) if root]


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="syncghrepo",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview: show what would be committed, synced and written; change nothing",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the report as JSON (schema_version 1)"
    )
    parser.add_argument(
        "--api",
        action="store_true",
        help="GitHub API only: no git, no clones read or written, no gh-dash config; "
        "syncs forks on GitHub and reports. Cannot save or sync local work",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {tool_version()}")
    return parser.parse_args(argv)


def clone_items(clone: dict) -> list[str]:
    """Problems and local leftovers in one clone."""
    where = local.short_path(clone["path"])
    items = []
    if clone["status"] in ("skipped", *PROBLEMS):
        items.append(f"{where}: {clone['status']}: {clone['note']}")
    for action in clone["actions"]:
        if action["ok"] is False:
            items.append(f"{action['cmd']} failed: {action['note']}")
        elif action["cmd"].endswith("(skipped)"):
            items.append(f"not synced: {action['note']}")
    if clone["upstream_gone"] and clone["status"] != "skipped":
        items.append(f"{clone['branch']}: upstream branch was deleted (merged?)")
    for held in clone["held_back"]:
        items.append(f"held back {held['path']}: {held['reason']}")
    for conflict in clone["conflicts"]:
        extra = f" ({conflict['error']})" if conflict.get("error") else ""
        items.append(f"conflict needs a person: {conflict['path'] or '?'}{extra}")
    if clone["backup_refs"] and clone["conflicts"]:
        items.append(f"backup: {', '.join(clone['backup_refs'])}")
    for branch in clone["branches"]:
        if branch["unpushed"] and branch["name"] != clone["branch"]:
            items.append(f"branch {branch['name']}: {branch['unpushed']} unpushed commit(s)")
        if branch["gone"]:
            items.append(f"branch {branch['name']}: upstream deleted (merged?)")
    stale = [b["name"] for b in clone["branches"] if b["stale"]]
    if stale:
        items.append(f"{len(stale)} stale branch(es): {', '.join(stale)}")
    if clone["stashes"]:
        first = clone["stashes"][0]
        items.append(f"{len(clone['stashes'])} stash(es): {first['ref']}: {first['message']}")
    if clone["worktrees"]:
        paths = ", ".join(local.short_path(p) for p in clone["worktrees"])
        items.append(f"{len(clone['worktrees'])} other worktree(s), not auto-saved: {paths}")
    return items


def attention(entry: dict) -> list[str]:
    """Short, human-readable list of unresolved work for one repo."""
    items = [item for clone in entry["clones"] for item in clone_items(clone)]
    remote = entry.get("remote")
    if entry.get("remote_error"):
        items.append(f"GitHub query failed: {entry['remote_error']}")
    if remote:
        if remote["ci"] in FAILING_CI and involved(entry):
            items.append(f"CI {remote['ci']} on {remote['default_branch']} ({remote['ci_commit']})")
        for pr in remote["prs"]:
            tags = [
                tag
                for tag, on in (
                    ("mine", pr["mine"]),
                    ("draft", pr["draft"]),
                    ("needs review", pr["needs_review"]),
                    ("review requested", pr["review_requested"]),
                )
                if on
            ]
            by = "" if pr["mine"] else f" by {pr['author']}"
            items.append(f"PR #{pr['number']}{by} [{', '.join(tags) or 'open'}]: {pr['title']}")
        if remote["open_prs"] > len(remote["prs"]) and involved(entry):
            items.append(f"{remote['open_prs'] - len(remote['prs'])} more open PR(s) not listed")
        for issue in remote["assigned_issues"]:
            items.append(f"issue #{issue['number']} assigned to me: {issue['title']}")
        if involved(entry):
            for branch in remote.get("stale_branches") or []:
                items.append(
                    f"branch {branch['name']} on GitHub: no commits for {branch['age_days']} days"
                )
    fork_sync = entry.get("fork_sync")
    if fork_sync and fork_sync["ok"] is False:
        items.append(f"{fork_sync['cmd']} failed: {fork_sync['note']}")
    for record in entry.get("clone_records") or []:
        items.append(f"not cloned: {record['note']}")
    return items


def item(kind: str, id_: str, repo: dict, title: str, **fields) -> dict:
    """One unit of unresolved work. Every item has exactly these keys."""
    return {
        "id": f"{kind}:{id_}",
        "kind": kind,
        "repo": repo["name"],
        "title": title,
        "url": fields.get("url"),
        "path": fields.get("path"),
        "branch": fields.get("branch"),
        "flags": fields.get("flags", []),
        "updated_at": fields.get("updated_at"),
        "detail": fields.get("detail"),
    }


def work_items(repos: list[dict]) -> list[dict]:
    """A flat list of unresolved work with stable ids, for reconcile-tasks and agents.

    An id is the same on every run while the work is the same, so a consumer can
    match items across runs. Items are sorted by id.
    """
    items: list[dict] = []
    for repo in repos:
        name = repo["name"]
        remote = repo.get("remote")
        if repo.get("remote_error"):
            items.append(
                item("github_error", name, repo, "GitHub query failed", detail=repo["remote_error"])
            )
        if remote:
            branch = remote["default_branch"]
            if remote["ci"] in FAILING_CI and involved(repo):
                items.append(
                    item(
                        "ci_failing",
                        f"{name}@{branch}",
                        repo,
                        f"CI {remote['ci']} on {branch}",
                        url=f"{remote['url']}/actions",
                        branch=branch,
                        detail=remote["ci_commit"],
                    )
                )
            for pr in remote["prs"]:
                flags = [
                    flag
                    for flag in ("mine", "draft", "needs_review", "review_requested")
                    if pr[flag]
                ]
                items.append(
                    item(
                        "pr",
                        f"{name}#{pr['number']}",
                        repo,
                        pr["title"],
                        url=pr["url"],
                        flags=flags,
                        updated_at=pr["updated_at"],
                        detail=f"by {pr['author']}",
                    )
                )
            for issue in remote["assigned_issues"]:
                items.append(
                    item(
                        "issue",
                        f"{name}#{issue['number']}",
                        repo,
                        issue["title"],
                        url=issue["url"],
                        flags=["assigned"],
                        updated_at=issue["updated_at"],
                    )
                )
            for branch in (remote.get("stale_branches") or []) if involved(repo) else []:
                items.append(
                    item(
                        "remote_stale_branch",
                        f"{name}:{branch['name']}",
                        repo,
                        f"{branch['name']} on GitHub: no commits for {branch['age_days']} days",
                        url=branch["url"],
                        branch=branch["name"],
                        updated_at=branch["committed_at"],
                    )
                )
        fork_sync = repo.get("fork_sync")
        if fork_sync and fork_sync["ok"] is False:
            items.append(
                item("fork_sync_failed", name, repo, fork_sync["cmd"], detail=fork_sync["note"])
            )
        for record in repo.get("clone_records") or []:
            items.append(
                item(
                    "clone_skipped",
                    name,
                    repo,
                    f"not cloned into {local.short_path(record['path'])}",
                    path=record["path"],
                    detail=record["note"],
                )
            )
        for clone in repo["clones"]:
            path, current = clone["path"], clone["branch"]
            at = {"path": path, "branch": current}
            if clone["status"] in ("skipped", "error", "conflict"):
                files = [c["path"] for c in clone["conflicts"]]
                detail = clone["note"] + (f"; files: {', '.join(files)}" if files else "")
                if clone["backup_refs"]:
                    detail += f"; backup: {', '.join(clone['backup_refs'])}"
                items.append(
                    item(
                        clone["status"],
                        f"{path}:{current}",
                        repo,
                        f"{clone['status']} in {local.short_path(path)}",
                        detail=detail,
                        **at,
                    )
                )
            for held in clone["held_back"]:
                items.append(
                    item(
                        "held_back",
                        f"{path}:{held['path']}",
                        repo,
                        f"not saved: {held['path']}",
                        detail=held["reason"],
                        **at,
                    )
                )
            for action in clone["actions"]:
                if action["cmd"].endswith("(skipped)"):
                    items.append(
                        item(
                            "not_pushed",
                            f"{path}:{current}",
                            repo,
                            f"{current} not pushed",
                            detail=action["note"],
                            **at,
                        )
                    )
            for branch in clone["branches"]:
                at_branch = {"path": path, "branch": branch["name"]}
                if branch["unpushed"] and branch["name"] != current:
                    items.append(
                        item(
                            "unpushed_branch",
                            f"{path}:{branch['name']}",
                            repo,
                            f"{branch['name']}: {branch['unpushed']} unpushed commit(s)",
                            **at_branch,
                        )
                    )
                if branch["gone"]:
                    items.append(
                        item(
                            "gone_branch",
                            f"{path}:{branch['name']}",
                            repo,
                            f"{branch['name']}: upstream deleted",
                            **at_branch,
                        )
                    )
                if branch["stale"]:
                    items.append(
                        item(
                            "stale_branch",
                            f"{path}:{branch['name']}",
                            repo,
                            f"{branch['name']}: no commits for {branch['age_days']} days",
                            **at_branch,
                        )
                    )
            for stash in clone["stashes"]:
                items.append(
                    item(
                        "stash",
                        f"{path}:{stash['commit']}",
                        repo,
                        stash["message"],
                        updated_at=stash["created"],
                        detail=stash["ref"],
                        **at,
                    )
                )
            for tree in clone["worktrees"]:
                items.append(
                    item(
                        "worktree",
                        tree,
                        repo,
                        f"worktree {local.short_path(tree)}",
                        path=tree,
                        detail="linked worktree; not auto-saved",
                    )
                )
    return sorted(items, key=lambda entry: entry["id"])


def summarize(repos: list[dict]) -> dict:
    clones = [clone for entry in repos for clone in entry["clones"]]
    prs = [pr for entry in repos if entry.get("remote") for pr in entry["remote"]["prs"]]
    return {
        "repos": len(repos),
        "on_github": sum(entry["github"] for entry in repos),
        "cloned": sum(bool(entry["clones"]) for entry in repos),
        "not_cloned": sum(
            entry["github"] and not entry["clones"] and not entry["archived"] for entry in repos
        ),
        "clones": len(clones),
        "clone_status": dict(sorted(Counter(clone["status"] for clone in clones).items())),
        "saved_files": sum(len((c["saved"] or {}).get("files", [])) for c in clones),
        "held_back_files": sum(len(clone["held_back"]) for clone in clones),
        "auto_resolved": sum(len(clone["resolved"]) for clone in clones),
        "conflicts": sum(len(clone["conflicts"]) for clone in clones),
        "open_prs": len(prs),
        "draft_prs": sum(pr["draft"] for pr in prs),
        "prs_needing_review": sum(pr["needs_review"] for pr in prs),
        "ci_failing": sum(
            bool(entry.get("remote")) and entry["remote"]["ci"] in FAILING_CI and involved(entry)
            for entry in repos
        ),
        "assigned_issues": sum(
            len(entry["remote"]["assigned_issues"]) for entry in repos if entry.get("remote")
        ),
        "stashes": sum(len(clone["stashes"]) for clone in clones),
        "unpushed_branches": sum(
            1 for clone in clones for branch in clone["branches"] if branch["unpushed"]
        ),
        "stale_branches": sum(
            1 for clone in clones for branch in clone["branches"] if branch["stale"]
        ),
        "remote_stale_branches": sum(
            len(entry["remote"].get("stale_branches") or [])
            for entry in repos
            if entry.get("remote") and involved(entry)
        ),
        "forks_synced": sum(bool((e.get("fork_sync") or {}).get("ok")) for e in repos)
        + sum(
            1
            for clone in clones
            for action in clone["actions"]
            if action["cmd"].startswith("gh repo sync ") and action["ok"]
        ),
        "needs_attention": sum(bool(entry["attention"]) for entry in repos),
    }


def origin_name(runner: Runner, path: Path) -> tuple[str | None, str | None]:
    """(owner/name on GitHub or None, origin URL or None) for a clone."""
    urls = [
        runner(cmd, str(path))
        for cmd in (
            ["git", "remote", "get-url", "origin"],  # after insteadOf rewrites
            ["git", "config", "--get", "remote.origin.url"],  # as written
        )
    ]
    names = [local.github_name(url.out) for url in urls if url.ok]
    url = urls[0].out.strip() if urls[0].ok else None
    return next((name for name in names if name), None), url


def register(runner: Runner, repos: dict[str, dict], path: Path) -> str:
    """The repos key for a clone, adding a repo entry for it if needed."""
    name, url = origin_name(runner, path)
    if name:
        key = name.lower()
        repos.setdefault(key, github.new_repo(name, "local"))
        return key
    key = f"local:{path}"
    repos.setdefault(
        key,
        {
            "name": path.name,
            "url": url,
            "github": False,
            "fork": None,
            "archived": None,
            "default_branch": None,
            "sources": ["local"],
        },
    )
    return key


def discover(runner: Runner) -> dict:
    """Repos (from gh), clones (ghq + ~/code, or $SYNCGHREPO_ROOTS) and the repos
    nested in them, with run errors. complete says every clone could be listed, so a
    repo without one really has none (only then are missing repos cloned)."""
    explicit = env_roots()
    roots = [Path(root).expanduser() for root in (explicit or ["~/code"])]
    errors: list[str] = []
    me = None
    repos: dict[str, dict] = {}
    listed = False
    try:
        me = github.whoami(runner)
    except github.GhError as err:
        errors.append(f"gh not usable, GitHub skipped and nothing pushed: {err}")
    if me:
        repos, found_errors = github.discover(runner, me)
        errors += found_errors
        listed = not any(error.startswith("listing repos") for error in found_errors)

    local_errors: list[str] = []
    clone_paths = local.find_clones(roots, DEPTH, errors=local_errors)
    ghq_count = None
    ghq_error = None
    if not explicit:  # $SYNCGHREPO_ROOTS means exactly these; otherwise ghq's clones too
        found, ghq_error = local.ghq_clones(runner)
        if ghq_error:
            errors.append(ghq_error)
        if found is not None:
            ghq_count = len(found)
            clone_paths = sorted(set(clone_paths) | set(found))

    clone_keys = [(path, register(runner, repos, path)) for path in clone_paths]
    found_nested = local.nested_repos(runner, clone_paths, errors=local_errors)
    errors += local_errors
    nested_keys = [(info, register(runner, repos, info["path"])) for info in found_nested]
    return {"roots": roots, "errors": errors, "me": me, "repos": repos,
            "clone_keys": clone_keys, "nested_keys": nested_keys, "ghq_clones": ghq_count,
            "complete": bool(me) and listed and not explicit and not ghq_error
                        and not local_errors}  # fmt: skip


def finish_report(
    repos: dict[str, dict], now: float, clone_records: list[dict] | None = None
) -> tuple[list[dict], list[dict], dict, str]:
    """(ordered repos, items, summary, generated_at): attention lines, work items, counts."""
    by_key = {entry["name"].lower(): entry for entry in repos.values()}
    for record in clone_records or []:
        if record["status"] == "skipped":
            by_key[record["name"].lower()].setdefault("clone_records", []).append(record)
    ordered = sorted(repos.values(), key=lambda entry: entry["name"].lower())
    for entry in ordered:
        entry["attention"] = attention(entry)
    ordered.sort(key=lambda entry: not entry["attention"])
    items = work_items(ordered)
    for entry in ordered:
        entry.pop("clone_records", None)
    summary = summarize(ordered)
    summary["items"] = dict(sorted(Counter(entry["kind"] for entry in items).items()))
    generated_at = datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")
    return ordered, items, summary, generated_at


def fetch_status(pool, runner: Runner, repos: dict, keys, me: str | None, **extra) -> None:
    """GitHub status (PRs, CI, issues, push access) for these repos, in parallel."""
    jobs = [
        (key, pool.submit(github.repo_status, runner, repos[key]["name"], me,
                          all_prs=involved(repos[key]), **extra))
        for key in keys
        if me and repos[key]["github"] and not repos[key]["archived"]
    ]  # fmt: skip
    for key, job in jobs:
        entry = repos[key]
        try:
            status = job.result()
        except (github.GhError, ValueError, KeyError, TypeError) as err:
            entry["remote_error"] = str(err)
            continue
        entry["remote"] = status
        entry.update(
            fork=status["fork"],
            archived=status["archived"],
            default_branch=status["default_branch"],
        )


def build_report(args: argparse.Namespace, runner: Runner, now: float | None = None) -> dict:
    """Clone what is missing, save and sync every clone (nested ones too), refresh the
    gh-dash config, report (a preview if dry_run). --api: see build_api_report."""
    now = time.time() if now is None else now
    if getattr(args, "api", False):
        return build_api_report(args, runner, now)
    apply = not args.dry_run
    found = discover(runner)
    roots, errors, me = found["roots"], found["errors"], found["me"]
    repos, clone_keys = found["repos"], list(found["clone_keys"])
    git_sync = gitsync.find()
    if not git_sync:
        errors.append("git-sync not found ($SYNCGHREPO_GIT_SYNC, PATH, ~/.local/bin)")
    for entry in repos.values():
        entry.update(clones=[], remote=None, remote_error=None, fork_sync=None)

    # Repos of yours with no clone anywhere (top-level, ghq, nested or submodule).
    records: list[dict] = []
    if found["complete"] and me:
        present = {key for _, key in clone_keys} | {key for _, key in found["nested_keys"]}
        records = cloner.plan(runner, repos, me, present, cloner.clone_dir())

    def run_clone(path: Path, key: str, info: dict | None = None) -> dict:
        remote = repos[key]["remote"] or {}
        can_push = bool(remote.get("can_push")) and not repos[key]["archived"]
        result = sync.process(
            runner,
            path,
            can_push=can_push,
            fork=remote["name"] if remote.get("fork") else None,
            dry_run=not apply,
            git_sync=git_sync,
            stale_days=STALE_DAYS,
            now=now,
            default_branch=repos[key]["default_branch"],
        )
        result["nested_in"] = str(info["parent"]) if info else None
        result["submodule"] = bool(info and info["submodule"])
        if result["submodule"]:
            nested.settle_submodule(runner, result)  # its parent's update --recursive covers it
        else:
            nested.update(runner, result, dry_run=not apply)
        return result

    nested_report: list[dict] = []
    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        if apply:
            todo = [record for record in records if record["status"] == "would-clone"]
            for record, job in [(r, pool.submit(cloner.clone, runner, r)) for r in todo]:
                job.result()
                if record["status"] == "cloned":
                    clone_keys.append((Path(record["path"]).resolve(), record["name"].lower()))
                elif record["status"] == "failed":
                    errors.append(f"gh repo clone {record['name']} failed: {record['note']}")

        # GitHub first: push access and fork status decide what each clone may do.
        fetch_status(pool, runner, repos, list(repos), me)

        # Explicit roots and ghq can include both a parent and its child.
        # Keep those in separate batches too, not just discovered nested clones.
        levels = {
            path: sum(path != other and path.is_relative_to(other) for other, _ in clone_keys)
            for path, _ in clone_keys
        }
        for level in sorted(set(levels.values())):
            jobs = [
                (key, pool.submit(run_clone, path, key))
                for path, key in clone_keys
                if levels[path] == level
            ]
            for key, job in jobs:
                repos[key]["clones"].append(job.result())

        # Nested repos, after their parents (whose submodule update may add some),
        # level by level so a repo and one inside it are never processed at once.
        if apply:
            top = [path for path, _ in clone_keys]
            nested_keys = [
                (info, register(runner, repos, info["path"]))
                for info in local.nested_repos(runner, top)
            ]
        else:
            nested_keys = found["nested_keys"]
        new = [key for _, key in nested_keys if "clones" not in repos[key]]
        for key in new:
            repos[key].update(clones=[], remote=None, remote_error=None, fork_sync=None)
        fetch_status(pool, runner, repos, new, me)
        infos = [info for info, _ in nested_keys]
        for level in sorted({local.nesting_level(i["path"], infos) for i in infos}):
            batch = [
                (info, key, pool.submit(run_clone, info["path"], key, info))
                for info, key in nested_keys
                if local.nesting_level(info["path"], infos) == level
            ]
            for info, key, job in batch:
                result = job.result()
                repos[key]["clones"].append(result)
                nested_report.append(
                    {
                        "path": str(info["path"]),
                        "parent": str(info["parent"]),
                        "repo": repos[key]["name"],
                        "submodule": info["submodule"],
                        "status": result["status"],
                    }
                )

    ordered, items, summary, generated_at = finish_report(repos, now, records)
    summary["new_clones"] = sum(r["status"] in ("cloned", "would-clone") for r in records)
    summary["clones_skipped"] = sum(r["status"] == "skipped" for r in records)
    summary["nested"] = len(nested_report)
    gh_dash = None
    if me:
        try:
            gh_dash = dash.save(ordered, generated_at, tool_version(), write=apply)
        except OSError as err:
            errors.append(f"could not write the gh-dash config: {err}")
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "syncghrepo",
        "version": tool_version(),
        "generated_at": generated_at,
        "user": me,
        "dry_run": not apply,
        "mode": "local",
        "roots": [str(root) for root in roots],
        "ghq_clones": found["ghq_clones"],
        "gh_dash": gh_dash,
        "cloned": records,
        "nested": sorted(nested_report, key=lambda entry: entry["path"]),
        "summary": summary,
        "items": items,
        "repos": ordered,
        "errors": errors,
    }


def build_api_report(args: argparse.Namespace, runner: Runner, now: float) -> dict:
    """--api: GitHub only. No git, no local clone read or written, no gh-dash config:
    gh api / gh search for the report, gh repo sync (on GitHub) for your forks."""
    apply = not args.dry_run
    errors: list[str] = []
    me = None
    repos: dict[str, dict] = {}
    try:
        me = github.whoami(runner)
    except github.GhError as err:
        errors.append(f"gh not usable: {err}")
    if me:
        repos, found_errors = github.discover(runner, me)
        errors += found_errors
    for entry in repos.values():
        entry.update(clones=[], remote=None, remote_error=None, fork_sync=None)
    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        plain = [key for key, entry in repos.items() if not involved(entry)]
        mine = [key for key, entry in repos.items() if involved(entry)]
        fetch_status(pool, runner, repos, plain, me)
        fetch_status(pool, runner, repos, mine, me, branches=True, stale_days=STALE_DAYS, now=now)
    for entry in repos.values():
        # fork and archived come from the status query, else from the repo listing
        # (when the query failed); a repo of yours can always be pushed to.
        remote = entry["remote"]
        owner = entry["name"].partition("/")[0]
        if not me or owner.lower() != me.lower():
            continue
        if not (entry["fork"] and not entry["archived"] and (remote or {}).get("can_push", True)):
            continue
        cmd = f"gh repo sync {entry['name']}"
        if apply:
            result = runner(["gh", "repo", "sync", entry["name"]], None)
            said = result.out.strip() or result.err.strip()
            note = result.last_line() if said or not result.ok else ""
            entry["fork_sync"] = {"cmd": cmd, "ok": result.ok, "note": note}
        else:
            entry["fork_sync"] = {"cmd": cmd, "ok": None, "note": "dry run: not synced"}
    ordered, items, summary, generated_at = finish_report(repos, now)
    summary.update(not_cloned=0, new_clones=0, clones_skipped=0, nested=0)  # clones unseen
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "syncghrepo",
        "version": tool_version(),
        "generated_at": generated_at,
        "user": me,
        "dry_run": not apply,
        "mode": "api",
        "roots": [],
        "ghq_clones": None,
        "gh_dash": None,
        "cloned": [],
        "nested": [],
        "summary": summary,
        "items": items,
        "repos": ordered,
        "errors": errors,
    }


def render(report: dict) -> str:
    """Plain-text summary for a human."""
    s = report["summary"]
    api = report.get("mode") == "api"
    forks = [e["fork_sync"] for e in report["repos"] if e.get("fork_sync")]
    mode = (
        "DRY RUN, nothing changed (run without --dry-run to do it)"
        if report["dry_run"]
        else "synced"
    )
    if api:
        mode = "API only, " + ("DRY RUN: forks not synced" if report["dry_run"] else "forks synced")
    roots = ", ".join(local.short_path(root) for root in report["roots"])
    statuses = ", ".join(f"{n} {status}" for status, n in s["clone_status"].items()) or "none"
    lines = [
        f"syncghrepo {report['version']} · {report['user'] or 'no GitHub'} · "
        f"{report['generated_at']} · {mode}",
    ]
    if api:
        lines += [
            f"{s['repos']} repos on GitHub; local clones not read (--api)",
            f"Forks: {len(forks)} to sync with gh repo sync"
            if report["dry_run"]
            else f"Forks: {s['forks_synced']} of {len(forks)} synced on GitHub",
        ]
    else:
        lines += [
            f"{s['repos']} repos ({s['on_github']} on GitHub), {s['clones']} clone(s) under "
            f"{roots}"
            + (f" and in ghq ({report['ghq_clones']})" if report["ghq_clones"] is not None else "")
            + f", {s['nested']} nested, {s['not_cloned']} not cloned",
            f"Clones: {statuses}",
            f"Saved: {s['saved_files']} file(s), {s['held_back_files']} held back; "
            f"conflicts: {s['auto_resolved']} auto-resolved, {s['conflicts']} need a person",
        ]
    lines.append(
        f"Work: {s['open_prs']} open PR(s) ({s['draft_prs']} draft, "
        f"{s['prs_needing_review']} need review), {s['ci_failing']} failing CI, "
        f"{s['assigned_issues']} assigned issue(s), "
        + (
            f"{s['remote_stale_branches']} stale branch(es) on GitHub"
            if api
            else f"{s['unpushed_branches']} unpushed branch(es), {s['stale_branches']} stale "
            f"branch(es), {s['stashes']} stash(es)"
        )
    )
    if report["cloned"]:
        verb = "Would clone" if report["dry_run"] else "Clone"
        lines += ["", f"{verb} (repos of yours with no clone anywhere under the roots):"]
        for record in report["cloned"]:
            tags = ", ".join(
                tag
                for tag, on in (("archived", record["archived"]), ("fork", record["fork"]))
                if on
            )
            extra = f" [{tags}]" if tags else ""
            if record["status"] == "skipped":
                lines.append(f"  skipped {record['name']}{extra}: {record['note']}")
                continue
            mark = {"would-clone": "-", "cloned": "ok", "failed": "FAILED"}[record["status"]]
            note = f"  # {record['note']}" if record["status"] == "failed" else ""
            where = local.short_path(record["path"])
            lines.append(f"  {mark} gh repo clone {record['name']} {where}{extra}{note}")
    if report["nested"]:
        lines += ["", f"Nested repos ({len(report['nested'])}), synced like any clone:"]
        for entry in report["nested"]:
            kind = "submodule" if entry["submodule"] else "nested clone"
            lines.append(
                f"  {local.short_path(entry['path'])} ({kind} of {entry['repo']} in "
                f"{local.short_path(entry['parent'])}): {entry['status']}"
            )
    if forks:
        lines += ["", "Forks (on GitHub):"]
        for fork in sorted(forks, key=lambda f: f["cmd"]):
            mark = {True: "ok", False: "FAILED", None: "-"}[fork["ok"]]
            note = f"  # {fork['note']}" if fork["note"] else ""
            lines.append(f"  {mark} {fork['cmd']}{note}")
    clones = [c for e in report["repos"] for c in e["clones"] if c["actions"]]
    if clones:
        lines += ["", "Would do:" if report["dry_run"] else "Done:"]
        for clone in sorted(clones, key=lambda c: c["path"]):
            lines.append(f"  {local.short_path(clone['path'])} ({clone['status']})")
            for action in clone["actions"]:
                mark = {True: "ok", False: "FAILED", None: "-"}[action["ok"]]
                note = f"  # {action['note']}" if action["note"] else ""
                lines.append(f"    {mark if not report['dry_run'] else '-'} {action['cmd']}{note}")
            saved = clone["saved"]
            if saved:
                shown = ", ".join(saved["files"][:8]) + (" ..." if len(saved["files"]) > 8 else "")
                lines.append(f"      files: {shown}")
            for item in clone["resolved"]:
                regen = f", then {item['regenerate']}" if item.get("regenerate") else ""
                lines.append(f"      resolve {item['path']}: {item['how']}{regen}")
    flagged = [entry for entry in report["repos"] if entry["attention"]]
    if flagged:
        lines += ["", f"Needs attention ({len(flagged)}):"]
        for entry in flagged:
            where = "; ".join(
                f"{local.short_path(c['path'])} on {c['branch'] or 'detached'}, {c['status']}"
                for c in entry["clones"]
            )
            if api:
                where = "on GitHub"
            lines.append(f"  {entry['name']}" + (f"  ({where})" if where else "  (not cloned)"))
            lines += [f"    - {item}" for item in entry["attention"][:SHOWN_PER_REPO]]
            if len(entry["attention"]) > SHOWN_PER_REPO:
                more = len(entry["attention"]) - SHOWN_PER_REPO
                lines.append(f"    - ... and {more} more (see --json)")
    clean = [e["name"] for e in report["repos"] if e["clones"] and not e["attention"]]
    if clean:
        lines += ["", f"Clean ({len(clean)}): {', '.join(clean)}"]
    missing = [
        e["name"]
        for e in report["repos"]
        if e["github"] and not e["clones"] and not e["attention"] and not e["archived"]
    ]
    if missing:
        label = "Nothing open" if api else "Not cloned, nothing open"
        lines += ["", f"{label} ({len(missing)}): {', '.join(missing)}"]
    if report["gh_dash"]:
        where = local.short_path(report["gh_dash"]["path"])
        flag = "" if where.endswith("/config.yml") else f" --config {where}"
        done = "wrote" if report["gh_dash"]["written"] else "would write"
        lines += ["", f"gh-dash config: {done} {where}; review with: gh dash{flag}"]
    if report["errors"]:
        lines += ["", "Errors:"] + [f"  - {error}" for error in report["errors"]]
    if api:
        lines += [
            "",
            "--api cannot save or sync local work (no git ran); run syncghrepo for that.",
        ]
    return "\n".join(lines)


def exit_code(report: dict) -> int:
    """0 all fine, 1 needs attention, 2 an error."""
    clones = [clone for entry in report["repos"] for clone in entry["clones"]]
    if report["errors"] or any(c["status"] == "error" for c in clones):
        return 2
    # A GitHub query that failed (rate limit, network, auth): push access, forks and CI
    # are unknown. A repo GitHub cannot find for you is only reported (github_error).
    errors = [entry.get("remote_error") for entry in report["repos"]]
    if any(error and not github.not_found(error) for error in errors):
        return 2
    if any((entry.get("fork_sync") or {}).get("ok") is False for entry in report["repos"]):
        return 2
    attention = any(
        c["status"] in ("conflict", "held-back")
        or c["changes"]
        or c["untracked"]
        or c["busy"]
        or (not c["branch"] and c["status"] != "up-to-date")  # a clean submodule is fine
        for c in clones
    ) or any(record["status"] == "skipped" for record in report.get("cloned") or [])
    return 1 if attention or report["summary"]["ci_failing"] else 0


def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args, runner or make_runner(TIMEOUT))
    print(json.dumps(report, indent=2) if args.json else render(report))
    return exit_code(report)
