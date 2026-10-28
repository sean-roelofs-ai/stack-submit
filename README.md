# stack-submit

Turn every commit in `origin/<default branch>..HEAD` into a pull request and link them as a
[native GitHub stack](https://github.com/github/gh-stack), so the whole stack can be merged at once.

It does only the submitting part. Navigation, rebasing and editing the stack are left to your usual git tools
(it pairs well with [git-branchless](https://github.com/arxanas/git-branchless)).

## Install

Requires [`gh`](https://cli.github.com/) (logged in) and git ≥ 2.36.

```sh
uv tool install git+https://github.com/sean-roelofs-ai/stack-submit
# or: pipx install git+https://github.com/sean-roelofs-ai/stack-submit
```

## Usage

```sh
stack-submit            # submit HEAD's stack against the remote's default branch
stack-submit --draft    # new PRs are drafts
```

For each commit, bottom to top, it:

1. Picks a branch: the one recorded in the commit, or `$USER/stack/<short sha>` for a new commit.
2. Force-pushes all branches in one atomic push (no local branches, no checkouts).
3. Creates a PR for each new commit, then records it in the commit message:

   ```
   stack-info: PR: https://github.com/owner/repo/pull/123, branch: alice/stack/1a2b3c4d5e
   ```

   This is the same format as [stack-pr](https://github.com/modular/stack-pr), so existing stack-pr stacks
   are picked up as-is. Messages are rewritten with `git commit-tree` (the working copy is never touched),
   and `post-rewrite` hooks run so tools like git-branchless follow the rewrite.
4. Sets each PR's base to the branch below it and its title/body to the commit message.
   The commit message is the source of truth: edits to the PR description on GitHub are overwritten.
5. Links the PRs into a GitHub stack, extending the existing stack if PRs were added on top,
   or rebuilding it if the stack was reordered or a commit was dropped.

Re-run it after any change to the stack (amend, reorder, rebase onto a new `master`).

## Options

All options can also be set per repository (or globally with `--global`) with git config:

| Flag | git config | Default |
|---|---|---|
| `--target` | `stack-submit.target` | the remote's default branch (`origin/HEAD`) |
| `--remote` | `stack-submit.remote` | `origin` |
| `--prefix` | `stack-submit.prefix` | `$USER/stack` |
| `--no-verify` | | run the `pre-push` hook |

```sh
git config stack-submit.target develop
```
