import json
from datetime import datetime

import pytest
from conftest import FakeRunner, commit, fake_token, git, remote_head
from test_github import pr, repo_line, status_payload

from syncghrepo import cli, dash, local, save
from syncghrepo.run import Result

WIP = f"wip/{datetime.now().astimezone().strftime('%Y%m%d')}-{save.host_tag()}"


def gh_replies(**permissions):
    """A fake GitHub where Ben owns proj (cloned locally) and other (not cloned)."""
    status = {
        "benpshore/proj": status_payload(
            "benpshore/proj",
            ci="FAILURE",
            prs=[pr(7, "dependabot")],
            permission=permissions.get("proj", "ADMIN"),
        ),
        "benpshore/other": status_payload("benpshore/other"),
    }

    def graphql(cmd):
        owner = next(a for a in cmd if a.startswith("owner="))[6:]
        name = next(a for a in cmd if a.startswith("name="))[5:]
        return Result(0, status[f"{owner}/{name}"])

    return {
        ("api", "user"): Result(0, "benpshore\n"),
        ("api", "--paginate"): Result(
            0, repo_line("benpshore/proj") + "\n" + repo_line("benpshore/other") + "\n"
        ),
        ("search", "prs"): Result(0, ""),
        ("api", "graphql"): graphql,
    }


def point_origin_at_github(clone, name="benpshore/proj"):
    """The clone looks like a GitHub clone, but fetches and pushes to the local bare repo."""
    bare = git(clone, "remote", "get-url", "origin")
    url = f"https://github.com/{name}.git"
    git(clone, "remote", "set-url", "origin", url)
    git(clone, "config", f"url.{bare}.insteadOf", url)


def test_dry_run_is_a_read_only_preview(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    (clone / "a.txt").write_text("edited\n")
    (tmp_path / "code" / "plain").mkdir()
    git(tmp_path / "code" / "plain", "init", "-q")
    runner = FakeRunner(gh_replies())

    code = cli.main(["--dry-run", "--json"], runner=runner)

    assert code == 1  # uncommitted work and failing CI need attention
    report = json.loads(capsys.readouterr().out)
    assert report["user"] == "benpshore" and report["dry_run"] is True
    repos = {r["name"]: r for r in report["repos"]}
    assert set(repos) == {"benpshore/proj", "benpshore/other", "plain"}
    proj = repos["benpshore/proj"]["clones"][0]
    assert proj["status"] == "would-update"
    planned = [a["cmd"] for a in proj["actions"]]
    assert planned[-1] == "git-sync"
    assert f"git push -u origin HEAD:refs/heads/{WIP}" in planned
    assert git(clone, "branch", "--show-current") == "main"
    assert git(clone, "diff", "--name-only") == "a.txt"  # still uncommitted
    assert git(clone, "diff", "--cached", "--name-only") == ""
    attention = repos["benpshore/proj"]["attention"]
    assert any(item.startswith("CI failure on main") for item in attention)
    assert any("PR #7 by dependabot [needs review]" in item for item in attention)
    assert repos["plain"]["github"] is False and repos["plain"]["remote"] is None
    assert report["summary"]["saved_files"] == 1
    assert report["summary"]["not_cloned"] == 1
    # Read-only: no fetch/pull/push/commit, and only read-only gh calls.
    assert not {"fetch", "pull", "push", "commit"} & set(runner.git_subcommands())
    assert {tuple(c[1:3]) for c in runner.calls if c[0] == "gh"} <= {
        ("api", "user"),
        ("api", "--paginate"),
        ("search", "prs"),
        ("api", "graphql"),
    }
    # No gh-dash config either.
    target = dash.config_dir() / "config.yml"
    assert report["gh_dash"] == {"path": str(target), "written": False, "existing_config": False}
    assert not target.parent.exists()


def test_one_command_saves_syncs_refreshes_dash_and_reports(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    new_main = origin.advance()
    (clone / "a.txt").write_text("edited\n")

    code = cli.main([], runner=FakeRunner(gh_replies()))

    out = capsys.readouterr().out
    assert code == 1, out  # saved and synced; only the failing CI is left
    assert git(clone, "rev-parse", "main") == new_main
    assert remote_head(origin, WIP) == git(clone, "rev-parse", "HEAD")
    assert out.splitlines()[0].endswith(" · synced")
    assert "Clones: 1 updated" in out
    assert "Saved: 1 file(s), 0 held back" in out
    assert "Done:" in out and "ok git-sync  # In sync, all fine." in out
    assert "CI failure on main (abcdef1)" in out
    assert "Not cloned, nothing open (1): benpshore/other" in out
    config = dash.config_dir() / "config.yml"
    assert f"gh-dash config: wrote {local.short_path(config)}; review with: gh dash\n" in out
    text = config.read_text()
    assert text.startswith(dash.HEADER)
    scope = " repo:benpshore/other repo:benpshore/proj"
    for title, filters in [*dash.PR_SECTIONS, *dash.ISSUE_SECTIONS]:
        assert f'  - title: "{title}"\n    filters: "{filters}{scope}"\n' in text
    assert f'repoPaths:\n  "benpshore/proj": "{local.short_path(clone.resolve())}"\n' in text
    assert text.endswith("defaults:\n  view: prs\n")


def test_gh_dash_config_is_refreshed_every_run(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    config = dash.config_dir() / "config.yml"
    cli.main(["--json"], runner=FakeRunner(gh_replies()))
    first = json.loads(capsys.readouterr().out)["gh_dash"]
    assert first == {"path": str(config), "written": True, "existing_config": False}
    config.write_text(config.read_text().replace("My drafts", "stale"))
    cli.main(["--json"], runner=FakeRunner(gh_replies()))
    again = json.loads(capsys.readouterr().out)["gh_dash"]
    assert again == first  # our own file: refreshed in place
    assert "My drafts" in config.read_text() and "stale" not in config.read_text()
    assert not config.with_name("config.syncghrepo.yml").exists()


def test_your_own_gh_dash_config_is_never_touched(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    mine = dash.config_dir() / "config.yml"
    mine.parent.mkdir(parents=True)
    mine.write_text("prSections: []\n")
    for _ in range(2):
        cli.main(["--json"], runner=FakeRunner(gh_replies()))
        report = json.loads(capsys.readouterr().out)
    assert mine.read_text() == "prSections: []\n"
    written = mine.with_name("config.syncghrepo.yml")
    assert report["gh_dash"] == {"path": str(written), "written": True, "existing_config": True}
    assert "My drafts" in written.read_text()
    cli.main([], runner=FakeRunner(gh_replies()))
    assert f"review with: gh dash --config {local.short_path(written)}" in capsys.readouterr().out


def test_all_fine_exits_0(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    replies = gh_replies()
    passing = status_payload("benpshore/proj", ci="SUCCESS")
    replies[("api", "graphql")] = lambda cmd: Result(0, passing)
    assert cli.main(["--dry-run"], runner=FakeRunner(replies)) == 0
    assert cli.main([], runner=FakeRunner(replies)) == 0


def test_read_only_permission_means_no_push(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    git(clone, "switch", "-q", "-c", "feat")
    (clone / "a.txt").write_text("edited\n")
    runner = FakeRunner(gh_replies(proj="READ"))
    assert cli.main([], runner=runner) == 1  # the work is still uncommitted
    assert "push" not in runner.git_subcommands()
    assert "not synced: no known push access; local work left as is" in capsys.readouterr().out


def test_githubs_default_branch_is_protected_when_origin_head_is_stale(origin, clone, capsys):
    """origin/HEAD is set at clone time and git fetch never moves it, so GitHub's
    default branch must be protected too: work on it goes to a wip/ branch."""
    point_origin_at_github(clone)
    git(origin.seed, "push", "-q", "origin", "main:develop")
    git(origin.bare, "symbolic-ref", "HEAD", "refs/heads/develop")  # default changed on GitHub
    git(clone, "fetch", "-q")
    git(clone, "switch", "-q", "develop")  # tracks origin/develop
    commit(clone, "local.txt", "not pushed\n")
    (clone / "a.txt").write_text("half-done\n")
    develop, mine = remote_head(origin, "develop"), git(clone, "rev-parse", "HEAD")
    payload = status_payload("benpshore/proj", default="develop")
    replies = gh_replies() | {("api", "graphql"): lambda cmd: Result(0, payload)}

    cli.main(["--json"], runner=FakeRunner(replies))

    report = json.loads(capsys.readouterr().out)
    (proj,) = next(r for r in report["repos"] if r["name"] == "benpshore/proj")["clones"]
    assert proj["default_branch"] == "main"  # the stale origin/HEAD
    assert remote_head(origin, "develop") == develop  # nothing pushed to the default branch
    assert git(clone, "rev-parse", "develop") == mine  # no auto-commit on it either
    assert proj["branch"] == WIP and remote_head(origin, WIP) == git(clone, "rev-parse", "HEAD")
    assert "branch.develop.sync" not in git(clone, "config", "--list")  # never opted in


def test_credentials_in_remote_urls_never_reach_the_report(tmp_path, origin, clone, capsys):
    git(clone, "remote", "set-url", "origin",
        f"https://x-access-token:{fake_token()}@github.com/benpshore/proj.git")  # fmt: skip
    plain = tmp_path / "code" / "plain"
    git(tmp_path, "init", "-q", str(plain))
    commit(plain, "x.txt", "x\n")
    git(plain, "remote", "add", "origin", "https://alice:hunter2@git.example.invalid/a/p.git")

    cli.main(["--dry-run", "--json"], runner=FakeRunner(gh_replies()))

    out = capsys.readouterr().out
    assert fake_token() not in out and "hunter2" not in out
    repos = {r["name"]: r for r in json.loads(out)["repos"]}
    assert repos["plain"]["url"] == "https://git.example.invalid/a/p.git"
    assert repos["plain"]["clones"][0]["origin"] == "https://git.example.invalid/a/p.git"
    proj = repos["benpshore/proj"]["clones"][0]["origin"]
    assert proj == "https://github.com/benpshore/proj.git"


def test_conflict_exits_1(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    git(clone, "switch", "-q", "-c", "feat")
    git(clone, "push", "-q", "-u", "origin", "feat")
    origin.advance("a.txt", "theirs\n", branch="feat")
    (clone / "a.txt").write_text("mine\n")

    code = cli.main([], runner=FakeRunner(gh_replies()))

    out = capsys.readouterr().out
    assert code == 1
    assert "conflict needs a person: a.txt" in out
    assert "backup: refs/syncghrepo/backup/" in out


def test_someone_elses_clone_only_shows_my_prs(tmp_path, origin, clone, capsys):
    git(clone, "remote", "set-url", "origin", "git@github.com:stranger/tool.git")
    payload = status_payload(
        "stranger/tool", ci="FAILURE", prs=[pr(1, "dependabot"), pr(2)], permission="READ"
    )
    replies = gh_replies() | {("api", "graphql"): lambda cmd: Result(0, payload)}
    cli.main(["--dry-run", "--json"], runner=FakeRunner(replies))
    repos = json.loads(capsys.readouterr().out)["repos"]
    tool = next(r for r in repos if r["name"] == "stranger/tool")
    assert tool["sources"] == ["local"]
    assert [p["number"] for p in tool["remote"]["prs"]] == [2]
    assert tool["attention"] == ["PR #2 [mine]: PR 2"]


def test_gh_unavailable_still_reports_local_and_exits_2(tmp_path, clone, capsys):
    runner = FakeRunner({("api", "user"): Result(4, "", "gh auth login required")})
    code = cli.main(["--dry-run"], runner=runner)
    out = capsys.readouterr().out
    assert code == 2
    assert "gh not usable" in out
    assert "Clean (1): proj" in out


def use_home(monkeypatch, tmp_path):
    """No $SYNCGHREPO_ROOTS, so roots are ghq + ~/code, with ~ in tmp_path."""
    monkeypatch.delenv("SYNCGHREPO_ROOTS")
    monkeypatch.setenv("HOME", str(tmp_path))


def test_ghq_clones_are_added_to_code_by_default(tmp_path, origin, clone, monkeypatch, capsys):
    use_home(monkeypatch, tmp_path)
    managed = tmp_path / "ghq" / "github.com" / "benpshore" / "proj"
    git(tmp_path, "clone", "-q", str(origin.bare), str(managed))
    listing = f"{managed}\n{clone}\n{tmp_path / 'ghq' / 'gone'}\n"  # dupes and stale entries
    runner = FakeRunner(gh_replies(), ghq=Result(0, listing))
    cli.main(["--dry-run", "--json"], runner=runner)
    report = json.loads(capsys.readouterr().out)
    assert report["roots"] == [str(tmp_path / "code")]
    assert report["ghq_clones"] == 2
    paths = sorted(c["path"] for r in report["repos"] for c in r["clones"])
    assert paths == sorted([str(managed.resolve()), str(clone.resolve())])
    assert ["ghq", "list", "-p"] in runner.calls


def test_roots_from_environment_replace_ghq_and_code(tmp_path, clone, capsys):
    runner = FakeRunner(gh_replies(), ghq=Result(0, "/elsewhere/repo\n"))
    cli.main(["--dry-run", "--json"], runner=runner)
    report = json.loads(capsys.readouterr().out)
    assert report["roots"] == [str(tmp_path / "code")] and report["ghq_clones"] is None
    assert not any(c[0] == "ghq" for c in runner.calls)


def test_ghq_missing_or_failing(tmp_path, clone, monkeypatch, capsys):
    use_home(monkeypatch, tmp_path)
    missing = FakeRunner(gh_replies(), ghq=Result(127, "", "ghq: No such file or directory"))
    cli.main(["--dry-run", "--json"], runner=missing)
    report = json.loads(capsys.readouterr().out)
    assert report["ghq_clones"] is None and report["errors"] == []
    broken = FakeRunner(gh_replies(), ghq=Result(1, "", "boom"))
    assert cli.main(["--dry-run", "--json"], runner=broken) == 2
    assert json.loads(capsys.readouterr().out)["errors"] == ["ghq list -p failed: boom"]


def test_the_three_flags():
    assert cli.parse_args([]).dry_run is False  # the bare command does it all
    assert cli.parse_args([]).api is False
    assert cli.parse_args(["--dry-run"]).dry_run is True
    args = cli.parse_args(["--json", "--dry-run", "--api"])
    assert args.json is True and args.dry_run is True and args.api is True


@pytest.mark.parametrize(
    "argv",
    [["sync"], ["dash"], ["status"], ["--apply"], ["--fetch"], ["--root", "/x"],
     ["--no-github"], ["--git-sync", "/x"], ["--depth", "2"], ["--jobs", "1"]],
)  # fmt: skip
def test_subcommands_and_old_flags_are_usage_errors(argv, capsys):
    with pytest.raises(SystemExit) as exit:
        cli.parse_args(argv)
    assert exit.value.code == 2


def test_help_describes_the_one_command(capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])
    out = capsys.readouterr().out
    assert "usage: syncghrepo [-h] [--dry-run] [--json] [--api] [--version]\n" in out
    for text in ("--dry-run", "--json", "--api", "git-sync", "gh repo sync", "gh-dash",
                 "gh repo clone", "git submodule update --init --recursive",
                 "cannot save or sync local", "SYNCGHREPO_ROOTS", "SYNCGHREPO_GIT_SYNC",
                 "Exit status: 0"):  # fmt: skip
        assert text in out


def test_env_roots(monkeypatch):
    monkeypatch.setenv("SYNCGHREPO_ROOTS", "/a:/b")
    assert cli.env_roots() == ["/a", "/b"]
    monkeypatch.delenv("SYNCGHREPO_ROOTS")
    assert cli.env_roots() == []
