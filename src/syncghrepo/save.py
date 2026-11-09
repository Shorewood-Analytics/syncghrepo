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


NESTED_SYNCED = "a nested git repo (synced on its own); .gitignore it or make it a submodule"
NESTED_UNSYNCED = (
    "a nested git repo that is not synced (in a hidden or build folder, or too deep); "
    ".gitignore it, move it or make it a submodule"
)
SUBMODULE_MOVED = (
    "a submodule at another commit than this clone records (git add -A would commit "
    "that); commit the submodule change or run git submodule update"
)


def changed_submodules(git: Git) -> dict[str, bool] | None:
    """Submodules (gitlinks) that git status lists as changed: path -> whether the
    checked-out commit differs from the recorded one. False means only the files
    inside it changed, which the parent's git add -A never commits."""
    # Same untracked mode as changed_paths: it also decides whether a submodule's own
    # untracked files count as a change there.
    result = git("status", "--porcelain=v2", "-z", "--untracked-files=all")
    if not result.ok:
        return None
    found: dict[str, bool] = {}
    fields = iter(result.out.split("\0"))
    for entry in fields:
        kind, _, rest = entry.partition(" ")
        if kind not in ("1", "2"):
            continue
        # 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>; 2 adds <score> and a source path
        parts = rest.split(" ", 8 if kind == "2" else 7)
        if kind == "2":
            next(fields, None)
        if len(parts) >= 8 and parts[1].startswith("S"):
            found[parts[-1]] = parts[1][1] == "C"
    return found


def hold_reason(root: Path, rel: str, synced: set[Path] | None = None) -> str | None:
    """Why this path must not be auto-committed, or None if it is safe. synced: the
    repos that are synced on their own (discovered clones), if known."""
    if SECRET_NAME.search(rel):
        return "secret-looking file name"
    path = root / rel
    try:
        info = path.lstat()
    except OSError:
        return None  # deleted: committing the deletion is safe
    if stat.S_ISDIR(info.st_mode) and (path / ".git").exists():
        # git add -A would embed it as a gitlink; it is synced on its own instead,
        # unless discovery skipped it (hidden or build folder, too deep).
        if synced is not None and path.resolve() not in synced:
            return NESTED_UNSYNCED
        return NESTED_SYNCED
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


def blob_reason(git: Git, mode: str, blob: str) -> str | None:
    """hold_reason's size and content rules for a committed file (git's mode and
    blob id); the name is checked by the caller."""
    if mode not in ("100644", "100755"):  # symlink or gitlink: no file content
        return None
    size = git.out("cat-file", "-s", blob)
    if not size or not size.isdigit():
        return "unreadable"
    if int(size) > MAX_FILE_BYTES:
        return f"larger than {MAX_FILE_BYTES // 2**20} MB"
    data = git("cat-file", "blob", blob)
    if not data.ok:
        return "unreadable"
    if SECRET_TEXT.search(data.out.encode("utf-8", "surrogateescape")):
        return "contains a secret-looking string"
    return None


def unpushed_held_back(git: Git) -> list[dict] | None:
    """Files added or changed by commits on HEAD that are on no remote and must not be
    published, by the same rules as uncommitted files (held_back). A file removed by a
    later commit still counts: the commit that added it would be pushed. None if git
    could not list the commits."""
    commits = git.out("rev-list", "HEAD", "--not", "--remotes")
    if commits is None:
        return None
    found: dict[str, str] = {}
    content: dict[tuple[str, str], str | None] = {}  # (mode, blob) -> reason
    for sha in commits.split():
        listed = git("diff-tree", "-r", "-z", "--root", "--no-commit-id", "--no-renames",
                     "--diff-filter=AM", sha)  # fmt: skip
        if not listed.ok:
            return None
        fields = listed.out.split("\0")  # ":<mode> <mode> <blob> <blob> <status>", <path>
        for meta, rel in zip(fields[::2], fields[1::2], strict=False):
            parts = meta.split()
            if rel in found or len(parts) < 4:
                continue
            if SECRET_NAME.search(rel):
                reason = "secret-looking file name"
            else:
                key = (parts[1], parts[3])
                if key not in content:
                    content[key] = blob_reason(git, *key)
                reason = content[key]
            if reason:
                found[rel] = f"{reason}, in unpushed commit {sha[:7]}"
    return [{"path": rel, "reason": reason} for rel, reason in sorted(found.items())]


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


def held_back(
    git: Git,
    root: Path,
    synced: set[Path] | None = None,
    submodules: dict[str, bool] | None = None,
) -> list[dict]:
    """Changed files that must never be committed: secret-looking or too large, a
    nested repo, or a submodule moved to another commit. A submodule whose own files
    changed is not held back: git add -A in the parent does not commit them."""
    paths = changed_paths(git) or []
    subs = changed_submodules(git) if submodules is None else submodules
    found = []
    for rel in paths:
        if subs and rel in subs:
            reason = SUBMODULE_MOVED if subs[rel] else None
        else:
            reason = hold_reason(root, rel, synced)
        if reason:
            found.append({"path": rel, "reason": reason})
    return found
