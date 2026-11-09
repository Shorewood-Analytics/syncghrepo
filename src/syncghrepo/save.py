"""What auto-save may touch: the secret check, and wip/ branch names.

git-sync makes the commits (git add -A, so ignored files are never added). Before
that, if any changed file is secret-looking (by name or content) or very large, the
whole clone is skipped and the files reported: nothing is committed, synced or
pushed there until a person looks.
"""

import re
import socket
import stat
from pathlib import Path

from .run import Git

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_FILES = 2000  # more than this usually means a missing .gitignore, not work
SECRET_NAME = re.compile(
    r"(^|/)(\.env(\.(?!example$|sample$|template$)[^/]+)?|\.envrc|\.netrc|\.pgpass"
    r"|\.npmrc|\.pypirc|credentials(\.json)?|secrets?\.[^/]+|id_(rsa|dsa|ecdsa|ed25519)"
    r"|[^/]+\.(pem|key|p12|pfx|jks|keystore|ppk|db|sqlite3?|duckdb))$",
    re.IGNORECASE,
)
SECRET_TEXT = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    rb"|\bgh[pousr]_[A-Za-z0-9]{36,}"
    rb"|\bgithub_pat_[A-Za-z0-9_]{40,}"
    rb"|\bglpat-[A-Za-z0-9_-]{20,}"
    rb"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    rb"|\bxox[abposr]-[A-Za-z0-9-]{10,}"
    rb"|\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}"
    rb"|\bAIza[0-9A-Za-z_-]{35}"
)


def changed_paths(git: Git) -> list[str] | None:
    """Every path with uncommitted changes, including untracked (not ignored) files."""
    result = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if not result.ok:
        return None
    fields = result.out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" and i < len(fields):  # rename/copy: the next field is the source
            paths.append(fields[i])
            i += 1
    return sorted(set(paths))


def hold_reason(root: Path, rel: str) -> str | None:
    """Why this path must not be auto-committed, or None if it is safe."""
    if SECRET_NAME.search(rel):
        return "secret-looking file name"
    path = root / rel
    try:
        info = path.lstat()
    except OSError:
        return None  # deleted: committing the deletion is safe
    if stat.S_ISDIR(info.st_mode) and (path / ".git").exists():
        # git add -A would embed it as a gitlink; it is synced on its own instead.
        return "a nested git repo (synced on its own); .gitignore it or make it a submodule"
    if not stat.S_ISREG(info.st_mode):
        return None
    if info.st_size > MAX_FILE_BYTES:
        return f"larger than {MAX_FILE_BYTES // 2**20} MB"
    try:
        data = path.read_bytes()
    except OSError as err:
        return f"unreadable: {err.strerror}"
    if SECRET_TEXT.search(data):
        return "contains a secret-looking string"
    return None


def host_tag() -> str:
    """Short, branch-safe host name: Bens-MacBook-Pro.local -> bens-macbook-pro."""
    name = socket.gethostname().split(".")[0].lower()
    return re.sub(r"[^a-z0-9-]+", "-", name).strip("-") or "host"


def wip_branch(git: Git, day: str) -> str:
    """wip/<YYYYMMDD>-<host>, plus -N if that name is taken locally or on origin."""
    for n in range(1, 1000):
        name = f"wip/{day}-{host_tag()}" + (f"-{n}" if n > 1 else "")
        taken = any(
            git("show-ref", "--quiet", "--verify", ref).ok
            for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}")
        )
        if not taken:
            return name
    raise RuntimeError("no free wip branch name")


def held_back(git: Git, root: Path) -> list[dict] | None:
    """Changed files that must never be committed: secret-looking or too large.

    None if git status failed: the check could not run, so nothing is safe to commit.
    """
    paths = changed_paths(git)
    if paths is None:
        return None
    return [{"path": p, "reason": r} for p in paths if (r := hold_reason(root, p))]
