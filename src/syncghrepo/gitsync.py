"""git-sync (github.com/simonthum/git-sync, CC0) does the auto-commit, fetch,
fast-forward or rebase, and push. syncghrepo does not reimplement it: it only
opts branches in, calls it and reports what happened.

git-sync exit codes: 0 in sync; 1 not opted in, changed files it will not handle
(e.g. staged changes) or a rebase that stopped on conflicts (left in progress);
2 unsafe state (merge/rebase in progress, detached HEAD, no remote); 3 fetch or
push failed.
"""

import os
import shutil
from pathlib import Path

from .run import Result

ENV = "SYNCGHREPO_GIT_SYNC"
# Lines git-sync prints when it changed something.
CHANGED = ("Committing local changes", "Pushing changes", "fast-forwarding", "Rebasing went fine")


def find(explicit: str | None = None) -> str | None:
    """The git-sync executable: explicit, $SYNCGHREPO_GIT_SYNC, PATH, ~/.local/bin."""
    for candidate in (
        explicit,
        os.environ.get(ENV),
        shutil.which("git-sync"),
        str(Path.home() / ".local" / "bin" / "git-sync"),
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def with_config(key: str, value: str) -> dict[str, str]:
    """Environment that adds one git config setting for a command, writing nothing
    (GIT_CONFIG_COUNT, git 2.31+), after any pairs already in the environment."""
    count = os.environ.get("GIT_CONFIG_COUNT", "")
    n = int(count) if count.isdigit() else 0
    return {
        "GIT_CONFIG_COUNT": str(n + 1),
        f"GIT_CONFIG_KEY_{n}": key,
        f"GIT_CONFIG_VALUE_{n}": value,
    }


def summary(result: Result) -> str:
    """git-sync's last own message ("In sync, all fine.", "Rebasing failed, ...")."""
    lines = [line[10:] for line in result.out.splitlines() if line.startswith("git-sync: ")]
    return lines[-1].strip() if lines else result.last_line()


def log(result: Result, keep: int = 12) -> list[str]:
    lines = [line.rstrip() for line in (result.out + result.err).splitlines() if line.strip()]
    return lines[-keep:]


def did_something(result: Result) -> bool:
    return any(marker in result.out for marker in CHANGED)
