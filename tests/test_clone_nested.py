"""Cloning missing repos, and syncing repos nested in clones (nested clones and
submodules). gh is a fake; gh repo clone clones a local bare repo."""

import json
import subprocess
from pathlib import Path

from conftest import FakeRunner, commit, git
from test_cli import point_origin_at_github, use_home
from test_github import repo_line, status_payload
from test_schema import CLONE_RECORD, CLONE_RECORD_STATUS, NESTED

from syncghrepo import cli, nested, save
from syncghrepo.run import Result


def bare_repo(tmp_path, name):
    """A bare 'GitHub' repo with one commit."""
    bare = tmp_path / "remote" / f"{name}.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seeds" / name
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    commit(seed, "README.md", f"{name}\n")
    git(seed, "push", "-q", "origin", "main")
    return bare


def fake_github(bares, *, archived=(), others=(), ci="SUCCESS"):
    """Ben owns the repos in bares (name -> bare repo); others are not his."""
    names = [*bares, *others]
    lines = "\n".join(
        repo_line(name, archived=name in archived, owner_type="User") for name in names
    )

    def graphql(cmd):
        owner = next(a for a in cmd if a.startswith("owner="))[6:]
        name = next(a for a in cmd if a.startswith("name="))[5:]
        return Result(0, status_payload(f"{owner}/{name}", ci=ci))

    def clone(cmd):
        name, path = cmd[3], cmd[4]
        done = subprocess.run(["git", "clone", "-q", str(bares[name]), path], check=False)
        if done.returncode == 0:
            point_origin_at_github(Path(path), name)
        return Result(done.returncode, "", "" if done.returncode == 0 else "clone failed")

    return {
        ("api", "user"): Result(0, "benpshore\n"),
        ("api", "--paginate"): Result(0, lines + "\n"),
        ("search", "prs"): Result(0, ""),
        ("api", "graphql"): graphql,
        ("repo", "clone"): clone,
    }


def nested_clone(tmp_path, parent, rel, name):
    """A clone of benpshore/<name> inside parent/rel (kept out of the parent's git)."""
    bare = bare_repo(tmp_path, name)
    path = parent / rel
    git(tmp_path, "clone", "-q", str(bare), str(path))
    point_origin_at_github(path, f"benpshore/{name}")
    with (parent / ".git" / "info" / "exclude").open("a") as exclude:
        exclude.write(f"/{rel}/\n")
    return bare, path


def clone_world(tmp_path, origin, clone, monkeypatch):
    """~ is tmp_path: proj is cloned (with a nested clone of inner), viaghq is a ghq
    clone; other and arch (archived) have no clone; ~/code/GitHub/taken is a folder."""
    use_home(monkeypatch, tmp_path)
    point_origin_at_github(clone)
    bares = {"benpshore/proj": origin.bare}
    bares["benpshore/inner"], _ = nested_clone(tmp_path, clone, "tools/inner", "inner")
    for name in ("viaghq", "other", "arch", "taken"):
        bares[f"benpshore/{name}"] = bare_repo(tmp_path, name)
    managed = tmp_path / "ghq" / "github.com" / "benpshore" / "viaghq"
    git(tmp_path, "clone", "-q", str(bares["benpshore/viaghq"]), str(managed))
    point_origin_at_github(managed, "benpshore/viaghq")
    taken = tmp_path / "code" / "GitHub" / "taken"
    taken.mkdir()
    (taken / "notes.txt").write_text("mine\n")
    replies = fake_github(bares, archived={"benpshore/arch"}, others=["friend/shared"])
    return replies, Result(0, f"{managed}\n")


def clone_calls(runner):
    return [c[3:] for c in runner.calls if c[:3] == ["gh", "repo", "clone"]]


def test_dry_run_lists_exactly_what_would_be_cloned_and_where(
    tmp_path, origin, clone, monkeypatch, capsys
):
    replies, ghq = clone_world(tmp_path, origin, clone, monkeypatch)
    runner = FakeRunner(replies, ghq=ghq)

    code = cli.main(["--dry-run", "--json"], runner=runner)

    report = json.loads(capsys.readouterr().out)
    base = tmp_path / "code" / "GitHub"
    assert [(r["name"], r["status"], r["path"], r["archived"]) for r in report["cloned"]] == [
        ("benpshore/arch", "would-clone", str(base / "arch"), True),
        ("benpshore/other", "would-clone", str(base / "other"), False),
        ("benpshore/taken", "skipped", str(base / "taken"), False),
    ]
    assert all(set(r) == CLONE_RECORD and r["status"] in CLONE_RECORD_STATUS
               for r in report["cloned"])  # fmt: skip
    assert "is not a clone of benpshore/taken" in report["cloned"][2]["note"]
    assert clone_calls(runner) == []
    assert not (base / "other").exists() and not (base / "arch").exists()
    assert report["summary"]["new_clones"] == 2 and report["summary"]["clones_skipped"] == 1
    assert "clone_skipped:benpshore/taken" in [i["id"] for i in report["items"]]
    assert code == 1  # the occupied folder needs a person

    cli.main(["--dry-run"], runner=FakeRunner(replies, ghq=ghq))
    out = capsys.readouterr().out
    assert "  - gh repo clone benpshore/arch ~/code/GitHub/arch [archived]\n" in out
    assert "  - gh repo clone benpshore/other ~/code/GitHub/other\n" in out
    assert "  skipped benpshore/taken: ~/code/GitHub/taken exists and is not a clone" in out


def test_missing_repos_are_cloned_once_and_nothing_is_overwritten(
    tmp_path, origin, clone, monkeypatch, capsys
):
    replies, ghq = clone_world(tmp_path, origin, clone, monkeypatch)
    runner = FakeRunner(replies, ghq=ghq)
    base = tmp_path / "code" / "GitHub"

    cli.main(["--json"], runner=runner)

    report = json.loads(capsys.readouterr().out)
    assert sorted(clone_calls(runner)) == [
        ["benpshore/arch", str(base / "arch")],
        ["benpshore/other", str(base / "other")],
    ]
    assert {r["name"]: r["status"] for r in report["cloned"]} == {
        "benpshore/arch": "cloned",
        "benpshore/other": "cloned",
        "benpshore/taken": "skipped",
    }
    assert (base / "other" / "README.md").read_text() == "other\n"
    assert (base / "taken" / "notes.txt").read_text() == "mine\n"
    assert sorted(p.name for p in (base / "taken").iterdir()) == ["notes.txt"]
    repos = {r["name"]: r for r in report["repos"]}
    assert [c["path"] for c in repos["benpshore/other"]["clones"]] == [str(base / "other")]
    assert repos["friend/shared"]["clones"] == []  # not Ben's: never cloned

    again = FakeRunner(replies, ghq=ghq)
    cli.main(["--json"], runner=again)
    report = json.loads(capsys.readouterr().out)
    assert clone_calls(again) == []
    assert [r["name"] for r in report["cloned"]] == ["benpshore/taken"]


def test_no_cloning_with_explicit_roots(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    bares = {"benpshore/proj": origin.bare, "benpshore/other": bare_repo(tmp_path, "other")}
    runner = FakeRunner(fake_github(bares))
    cli.main(["--json"], runner=runner)
    assert json.loads(capsys.readouterr().out)["cloned"] == []
    assert clone_calls(runner) == []


def test_nested_clone_is_synced_with_the_same_rules(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    inner_bare, inner = nested_clone(tmp_path, clone, "tools/inner", "inner")
    git(inner, "switch", "-q", "-c", "feat")
    git(inner, "push", "-q", "-u", "origin", "feat")
    (inner / "README.md").write_text("edited\n")
    replies = fake_github({"benpshore/proj": origin.bare, "benpshore/inner": inner_bare})

    cli.main(["--dry-run", "--json"], runner=FakeRunner(replies))
    report = json.loads(capsys.readouterr().out)
    (entry,) = report["nested"]
    assert set(entry) == NESTED
    assert entry == {"path": str(inner.resolve()), "parent": str(clone.resolve()),
                     "repo": "benpshore/inner", "submodule": False,
                     "status": "would-update"}  # fmt: skip
    assert git(inner, "status", "--porcelain") == "M README.md"

    runner = FakeRunner(replies)
    cli.main(["--json"], runner=runner)
    report = json.loads(capsys.readouterr().out)
    assert report["nested"][0]["status"] == "updated"
    assert git(inner, "status", "--porcelain") == ""
    assert git(inner_bare, "rev-parse", "refs/heads/feat") == git(inner, "rev-parse", "HEAD")
    repos = {r["name"]: r for r in report["repos"]}
    (inner_clone,) = repos["benpshore/inner"]["clones"]
    assert inner_clone["nested_in"] == str(clone.resolve()) and not inner_clone["submodule"]
    (top,) = repos["benpshore/proj"]["clones"]
    assert top["nested_in"] is None and top["status"] == "up-to-date"
    assert "submodule" not in runner.git_subcommands()  # no .gitmodules anywhere


def test_untracked_nested_repo_holds_back_its_parent(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    inner_bare, _ = nested_clone(tmp_path, clone, "tools/inner", "inner")
    (clone / ".git" / "info" / "exclude").write_text("")  # now untracked in proj
    before = git(clone, "rev-parse", "HEAD")
    replies = fake_github({"benpshore/proj": origin.bare, "benpshore/inner": inner_bare})

    cli.main(["--json"], runner=FakeRunner(replies))

    repos = {r["name"]: r for r in json.loads(capsys.readouterr().out)["repos"]}
    (top,) = repos["benpshore/proj"]["clones"]
    assert top["status"] == "held-back"
    assert top["held_back"] == [{"path": "tools/inner/", "reason": save.NESTED_SYNCED}]
    assert git(clone, "rev-parse", "HEAD") == before  # never embedded as a gitlink
    assert git(clone, "branch", "--show-current") == "main"
    assert repos["benpshore/inner"]["clones"][0]["status"] == "up-to-date"


def test_a_nested_repo_that_is_also_top_level_is_processed_once(
    tmp_path, origin, clone, monkeypatch, capsys
):
    point_origin_at_github(clone)
    inner_bare, inner = nested_clone(tmp_path, clone, "tools/inner", "inner")
    monkeypatch.setenv("SYNCGHREPO_ROOTS", f"{tmp_path / 'code'}:{inner}")
    replies = fake_github({"benpshore/proj": origin.bare, "benpshore/inner": inner_bare})
    cli.main(["--dry-run", "--json"], runner=FakeRunner(replies))
    report = json.loads(capsys.readouterr().out)
    assert report["nested"] == []
    paths = [c["path"] for r in report["repos"] for c in r["clones"]]
    assert sorted(paths) == sorted([str(clone.resolve()), str(inner.resolve())])


def allow_file_submodules(monkeypatch):
    """Submodule URLs here are local paths, which git refuses by default."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


def super_clone(tmp_path, origin, monkeypatch):
    """A fresh clone of proj, whose main records a submodule libs/subby (not initialized)."""
    allow_file_submodules(monkeypatch)
    sub = bare_repo(tmp_path, "subby")
    git(origin.seed, "submodule", "add", "-q", str(sub), "libs/subby")
    git(origin.seed, "commit", "-q", "-m", "add subby")
    git(origin.seed, "push", "-q", "origin", "main")
    path = tmp_path / "code" / "GitHub" / "super"
    git(tmp_path, "clone", "-q", str(origin.bare), str(path))
    point_origin_at_github(path)
    return path


def test_submodules_are_initialized_and_reported(tmp_path, origin, monkeypatch, capsys):
    path = super_clone(tmp_path, origin, monkeypatch)
    replies = fake_github({"benpshore/proj": origin.bare})

    assert cli.main(["--dry-run", "--json"], runner=FakeRunner(replies)) == 0
    report = json.loads(capsys.readouterr().out)
    (top,) = report["repos"][0]["clones"]
    assert top["actions"] == [
        {"cmd": nested.UPDATE, "ok": None, "note": "init 1, update 0 submodule(s)"}
    ]
    assert top["status"] == "would-update"
    assert not (path / "libs" / "subby" / ".git").exists()

    assert cli.main(["--json"], runner=FakeRunner(replies)) == 0
    report = json.loads(capsys.readouterr().out)
    assert (path / "libs" / "subby" / "README.md").read_text() == "subby\n"
    assert report["nested"] == [{"path": str((path / "libs" / "subby").resolve()),
                                 "parent": str(path.resolve()), "repo": "subby",
                                 "submodule": True, "status": "up-to-date"}]  # fmt: skip
    top = next(c for r in report["repos"] for c in r["clones"] if c["path"] == str(path.resolve()))
    assert top["actions"][-1]["cmd"] == nested.UPDATE and top["actions"][-1]["ok"] is True


def test_submodule_update_never_moves_local_work(tmp_path, origin, monkeypatch, capsys):
    path = super_clone(tmp_path, origin, monkeypatch)
    git(path, "submodule", "update", "-q", "--init")
    subby = path / "libs" / "subby"
    git(subby, "switch", "-q", "-c", "work")
    commit(subby, "w.txt", "work\n")
    replies = fake_github({"benpshore/proj": origin.bare})

    cli.main(["--dry-run", "--json"], runner=FakeRunner(replies))

    report = json.loads(capsys.readouterr().out)
    top = next(c for r in report["repos"] for c in r["clones"] if c["path"] == str(path.resolve()))
    update = next(a for a in top["actions"] if a["cmd"] == nested.UPDATE)
    assert update["ok"] is None
    assert update["note"] == "not run, it could move local work: libs/subby (on branch work)"
    assert git(subby, "branch", "--show-current") == "work"


def parent_clone(report, path):
    return next(c for r in report["repos"] for c in r["clones"] if c["path"] == str(path.resolve()))


def test_files_changed_inside_a_submodule_do_not_hold_back_its_parent(
    tmp_path, origin, monkeypatch, capsys
):
    path = super_clone(tmp_path, origin, monkeypatch)
    git(path, "submodule", "update", "-q", "--init")
    git(path, "switch", "-q", "-c", "feat")
    git(path, "push", "-q", "-u", "origin", "feat")
    subby = path / "libs" / "subby"
    (subby / "notes.txt").write_text("scratch\n")  # untracked inside the submodule
    (subby / "README.md").write_text("edited\n")  # and a tracked file edited there
    (path / "a.txt").write_text("parent work\n")
    replies = fake_github({"benpshore/proj": origin.bare})

    for _ in range(2):  # and again: nothing left behind that blocks the next run
        cli.main(["--json"], runner=FakeRunner(replies))
        top = parent_clone(json.loads(capsys.readouterr().out), path)
        assert top["held_back"] == [] and top["status"] != "error", top["actions"]
    assert git(origin.bare, "rev-parse", "refs/heads/feat") == git(path, "rev-parse", "HEAD")
    assert git(path, "show", "--name-only", "--format=", "HEAD") == "a.txt"
    assert git(subby, "status", "--porcelain") == "M README.md\n?? notes.txt"  # untouched


def test_a_moved_submodule_holds_back_its_parent_with_its_own_reason(
    tmp_path, origin, monkeypatch, capsys
):
    # git add -A would commit the submodule's new commit as the pointer, which could
    # undo a pointer update the clone pulled; the parent waits for a person.
    path = super_clone(tmp_path, origin, monkeypatch)
    git(path, "submodule", "update", "-q", "--init")
    subby = path / "libs" / "subby"
    git(subby, "switch", "-q", "-c", "work")
    commit(subby, "w.txt", "work\n")
    (path / "a.txt").write_text("parent work\n")
    before = git(path, "rev-parse", "HEAD")

    cli.main(["--json"], runner=FakeRunner(fake_github({"benpshore/proj": origin.bare})))

    top = parent_clone(json.loads(capsys.readouterr().out), path)
    assert top["status"] == "held-back"
    assert top["held_back"] == [{"path": "libs/subby", "reason": save.SUBMODULE_MOVED}]
    assert "secret-looking" not in top["note"]
    assert git(path, "rev-parse", "HEAD") == before


def test_an_untracked_nested_repo_that_is_not_synced_is_not_called_synced(
    tmp_path, origin, clone, capsys
):
    point_origin_at_github(clone)
    inner = clone / "vendor" / "inner"  # vendor/ is not searched for nested repos
    inner.mkdir(parents=True)
    git(inner, "init", "-q", "-b", "main")
    commit(inner, "x.txt", "x\n")
    (inner / "unsaved.txt").write_text("unsaved\n")

    cli.main(["--json"], runner=FakeRunner(fake_github({"benpshore/proj": origin.bare})))

    report = json.loads(capsys.readouterr().out)
    assert report["nested"] == []  # not discovered, so not synced
    top = parent_clone(report, clone)
    assert top["held_back"] == [{"path": "vendor/inner/", "reason": save.NESTED_UNSYNCED}]
