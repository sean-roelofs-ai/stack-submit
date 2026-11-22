"""Submit each commit in `<target>..HEAD` as a pull request in a native GitHub stack.

The link between a commit and its PR lives in the commit message, in the same
format stack-pr uses, so existing stack-pr stacks keep working:

    stack-info: PR: https://github.com/owner/repo/pull/123, branch: user/stack/abc123

To fold a commit into the PR below it instead of giving it its own PR, replace its
stack-info line with a bare:

    stack-info:
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
# Written by hand (never by this tool): any other stack-info line, e.g. a bare `stack-info:`,
# makes the commit continue the PR below it.
CONTINUATION_RE = re.compile(r"^stack-info:(?![ \t]*PR:).*$", re.MULTILINE)


@dataclass
class Commit:
    sha: str
    message: str


@dataclass
class Pr:
    """A PR and its commits, bottom first. The first commit holds the PR's title, body and
    stack-info line; any later ones are marked with a bare `stack-info:`."""

    commits: list[Commit]
    pr_url: str | None
    branch: str

    @property
    def head(self) -> str:
        """The commit the PR's branch points at."""
        return self.commits[-1].sha

    @property
    def title(self) -> str:
        return self.commits[0].message.splitlines()[0]

    @property
    def body(self) -> str:
        body = "\n".join(self.commits[0].message.splitlines()[1:])
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


def read_stack(remote: str, target: str, prefix: str) -> list[Pr]:
    """The PRs for the commits in `<remote>/<target>..HEAD`, bottom first."""
    if run("git", "rev-list", "--merges", f"{remote}/{target}..HEAD"):
        sys.exit("error: the stack contains merge commits")
    log = run("git", "log", "--reverse", "--format=%H%x00%B%x1e", f"{remote}/{target}..HEAD")
    prs: list[Pr] = []
    for entry in filter(None, (e.strip("\n") for e in log.split("\x1e"))):
        sha, message = entry.split("\x00", 1)
        commit = Commit(sha, message.strip())
        match = STACK_INFO_RE.search(message)
        if CONTINUATION_RE.search(message):
            if match:
                sys.exit(f"error: commit {sha[:10]} has both a bare `stack-info:` and a PR stack-info line")
            if not prs:
                sys.exit(f"error: commit {sha[:10]} has a bare `stack-info:` but no PR below it to continue")
            prs[-1].commits.append(commit)
        elif match:
            prs.append(Pr([commit], pr_url=match[1], branch=match[2]))
        else:
            # Name new branches after the original commit; the name is stored in the
            # message below, so it stays stable when the commit is later rewritten.
            prs.append(Pr([commit], pr_url=None, branch=f"{prefix}/{sha[:10]}"))
    return prs


def push(prs: list[Pr], remote: str, no_verify: bool) -> None:
    print(f"Pushing {len(prs)} branches")
    refspecs = [f"{p.head}:refs/heads/{p.branch}" for p in prs]
    run("git", "push", "--atomic", "--force", *(["--no-verify"] if no_verify else []), remote, *refspecs)


def create_pr(pr: Pr, base: str, draft: bool) -> None:
    # Reuse an open PR for this branch, e.g. when the commit lost its stack-info line.
    existing = subprocess.run(
        ["gh", "pr", "view", pr.branch, "--json", "url,state"], capture_output=True, text=True
    )
    if existing.returncode == 0 and json.loads(existing.stdout)["state"] == "OPEN":
        pr.pr_url = json.loads(existing.stdout)["url"]
        print(f"Found {pr.pr_url}")
        return
    out = run(
        "gh", "pr", "create", "--base", base, "--head", pr.branch,
        "--title", pr.title, "--body-file", "-", *(["--draft"] if draft else []),
        input=pr.body,
    )  # fmt: skip
    pr.pr_url = out.splitlines()[-1]
    print(f"Created {pr.pr_url}")


def pr_body(pr: Pr, prs: list[Pr]) -> str:
    """The PR's commit body plus a list of the stack's PRs (top first), in the same format as stack-pr."""
    lines = [f" * {'__->__' if p is pr else ''}#{p.pr_number}" for p in reversed(prs)]
    return f"{pr.body}\n\nStacked PRs:\n" + "\n".join(lines)


def update_pr(pr: Pr, base: str, body: str) -> None:
    """Make the PR's base, title and body match the stack, editing only what differs.

    GitHub rejects any `--base` on a stacked PR, even an unchanged one.
    """
    info = json.loads(run("gh", "pr", "view", str(pr.pr_number), "--json", "baseRefName,title,body"))
    edits = []
    if info["baseRefName"] != base:
        edits += ["--base", base]
    if info["title"] != pr.title:
        edits += ["--title", pr.title]
    if info["body"].strip() != body.strip():
        edits += ["--body-file", "-"]
    if edits:
        run("gh", "pr", "edit", str(pr.pr_number), *edits, input=body)


def write_stack_info(prs: list[Pr]) -> None:
    """Add missing `stack-info` lines by rebuilding the commits with `git commit-tree`.

    Trees are unchanged, so the working copy and index are never touched.
    """
    rewritten = []  # (old sha, new sha) pairs for the post-rewrite hook
    parent = run("git", "rev-parse", f"{prs[0].commits[0].sha}^")
    for p in prs:
        if not STACK_INFO_RE.search(p.commits[0].message):
            p.commits[0].message += f"\n\nstack-info: PR: {p.pr_url}, branch: {p.branch}"
        for c in p.commits:
            parent = rebuild_commit(c, parent, rewritten)
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


def rebuild_commit(c: Commit, parent: str, rewritten: list[tuple[str, str]]) -> str:
    """Recreate `c` on `parent` with its (possibly edited) message; returns and records the new sha."""
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
    c.sha = new_sha
    return new_sha


def find_github_stacks(prs: list[Pr]) -> list[dict]:
    """Every native GitHub stack holding any of the already-submitted PRs. After a reorder
    or a partial submit the PRs can be spread over several stacks."""
    def lookup(p: Pr) -> list[dict]:
        return json.loads(run("gh", "api", f"repos/{{owner}}/{{repo}}/stacks?pull_request={p.pr_number}"))

    with ThreadPoolExecutor() as pool:
        found = pool.map(lookup, [p for p in prs if p.pr_url])
        return list({s["number"]: s for stacks in found for s in stacks}.values())


def prepare_github_stack(prs: list[Pr], bases: list[str], target: str) -> dict | None:
    """Unstack and retarget PRs whose base changes, before anything is pushed.

    GitHub marks a PR as merged once its base branch contains its head. After a
    reorder, the push can do exactly that to a PR's old base (e.g. A,B -> B,A makes
    A's branch contain B while B's PR still targets it), so every PR whose base will
    change is first moved onto the target branch, which never contains the stack.
    Bases can't be edited inside a native stack, so the stack is dissolved first
    unless it only grows at the top.

    Returns the existing stack if it can be extended, else None.
    """
    stacks = find_github_stacks(prs)
    ours = [p.pr_number if p.pr_url else None for p in prs]
    stack, start = None, 0
    if len(stacks) == 1 and [p["number"] for p in stacks[0]["pull_requests"]] == ours[: len(stacks[0]["pull_requests"])]:
        stack, start = stacks[0], len(stacks[0]["pull_requests"])
    else:
        for s in stacks:  # any stack left behind would block editing its PRs' bases
            print(f"Unstacking stack #{s['number']}")
            run("gh", "api", "-X", "POST", f"repos/{{owner}}/{{repo}}/stacks/{s['number']}/unstack")

    def retarget(pr: Pr, base: str) -> None:
        if pr.pr_url is None:
            return
        info = json.loads(run("gh", "pr", "view", str(pr.pr_number), "--json", "baseRefName"))
        if info["baseRefName"] not in (base, target):
            print(f"Retargeting {pr.pr_url} to {target}")
            run("gh", "pr", "edit", str(pr.pr_number), "--base", target)

    with ThreadPoolExecutor() as pool:
        list(pool.map(retarget, prs[start:], bases[start:]))
    return stack


def sync_github_stack(prs: list[Pr], bases: list[str], stack: dict | None) -> None:
    """Fix PR bases, titles and bodies (with links to the other PRs), and make the PRs one native GitHub stack."""
    numbers = [p.pr_number for p in prs]
    print(f"Syncing {len(prs)} PRs")
    with ThreadPoolExecutor() as pool:
        list(pool.map(update_pr, prs, bases, [pr_body(p, prs) for p in prs]))

    if len(numbers) < 2:
        return
    if stack:
        new_numbers = numbers[len(stack["pull_requests"]) :]
        if new_numbers:
            gh_post(f"repos/{{owner}}/{{repo}}/stacks/{stack['number']}/add", new_numbers)
        print(f"Stack #{stack['number']}: {prs[-1].pr_url}")
    else:
        number = gh_post("repos/{owner}/{repo}/stacks", numbers)
        print(f"Created stack #{number}: {prs[-1].pr_url}")


def gh_post(path: str, numbers: list[int]) -> str:
    """POST a list of PR numbers to the stacks API; returns the stack number."""
    body = json.dumps({"pull_requests": numbers})
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

    prs = read_stack(args.remote, target, args.prefix)
    if not prs:
        sys.exit(f"Nothing to submit: HEAD is already in {args.remote}/{target}")
    bases = [target] + [p.branch for p in prs[:-1]]

    stack = prepare_github_stack(prs, bases, target)
    push(prs, args.remote, args.no_verify)
    new = [(p, base) for p, base in zip(prs, bases) if p.pr_url is None]
    for p, base in new:  # in order, so each base branch has its PR before the next
        create_pr(p, base, args.draft)
    if new:
        write_stack_info(prs)
        push(prs, args.remote, args.no_verify)

    sync_github_stack(prs, bases, stack)


if __name__ == "__main__":
    main()
