import time

import pytest
from conftest import FakeRunner, commit, git

from syncghrepo import local


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://github.com/benpshore/syncghrepo.git", "benpshore/syncghrepo"),
        ("https://github.com/benpshore/syncghrepo", "benpshore/syncghrepo"),
        ("https://github.com/benpshore/syncghrepo/", "benpshore/syncghrepo"),
        ("git@github.com:caligoapp/pdf.git", "caligoapp/pdf"),
        ("ssh://git@github.com/benpshore/tz.util.git", "benpshore/tz.util"),
        ("https://ben@github.com/benpshore/x-y_z.git\n", "benpshore/x-y_z"),
        ("https://gitlab.com/benpshore/syncghrepo.git", None),
        ("../remote/proj.git", None),
        (None, None),
    ],
)
def test_github_name(url, name):
    assert local.github_name(url) == name


def test_find_clones_skips_worktrees_hidden_and_deep_dirs(tmp_path):
    root = tmp_path / "code"
    for repo in ("GitHub/a", "GitHub/b", "c", "GitHub/org/deep/too-deep"):
        (root / repo / ".git").mkdir(parents=True)
    (root / "GitHub/a/nested/.git").mkdir(parents=True)  # inside a clone: not searched
    (root / "worktrees/a-feature").mkdir(parents=True)
    (root / "worktrees/a-feature/.git").write_text("gitdir: ../../GitHub/a/.git/worktrees/x\n")
    (root / "GitHub/node_modules/pkg/.git").mkdir(parents=True)
    (root / ".hidden/repo/.git").mkdir(parents=True)

    found = local.find_clones([root, tmp_path / "missing"], max_depth=3)

    assert [p.relative_to(root.resolve()).as_posix() for p in found] == [
        "GitHub/a",
        "GitHub/b",
        "c",
    ]
    assert len(local.find_clones([root], max_depth=4)) == 4
    assert local.find_clones([root / "c"]) == [(root / "c").resolve()]  # a root may be a clone


@pytest.mark.parametrize(
    ("branch", "default", "protected"),
    [("main", "main", True), ("master", None, True), ("trunk", "trunk", True),
     ("main", "trunk", True), ("feat", "main", False), (None, "main", False)],
)  # fmt: skip
def test_is_protected(branch, default, protected):
    assert local.is_protected(branch, default) is protected


def test_every_known_default_branch_is_protected():
    assert local.is_protected("develop", "main", "develop")  # stale origin/HEAD, GitHub's
    assert local.is_protected("develop", None, "develop")  # no origin/HEAD at all
    assert not local.is_protected("feat", "main", "develop")


def test_inspect_reports_branches_stashes_and_worktrees(tmp_path, origin, clone):
    git(clone, "switch", "-q", "-c", "wip")
    commit(clone, "w.txt", "wip\n")
    git(clone, "switch", "-q", "-c", "old", "main")
    old = "2020-01-01T00:00:00"
    commit(clone, "o.txt", "old\n", GIT_COMMITTER_DATE=old, GIT_AUTHOR_DATE=old)
    git(clone, "push", "-q", "-u", "origin", "old")
    git(clone, "switch", "-q", "-c", "merged", "main")
    commit(clone, "m.txt", "merged\n")
    git(clone, "push", "-q", "-u", "origin", "merged")
    git(clone, "push", "-q", "origin", "--delete", "merged")
    git(clone, "fetch", "-q", "--prune")
    assert local.inspect(FakeRunner(), clone)["upstream_gone"] is True
    git(clone, "switch", "-q", "main")
    (clone / "a.txt").write_text("stashed\n")
    git(clone, "stash", "push", "-q", "-m", "half done")
    git(clone, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "side")

    result = local.inspect(FakeRunner(), clone, stale_days=30, now=time.time())

    branches = {b["name"]: b for b in result["branches"]}
    assert set(branches) == {"wip", "old", "merged"}
    assert branches["wip"]["unpushed"] == 1 and branches["wip"]["upstream"] is None
    assert branches["old"]["stale"] and not branches["old"]["unpushed"]
    assert branches["merged"]["gone"] and branches["merged"]["unpushed"] == 1
    (stash,) = result["stashes"]
    assert stash["ref"] == "stash@{0}" and "half done" in stash["message"]
    assert len(stash["commit"]) == 40 and stash["created"].startswith("20")
    assert [p.endswith("wt") for p in result["worktrees"]] == [True]
    assert result["default_branch"] == "main"
    assert result["changes"] == 0 and result["upstream_gone"] is False


@pytest.mark.parametrize(
    ("url", "shown"),
    [("https://x-access-token:tok@github.com/o/r.git", "https://github.com/o/r.git"),
     ("https://tok@github.com/o/r", "https://github.com/o/r"),
     ("http://u:p%40ss@host:8080/r.git", "http://host:8080/r.git"),
     ("ssh://git@github.com/o/r.git", "ssh://git@github.com/o/r.git"),
     ("ssh://ben:pw@host/r.git", "ssh://ben@host/r.git"),
     ("git@github.com:o/r.git", "git@github.com:o/r.git"),
     ("/srv/git/r.git", "/srv/git/r.git"), ("file:///srv/r.git", "file:///srv/r.git"),
     (None, None)],
)  # fmt: skip
def test_redact_url_drops_credentials(url, shown):
    assert local.redact_url(url) == shown
