#!/usr/bin/env python3
"""Fetch every review thread on a GitHub PR, deterministically.

Emits one JSON document on stdout: all inline review threads (paginated
GraphQL, looped until exhausted) plus review bodies and PR-level issue
comments. Verifies the collected thread count against the API's totalCount —
a mismatch means pagination silently dropped threads, which must be a hard
failure (exit 2, no partial output), never a shorter-looking list.

Usage: pr_review_threads.py <owner>/<repo> <pr-number>

GitHub access is via the `gh` CLI (reuses your auth). Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from typing import Any

GhRunner = Callable[[list[str]], str]

THREADS_QUERY = """
query($owner:String!,$repo:String!,$pr:Int!,$after:String){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$pr){
      reviewThreads(first:100,after:$after){
        totalCount pageInfo{ hasNextPage endCursor }
        nodes{
          isResolved isOutdated path line
          comments(first:50){ nodes{ databaseId author{login} body diffHunk }}
        }
      }
    }
  }
}
"""


def run_gh(args: list[str]) -> str:
    p = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if p.returncode != 0:
        print(f"error: gh {' '.join(args[:2])}… failed: {p.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)
    return p.stdout


def _thread(node: dict[str, Any]) -> dict[str, Any]:
    comments = [
        {
            "id": c["databaseId"],
            "author": (c.get("author") or {}).get("login"),
            "body": c.get("body", ""),
            "diffHunk": c.get("diffHunk", ""),
        }
        for c in node["comments"]["nodes"]
    ]
    return {
        "isResolved": node["isResolved"],
        "isOutdated": node["isOutdated"],
        "path": node.get("path"),
        "line": node.get("line"),
        "root_id": comments[0]["id"] if comments else None,
        "comments": comments,
    }


def fetch_threads(slug: str, pr: int, gh: GhRunner) -> tuple[list[dict[str, Any]], int]:
    owner, repo = slug.split("/", 1)
    threads: list[dict[str, Any]] = []
    after: str | None = None
    total = 0
    while True:
        args = [
            "api",
            "graphql",
            "-f",
            f"owner={owner}",
            "-f",
            f"repo={repo}",
            "-F",
            f"pr={pr}",
            "-f",
            f"query={THREADS_QUERY}",
        ]
        if after:
            args += ["-f", f"after={after}"]
        conn = json.loads(gh(args))["data"]["repository"]["pullRequest"]["reviewThreads"]
        total = conn["totalCount"]
        threads += [_thread(n) for n in conn["nodes"]]
        if not conn["pageInfo"]["hasNextPage"]:
            return threads, total
        after = conn["pageInfo"]["endCursor"]


def fetch_rest_pages(slug: str, pr: int, kind: str, gh: GhRunner) -> list[dict[str, Any]]:
    """kind: 'pulls/<pr>/reviews' review bodies, or 'issues/<pr>/comments' PR-level comments."""
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = json.loads(gh(["api", f"repos/{slug}/{kind}?per_page=100&page={page}"]))
        items += batch
        if len(batch) < 100:
            return items
        page += 1


def main(argv: list[str], gh: GhRunner = run_gh) -> int:
    if len(argv) != 2:
        print("usage: pr_review_threads.py <owner>/<repo> <pr-number>", file=sys.stderr)
        raise SystemExit(64)
    slug, pr = argv[0], int(argv[1])

    threads, total = fetch_threads(slug, pr, gh)
    if len(threads) != total:
        print(
            f"error: collected {len(threads)} threads but the API reports totalCount {total} — "
            "pagination dropped threads; not emitting partial output",
            file=sys.stderr,
        )
        raise SystemExit(2)

    reviews = fetch_rest_pages(slug, pr, f"pulls/{pr}/reviews", gh)
    issue_comments = fetch_rest_pages(slug, pr, f"issues/{pr}/comments", gh)

    out = {
        "repo": slug,
        "pr": pr,
        "total_count": total,
        "threads": threads,
        "review_bodies": [
            {
                "id": r.get("id"),
                "author": (r.get("user") or {}).get("login"),
                "state": r.get("state"),
                "body": r.get("body", ""),
            }
            for r in reviews
        ],
        "issue_comments": [
            {"id": c.get("id"), "author": (c.get("user") or {}).get("login"), "body": c.get("body", "")}
            for c in issue_comments
        ],
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
