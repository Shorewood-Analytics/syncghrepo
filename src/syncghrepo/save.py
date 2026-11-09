"""What auto-save may touch: the secret check, and wip/ branch names.

git-sync makes the commits (git add -A, so ignored files are never added). Before
that, if any changed file is secret-looking (by name or content) or very large, the
whole clone is skipped and the files reported: nothing is committed, synced or
pushed there until a person looks. The same check covers files in commits that are
on no remote yet, before syncghrepo pushes them.
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


def held_back(git: Git, root: Path) -> list[dict]:
    """Changed files that must never be committed: secret-looking or too large."""
    paths = changed_paths(git) or []
    return [{"path": p, "reason": r} for p in paths if (r := hold_reason(root, p))]


def blob_reason(git: Git, path: str, mode: str, blob: str) -> str | None:
    """hold_reason for a file as committed: mode and blob as in git log --raw."""
    if not blob.strip("0"):
        return None  # deleted: pushing the deletion is safe
    if SECRET_NAME.search(path):
        return "secret-looking file name"
    if mode not in ("100644", "100755"):
        return None  # a symlink or a submodule
    size = git.out("cat-file", "-s", blob)
    if not size or not size.isdigit():
        return "unreadable"
    if int(size) > MAX_FILE_BYTES:
        return f"larger than {MAX_FILE_BYTES // 2**20} MB"
    data = git("cat-file", "blob", blob)
    if not data.ok:
        return f"unreadable: {data.last_line()}"
    if SECRET_TEXT.search(data.out.encode("utf-8", "surrogateescape")):
        return "contains a secret-looking string"
    return None


def unpushed_held_back(git: Git) -> list[dict]:
    """Files in commits on HEAD that are on no remote (pushing HEAD publishes them) that
    must never be pushed: secret-looking or too large, in any of those commits."""
    listing = git("log", "--root", "--raw", "--no-renames", "--no-abbrev", "-z", "--format=",
                  "HEAD", "--not", "--remotes")  # fmt: skip
    if not listing.ok:
        return [{"path": "HEAD", "reason": f"commits not checked: {listing.last_line()}"}]
    found: dict[str, str] = {}
    fields = iter(listing.out.split("\0"))
    for meta in fields:  # ":<old mode> <new mode> <old blob> <new blob> <status>", then path
        parts = meta.strip().split(" ")
        if len(parts) != 5 or not parts[0].startswith(":"):
            continue
        path = next(fields, "")
        if path not in found and (reason := blob_reason(git, path, parts[1], parts[3])):
            found[path] = reason
    return [{"path": path, "reason": f"{reason} (in a commit on no remote)"}
            for path, reason in sorted(found.items())]  # fmt: skip
