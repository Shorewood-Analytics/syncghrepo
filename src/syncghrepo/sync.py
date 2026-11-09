"""One clone: secret check, opt in, back up, call git-sync, clean up, report.

git-sync (simonthum/git-sync) does the actual auto-commit, fetch, fast-forward or
rebase, and push; syncghrepo does not reimplement it. Around it, syncghrepo:

- skips the clone if any changed file looks like a secret or is huge;
- never runs git-sync on the default branch: work found there moves to a new
  wip/<YYYYMMDD>-<host> branch, which is pushed and opted in; the default branch
  itself is only ever fast-forwarded;
- opts a branch in (branch.<b>.sync, branch.<b>.syncNewFiles) only when it has
  something to sync and you can push;
- runs git-sync's read-only check first, so a clone it would refuse is untouched;
- makes a backup ref (refs/syncghrepo/backup/<ts>) of HEAD plus any uncommitted
  work before git-sync rebases;
- if git-sync's rebase stops on conflicts, resolves trivial ones and reruns
  git-sync to push, or aborts the rebase: a branch is never left mid-rebase.

Nothing is ever forced, reset, cleaned or discarded. With dry_run nothing is
written: the steps are listed, git-sync runs only in its read-only check mode,
and conflicts are predicted in a scratch object store.
"""

import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import gitsync, local, resolve, save
from .run import Git, Result, Runner

BACKUP_PREFIX = "refs/syncghrepo/backup"
OPT_IN = ("sync", "syncNewFiles")


def make_backup(git: Git, files: list[str], moment: datetime) -> tuple[str | None, str]:
    """(ref, note). Back up HEAD plus uncommitted files; reuse a backup of the same tree."""
    if files:
        with tempfile.TemporaryDirectory(prefix="syncghrepo-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp, "index")), **resolve.SCRATCH_IDENTITY}
            commit = resolve.snapshot(git, files, env)
    else:
        commit = git.out("rev-parse", "HEAD")
    tree = git.out("rev-parse", f"{commit}^{{tree}}") if commit else None
    if not tree:
        return None, "could not snapshot the work"
    existing = git.out("for-each-ref", "--format=%(refname) %(tree)", f"{BACKUP_PREFIX}/") or ""
    for line in existing.splitlines():
        ref, _, ref_tree = line.partition(" ")
        if ref_tree == tree:
            return ref, "an earlier backup holds the same work"
    ref = f"{BACKUP_PREFIX}/{moment.strftime('%Y%m%dT%H%M%S')}"
    made = git("update-ref", ref, commit, "")
    return (ref, "backup before rebase") if made.ok else (None, made.last_line())


def process(
    runner: Runner,
    path: Path,
    *,
    can_push: bool = False,
    fork: str | None = None,
    dry_run: bool = True,
    fetch: bool = False,
    git_sync: str | None = None,
    stale_days: int = 30,
    now: float | None = None,
    default_branch: str | None = None,
) -> dict:
    """Process one clone. fetch makes a dry run fetch first (it only updates
    remote-tracking refs), so its plan reflects what is on the remote now.
    default_branch is GitHub's default branch, protected like origin/HEAD's."""
    now = time.time() if now is None else now
    moment = datetime.fromtimestamp(now).astimezone()
    git = Git(runner, path)
    where = str(path)
    actions: list[dict] = []
    changed = {"any": False}
    report: dict = {
        "saved": None,
        "held_back": [],
        "save_skipped": None,
        "resolved": [],
        "conflicts": [],
        "backup_refs": [],
        "git_sync": None,
        "actions": actions,
    }

    def act(cmd, result: Result | None = None, note="", ok=None, change=False):
        if result is not None:
            ok = result.ok
            if not result.ok:
                note = f"{note}: {result.last_line()}" if note else result.last_line()
        actions.append({"cmd": cmd, "ok": ok, "note": note})
        if change and ok is not False:
            changed["any"] = True

    def inspect() -> dict:
        return local.inspect(runner, path, stale_days=stale_days, now=now)

    def finish(status: str, note: str = "") -> dict:
        extra = {"status": status, "note": note, "fork": fork, "can_push": can_push}
        return {**inspect(), **report, **extra}

    def done() -> dict:
        if report["conflicts"]:
            if dry_run:
                return finish("conflict", "predicted conflicts; the rebase would be aborted")
            return finish("conflict", "unresolved conflicts; rebase aborted, nothing lost")
        if any(action["ok"] is False for action in actions):
            return finish("error", next(a["note"] for a in actions if a["ok"] is False))
        if report["save_skipped"]:
            return finish("held-back", report["save_skipped"])
        if changed["any"]:
            return finish("would-update" if dry_run else "updated")
        return finish("up-to-date")

    def fast_forward_default(branch: str | None) -> None:
        """Fast-forward the default branch when it is not checked out here."""
        default = info["default_branch"]
        if not default or branch == default:
            return
        if not git("show-ref", "--quiet", "--verify", f"refs/heads/{default}").ok:
            return
        tracking = f"refs/remotes/origin/{default}"
        lag = git.out("rev-list", "--count", f"refs/heads/{default}..{tracking}")
        if lag and lag != "0" and git("merge-base", "--is-ancestor", default, tracking).ok:
            cmd, note = (
                f"git fetch . origin/{default}:{default}",
                f"fast-forward {default} by {lag}",
            )
            # git refuses non-fast-forwards and branches checked out in any worktree.
            fetched = (
                None
                if dry_run
                else git("fetch", "--quiet", ".", f"{tracking}:refs/heads/{default}")
            )
            act(cmd, fetched, note, change=True)

    def secret_check(outcome: str) -> dict | None:
        """The finished report if a changed file is secret-looking or too large."""
        report["held_back"] = save.held_back(git, path)
        if not report["held_back"]:
            return None
        report["saved"] = None
        files = ", ".join(h["path"] for h in report["held_back"][:5])
        return finish("held-back", f"secret-looking or oversized files ({files}); {outcome}")

    info = inspect()
    if info["busy"]:
        return finish("skipped", f"{info['busy']} in progress; left alone")
    if not info["branch"]:
        return finish("skipped", "detached HEAD; left alone")
    if not info["head"]:
        return finish("skipped", f"no commits yet on {info['branch']}; left alone")

    # 1. Secret check: a hit skips the whole clone (nothing saved, synced or pushed).
    if held := secret_check("clone skipped"):
        return held

    # 2. Forks: bring the fork on GitHub up to date with its parent (never --force).
    if fork and can_push:
        cmd = f"gh repo sync {fork}"
        act(cmd) if dry_run else act(cmd, runner(["gh", "repo", "sync", fork], where))

    # 3. Fetch. Without fetch, a dry run uses remote-tracking refs as of the last fetch.
    if not dry_run or fetch:
        act("git fetch --prune", git("fetch", "--quiet", "--prune"))
        info = inspect()
        if held := secret_check("clone skipped"):  # files written while it fetched
            return held
    branch, upstream = info["branch"], info["upstream"]
    ahead, behind = info["ahead"] or 0, info["behind"] or 0
    files = save.changed_paths(git) or []
    if len(files) > save.MAX_FILES:
        report["save_skipped"] = (
            f"{len(files)} files changed (over {save.MAX_FILES}; missing .gitignore?)"
        )
        return done()
    protected = local.is_protected(branch, info["default_branch"], default_branch)

    # 4a. The default branch with no uncommitted work: fast-forward only, never git-sync.
    if protected and not files:
        if upstream and ahead and behind:
            act(
                "git merge --ff-only (skipped)",
                note=f"{branch} diverged from {upstream} ({ahead} ahead, {behind} behind)",
            )
        elif upstream and ahead:
            act("git push (skipped)", note=f"{ahead} local commit(s) on {branch} are not pushed")
        elif upstream and behind:
            merged = None if dry_run else git("merge", "--ff-only", "--quiet", upstream)
            act(f"git merge --ff-only {upstream}", merged, f"fast-forward by {behind}", change=True)
        elif not upstream and info["unpushed"]:
            act(
                "git push (skipped)",
                note=f"{info['unpushed']} local commit(s) on {branch} are not pushed",
            )
        return done()

    pending = ahead if upstream else (info["unpushed"] or 0)
    if not (files or pending or behind):
        fast_forward_default(branch)
        return done()

    # 4b. Something to sync. Can git-sync do it?
    if info["upstream_gone"] and not protected:
        act("git-sync (skipped)", note="upstream branch was deleted (merged?); not recreating it")
        fast_forward_default(branch)
        return done()
    if not can_push:
        act("git-sync (skipped)", note="no known push access; local work left as is")
        fast_forward_default(branch)
        return done()
    if not git_sync:
        act("git-sync (skipped)", ok=False, note="git-sync not found; see $SYNCGHREPO_GIT_SYNC")
        return done()
    remote = git.out("config", f"branch.{branch}.remote")
    if upstream and not protected and upstream != f"{remote}/{branch}":
        act(
            "git-sync (skipped)",
            note=f"upstream {upstream} is not {remote}/{branch}; git-sync needs the same name",
        )
        return done()

    # Commits on no remote are about to be pushed (push -u or git-sync): check them too.
    if info["unpushed"]:
        report["held_back"] = save.unpushed_held_back(git)
        if report["held_back"]:
            names = ", ".join(h["path"] for h in report["held_back"][:5])
            return finish(
                "held-back",
                f"secret-looking or oversized files in local commits ({names}); not pushed",
            )

    # git-sync's own read-only check: refuses staged changes, odd file states, ...
    if remote:
        check = runner([git_sync, "-s", "-n", "check"], where)
        act("git-sync -s -n check", ok=check.ok, note=gitsync.summary(check))
        if not check.ok:
            report["git_sync"] = {"code": check.code, "log": gitsync.log(check)}
            return done()

    # 5. Work on the default branch moves to a new wip/ branch first.
    if protected:
        target = save.wip_branch(git, moment.strftime("%Y%m%d"))
        switched = None if dry_run else git("switch", "--quiet", "-c", target)
        act(f"git switch -c {target}", switched, f"never commit to {branch}", change=True)
        if switched is not None and not switched.ok:
            return done()
        branch, upstream, behind, pending = target, None, 0, 0
    if not upstream:  # git-sync needs a same-named remote branch: create it
        pushed = (
            None if dry_run else git("push", "--quiet", "-u", "origin", f"HEAD:refs/heads/{branch}")
        )
        act(
            f"git push -u origin HEAD:refs/heads/{branch}",
            pushed,
            "create the remote branch",
            change=True,
        )
        if pushed is not None and not pushed.ok:
            return done()
        pending = 0
    fast_forward_default(branch)

    # 6. Opt the branch in (never the default branch: we are not on it any more).
    for key in OPT_IN:
        name = f"branch.{branch}.{key}"
        if git.out("config", "--bool", "--get", name) != "true":
            set_ = None if dry_run else git("config", "--bool", name, "true")
            act(f"git config --bool {name} true", set_, "opt in for git-sync")

    # 7. Back up before a possible rebase, and predict it in a dry run.
    if behind and (files or pending):
        what = f"HEAD + {len(files)} uncommitted file(s)" if files else "HEAD"
        if dry_run:
            act(f"git update-ref {BACKUP_PREFIX}/<ts> <{what}>", note="backup before rebase")
            with resolve.scratch(git) as env:
                mine = resolve.snapshot(git, files, env) if files else "HEAD"
                predicted = resolve.predict(git, upstream, mine or "HEAD", env)
            if predicted:
                report["resolved"], report["conflicts"] = predicted
        else:
            ref, note = make_backup(git, files, moment)
            act(
                f"git update-ref {ref or BACKUP_PREFIX + '/<ts>'} <{what}>", ok=bool(ref), note=note
            )
            if not ref:
                return done()
            report["backup_refs"].append(ref)

    if files:
        report["saved"] = {"branch": branch, "files": files, "commit": None}

    # 8. git-sync.
    if dry_run:
        steps = [f"commit {len(files)} file(s)"] if files else []
        if behind:
            steps.append(
                f"rebase onto {behind} new commit(s)"
                if (files or pending)
                else f"fast-forward {behind} commit(s)"
            )
        if files or pending:
            steps.append("push")
        note = ", ".join(steps)
        if report["conflicts"]:
            names = ", ".join(c["path"] for c in report["conflicts"])
            note += f"; would stop on conflicts in {names}, then git rebase --abort"
        elif report["resolved"]:
            note += f"; would auto-resolve {len(report['resolved'])} conflict(s), rerun git-sync"
        act("git-sync", note=note, change=True)
        return done()

    # Last look right before git-sync commits (git add -A): the push above took a while.
    if held := secret_check("not committed"):
        return held
    result = runner([git_sync], where)
    report["git_sync"] = {"code": result.code, "log": gitsync.log(result)}
    if result.ok:
        act("git-sync", ok=True, note=gitsync.summary(result), change=gitsync.did_something(result))
    elif local.busy(git) == "rebase":
        report["resolved"], report["conflicts"] = resolve.finish_rebase(git, runner, path)
        if report["conflicts"]:
            names = ", ".join(c["path"] or "?" for c in report["conflicts"])
            act("git-sync", ok=False, note=f"rebase stopped on conflicts in {names}")
            act(
                "git rebase --abort", git("rebase", "--abort"), "branch back as it was; backup kept"
            )
        else:
            act(
                "git-sync",
                ok=True,
                change=True,
                note=f"rebase stopped; auto-resolved {len(report['resolved'])} conflict(s)",
            )
            again = runner([git_sync], where)
            report["git_sync"] = {"code": again.code, "log": gitsync.log(again)}
            act("git-sync", ok=again.ok, note=gitsync.summary(again), change=True)
    else:
        act("git-sync", ok=False, note=f"exit {result.code}: {gitsync.summary(result)}")

    if report["saved"] and not set(files) & set(save.changed_paths(git) or []):
        report["saved"]["commit"] = git.out("rev-parse", "--short", "HEAD")
    return done()
