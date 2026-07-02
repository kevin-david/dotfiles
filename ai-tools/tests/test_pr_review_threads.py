"""pr_review_threads: deterministic PR-thread fetch — pagination, count verify, output shape."""

import json

import pytest

import pr_review_threads as prt


def graphql_page(nodes, total, has_next, cursor):
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "totalCount": total,
                            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                            "nodes": nodes,
                        }
                    }
                }
            }
        }
    )


def thread_node(root_id, n_replies=0, path="a.py"):
    comments = [
        {"databaseId": root_id + i, "author": {"login": "rev"}, "body": f"c{i}", "diffHunk": "@@"}
        for i in range(n_replies + 1)
    ]
    return {"isResolved": False, "isOutdated": False, "path": path, "line": 3, "comments": {"nodes": comments}}


class FakeGh:
    """Returns canned stdout per call; records argv of every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return self.responses.pop(0)


def rest_empty():
    return json.dumps([])


def test_pagination_follows_end_cursor():
    page1 = graphql_page([thread_node(100)], total=2, has_next=True, cursor="CUR1")
    page2 = graphql_page([thread_node(200)], total=2, has_next=False, cursor=None)
    gh = FakeGh([page1, page2])
    threads, total = prt.fetch_threads("o/r", 7, gh)
    assert total == 2
    assert [t["root_id"] for t in threads] == [100, 200]
    # Second GraphQL call must carry the first page's endCursor.
    assert any("CUR1" in a for a in gh.calls[1])


def test_count_mismatch_exits_2_no_output(capsys):
    short_page = graphql_page([thread_node(100)], total=5, has_next=False, cursor=None)
    gh = FakeGh([short_page])
    with pytest.raises(SystemExit) as exc:
        prt.main(["o/r", "7"], gh)
    assert exc.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "1" in err and "5" in err


def test_includes_review_bodies_and_issue_comments(capsys):
    page = graphql_page([thread_node(100)], total=1, has_next=False, cursor=None)
    reviews = json.dumps([{"id": 9, "user": {"login": "rev"}, "state": "CHANGES_REQUESTED", "body": "do X overall"}])
    issue_comments = json.dumps([{"id": 11, "user": {"login": "kev"}, "body": "PR-level note"}])
    gh = FakeGh([page, reviews, issue_comments])
    assert prt.main(["o/r", "7"], gh) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["review_bodies"][0]["body"] == "do X overall"
    assert data["issue_comments"][0]["body"] == "PR-level note"
    assert data["total_count"] == 1


def test_thread_root_id_exposed_for_replies(capsys):
    page = graphql_page([thread_node(500, n_replies=2)], total=1, has_next=False, cursor=None)
    gh = FakeGh([page, rest_empty(), rest_empty()])
    assert prt.main(["o/r", "7"], gh) == 0
    data = json.loads(capsys.readouterr().out)
    t = data["threads"][0]
    assert t["root_id"] == 500
    assert [c["id"] for c in t["comments"]] == [500, 501, 502]
