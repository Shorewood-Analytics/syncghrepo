import os
from pathlib import Path

import pytest
from conftest import GIT_SYNC

from syncghrepo import gitsync
from syncghrepo.run import Result, run

INSTALLED = Path.home() / ".local" / "bin" / "git-sync"


def test_fixture_is_executable_and_untouched_upstream_script():
    text = GIT_SYNC.read_text()
    assert os.access(GIT_SYNC, os.X_OK)
    assert "Licensed as: CC0" in text and "synchronize tracking repositories" in text


@pytest.mark.skipif(not INSTALLED.exists(), reason="git-sync not installed here")
def test_fixture_matches_the_installed_git_sync():
    assert GIT_SYNC.read_bytes() == INSTALLED.read_bytes()


def test_find_prefers_explicit_then_env(tmp_path, monkeypatch):
    assert gitsync.find(str(GIT_SYNC)) == str(GIT_SYNC)
    monkeypatch.setenv(gitsync.ENV, str(GIT_SYNC))
    assert gitsync.find() == str(GIT_SYNC)
    not_executable = tmp_path / "git-sync"
    not_executable.write_text("#!/bin/sh\n")
    monkeypatch.setenv(gitsync.ENV, str(not_executable))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert gitsync.find() is None


def test_summary_and_did_something():
    out = "git-sync: Mode sync\ngit-sync: Pushing changes...\ngit-sync: In sync, all fine.\n"
    result = Result(0, out, "To github.com:x/y\n")
    assert gitsync.summary(result) == "In sync, all fine."
    assert gitsync.did_something(result)
    assert not gitsync.did_something(Result(0, "git-sync: In sync, all fine.\n"))
    assert gitsync.log(result)[-1] == "To github.com:x/y"


def test_with_config_adds_after_pairs_already_in_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    assert gitsync.with_config("branch.x.remote", "origin") == {
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "branch.x.remote",
        "GIT_CONFIG_VALUE_0": "origin",
    }  # fmt: skip
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Ben")
    env = gitsync.with_config("branch.x.remote", "origin")
    assert env["GIT_CONFIG_COUNT"] == "2" and env["GIT_CONFIG_KEY_1"] == "branch.x.remote"
    seen = run(["git", "config", "--get-regexp", "^(user.name|branch.x.remote)$"], str(tmp_path),
               env=env)  # fmt: skip
    assert seen.out.splitlines() == ["user.name Ben", "branch.x.remote origin"]
