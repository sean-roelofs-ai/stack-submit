"""Submit each commit in `<target>..HEAD` as a pull request in a native GitHub stack.

The link between a commit and its PR lives in the commit message, in the same
format stack-pr uses, so existing stack-pr stacks keep working:

    stack-info: PR: https://github.com/owner/repo/pull/123, branch: user/stack/abc123
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

STACK_INFO_RE = re.compile(r"^stack-info: PR: (\S+), branch: (\S+)$", re.MULTILINE)


@dataclass
class Commit:
    sha: str
    message: str
    pr_url: str | None
    branch: str

    @property
    def title(self) -> str:
        return self.message.splitlines()[0]

    @property
    def body(self) -> str:
        body = "\n".join(self.message.splitlines()[1:])
        return STACK_INFO_RE.sub("", body).strip()

    @property
    def pr_number(self) -> int:
        assert self.pr_url
        return int(self.pr_url.rstrip("/").split("/")[-1])


def run(*cmd: str, input: str | None = None) -> str:
    result = subprocess.run(cmd, input=input, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"error: `{' '.join(cmd)}` failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def git_config(key: str) -> str | None:
    result = subprocess.run(["git", "config", f"stack-submit.{key}"], capture_output=True, text=True)
    return result.stdout.strip() or None


def default_target(remote: str) -> str:
    # e.g. refs/remotes/origin/master -> master
    ref = run("git", "symbolic-ref", f"refs/remotes/{remote}/HEAD")
    return ref.removeprefix(f"refs/remotes/{remote}/")


def read_stack(remote: str, target: str, prefix: str) -> list[Commit]:
    """Commits in `<remote>/<target>..HEAD`, bottom first."""
    if run("git", "rev-list", "--merges", f"{remote}/{target}..HEAD"):
        sys.exit("error: the stack contains merge commits")
    log = run("git", "log", "--reverse", "--format=%H%x00%B%x1e", f"{remote}/{target}..HEAD")
    commits = []
    for entry in filter(None, (e.strip("\n") for e in log.split("\x1e"))):
        sha, message = entry.split("\x00", 1)
        match = STACK_INFO_RE.search(message)
        if match:
            commits.append(Commit(sha, message.strip(), pr_url=match[1], branch=match[2]))
        else:
            # Name new branches after the original commit; the name is stored in the
            # message below, so it stays stable when the commit is later rewritten.
            commits.append(Commit(sha, message.strip(), pr_url=None, branch=f"{prefix}/{sha[:10]}"))
    return commits


def push(commits: list[Commit], remote: str, no_verify: bool) -> None:
    print(f"Pushing {len(commits)} branches")
    refspecs = [f"{c.sha}:refs/heads/{c.branch}" for c in commits]
    run("git", "push", "--atomic", "--force", *(["--no-verify"] if no_verify else []), remote, *refspecs)


def create_pr(commit: Commit, base: str, draft: bool) -> None:
    # Reuse an open PR for this branch, e.g. when the commit lost its stack-info line.
    existing = subprocess.run(
        ["gh", "pr", "view", commit.branch, "--json", "url,state"], capture_output=True, text=True
    )
    if existing.returncode == 0 and json.loads(existing.stdout)["state"] == "OPEN":
        commit.pr_url = json.loads(existing.stdout)["url"]
        print(f"Found {commit.pr_url}")
        return
    out = run(
        "gh", "pr", "create", "--base", base, "--head", commit.branch,
        "--title", commit.title, "--body-file", "-", *(["--draft"] if draft else []),
        input=commit.body,
    )  # fmt: skip
    commit.pr_url = out.splitlines()[-1]
    print(f"Created {commit.pr_url}")


def update_pr(commit: Commit, base: str) -> None:
    """Make the PR's base, title and body match the stack, editing only what differs.

    GitHub rejects any `--base` on a stacked PR, even an unchanged one.
    """
    pr = json.loads(run("gh", "pr", "view", str(commit.pr_number), "--json", "baseRefName,title,body"))
    edits = []
    if pr["baseRefName"] != base:
        edits += ["--base", base]
    if pr["title"] != commit.title:
        edits += ["--title", commit.title]
    if pr["body"].strip() != commit.body:
        edits += ["--body-file", "-"]
    if edits:
        run("gh", "pr", "edit", str(commit.pr_number), *edits, input=commit.body)


def write_stack_info(commits: list[Commit]) -> None:
    """Add missing `stack-info` lines by rebuilding the commits with `git commit-tree`.

    Trees are unchanged, so the working copy and index are never touched.
    """
    rewritten = []  # (old sha, new sha) pairs for the post-rewrite hook
    parent = run("git", "rev-parse", f"{commits[0].sha}^")
    for c in commits:
        if not STACK_INFO_RE.search(c.message):
            c.message += f"\n\nstack-info: PR: {c.pr_url}, branch: {c.branch}"
        fields = run("git", "show", "-s", "--format=%T%x00%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%cd", "--date=raw", c.sha)
        tree, an, ae, ad, cn, ce, cd = fields.split("\x00")
        env = dict(os.environ, GIT_AUTHOR_NAME=an, GIT_AUTHOR_EMAIL=ae, GIT_AUTHOR_DATE=ad,
                   GIT_COMMITTER_NAME=cn, GIT_COMMITTER_EMAIL=ce, GIT_COMMITTER_DATE=cd)  # fmt: skip
        new_sha = subprocess.run(
            ["git", "commit-tree", tree, "-p", parent, "-F", "-"],
            input=c.message, capture_output=True, text=True, env=env, check=True,
        ).stdout.strip()  # fmt: skip
        if new_sha != c.sha:
            rewritten.append((c.sha, new_sha))
        c.sha = parent = new_sha
    if not rewritten:
        return

    # Move local branches (including the checked-out one) from the old commits to the new ones.
    for old, new in rewritten:
        for ref in run("git", "for-each-ref", f"--points-at={old}", "--format=%(refname)", "refs/heads").split():
            run("git", "update-ref", "-m", "stack-submit: add stack-info", ref, new, old)
    if not run("git", "branch", "--show-current"):  # detached HEAD
        run("git", "update-ref", "--no-deref", "-m", "stack-submit: add stack-info", "HEAD", parent)
    # Let git-branchless (and any other post-rewrite hooks) know about the rewrite.
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:
        f.write("".join(f"{old} {new}\n" for old, new in rewritten))
        f.flush()
        run("git", "hook", "run", "--ignore-missing", f"--to-stdin={f.name}", "post-rewrite", "--", "rebase")


def find_github_stack(commits: list[Commit]) -> dict | None:
    """The native GitHub stack holding any of the already-submitted PRs."""
    for c in commits:
        if c.pr_url:
            stacks = json.loads(run("gh", "api", f"repos/{{owner}}/{{repo}}/stacks?pull_request={c.pr_number}"))
            if stacks:
                return stacks[0]
    return None


def prepare_github_stack(commits: list[Commit], bases: list[str], target: str) -> dict | None:
    """Unstack and retarget PRs whose base changes, before anything is pushed.

    GitHub marks a PR as merged once its base branch contains its head. After a
    reorder, the push can do exactly that to a PR's old base (e.g. A,B -> B,A makes
    A's branch contain B while B's PR still targets it), so every PR whose base will
    change is first moved onto the target branch, which never contains the stack.
    Bases can't be edited inside a native stack, so the stack is dissolved first
    unless it only grows at the top.

    Returns the existing stack if it can be extended, else None.
    """
    stack = find_github_stack(commits)
    start = 0
    if stack:
        stacked = [p["number"] for p in stack["pull_requests"]]
        ours = [c.pr_number if c.pr_url else None for c in commits]
        if stacked == ours[: len(stacked)]:
            start = len(stacked)
        else:
            print(f"Unstacking stack #{stack['number']}")
            run("gh", "api", "-X", "POST", f"repos/{{owner}}/{{repo}}/stacks/{stack['number']}/unstack")
            stack = None

    def retarget(commit: Commit, base: str) -> None:
        if commit.pr_url is None:
            return
        pr = json.loads(run("gh", "pr", "view", str(commit.pr_number), "--json", "baseRefName"))
        if pr["baseRefName"] not in (base, target):
            print(f"Retargeting {commit.pr_url} to {target}")
            run("gh", "pr", "edit", str(commit.pr_number), "--base", target)

    with ThreadPoolExecutor() as pool:
        list(pool.map(retarget, commits[start:], bases[start:]))
    return stack


def sync_github_stack(commits: list[Commit], bases: list[str], stack: dict | None) -> None:
    """Fix PR bases/titles/bodies and make the PRs one native GitHub stack."""
    prs = [c.pr_number for c in commits]
    print(f"Syncing {len(commits)} PRs")
    with ThreadPoolExecutor() as pool:
        list(pool.map(update_pr, commits, bases))

    if len(prs) < 2:
        return
    if stack:
        new_prs = prs[len(stack["pull_requests"]) :]
        if new_prs:
            gh_post(f"repos/{{owner}}/{{repo}}/stacks/{stack['number']}/add", new_prs)
        print(f"Stack #{stack['number']}: {commits[-1].pr_url}")
    else:
        number = gh_post("repos/{owner}/{repo}/stacks", prs)
        print(f"Created stack #{number}: {commits[-1].pr_url}")


def gh_post(path: str, prs: list[int]) -> str:
    """POST a list of PR numbers to the stacks API; returns the stack number."""
    body = json.dumps({"pull_requests": prs})
    return run("gh", "api", "-X", "POST", path, "--input", "-", "--jq", ".number", input=body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--remote", default=git_config("remote") or "origin")
    parser.add_argument("--target", default=git_config("target"), help="default: the remote's default branch")
    parser.add_argument("--prefix", default=git_config("prefix") or f"{os.environ.get('USER', 'user')}/stack",
                        help="prefix for new branch names (default: $USER/stack)")  # fmt: skip
    parser.add_argument("--draft", action="store_true", help="create new PRs as drafts")
    parser.add_argument("--no-verify", action="store_true", help="skip the pre-push hook")
    args = parser.parse_args()
    target = args.target or default_target(args.remote)

    commits = read_stack(args.remote, target, args.prefix)
    if not commits:
        sys.exit(f"Nothing to submit: HEAD is already in {args.remote}/{target}")
    bases = [target] + [c.branch for c in commits[:-1]]

    stack = prepare_github_stack(commits, bases, target)
    push(commits, args.remote, args.no_verify)
    new = [(c, base) for c, base in zip(commits, bases) if c.pr_url is None]
    for c, base in new:  # in order, so each base branch has its PR before the next
        create_pr(c, base, args.draft)
    if new:
        write_stack_info(commits)
        push(commits, args.remote, args.no_verify)

    sync_github_stack(commits, bases, stack)


if __name__ == "__main__":
    main()
