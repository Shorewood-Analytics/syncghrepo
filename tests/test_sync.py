"""End-to-end behaviour of one clone, against real git repos and the real git-sync
(tests/fixtures/git-sync) in tmp_path."""

import hashlib
import re
from datetime import datetime

import pytest
from conftest import GIT_SYNC, FakeRunner, commit, fake_token, git, remote_head

from syncghrepo import save, sync
from syncghrepo.run import Git, Result

WIP = f"wip/{datetime.now().astimezone().strftime('%Y%m%d')}-{save.host_tag()}"
# Anything that could lose work or rewrite published history. None of it may ever run.
FORBIDDEN = ("reset", "clean", "checkout", "restore", "stash drop", "stash pop", "stash clear")


def assert_safe(runner: FakeRunner) -> None:
    for cmd in runner.git_commands():
        assert not cmd.startswith(FORBIDDEN), cmd
        assert "--force" not in cmd and " -f " not in f" {cmd} ", cmd


def apply(clone, runner=None, **kw):
    runner = runner or FakeRunner()
    kw.setdefault("can_push", True)
    kw.setdefault("git_sync", str(GIT_SYNC))
    return sync.process(runner, clone, dry_run=False, **kw), runner


def plan(clone, runner=None, **kw):
    kw.setdefault("can_push", True)
    kw.setdefault("git_sync", str(GIT_SYNC))
    return sync.process(runner or FakeRunner(), clone, dry_run=True, **kw)


def head(repo, ref="HEAD"):
    return git(repo, "rev-parse", ref)


def config(repo, key):
    try:
        return git(repo, "config", "--get", key)
    except Exception:
        return None


def cmds(result):
    return [a["cmd"] for a in result["actions"]]


def feature(clone, name="feat"):
    git(clone, "switch", "-q", "-c", name)
    git(clone, "push", "-q", "-u", "origin", name)


def test_clean_clone_is_not_touched(origin, clone):
    result, runner = apply(clone)
    assert result["status"] == "up-to-date"
    assert cmds(result) == ["git fetch --prune"]
    assert runner.git_sync_calls() == []
    assert config(clone, "branch.main.sync") is None


def test_default_branch_behind_is_fast_forwarded_without_git_sync(origin, clone):
    new = origin.advance()
    result, runner = apply(clone)
    assert result["status"] == "updated"
    assert head(clone) == new
    assert "git merge --ff-only origin/main" in cmds(result)
    assert runner.git_sync_calls() == []
    assert config(clone, "branch.main.sync") is None


def test_work_on_main_moves_to_wip_branch_and_git_sync_pushes_it(origin, clone):
    (clone / "a.txt").write_text("edited\n")
    (clone / "new.txt").write_text("new\n")
    main_before = head(clone)

    result, runner = apply(clone)

    assert re.fullmatch(r"wip/\d{8}-[a-z0-9-]+", WIP)
    assert result["status"] == "updated", result["actions"]
    assert cmds(result) == [
        "git fetch --prune",
        "git-sync -s -n check",
        f"git switch -c {WIP}",
        f"git push -u origin HEAD:refs/heads/{WIP}",
        f"git config --bool branch.{WIP}.sync true",
        f"git config --bool branch.{WIP}.syncNewFiles true",
        "git-sync",
    ]
    assert result["saved"] == {"branch": WIP, "files": ["a.txt", "new.txt"],
                               "commit": git(clone, "rev-parse", "--short", "HEAD")}  # fmt: skip
    assert git(clone, "branch", "--show-current") == WIP
    assert head(clone, "main") == main_before == remote_head(origin, "main")
    assert remote_head(origin, WIP) == head(clone)
    assert git(clone, "status", "--porcelain") == ""
    assert git(clone, "log", "-1", "--format=%s").startswith("changes from ")  # git-sync's
    assert config(clone, "branch.main.sync") is None  # never opted in
    assert result["git_sync"]["code"] == 0
    assert_safe(runner)


def test_second_wip_branch_the_same_day_gets_a_suffix(origin, clone):
    (clone / "a.txt").write_text("first\n")
    apply(clone)
    git(clone, "switch", "-q", "main")
    (clone / "a.txt").write_text("second\n")
    result, _ = apply(clone)
    assert result["saved"]["branch"] == f"{WIP}-2"


def test_a_wip_branch_whose_name_is_taken_on_origin_is_renamed_then_pushed(origin, clone):
    # Left by an earlier run: the clone moved to wip/..., but another clone of the
    # repo had pushed that name first, so its push -u is rejected on every run.
    git(origin.seed, "switch", "-q", "-c", WIP)
    theirs = commit(origin.seed, "theirs.txt", "other clone\n")
    git(origin.seed, "push", "-q", "origin", WIP)
    git(clone, "fetch", "-q")
    git(clone, "switch", "-q", "-c", WIP, "main")  # no upstream
    (clone / "mine.txt").write_text("mine\n")

    result, _ = apply(clone)

    assert result["status"] == "updated", result["actions"]
    assert result["branch"] == f"{WIP}-2"
    assert remote_head(origin, f"{WIP}-2") == head(clone)
    assert remote_head(origin, WIP) == theirs  # never overwritten
    assert "mine.txt" in git(clone, "show", "--name-only", "--format=", "HEAD").split()


def test_main_fast_forwards_while_work_moves_to_wip(origin, clone):
    (clone / "a.txt").write_text("edited\n")
    new = origin.advance()
    result, _ = apply(clone)
    assert head(clone, "main") == new
    assert "git fetch . origin/main:main" in cmds(result)


def test_feature_branch_is_opted_in_and_synced(origin, clone):
    feature(clone)
    (clone / "f.txt").write_text("more\n")
    result, runner = apply(clone)
    assert result["status"] == "updated"
    assert remote_head(origin, "feat") == head(clone)
    assert config(clone, "branch.feat.sync") == "true"
    assert config(clone, "branch.feat.syncNewFiles") == "true"
    assert "switch" not in runner.git_subcommands()


def test_local_only_branch_gets_a_remote_branch_then_git_sync(origin, clone):
    git(clone, "switch", "-q", "-c", "idea")
    commit(clone, "i.txt", "idea\n")
    (clone / "more.txt").write_text("more\n")
    result, _ = apply(clone)
    assert "git push -u origin HEAD:refs/heads/idea" in cmds(result)
    assert remote_head(origin, "idea") == head(clone)
    assert result["status"] == "updated"


def test_secret_hit_skips_the_whole_clone(origin, clone, monkeypatch):
    monkeypatch.setattr(save, "MAX_FILE_BYTES", 1000)
    (clone / ".gitignore").write_text("*.log\n")
    (clone / "debug.log").write_text("ignored\n")
    (clone / ".env").write_text("TOKEN=x\n")
    (clone / ".env.example").write_text("TOKEN=x\n")
    (clone / "config.py").write_text("TOKEN = '" + fake_token() + "'\n")
    (clone / "big.bin").write_bytes(b"0" * 2000)
    (clone / "ok.py").write_text("print('hi')\n")
    (clone / "server.pem").write_text("cert\n")
    git(clone, "add", "server.pem")  # staged by hand: still a hit
    origin.advance()  # upstream moved, but the clone must not be touched at all
    before = head(clone)

    result, runner = apply(clone)

    held = {h["path"]: h["reason"] for h in result["held_back"]}
    assert set(held) == {".env", "config.py", "big.bin", "server.pem"}  # not debug.log
    assert held["config.py"] == "contains a secret-looking string"
    assert fake_token() not in str(result)
    assert result["status"] == "held-back" and "clone skipped" in result["note"]
    assert result["saved"] is None and result["actions"] == []
    assert head(clone) == before
    assert runner.git_sync_calls() == []
    assert not {"commit", "add", "fetch", "pull", "push", "switch"} & set(runner.git_subcommands())
    assert_safe(runner)


def test_secret_in_a_never_pushed_commit_is_not_published(origin, clone):
    # The same check as for uncommitted files, before git push -u publishes the
    # branch's commits for the first time.
    git(clone, "switch", "-q", "-c", "try-local")
    commit(clone, "ok.py", "print('hi')\n")
    commit(clone, "config.py", "TOKEN = '" + fake_token() + "'\n")
    git(clone, "rm", "-q", "config.py")  # removed again, but still in a commit
    git(clone, "commit", "-q", "-m", "remove it")
    (clone / "more.txt").write_text("more\n")

    planned = plan(clone)
    result, runner = apply(clone)

    for report in (planned, result):
        assert report["status"] == "held-back", report["actions"]
        (held,) = report["held_back"]
        assert held["path"] == "config.py"
        assert held["reason"].startswith("contains a secret-looking string, in unpushed commit ")
    assert fake_token() not in str(result)
    assert remote_head(origin, "try-local") is None
    assert "push" not in runner.git_subcommands() and runner.git_sync_calls() == []
    assert config(clone, "branch.try-local.sync") is None


def test_secret_in_a_local_commit_on_main_is_not_carried_to_a_wip_branch(origin, clone):
    commit(clone, "deploy.pem", "not really a key\n")  # local commit, never pushed
    (clone / "a.txt").write_text("small edit\n")  # work on main moves to wip/ ...

    result, runner = apply(clone)

    assert result["status"] == "held-back", result["actions"]
    assert [h["path"] for h in result["held_back"]] == ["deploy.pem"]
    assert result["held_back"][0]["reason"].startswith("secret-looking file name, in unpushed")
    assert git(clone, "branch", "--show-current") == "main"
    assert remote_head(origin, WIP) is None
    assert "push" not in runner.git_subcommands() and runner.git_sync_calls() == []


def test_unpushed_scan_checks_names_sizes_and_only_unpushed_commits(origin, clone, monkeypatch):
    monkeypatch.setattr(save, "MAX_FILE_BYTES", 100)
    commit(clone, "fixture.pem", "already on the remote\n")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "switch", "-q", "-c", "x")
    commit(clone, "notes.txt", "same bytes\n")
    commit(clone, ".env", "same bytes\n")  # same blob as notes.txt: still a name hit
    commit(clone, "big.bin", "0" * 200)
    commit(clone, "fixture.pem", "changed in an unpushed commit\n")

    held = save.unpushed_held_back(Git(FakeRunner(), clone))

    assert {h["path"]: h["reason"].split(",")[0] for h in held} == {
        ".env": "secret-looking file name",
        "big.bin": "larger than 0 MB",
        "fixture.pem": "secret-looking file name",
    }


def test_secret_added_in_a_merge_commit_is_not_published(origin, clone):
    # A merge commit's own changes (a conflict resolution, an "evil merge") are
    # published by git push -u like those of any other commit.
    git(clone, "switch", "-q", "-c", "side")
    commit(clone, "side.txt", "side work\n")
    git(clone, "switch", "-q", "-c", "local-only", "main")
    commit(clone, "ok.py", "print('hi')\n")
    git(clone, "merge", "-q", "--no-commit", "side")
    (clone / "config.py").write_text("TOKEN = '" + fake_token() + "'\n")
    git(clone, "add", "config.py")
    git(clone, "commit", "-q", "-m", "merge side")
    (clone / "more.txt").write_text("more\n")

    planned = plan(clone)
    result, runner = apply(clone)

    for report in (planned, result):
        assert report["status"] == "held-back", report["actions"]
        (held,) = report["held_back"]
        assert held["path"] == "config.py"
        assert held["reason"].startswith("contains a secret-looking string, in unpushed commit ")
    assert fake_token() not in str(result)
    assert remote_head(origin, "local-only") is None
    assert "push" not in runner.git_subcommands() and runner.git_sync_calls() == []


def test_unpushed_scan_sees_merge_resolutions_and_type_changes(origin, clone):
    (clone / "settings.py").symlink_to("a.txt")
    git(clone, "add", "settings.py")
    git(clone, "commit", "-q", "-m", "link")
    git(clone, "push", "-q", "origin", "main")  # a published symlink: no content
    git(clone, "switch", "-q", "-c", "side")
    commit(clone, "c.txt", "side\n")
    git(clone, "switch", "-q", "-c", "x", "main")
    (clone / "settings.py").unlink()  # symlink -> regular file: a type change (T)
    commit(clone, "settings.py", "TOKEN = '" + fake_token() + "'\n")
    commit(clone, "c.txt", "mine\n")
    git(clone, "merge", "-q", "--no-commit", "-s", "ours", "side")
    (clone / "c.txt").write_text("resolved " + fake_token() + "\n")  # in neither parent
    (clone / ".env").write_text("added by the merge\n")
    git(clone, "add", "c.txt", ".env")
    git(clone, "commit", "-q", "-m", "merge side")

    held = save.unpushed_held_back(Git(FakeRunner(), clone))

    assert {h["path"]: h["reason"].split(",")[0] for h in held} == {
        ".env": "secret-looking file name",
        "c.txt": "contains a secret-looking string",
        "settings.py": "contains a secret-looking string",
    }


def test_new_files_are_saved_when_git_status_hides_untracked_files(origin, clone):
    feature(clone)
    git(clone, "config", "status.showUntrackedFiles", "no")  # common in big repos
    (clone / "notes.md").write_text("new notes\n")

    result, _ = apply(clone)

    assert result["status"] == "updated", result["actions"]
    assert result["saved"]["files"] == ["notes.md"]
    assert "notes.md" in git(origin.bare, "ls-tree", "--name-only", "feat").split()
    assert git(clone, "status", "--porcelain", "--untracked-files=all") == ""


def test_hidden_untracked_files_are_still_counted(origin, clone):
    feature(clone)
    git(clone, "config", "status.showUntrackedFiles", "no")
    (clone / "notes.md").write_text("new notes\n")
    result, _ = apply(clone, can_push=False)  # left as is: must still need attention
    assert result["untracked"] == 1


def test_ignored_files_are_never_committed(origin, clone):
    feature(clone)
    (clone / ".gitignore").write_text("*.log\n")
    (clone / "debug.log").write_text("ignored\n")
    (clone / ".env.example").write_text("TOKEN=x\n")
    (clone / "ok.py").write_text("print('hi')\n")
    result, _ = apply(clone)
    committed = git(clone, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == [".env.example", ".gitignore", "ok.py"]
    assert result["status"] == "updated"
    assert (clone / "debug.log").exists()


def test_too_many_files_is_not_auto_saved(origin, clone, monkeypatch):
    monkeypatch.setattr(save, "MAX_FILES", 2)
    for n in range(3):
        (clone / f"f{n}.txt").write_text("x\n")
    result, runner = apply(clone)
    assert result["status"] == "held-back"
    assert "over 2" in result["save_skipped"]
    assert runner.git_sync_calls() == []


def test_git_sync_refusal_is_reported_and_nothing_changes(origin, clone):
    feature(clone)
    (clone / "staged.txt").write_text("staged\n")
    git(clone, "add", "staged.txt")  # git-sync refuses staged changes
    before = head(clone)

    result, runner = apply(clone)

    assert result["status"] == "error"
    assert "handle manually" in result["note"]
    assert runner.git_sync_calls() == [["-s", "-n", "check"]]
    assert head(clone) == before
    assert config(clone, "branch.feat.sync") is None
    assert result["git_sync"]["code"] == 1


@pytest.mark.parametrize("where", ["local-only branch", "default branch without upstream"])
def test_git_sync_refusal_without_a_remote_changes_nothing(origin, clone, where):
    # git-sync's check needs branch.<b>.remote; without one it must still run first,
    # before the branch is pushed, switched to wip/ or opted in.
    if where == "local-only branch":
        git(clone, "switch", "-q", "-c", "feat")
    else:
        git(clone, "branch", "--unset-upstream")
    branch = git(clone, "branch", "--show-current")
    (clone / "a.txt").write_text("staged\n")
    git(clone, "add", "a.txt")  # git-sync refuses staged changes
    (clone / "b.txt").write_text("untracked\n")
    remote_before = git(origin.bare, "for-each-ref", "--format=%(refname) %(objectname)")
    config_before = git(clone, "config", "--list", "--local")

    planned = plan(clone)
    result, runner = apply(clone)

    for report in (planned, result):
        assert report["status"] == "error", report["actions"]
        assert "handle manually" in report["note"]
        assert cmds(report)[-1] == "git-sync -s -n check"
    assert runner.git_sync_calls() == [["-s", "-n", "check"]]
    assert git(origin.bare, "for-each-ref", "--format=%(refname) %(objectname)") == remote_before
    assert git(clone, "config", "--list", "--local") == config_before
    assert git(clone, "branch", "--show-current") == branch


def diverge(origin, clone, files_upstream: dict, files_mine: dict):
    """Put Ben's clone on a pushed feature branch, then change both sides."""
    feature(clone)
    for name, text in files_upstream.items():
        origin.advance(name, text, branch="feat")
    for name, text in files_mine.items():
        commit(clone, name, text)
    git(clone, "fetch", "-q")


def test_trivial_conflicts_are_resolved_then_pushed(origin, clone):
    commit(clone, "uv.lock", "lock base\n")
    commit(clone, "style.py", "x = 1\ny = 2\n")
    git(clone, "push", "-q", "origin", "main")
    diverge(origin, clone, {"uv.lock": "lock upstream\n", "style.py": "x = 1   \ny = 2\n"},
            {"uv.lock": "lock mine\n"})  # fmt: skip
    (clone / "style.py").write_bytes(b"x = 1\r\ny = 2\n")
    (clone / "feature.py").write_text("feature = True\n")
    git(clone, "add", "style.py", "feature.py")
    git(clone, "commit", "-q", "-m", "feature plus line endings")
    mine_before = head(clone)
    regenerated = []

    def uv_lock(cmd, cwd):
        regenerated.append(cwd)
        (clone / "uv.lock").write_text("lock regenerated\n")
        return Result(0)

    result, runner = apply(clone, FakeRunner(tools={("uv", "lock"): uv_lock}))

    assert result["status"] == "updated", result["actions"]
    assert {r["path"]: r["how"] for r in result["resolved"]} == {
        "uv.lock": "lockfile",
        "style.py": "whitespace",
    }
    assert regenerated == [str(clone)]
    assert (clone / "uv.lock").read_text() == "lock regenerated\n"
    assert (clone / "style.py").read_bytes() == b"x = 1\r\ny = 2\n"  # mine kept
    assert (clone / "feature.py").exists()
    assert git(clone, "merge-base", "--is-ancestor", "origin/feat", "HEAD") == ""
    assert remote_head(origin, "feat") == head(clone)  # git-sync reran and pushed
    assert len(runner.git_sync_calls()) == 3  # check, sync (stopped), sync (push)
    (backup,) = result["backup_refs"]
    assert re.fullmatch(r"refs/syncghrepo/backup/\d{8}T\d{6}", backup)
    assert head(clone, backup) == mine_before
    assert_safe(runner)


def test_real_conflict_is_aborted_and_nothing_is_lost(origin, clone):
    diverge(origin, clone, {"a.txt": "upstream edit\n"}, {"a.txt": "my edit\n"})
    (clone / "notes.txt").write_text("uncommitted\n")
    remote_before = remote_head(origin, "feat")

    result, runner = apply(clone)

    assert result["status"] == "conflict"
    assert [c["path"] for c in result["conflicts"]] == ["a.txt"]
    assert not (clone / ".git" / "rebase-merge").exists()  # git-sync left it; we aborted
    (backup,) = result["backup_refs"]
    # The backup holds HEAD plus the uncommitted file, i.e. exactly what git-sync committed.
    assert head(clone, f"{backup}^{{tree}}") == head(clone, "HEAD^{tree}")
    assert (clone / "a.txt").read_text() == "my edit\n"
    assert "notes.txt" in git(clone, "show", "--name-only", "--format=", "HEAD")
    assert result["saved"]["commit"] == git(clone, "rev-parse", "--short", "HEAD")
    assert remote_head(origin, "feat") == remote_before  # nothing pushed over upstream
    assert "rebase --abort" in runner.git_commands()
    assert_safe(runner)

    again, _ = apply(clone)  # still conflicted: the same backup is reused, not duplicated
    assert again["backup_refs"] == [backup]
    assert git(clone, "for-each-ref", "refs/syncghrepo").count("\n") == 0


def test_rejected_push_is_reported_never_forced(origin, clone):
    feature(clone)
    hook = origin.bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho rejected by test >&2\nexit 1\n")
    hook.chmod(0o755)
    (clone / "w.txt").write_text("work\n")

    result, runner = apply(clone)

    assert result["status"] == "error"
    assert result["git_sync"]["code"] == 3
    assert remote_head(origin, "feat") != head(clone)
    assert_safe(runner)


def test_dry_run_writes_nothing_and_shows_the_plan(origin, clone):
    commit(clone, "uv.lock", "base\n")
    git(clone, "push", "-q", "origin", "main")
    diverge(origin, clone, {"uv.lock": "upstream\n", "a.txt": "theirs\n"}, {})
    (clone / "uv.lock").write_text("mine\n")
    (clone / "a.txt").write_text("ours\n")

    def fingerprint():
        files = sorted(p for p in (clone / ".git").rglob("*") if p.is_file())
        digest = hashlib.sha256()
        for path in files:
            if path.name != "index":  # git may refresh index stat data on reads
                digest.update(str(path).encode() + path.read_bytes())
        return digest.hexdigest(), git(clone, "status", "--porcelain"), len(files)

    before = fingerprint()
    runner = FakeRunner()
    result = plan(clone, runner, fork="me/proj")

    assert fingerprint() == before
    assert result["status"] == "conflict"
    assert cmds(result) == [
        "gh repo sync me/proj",
        "git-sync -s -n check",
        "git config --bool branch.feat.sync true",
        "git config --bool branch.feat.syncNewFiles true",
        "git update-ref refs/syncghrepo/backup/<ts> <HEAD + 2 uncommitted file(s)>",
        "git-sync",
    ]
    note = result["actions"][-1]["note"]
    assert note.startswith("commit 2 file(s), rebase onto 2 new commit(s), push")
    assert "would stop on conflicts in a.txt" in note
    assert {r["path"]: r["how"] for r in result["resolved"]} == {"uv.lock": "lockfile"}
    assert runner.git_sync_calls() == [["-s", "-n", "check"]]  # read-only mode only
    assert not {"fetch", "pull", "push", "commit", "rebase", "switch"} & set(
        runner.git_subcommands()
    )
    assert not any(c[0] == "gh" for c in runner.calls)


def test_dry_run_on_main_plans_wip_branch(origin, clone):
    (clone / "a.txt").write_text("edited\n")
    result = plan(clone)
    assert cmds(result) == [
        "git-sync -s -n check",
        f"git switch -c {WIP}",
        f"git push -u origin HEAD:refs/heads/{WIP}",
        f"git config --bool branch.{WIP}.sync true",
        f"git config --bool branch.{WIP}.syncNewFiles true",
        "git-sync",
    ]
    assert result["actions"][-1]["note"] == "commit 1 file(s), push"
    assert result["status"] == "would-update"
    assert git(clone, "branch", "--show-current") == "main"


def test_dry_run_secret_skip(origin, clone):
    (clone / ".env").write_text("TOKEN=x\n")
    (clone / "a.txt").write_text("edited\n")
    result = plan(clone)
    assert result["status"] == "held-back"
    assert [h["path"] for h in result["held_back"]] == [".env"]
    assert result["actions"] == []


def test_dry_run_fetch_sees_new_upstream_commits(origin, clone):
    origin.advance()
    assert plan(clone)["status"] == "up-to-date"
    fresh = plan(clone, fetch=True)
    assert fresh["status"] == "would-update"
    assert cmds(fresh) == ["git fetch --prune", "git merge --ff-only origin/main"]


def test_commits_on_main_are_never_pushed(origin, clone):
    commit(clone, "local.txt", "on main\n")
    before = remote_head(origin, "main")
    result, runner = apply(clone)
    assert remote_head(origin, "main") == before
    assert "push" not in runner.git_subcommands() and runner.git_sync_calls() == []
    assert any("not pushed" in a["note"] for a in result["actions"])


def test_deleted_upstream_is_not_recreated(origin, clone):
    feature(clone, "merged")
    git(origin.bare, "branch", "-D", "merged")
    (clone / "late.txt").write_text("late\n")
    result, runner = apply(clone)
    assert remote_head(origin, "merged") is None
    assert runner.git_sync_calls() == []
    assert any("deleted" in a["note"] for a in result["actions"])
    assert (clone / "late.txt").exists()


def test_no_push_access_leaves_work_alone(origin, clone):
    feature(clone)
    (clone / "x.txt").write_text("x\n")
    result, runner = apply(clone, can_push=False)
    assert runner.git_sync_calls() == []
    assert "push" not in runner.git_subcommands()
    assert any("no known push access" in a["note"] for a in result["actions"])
    assert git(clone, "status", "--porcelain") == "?? x.txt"


def test_missing_git_sync_is_an_error(origin, clone):
    feature(clone)
    (clone / "x.txt").write_text("x\n")
    result, _ = apply(clone, git_sync=None)
    assert result["status"] == "error" and "git-sync not found" in result["note"]


def test_fork_is_synced_with_gh(origin, clone):
    def gh_sync(cmd):
        origin.advance("upstream.txt", "from parent\n")
        return Result(0, "", "✓ Synced")

    runner = FakeRunner({("repo", "sync"): gh_sync})
    result, _ = apply(clone, runner, fork="benpshore/proj")
    assert ["gh", "repo", "sync", "benpshore/proj"] in runner.calls
    assert (clone / "upstream.txt").exists()
    assert result["status"] == "updated"


def test_empty_repo_is_skipped(tmp_path):
    repo = tmp_path / "empty"
    git(tmp_path, "init", "-q", str(repo))
    (repo / "x.txt").write_text("x\n")
    result, runner = apply(repo)
    assert result["status"] == "skipped" and "no commits yet" in result["note"]
    assert runner.git_sync_calls() == []


@pytest.mark.parametrize("state", ["detached", "merge"])
def test_busy_or_detached_clones_are_left_alone(origin, clone, state):
    (clone / "a.txt").write_text("edit\n")
    if state == "detached":
        git(clone, "checkout", "-q", "--detach")
    else:
        (clone / ".git" / "MERGE_HEAD").write_text(head(clone) + "\n")
    result, runner = apply(clone)
    assert result["status"] == "skipped"
    assert runner.git_sync_calls() == []
    assert (clone / "a.txt").read_text() == "edit\n"


# Which changed_paths() status calls fail (1: secret check, 2: file limit after the
# fetch), and the untracked file that must not reach the remote. The submodule scan's
# porcelain=v2 status is left alone.
@pytest.mark.parametrize(("fail", "name"), [({1, 2}, ".env"), ({1}, ".env"), ({2}, "x.txt")])
def test_failed_change_list_skips_the_clone(origin, clone, fail, name):
    """If git status cannot list the changed files (before or after the fetch), the
    secret check and the file limit cannot run: the clone is an error and nothing is
    committed or pushed."""
    feature(clone)
    commit(clone, "c.txt", "local\n")
    (clone / name).write_text("X=1\n")
    before = remote_head(origin, "feat")
    local_before = git(clone, "rev-parse", "HEAD")
    calls = []

    def status_fails(cmd):
        if "status" in cmd and "--porcelain=v1" in cmd and "--untracked-files=all" in cmd:
            calls.append(cmd)
            if len(calls) in fail:
                return Result(124, "", "git timed out after 120s")
        return None

    result, runner = apply(clone, FakeRunner(git_hook=status_fails))
    assert result["status"] == "error" and "git status failed" in result["note"]
    assert result["actions"][-1]["ok"] is False
    assert "git status failed" in result["actions"][-1]["note"]
    assert runner.git_sync_calls() == []
    assert git(clone, "rev-parse", "HEAD") == local_before
    assert ("fetch" in runner.git_subcommands()) == (1 not in fail)
    assert remote_head(origin, "feat") == before
    assert name not in git(origin.bare, "ls-tree", "--name-only", "feat")
    assert_safe(runner)


def test_failed_submodule_scan_skips_the_clone(origin, clone):
    """If only the submodule scan's git status fails, a moved submodule could not be
    held back: the clone is an error and nothing is committed or pushed."""
    feature(clone)
    commit(clone, "c.txt", "local\n")
    (clone / "x.txt").write_text("x\n")
    before = remote_head(origin, "feat")
    local_before = git(clone, "rev-parse", "HEAD")

    def scan_fails(cmd):
        if "status" in cmd and "--porcelain=v2" in cmd:
            return Result(124, "", "git timed out after 120s")
        return None

    result, runner = apply(clone, FakeRunner(git_hook=scan_fails))
    assert result["status"] == "error" and "git status failed" in result["note"]
    assert result["actions"] == [
        {
            "cmd": "git status --porcelain=v2 -z --untracked-files=all",
            "ok": False,
            "note": f"{sync.STATUS_FAILED}: git timed out after 120s",
        }
    ]
    assert runner.git_sync_calls() == []
    assert git(clone, "rev-parse", "HEAD") == local_before
    assert remote_head(origin, "feat") == before
    assert "x.txt" not in git(origin.bare, "ls-tree", "--name-only", "feat")
    assert_safe(runner)


@pytest.mark.parametrize(
    ("read", "porcelain"), [(save.changed_paths, "v1"), (save.changed_submodules, "v2")]
)
def test_failed_git_status_raises(tmp_path, read, porcelain):
    def runner(cmd, cwd=None, env=None):
        return Result(128, "", "fatal: index file corrupt\n")

    with pytest.raises(save.StatusFailed) as failed:
        read(Git(runner, tmp_path))
    assert failed.value.cmd == f"git status --porcelain={porcelain} -z --untracked-files=all"
    assert failed.value.detail == "fatal: index file corrupt"
