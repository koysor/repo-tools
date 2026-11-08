import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

import review_prs
from review_prs import (
    Choice,
    PullRequest,
    Result,
    Verdict,
    age,
    assess,
    decide,
    github_slug,
    list_prs,
    merge_requested,
    parse_checks,
    render_json,
    render_report,
    render_summary,
    repo_slug,
    review,
)

SHA = "a" * 40
OTHER_SHA = "b" * 40
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def git(path: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def make_repo(root: Path, name: str, origin: str | None = None) -> Path:
    path = root / name
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    if origin:
        git(path, "remote", "add", "origin", origin)
    return path


def check_run(name="test", status="COMPLETED", conclusion="SUCCESS"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }


def pr_data(number=1, **overrides):
    data = {
        "number": number,
        "title": f"Bump things {number}",
        "url": f"https://github.com/koysor/alpha/pull/{number}",
        "author": {"login": "app/dependabot", "is_bot": True},
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "statusCheckRollup": [check_run()],
        "additions": 3,
        "deletions": 1,
        "changedFiles": 1,
        "files": [{"path": "uv.lock", "additions": 3, "deletions": 1}],
        "headRefName": f"dependabot/uv/thing-{number}",
        "headRefOid": SHA,
        "baseRefName": "main",
        "createdAt": "2026-11-07T12:00:00Z",
    }
    data.update(overrides)
    return data


def make_pr(**overrides) -> PullRequest:
    return PullRequest.from_gh("alpha", "koysor/alpha", pr_data(**overrides))


class FakeGh:
    """Stands in for the gh CLI: canned `pr list`/`pr view` JSON, recorded merges."""

    def __init__(self, lists=None, views=None, merge_rc=0, list_rc=0):
        self.lists: dict[str, list] = lists or {}
        self.views: dict[tuple[str, int], list] = views or {}
        self.merge_rc = merge_rc
        self.list_rc = list_rc
        self.calls: list[list[str]] = []

    @property
    def merges(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        slug = args[args.index("-R") + 1]
        if args[:2] == ["pr", "list"]:
            if self.list_rc:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 404: Not Found")
            return subprocess.CompletedProcess(
                args, 0, json.dumps(self.lists.get(slug, [])), ""
            )
        if args[:2] == ["pr", "view"]:
            queue = self.views[(slug, int(args[2]))]
            data = queue.pop(0) if len(queue) > 1 else queue[0]
            return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
        if args[:2] == ["pr", "merge"]:
            if self.merge_rc:
                return subprocess.CompletedProcess(
                    args, 1, "", "Head branch was modified"
                )
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected gh call {args}")


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(review_prs, "RETRY_DELAY", 0)


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "root"
    make_repo(root, "alpha", "https://github.com/koysor/alpha.git")
    return root


def collect():
    chunks: list[str] = []
    return chunks, chunks.append


def answers(*replies: str):
    queue = list(replies)

    def prompt(_message: str) -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    return prompt


# --- remotes ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/koysor/alpha.git",
        "https://github.com/koysor/alpha",
        "https://github.com/koysor/alpha/",
        "https://user@github.com/koysor/alpha.git",
        "git@github.com:koysor/alpha.git",
        "ssh://git@github.com/koysor/alpha.git",
    ],
)
def test_github_slug_accepts_github_urls(url):
    assert github_slug(url) == "koysor/alpha"


def test_github_slug_keeps_dots_in_names():
    assert github_slug("https://github.com/koysor/my.repo.git") == "koysor/my.repo"


@pytest.mark.parametrize(
    "url", ["https://gitlab.com/koysor/alpha.git", "/srv/git/alpha.git", ""]
)
def test_github_slug_rejects_other_hosts(url):
    assert github_slug(url) is None


def test_repo_slug_reads_origin(root):
    assert repo_slug(root / "alpha") == "koysor/alpha"


def test_repo_without_origin_needs_attention(root):
    found = list_prs(make_repo(root, "beta"), FakeGh())
    assert found.error == "no 'origin' remote"


def test_repo_not_on_github_needs_attention(root):
    path = make_repo(root, "beta", "https://gitlab.com/koysor/beta.git")
    found = list_prs(path, FakeGh())
    assert found.error is not None
    assert "not on GitHub" in found.error


# --- checks ----------------------------------------------------------------


def test_parse_checks_counts_check_runs():
    checks = parse_checks(
        [
            check_run("a"),
            check_run("b", conclusion="SKIPPED"),
            check_run("c", conclusion="NEUTRAL"),
            check_run("d", conclusion="FAILURE"),
            check_run("e", conclusion="CANCELLED"),
            check_run("f", status="IN_PROGRESS", conclusion=""),
        ]
    )
    assert (checks.passed, checks.skipped) == (1, 2)
    assert checks.failed == ["d", "e"]
    assert checks.pending == ["f"]
    assert checks.total == 6


def test_parse_checks_counts_status_contexts():
    rollup = [
        {"__typename": "StatusContext", "context": "ci/a", "state": "SUCCESS"},
        {"__typename": "StatusContext", "context": "ci/b", "state": "PENDING"},
        {"__typename": "StatusContext", "context": "ci/c", "state": "ERROR"},
    ]
    checks = parse_checks(rollup)
    assert checks.passed == 1
    assert checks.pending == ["ci/b"]
    assert checks.failed == ["ci/c"]


def test_parse_checks_handles_no_rollup():
    assert parse_checks(None).total == 0
    assert parse_checks(None).describe() == "no checks"


# --- assess ----------------------------------------------------------------


def test_green_bot_pr_is_recommended():
    assert assess(make_pr()).verdict is Verdict.MERGE


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"isDraft": True}, "draft"),
        ({"mergeable": "CONFLICTING"}, "conflicts with main"),
        ({"mergeStateStatus": "DIRTY"}, "conflicts with main"),
        (
            {"statusCheckRollup": [check_run("lint", conclusion="FAILURE")]},
            "checks failed: lint",
        ),
        ({"reviewDecision": "CHANGES_REQUESTED"}, "changes requested in review"),
    ],
)
def test_blocked_rules(overrides, reason):
    assessment = assess(make_pr(**overrides))
    assert assessment.verdict is Verdict.BLOCKED
    assert reason in assessment.reasons
    assert not assessment.offered


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"statusCheckRollup": [check_run(status="QUEUED", conclusion="")]},
            "checks still running",
        ),
        ({"statusCheckRollup": []}, "no CI checks ran"),
        ({"mergeable": "UNKNOWN"}, "not yet worked out"),
        ({"mergeStateStatus": "BEHIND"}, "behind main"),
        ({"mergeStateStatus": "BLOCKED"}, "branch protection"),
        ({"author": {"login": "koysor", "is_bot": False}}, "not a bot"),
    ],
)
def test_review_rules(overrides, reason):
    assessment = assess(make_pr(**overrides))
    assert assessment.verdict is Verdict.REVIEW
    assert any(reason in r for r in assessment.reasons)
    assert assessment.offered


def test_human_dependency_change_is_called_out():
    pr = make_pr(
        author={"login": "koysor", "is_bot": False},
        files=[{"path": "pyproject.toml"}],
    )
    assert "changes dependencies" in assess(pr).reasons


def test_blocked_wins_over_review():
    pr = make_pr(isDraft=True, author={"login": "koysor", "is_bot": False})
    assert assess(pr).verdict is Verdict.BLOCKED


# --- listing ---------------------------------------------------------------


def test_list_prs_reads_open_prs(root):
    gh = FakeGh(lists={"koysor/alpha": [pr_data(1), pr_data(2)]})
    found = list_prs(root / "alpha", gh)
    assert found.error is None
    assert [pr.ref for pr in found.prs] == ["alpha#1", "alpha#2"]
    assert found.prs[0].files == ["uv.lock"]
    assert found.prs[0].author_is_bot


def test_unknown_mergeable_is_fetched_again(root):
    gh = FakeGh(
        lists={"koysor/alpha": [pr_data(1, mergeable="UNKNOWN")]},
        views={("koysor/alpha", 1): [pr_data(1, mergeable="MERGEABLE")]},
    )
    found = list_prs(root / "alpha", gh)
    assert found.prs[0].mergeable == "MERGEABLE"
    assert gh.calls[-1][:3] == ["pr", "view", "1"]


def test_unknown_mergeable_gives_up_after_retries(root):
    gh = FakeGh(
        lists={"koysor/alpha": [pr_data(1, mergeable="UNKNOWN")]},
        views={("koysor/alpha", 1): [pr_data(1, mergeable="UNKNOWN")]},
    )
    found = list_prs(root / "alpha", gh)
    assert found.prs[0].mergeable == "UNKNOWN"
    views = [c for c in gh.calls if c[:2] == ["pr", "view"]]
    assert len(views) == review_prs.UNKNOWN_RETRIES


def test_gh_failure_is_reported_not_raised(root):
    found = list_prs(root / "alpha", FakeGh(list_rc=1))
    assert found.error is not None
    assert "HTTP 404" in found.error


def test_gh_invalid_json_is_reported(root):
    def gh(args):
        return subprocess.CompletedProcess(list(args), 0, "not json", "")

    found = list_prs(root / "alpha", gh)
    assert found.error is not None
    assert "invalid JSON" in found.error


def test_gh_timeout_is_reported(root):
    def gh(args):
        raise subprocess.TimeoutExpired(["gh"], 1)

    found = list_prs(root / "alpha", gh)
    assert found.error is not None
    assert "timed out" in found.error


# --- rendering -------------------------------------------------------------


def test_age():
    assert age("2026-11-07T12:00:00Z", NOW) == "1d ago"
    assert age("2026-11-08T09:00:00Z", NOW) == "3h ago"
    assert age("2026-11-08T11:59:30Z", NOW) == "just now"
    assert age("garbage", NOW) == "?"


def test_render_report_lists_prs_problems_and_quiet_repos(root, tmp_path):
    make_repo(root, "beta", "https://github.com/koysor/beta.git")
    make_repo(root, "gamma")
    gh = FakeGh(lists={"koysor/alpha": [pr_data(1)]})
    repos = [list_prs(p, gh) for p in sorted(root.iterdir())]
    text = render_report(repos, now=NOW)
    assert "alpha#1  Bump things 1" in text
    assert "+3/-1 in 1 file | opened 1d ago" in text
    assert "checks: 1 passed" in text
    assert "-> merge:" in text
    assert "gamma: no 'origin' remote" in text
    assert "No open pull requests: beta" in text


def test_render_report_truncates_file_list():
    files = [{"path": f"f{i}.py"} for i in range(5)]
    repo = review_prs.RepoPRs(Path("alpha"), "koysor/alpha", [make_pr(files=files)])
    text = render_report([repo], max_files=2, now=NOW)
    assert "    f1.py" in text
    assert "    f2.py" not in text
    assert "... and 3 more" in text
    assert "    f4.py" in render_report([repo], max_files=0, now=NOW)


def test_render_json_shape():
    repo = review_prs.RepoPRs(Path("alpha"), "koysor/alpha", [make_pr()])
    payload = json.loads(render_json([repo]))
    item = payload["repos"][0]["prs"][0]
    assert payload["repos"][0]["slug"] == "koysor/alpha"
    assert item["ref"] == "alpha#1"
    assert item["merge_arg"] == f"alpha#1@{SHA}"
    assert item["verdict"] == "merge"
    assert item["checks"]["total"] == 1
    assert item["files"] == ["uv.lock"]


def test_render_summary_reminds_to_pull():
    text = render_summary(
        [
            review_prs.Outcome("alpha#1", Result.MERGED),
            review_prs.Outcome("beta#2", Result.FAILED, "boom"),
        ]
    )
    assert "beta#2: failed (boom)" in text
    assert "1 merged, 1 failed." in text
    assert "behind (alpha)" in text
    assert "sync_repos.py" in text


# --- decide ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "choice", "merge_all"),
    [
        ("y", Choice.YES, False),
        ("YES", Choice.YES, False),
        ("a", Choice.YES, True),
        ("q", Choice.QUIT, False),
        ("", Choice.NO, False),
        ("nope", Choice.NO, False),
    ],
)
def test_decide_answers(reply, choice, merge_all):
    pr = make_pr()
    assert decide(pr, assess(pr), answers(reply), False) == (choice, merge_all)


def test_decide_eof_quits():
    pr = make_pr()
    assert decide(pr, assess(pr), answers(), False) == (Choice.QUIT, False)


def test_decide_all_still_asks_about_review_prs():
    pr = make_pr(statusCheckRollup=[])
    asked = []

    def prompt(message):
        asked.append(message)
        return "n"

    assert decide(pr, assess(pr), prompt, True) == (Choice.NO, True)
    assert "needs review" in asked[0]


# --- whole workflow --------------------------------------------------------


def test_check_reports_and_never_merges(root):
    gh = FakeGh(lists={"koysor/alpha": [pr_data(1)]})
    chunks, out = collect()
    assert review(root, check=True, gh=gh, out=out, now=NOW) == 1
    assert not gh.merges
    assert "alpha#1" in "".join(chunks)


def test_check_with_nothing_open_exits_zero(root):
    assert review(root, check=True, gh=FakeGh(), out=collect()[1]) == 0


def test_check_with_broken_repo_exits_two(root):
    make_repo(root, "beta")
    assert review(root, check=True, gh=FakeGh(), out=collect()[1]) == 2


def test_json_never_merges(root):
    gh = FakeGh(lists={"koysor/alpha": [pr_data(1)]})
    chunks, out = collect()
    assert review(root, as_json=True, gh=gh, out=out) == 1
    assert not gh.merges
    assert json.loads("".join(chunks))["repos"][0]["prs"][0]["number"] == 1


def test_approved_pr_is_squash_merged_at_reviewed_head(root):
    gh = FakeGh(lists={"koysor/alpha": [pr_data(1)]})
    chunks, out = collect()
    assert review(root, gh=gh, prompt=answers("y"), out=out) == 0
    assert gh.merges == [
        [
            "pr",
            "merge",
            "1",
            "-R",
            "koysor/alpha",
            "--squash",
            "--delete-branch",
            "--match-head-commit",
            SHA,
        ]
    ]
    assert "alpha#1: merged" in "".join(chunks)


def test_declined_pr_is_left_alone(root):
    gh = FakeGh(lists={"koysor/alpha": [pr_data(1)]})
    assert review(root, gh=gh, prompt=answers("n"), out=collect()[1]) == 0
    assert not gh.merges


def test_blocked_pr_is_never_offered(root):
    gh = FakeGh(lists={"koysor/alpha": [pr_data(1, isDraft=True)]})
    asked = []

    def prompt(message):
        asked.append(message)
        return "y"

    chunks, out = collect()
    assert review(root, gh=gh, prompt=prompt, out=out) == 0
    assert not asked
    assert not gh.merges
    assert "alpha#1: skipped (draft)" in "".join(chunks)


def test_all_covers_only_recommended_prs(root):
    gh = FakeGh(
        lists={
            "koysor/alpha": [
                pr_data(1),
                pr_data(2, statusCheckRollup=[]),  # REVIEW
                pr_data(3),
            ]
        }
    )
    asked = []

    def prompt(message):
        asked.append(message)
        return "a" if len(asked) == 1 else "n"

    review(root, gh=gh, prompt=prompt, out=collect()[1])
    assert len(asked) == 2  # PR 1 ("a") and PR 2 (review, declined)
    assert [m[2] for m in gh.merges] == ["1", "3"]


def test_quit_stops_asking(root):
    gh = FakeGh(lists={"koysor/alpha": [pr_data(1), pr_data(2)]})
    review(root, gh=gh, prompt=answers("q", "y"), out=collect()[1])
    assert not gh.merges


def test_yes_merges_recommended_prs_only(root):
    gh = FakeGh(lists={"koysor/alpha": [pr_data(1), pr_data(2, statusCheckRollup=[])]})
    chunks, out = collect()
    assert review(root, assume_yes=True, gh=gh, out=out) == 0
    assert [m[2] for m in gh.merges] == ["1"]
    assert "alpha#2: declined (needs review" in "".join(chunks)


def test_failed_merge_exits_one(root):
    gh = FakeGh(lists={"koysor/alpha": [pr_data(1)]}, merge_rc=1)
    chunks, out = collect()
    assert review(root, gh=gh, prompt=answers("y"), out=out) == 1
    assert "alpha#1: failed (Head branch was modified)" in "".join(chunks)


def test_interrupt_exits_130(root):
    gh = FakeGh(lists={"koysor/alpha": [pr_data(1)]})

    def prompt(_message):
        raise KeyboardInterrupt

    assert review(root, gh=gh, prompt=prompt, out=collect()[1]) == 130
    assert not gh.merges


def test_no_repos(tmp_path):
    chunks, out = collect()
    assert review(tmp_path, gh=FakeGh(), out=out) == 0
    assert "No git repos found" in "".join(chunks)


# --- --merge ---------------------------------------------------------------


def test_merge_requested_merges_named_pr(root):
    gh = FakeGh(views={("koysor/alpha", 1): [pr_data(1)]})
    chunks, out = collect()
    assert merge_requested(root, [f"alpha#1@{SHA}"], gh, out) == 0
    assert gh.merges[0][-1] == SHA
    assert "alpha#1: merged" in "".join(chunks)


def test_merge_requested_refuses_moved_head(root):
    gh = FakeGh(views={("koysor/alpha", 1): [pr_data(1, headRefOid=OTHER_SHA)]})
    chunks, out = collect()
    assert merge_requested(root, [f"alpha#1@{SHA}"], gh, out) == 1
    assert not gh.merges
    assert "new commits since review" in "".join(chunks)


def test_merge_requested_refuses_blocked_pr(root):
    gh = FakeGh(views={("koysor/alpha", 1): [pr_data(1, mergeable="CONFLICTING")]})
    chunks, out = collect()
    assert merge_requested(root, [f"alpha#1@{SHA}"], gh, out) == 1
    assert not gh.merges
    assert "conflicts with main" in "".join(chunks)


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("alpha#1", "expected REPO#NUMBER@"),
        ("alpha#1@abc123", "expected REPO#NUMBER@"),
        (f"nope#1@{SHA}", "no repo 'nope'"),
    ],
)
def test_merge_requested_rejects_bad_refs(root, ref, message):
    gh = FakeGh()
    chunks, out = collect()
    assert merge_requested(root, [ref], gh, out) == 1
    assert not gh.calls
    assert message in "".join(chunks)
