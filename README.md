# syncghrepo

Keep all work in your local clones saved and in sync, and list what is still
open, for you or for an agent to triage. One plain CLI with exit codes and
`--json`, so a Makefile can call it. Standard library plus existing tools it
drives instead of reimplementing them: `git`, `gh`,
[git-sync](https://github.com/simonthum/git-sync) (saving and syncing),
[ghq](https://github.com/x-motemen/ghq) (finding clones) and
[gh-dash](https://github.com/dlvhdr/gh-dash) (reviewing PRs and issues). No other
dependencies, and no code shared with makeghrepo or reconcile-tasks.

Pipeline: `makeghrepo` (create repos) → **`syncghrepo`** (save, sync, list open
work) → `reconcile-tasks` (turn that work into tasks). The only contract between
syncghrepo and reconcile-tasks is the versioned `--json` report below.

**No AI.** syncghrepo makes no AI/LLM calls and has no network code of its own:
only `git` (including `git submodule`), `git-sync` (a bash script that runs git), `gh`
(`api`, `search prs`, `repo sync`, `repo clone`), `ghq list`, the lockfile tools listed below, and the Python standard library. `run.ALLOWED` enforces this: any other command
(`gh models`, `gh copilot`, `curl`, an AI CLI, ...) is refused, and tests check
that the code imports only the standard library and has no dependencies.

**One command does it all.** Plain `syncghrepo`, in one run:

1. discovers repos (`gh`) and clones (`ghq` and the roots), plus git repos nested
   inside clones and submodules;
2. clones each repo of yours that has no clone anywhere yet into
   `~/code/GitHub/<name>` ([Missing clones](#missing-clones));
3. saves and syncs every clone, nested ones too, with git-sync (secret skip, `wip/`
   branch, backup ref), and runs `git submodule update --init --recursive` where a
   `.gitmodules` exists ([Nested repos](#nested-repos-and-submodules));
4. runs `gh repo sync` for forks;
5. writes or refreshes the gh-dash config;
6. prints the open-work report.

It changes your repos. Three flags only: `--dry-run` previews all of it and changes
nothing; `--json` prints the report as JSON (`schema_version` 1, for
reconcile-tasks); `--api` works through the GitHub API alone and touches no local
clone ([API-only mode](#api-only-mode---api)). No subcommands; see [Use](#use) for
the two environment variables.

## What it does, per clone

The saving and syncing is done by [git-sync](https://github.com/simonthum/git-sync)
(Simon Thum, CC0): it auto-commits, fetches, fast-forwards or rebases, and pushes
the current branch, and stops when anything is not trivial. syncghrepo does not
reimplement that. It decides where git-sync may run, prepares the branch, calls
it and reports what happened:

1. **Secret check.** If any changed file has a secret-looking name (`.env`,
   `.envrc`, keys, `credentials*`, databases, ...), contains a secret-looking
   string (GitHub/AWS/Slack/OpenAI/Google tokens, private keys) or is over 50 MB,
   the whole clone is skipped and the files are reported. Over 2,000 changed files
   (a missing `.gitignore`?) is skipped too. Before a branch gets a new remote
   branch (a local-only branch's first push, or a `wip/` branch that carries the
   default branch's local commits), the files added or changed by its commits that
   are on no remote yet (merge commits' own changes and file type changes
   included) are checked the same way. Commits on a branch that is
   already pushed are pushed by git-sync without this check.
2. **Never the default branch.** git-sync never runs on the default branch (as
   GitHub reports it, and as the clone's `origin/HEAD` says, which can be missing or
   stale) or on `main`/`master`. Uncommitted work found there moves to a new
   `wip/<YYYYMMDD>-<host>` branch (`-2`, `-3`, ... if taken), which is pushed so it
   has the same-named remote branch git-sync needs. A `wip/` branch that never got
   its remote branch because the name was taken on GitHub meanwhile is renamed to
   the next free name first. The default branch itself is
   only fast-forwarded (`git merge --ff-only`, or `git fetch . origin/main:main`
   when it is not checked out); local commits on it are reported, never pushed.
3. **Opt in.** Only for a branch with something to sync, and only if GitHub says
   you can push: `git config --bool branch.<b>.sync true` and
   `branch.<b>.syncNewFiles true` (so new, non-ignored files are committed;
   git-sync uses `git add -A`, which never adds ignored files). A local-only branch
   is first pushed with `-u`. Branches whose upstream was deleted (merged PRs) are
   not recreated. Clean, in-sync clones are not touched, not even their config.
4. **git-sync check first.** `git-sync -s -n check` (read-only) runs before
   anything is changed, also on a branch that has no remote branch yet, so a clone
   it would refuse (staged changes, a merge in progress, ...) is left untouched and
   its message reported.
5. **Backup, then git-sync.** If the branch is behind its upstream and has local
   work, a backup ref of HEAD plus any uncommitted files is made first:
   `refs/syncghrepo/backup/<YYYYMMDDTHHMMSS>` (reused when a backup of the same
   tree exists). Then `git-sync` runs. It commits ("changes from <host> on
   <date>"), fetches, fast-forwards or rebases, and pushes. It never forces.
6. **Conflicts.** git-sync leaves a conflicted rebase in progress. syncghrepo then
   resolves trivial conflicts:
   - *identical*: both sides made the same change;
   - *whitespace*: the sides differ only in trailing whitespace, line endings or
     trailing blank lines; mine is kept. Indentation is never ignored;
   - *lockfiles*: take upstream's, then regenerate (`uv lock`, `cargo update
     --workspace`, `npm install --package-lock-only --ignore-scripts`, `pnpm install
     --lockfile-only --ignore-scripts`, `bundle lock`); `go.sum` takes the union.

   If they are all trivial, it reruns git-sync to push. Otherwise it runs
   `git rebase --abort`: the branch is back exactly as it was (with git-sync's WIP
   commit), the backup ref is kept, and the conflict is reported.
7. **Forks** get `gh repo sync owner/name` first (fast-forward only).
8. **Report** open PRs (yours, drafts, needing review, review requested), failing
   CI on the default branch, issues assigned to you, unpushed, stale and
   deleted-upstream branches, stashes and linked worktrees. Repos come from `gh`:
   yours, your organisations', collaborations, and repos you opened PRs in.

**Finding clones.** Clones come from [ghq](https://github.com/x-motemen/ghq)
(`ghq list -p`) plus `~/code` (searched 3 levels deep), deduplicated; if
`$SYNCGHREPO_ROOTS` is set, from exactly those folders instead. syncghrepo only lists: it never changes `ghq.root`,
runs `ghq get` or moves a clone. Without ghq, the roots alone are used.

## Missing clones

Every repo of yours (owned by the `gh` user) that has **no clone anywhere under the
roots** is cloned with `gh repo clone owner/name ~/code/GitHub/<name>`. A clone
counts wherever it is: under `~/code`, in ghq, nested inside another clone, or as a
submodule; clones are matched by their `origin` URL. Archived repos are cloned too,
and marked `archived` in the report. Organisation repos and repos you only
collaborate on or contributed to are not cloned.

An existing folder is never overwritten or moved: if `~/code/GitHub/<name>` exists
and is not a clone of that repo (another repo, a plain folder, a file), the repo is
skipped and reported (`clone_skipped`, exit 1) for you to sort out. Cloning only
happens with the default roots (ghq + `~/code`) and when `gh` listed all your repos
and ghq worked: with `$SYNCGHREPO_ROOTS` set, syncghrepo cannot know whether a repo
is cloned elsewhere, so it clones nothing. `--dry-run` lists exactly which repos
would be cloned and where.

## Nested repos and submodules

Git repos inside other clones are synced too: nested clones (a folder with its own
`.git`, searched 4 levels into each clone, skipping hidden and build folders) and
initialized submodules (from `git submodule status --recursive`, wherever they are).
Each one goes through the same per-clone steps as a top-level clone (secret skip,
`wip/` branch off the default branch, opt-in, git-sync check, backup ref), after
its parent. A repo that is both nested and a top-level clone (e.g. listed in
`$SYNCGHREPO_ROOTS`) is processed once. Linked worktrees inside a clone are not
nested repos; they are listed under their main clone.

- **Submodules.** In a clone with a `.gitmodules`, after it is synced, `git submodule
  update --init --recursive` runs when a submodule is missing or not at the
  recorded commit. It is skipped (and the reason noted) if it could move local work:
  a submodule on a branch, with uncommitted changes, with commits on no remote, or
  in a merge conflict. A submodule on a detached HEAD with nothing unsaved is
  `up-to-date`, not `skipped`. Files changed inside a submodule do not hold back
  its parent (the parent's `git add -A` never commits them). A submodule checked out
  at another commit than the parent records does (`held_back` reason "a submodule at
  another commit ..."): `git add -A` would commit that commit as the new pointer,
  which could undo a pointer update the parent pulled, so a person decides.
- **Untracked nested clones.** A nested clone that the parent does not ignore would
  be embedded by `git add -A` as a gitlink, so the parent is held back (`held_back`
  reason "a nested git repo") until the folder is in `.gitignore` or made a
  submodule. The nested clone itself is still synced, unless it is in a hidden or
  build folder or more than 4 levels down, where nested repos are not searched for:
  then the reason says it is not synced.

## API-only mode (`--api`)

`syncghrepo --api` minimises disk writes: it works purely through the GitHub API
and writes nothing but its report to stdout. It runs no git, no clone and no fetch,
does not read or touch local clones (no roots, no ghq, no git-sync), and writes no
gh-dash config. It:

- runs `gh repo sync owner/name` for your forks that you can push to (a server-side
  fast-forward from the parent; no disk). With `--dry-run` it does not;
- reports open PRs, drafts, review requests, failing CI on the default branch,
  issues assigned to you, and **stale branches on GitHub** (no commits for 30 days,
  not the default branch, no open PR; in your own, your organisations' and
  collaboration repos).

**It cannot save or sync local work**: uncommitted changes, unpushed commits, stashes
and local branches are not seen at all. Run plain `syncghrepo` for that.
`--api` works with `--json` (same schema, `mode` is `api`, every repo's `clones`
is empty) and `--dry-run`. Exit status: 2 for an error, a failed GitHub query or a
failed fork sync, 1 for failing CI, else 0. Your forks are synced even when their
status query failed (the repo listing says they are forks).

**Review UI.** Every run writes or refreshes a
[gh-dash](https://github.com/dlvhdr/gh-dash) config with sections for your open
PRs, your drafts, review requests and issues assigned to you, all scoped to the
discovered repos (`repo:owner/name ...`), plus `repoPaths` so gh dash can check a PR
out in your clone. It goes to `~/.config/gh-dash/config.yml` (or
`$XDG_CONFIG_HOME/gh-dash/`) when no file is there or the file is one syncghrepo
generated (first line `# Generated by syncghrepo`); a `config.yml` of your own is
never touched, and the config goes next to it as `config.syncghrepo.yml` instead
(then: `gh dash --config ~/.config/gh-dash/config.syncghrepo.yml`). The report
prints the `gh dash` command to run; syncghrepo never launches it.

Clones in the middle of a merge or rebase, with a detached HEAD, or with no
commits yet, are left alone and reported. Linked worktrees are listed but not
synced. Nothing is ever reset, cleaned, force-pushed or discarded.

git-sync is found via `$SYNCGHREPO_GIT_SYNC`, `PATH`, then
`~/.local/bin/git-sync`. Without it, clones with work are reported, not synced,
and the run exits 2. To stop syncing a branch: `git config --unset branch.<b>.sync`.

## Use

```sh
uv tool install git+https://github.com/benpshore/syncghrepo
syncghrepo --dry-run           # preview: exactly what would be committed, pushed, resolved
syncghrepo                     # do it all: save, sync, refresh gh-dash, report
syncghrepo --json              # the same, report as JSON (with --dry-run too)
syncghrepo --api               # GitHub API only: sync forks on GitHub, report; no local clone touched
gh dash                        # then review your PRs and issues
SYNCGHREPO_ROOTS=~/code/GitHub/proj syncghrepo   # one clone (colon-separated list)
```

Environment: `SYNCGHREPO_ROOTS` (folders of clones, colon-separated, instead of
ghq + `~/code`); `SYNCGHREPO_GIT_SYNC` (the git-sync to run). Fixed settings: roots
are searched 3 levels deep, a branch is stale after 30 days without commits, each
command times out after 120 s, and 8 clones are processed in parallel (clones of
the same repo one after another, so they never pick the same `wip/` name).

From a checkout: `uv run syncghrepo`. In a Makefile:

```make
sync: ; syncghrepo
plan: ; syncghrepo --dry-run --json > sync-plan.json
```

`--dry-run` writes nothing: no commits, refs, config, index or working-tree
changes, no gh-dash config, and no fetch, so it works from the remote-tracking refs
of the last fetch (a real run fetches). git-sync runs only in its read-only `check`
mode. Conflicts are predicted with `git merge-tree` in a scratch object store
outside the clone; a rebase replays commit by commit, so the prediction is close
but not a guarantee.

**Exit status:** 0 when all is fine (open PRs are reported, not counted); 1 when
something needs attention: uncommitted work, a conflict, a held-back file, a clone
mid-merge/rebase or detached (a clean submodule is fine), a repo of yours whose clone
folder is taken, or failing CI on a default branch; 2 for an error (a
step failed, `gh` or git-sync unusable, a GitHub status query that failed, bad
arguments). A repo that GitHub cannot resolve for you (deleted, renamed or no longer
visible) is only reported, as a `github_error` item.

The JSON report is documented below.

To recover from a backup ref: `git switch -c rescue refs/syncghrepo/backup/<YYYYMMDDTHHMMSS>`
(list them with `git for-each-ref refs/syncghrepo`).

## JSON schema

`syncghrepo --json` prints one JSON object. `schema_version` is `1`.

**Stability.** Within a schema version, fields are never removed or renamed and
never change type or meaning; new fields may be added, so consumers must ignore
keys they do not know. Anything else bumps `schema_version`. `tests/test_schema.py`
locks every key below, so a change cannot slip in unnoticed. `attention` and
`title` strings are for people; match on `id`, `kind` and the structured fields.

**Consumers (reconcile-tasks) should read `items`**: a flat, sorted list of
unresolved work whose `id` stays the same across runs while the work is the same.

### Top level

| key | type | meaning |
| --- | --- | --- |
| `schema_version` | int | this schema's version (`1`) |
| `tool` | string | always `"syncghrepo"` |
| `version` | string | syncghrepo's version |
| `generated_at` | string | UTC time, ISO 8601 |
| `user` | string or null | GitHub login, null if GitHub was not used |
| `dry_run` | bool | true with `--dry-run`, else false |
| `mode` | string | `local` (the normal run) or `api` (`--api`: GitHub only, no clones) |
| `roots` | [string] | folders searched for clones |
| `ghq_clones` | int or null | clones found with `ghq list -p`; null when ghq was not used (`$SYNCGHREPO_ROOTS` set) or is not installed |
| `gh_dash` | object or null | the gh-dash config: `path`, `written` (false with `--dry-run`), `existing_config` (a `config.yml` of your own is there, so `path` is `config.syncghrepo.yml`); null without GitHub |
| `cloned` | [clone record] | repos of yours with no clone yet, below (`[]` with `--api` or `$SYNCGHREPO_ROOTS`) |
| `nested` | [nested repo] | git repos nested inside clones and submodules, below (`[]` with `--api`) |
| `summary` | object | counts, below |
| `items` | [item] | unresolved work, below |
| `repos` | [repo] | full detail per repo, below |
| `errors` | [string] | run-level problems (e.g. `gh` unusable); non-empty means exit 2 |

### `items`: unresolved work

Every item has exactly these keys; absent values are `null` (`flags` is `[]`).

| key | type | meaning |
| --- | --- | --- |
| `id` | string | `<kind>:<key>`, stable across runs, unique in a report |
| `kind` | string | one of the kinds below |
| `repo` | string | `owner/name`, or the folder name for clones not on GitHub |
| `title` | string | short human-readable text |
| `url` | string or null | link on GitHub |
| `path` | string or null | local clone (or worktree) path |
| `branch` | string or null | branch concerned |
| `flags` | [string] | for `pr`: `mine`, `draft`, `needs_review`, `review_requested`; for `issue`: `assigned` |
| `updated_at` | string or null | ISO 8601 time of last change (PRs, issues, stashes) |
| `detail` | string or null | extra text: author, failing commit, reason, backup ref |

| kind | key in `id` | what it means |
| --- | --- | --- |
| `pr` | `owner/name#N` | open pull request (all PRs in your repos; only yours and review requests elsewhere) |
| `issue` | `owner/name#N` | open issue assigned to you |
| `ci_failing` | `owner/name@branch` | CI failing or erroring on the default branch |
| `github_error` | `owner/name` | the repo could not be queried (exit 2, unless GitHub cannot resolve the repo for you) |
| `conflict` | `path:branch` | pull aborted on conflicts that need a person; `detail` lists files and the backup ref |
| `error` | `path:branch` | a step failed (see the clone's `actions`) |
| `skipped` | `path:branch` | clone left alone: merge/rebase in progress, detached HEAD or no commits |
| `held_back` | `path:file` | secret-looking or oversized file, untracked nested repo or moved submodule; the whole clone was skipped |
| `save_skipped` | `path:branch` | over 2,000 changed files (a missing `.gitignore`?); the whole clone was skipped; `detail` says how many |
| `not_pushed` | `path:branch` | work not synced or pushed: commits on the default branch, a diverged default branch, deleted upstream, no push access, or a branch git-sync cannot handle |
| `unpushed_branch` | `path:branch` | another local branch with commits on no remote |
| `gone_branch` | `path:branch` | local branch whose upstream was deleted (merged?) |
| `stale_branch` | `path:branch` | no commits for 30 days |
| `stash` | `path:commit` | a git stash |
| `worktree` | `path` | linked worktree (listed, not auto-saved) |
| `remote_stale_branch` | `owner/name:branch` | `--api`: branch on GitHub with no commits for 30 days and no open PR |
| `fork_sync_failed` | `owner/name` | `--api`: `gh repo sync` failed for a fork; `detail` says why |
| `clone_skipped` | `owner/name` | a repo of yours with no clone, not cloned because its folder is taken; `path` is the folder |

### `summary`

Integers unless noted: `repos`, `on_github`, `cloned`, `not_cloned`, `clones`,
`clone_status` (object: status → count), `saved_files`, `held_back_files`,
`auto_resolved`, `conflicts`, `open_prs`, `draft_prs`, `prs_needing_review`,
`ci_failing`, `assigned_issues`, `stashes`, `unpushed_branches`, `stale_branches`,
`needs_attention` (repos with any `attention`), `items` (object: kind → count),
`remote_stale_branches` (`--api`), `forks_synced` (`gh repo sync` runs that
succeeded), `new_clones` (cloned, or would be with `--dry-run`), `clones_skipped`,
`nested` (nested repos processed). With `--api` the clone counts (`cloned`,
`not_cloned`, `clones`, `new_clones`, ...) are all 0: clones are not looked at.

### `repos[]`

- `name`, `url`; `github` (bool); `fork`, `archived` (bool or null);
  `default_branch`; `sources`: how it was found, from `owner`, `org`,
  `collaborator`, `contributed` (you opened PRs there), `local` (only cloned).
- `attention` ([string]): the human summary lines.
- `remote_error` (string or null).
- `remote` (object or null, from GitHub): `name`, `url`, `fork`, `archived`,
  `parent` (`owner/name` or null), `can_push`, `default_branch`, `ci` (`success`,
  `failure`, `error`, `pending`, `expected` or null), `ci_commit`, `open_prs` (total),
  `assigned_issues_total`, `prs` and `assigned_issues` (first 50 each; when a repo
  has more open PRs, yours and those requesting your review are added from a GitHub
  search, so they are never cut off),
  `stale_branches` (`--api` only, else null): each `name`, `age_days`,
  `committed_at` (ISO 8601), `url`.
  - each PR: `number`, `title`, `url`, `author`, `mine`, `draft`,
    `review_decision` (GitHub's, or null), `review_requested` (from you),
    `needs_review`, `updated_at`.
  - each issue: `number`, `title`, `url`, `updated_at`.
- `clones` ([clone]): every local clone of the repo, nested ones included (always
  `[]` with `--api`).
- `fork_sync` (object or null): `--api` only, for your forks: `cmd`, `ok` (null with
  `--dry-run`), `note` (gh's message). In a normal run forks are synced per clone
  (see `actions`), and this is null.

### `clones[]`

- `path`, `origin` (URL), `branch` (null if detached), `upstream`, `upstream_gone`,
  `default_branch`, `head` (short SHA), `changes` (tracked files changed),
  `untracked`, `busy` (`merge`, `rebase`, ... or null), `ahead`, `behind`,
  `unpushed` (commits on HEAD on no remote), `fork`, `can_push`. All describe the
  clone after the run.
- `status`: `up-to-date`, `updated`, `would-update` (`--dry-run`), `held-back`,
  `conflict`, `error` or `skipped`; `note` explains it.
- `actions`: each `cmd`, `ok` (true/false; null with `--dry-run` or when skipped) and
  `note`.
- `git_sync` (null if git-sync did not run, or ran only its check and passed): the
  last git-sync run's `code` (exit status: 0 in sync, 1 refused or rebase stopped,
  2 unsafe state, 3 fetch/push failed) and `log` (its last output lines).
- `saved` (null if nothing was saved): `branch`, `files`, `commit` (null with `--dry-run`).
- `held_back`: each `path` and `reason`. `save_skipped`: reason or null.
- `resolved`: each `path` and `how` (`identical`, `whitespace`, `union`, `lockfile`;
  lockfiles add `regenerate`, the command). `conflicts`: each `path` and `how`
  (`manual`), plus `error` if a step failed. `backup_refs`: [string].
- `branches`: local branches worth a look, each `name`, `upstream`, `unpushed`,
  `age_days`, `gone`, `stale`.
- `stashes`: each `commit`, `ref` (`stash@{N}`), `message`, `created` (ISO 8601).
- `worktrees`: [string] paths of linked worktrees.
- `nested_in`: path of the clone this repo is nested in, or null for a top-level
  clone; `submodule` (bool): it is a submodule there.

### `cloned[]` and `nested[]`

- `cloned[]`, one per repo of yours with no clone: `name`, `path` (where it is, or
  would be, cloned), `archived`, `fork`, `status` (`would-clone` with `--dry-run`,
  `cloned`, `failed`, or `skipped` when the folder is taken), `note`.
- `nested[]`, one per nested repo processed: `path`, `parent` (the enclosing clone),
  `repo` (`owner/name`, or the folder name off GitHub), `submodule`, `status` (the
  clone status, as in `clones[]`).

## Checks

Run these before opening a PR. CI runs the same ones.

```sh
uv run ruff format && uv run ruff check && uv run pytest && uv audit --preview-features audit-command
shellcheck scripts/*.sh
sqlite3 -bail :memory: ".read sqlite/schema.sql"   # the app opens ${APP_DATA:-.}/app.db
```

The owner (`benpshore`) may push their own work directly to `main` without a PR or passing CI. CI still runs after a push; releases retain their CI gates. Force-push, deletion, and merge commits remain blocked for everyone.

Other accounts must use a squash PR with passing, up-to-date `ci`. Agents must use PRs and CI even when authenticated as the owner (see `AGENTS.md`). GitHub checks the pushing account, so enforcing a human/agent boundary requires separate credentials.

### Qualification of automatic cloning and nested synchronization

See [PR #11 qualification](docs/pr11-qualification.md) for reproduced failure cases,
regression coverage, and limits. Automatic cloning includes archived repositories.
Discovery is bounded by the depth and directory exclusions above; it cannot establish
that a repository has no clone elsewhere on a machine. Local enumeration or submodule
status errors suppress automatic cloning. Overlapping roots are synchronized parent
first, and recursive submodule updates check every initialized descendant for local
work before proceeding. Use `--dry-run` to inspect the plan on each machine.
