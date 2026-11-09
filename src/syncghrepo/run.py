"""Run commands with a timeout. Every git, gh and lockfile command goes through a Runner.

syncghrepo makes no AI/LLM calls and no network calls of its own: it is plain git,
gh, git-sync, ghq and the standard library. run() enforces that with an allowlist, so nothing
else (e.g. `gh models`, `gh copilot`, curl, an AI CLI) can ever be started.
"""

import os
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

# Never wait on a prompt: no terminal, no credential or update prompts, no editor.
QUIET = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_EDITOR": "true",
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
}


# The only commands syncghrepo may run (matched as argument prefixes).
ALLOWED = (
    ("git",),  # includes git submodule (status, update --init --recursive)
    ("git", "submodule"),  # listed on its own so the allowlist documents it
    ("git-sync",),  # simonthum/git-sync: auto-commit, rebase, push (plain bash + git)
    ("gh", "api"),  # user, user/repos and the fixed GraphQL queries in github.py
    ("gh", "search", "prs"),
    ("gh", "repo", "sync"),
    ("gh", "repo", "clone"),  # only repos of yours with no clone anywhere under the roots
    ("ghq", "list"),  # clone discovery only; never ghq get, never ghq.root changes
    # Lockfile regeneration for conflicts (resolve.LOCKFILES).
    ("uv", "lock"),
    ("cargo", "update", "--workspace"),
    ("npm", "install", "--package-lock-only"),
    ("pnpm", "install", "--lockfile-only"),
    ("bundle", "lock"),
)


def allowed(cmd: Sequence[str]) -> bool:
    """Matched on the program's base name, so /path/to/git-sync counts as git-sync."""
    if not cmd:
        return False
    head = (os.path.basename(cmd[0]), *cmd[1:])
    return any(tuple(head[: len(prefix)]) == prefix for prefix in ALLOWED)


@dataclass(frozen=True)
class Result:
    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0

    def last_line(self) -> str:
        """The most useful single line of output, for short reports."""
        for text in (self.err, self.out):
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if lines:
                return lines[-1][:200]
        return f"exit {self.code}"


# runner(cmd, cwd=None, env=None) -> Result. env adds to the environment. Tests pass a fake.
Runner = Callable[..., Result]


def run(
    cmd: Sequence[str],
    cwd: str | None = None,
    timeout: float = 60.0,
    env: Mapping[str, str] | None = None,
) -> Result:
    """Run cmd; on timeout kill its whole process group. Refuses commands not in ALLOWED."""
    if not allowed(cmd):
        return Result(126, "", f"refused: {' '.join(cmd[:3])} is not an allowed command")
    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            env={**os.environ, **QUIET, **(env or {})},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as err:
        return Result(127, "", f"{cmd[0]}: {err.strerror}")
    with proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return Result(124, "", f"{cmd[0]} timed out after {timeout:g}s")
    # No newline translation, and undecodable bytes survive a round trip:
    # out.encode("utf-8", "surrogateescape") gives back git's exact bytes.
    return Result(proc.returncode, decode(out), decode(err))


def decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


# Commands that may download a whole repo get more time than the default.
LONG_TIMEOUT = 1800.0


def long_running(cmd: Sequence[str]) -> bool:
    """gh repo clone and git submodule update download repos."""
    words = list(cmd)
    if words[:3] == ["gh", "repo", "clone"]:
        return True
    return bool(words) and words[0] == "git" and "submodule" in words and "update" in words


def make_runner(timeout: float) -> Runner:
    def runner(cmd: Sequence[str], cwd: str | None = None, env=None) -> Result:
        limit = max(timeout, LONG_TIMEOUT) if long_running(cmd) else timeout
        return run(cmd, cwd, limit, env)

    return runner


class Git:
    """git in one clone. Read commands never take optional locks or start gc."""

    def __init__(self, runner: Runner, path):
        self.runner = runner
        self.where = str(path)

    def __call__(self, *args: str, env: Mapping[str, str] | None = None) -> Result:
        cmd = ["git", "--no-optional-locks", "-c", "gc.auto=0", *args]
        return self.runner(cmd, self.where, env) if env else self.runner(cmd, self.where)

    def out(self, *args: str, env: Mapping[str, str] | None = None) -> str | None:
        result = self(*args, env=env)
        return result.out.strip() if result.ok else None
