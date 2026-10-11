"""Guards for the dormant QA-to-Developer bridge."""

import json
from pathlib import Path
from urllib.request import Request

import pytest

from automations.qa_loop.bridge import (
    QA_AUTOMATION_ID,
    CursorWebhook,
    GithubCommentStore,
    MemoryStore,
    MemoryWebhook,
    VerdictError,
    decide,
    event_from_github,
    execute,
    initial_state,
    normalize_token,
    parse_done_marker,
    parse_qa_review,
    render_state_comment,
    run_from_env,
    state_from_comment,
    validate_webhook_url,
)

ROOT = Path(__file__).resolve().parents[2]
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
SHA_E = "e" * 40


def qa_body(result: str, sha: str = SHA_A, automation: str = QA_AUTOMATION_ID) -> str:
    return (
        f"<!-- CURSOR_AUTOMATION_ID: {automation} | RUN_ID: bc-test -->\n"
        f'<!-- pdf-qa-verdict: {{"schema":1,"result":"{result}","sha":"{sha}"}} -->\n'
        f"## QA 结论：{result}\n"
    )


def verdict(result: str, sha: str, review_id: str, submitted_at: str, **extra) -> dict:
    event = {
        "kind": "qa_verdict",
        "pr": 1,
        "sha": sha,
        "result": result,
        "machine_sha": sha,
        "review_id": review_id,
        "submitted_at": submitted_at,
        "github_review_state": "COMMENTED",
    }
    event.update(extra)
    return event


def done(attempted: str, status: str, review_id: str, submitted_at: str, pushed: str | None = None) -> dict:
    event = {
        "kind": "autofix_done",
        "pr": 1,
        "attempted_sha": attempted,
        "status": status,
        "review_id": review_id,
        "submitted_at": submitted_at,
    }
    if pushed:
        event["pushed_sha"] = pushed
    return event


def apply_all(events: list[dict]) -> list[dict]:
    state = initial_state(1)
    decisions = []
    for event in events:
        decision = decide(state, event)
        decisions.append(decision)
        state = decision["state"]
    return decisions


def test_historical_review_headings_parse():
    fail = (
        f"<!-- CURSOR_AUTOMATION_ID: {QA_AUTOMATION_ID} | RUN_ID: bc-9f0f4777 -->\n"
        "## 审查结论：FAIL\n"
    )
    passed = (
        f"<!-- CURSOR_AUTOMATION_ID: {QA_AUTOMATION_ID} | RUN_ID: bc-1633051e -->\n"
        "## QA 结论：PASS\n"
    )
    assert parse_qa_review(fail)["result"] == "FAIL"
    assert parse_qa_review(passed)["result"] == "PASS"


def test_conflicting_or_foreign_reviews_are_rejected():
    with pytest.raises(VerdictError, match="mixed_automation"):
        parse_qa_review(qa_body("FAIL") + "\n<!-- CURSOR_AUTOMATION_ID: 11111111-1111-1111-1111-111111111111 -->")
    with pytest.raises(VerdictError, match="verdict_conflict"):
        parse_qa_review(
            f"<!-- CURSOR_AUTOMATION_ID: {QA_AUTOMATION_ID} -->\n"
            f'<!-- pdf-qa-verdict: {{"schema":1,"result":"PASS","sha":"{SHA_A}"}} -->\n'
            "## QA 结论：FAIL\n"
        )
    with pytest.raises(VerdictError, match="not_qa_automation"):
        parse_qa_review("## QA 结论：FAIL\n")


def test_done_marker_rejects_qa_body_and_unchanged_sha():
    with pytest.raises(VerdictError, match="done_mixed_with_qa"):
        parse_done_marker(qa_body("FAIL") + done_body(SHA_A, "no_change"))
    with pytest.raises(VerdictError, match="pushed_sha_unchanged"):
        parse_done_marker(done_body(SHA_A, "pushed", SHA_A))


def done_body(attempted: str, status: str, pushed: str | None = None) -> str:
    payload = {"schema": 1, "pr": 1, "attempted_sha": attempted, "status": status}
    if pushed:
        payload["pushed_sha"] = pushed
    return f"<!-- pdf-autofix-done: {json.dumps(payload)} -->"


def test_three_rounds_one_active_fix_and_no_repeat_sha():
    decisions = apply_all(
        [
            verdict("FAIL", SHA_A, "r1", "2026-10-10T15:01:00Z"),
            verdict("FAIL", SHA_A, "r2", "2026-10-10T15:02:00Z"),
            done(SHA_A, "pushed", "d1", "2026-10-10T15:03:00Z", SHA_B),
            verdict("FAIL", SHA_B, "r3", "2026-10-10T15:04:00Z"),
            verdict("FAIL", SHA_C, "r4", "2026-10-10T15:05:00Z"),
            done(SHA_B, "pushed", "d2", "2026-10-10T15:06:00Z", SHA_C),
            done(SHA_C, "no_change", "d3", "2026-10-10T15:07:00Z"),
            verdict("FAIL", SHA_D, "r5", "2026-10-10T15:08:00Z"),
        ]
    )
    assert [item["action"] for item in decisions] == [
        "dispatch",
        "ignore",
        "clear_in_progress",
        "dispatch",
        "defer_in_progress",
        "dispatch",
        "clear_in_progress",
        "stop_max_rounds",
    ]
    assert [item["dispatch"] for item in decisions] == [
        True,
        False,
        False,
        True,
        False,
        True,
        False,
        False,
    ]
    assert decisions[-1]["state"]["round"] == 3
    assert decisions[-1]["state"]["in_progress"] is False
    assert decisions[4]["state"]["in_progress"] is True
    assert decisions[5]["state"]["active_run_sha"] == SHA_C


def test_pass_waits_for_human_and_not_verified_is_not_pass():
    state = initial_state(1)
    paused = decide(state, verdict("NOT VERIFIED", SHA_A, "n1", "2026-10-10T15:01:00Z"))
    assert paused["action"] == "pause_not_verified"
    assert paused["dispatch"] is False
    assert paused["state"]["round"] == 0
    assert paused["state"]["stopped_reason"] == "not_verified"
    assert paused["action"] != "stop_pass"

    failed = decide(paused["state"], verdict("FAIL", SHA_A, "f1", "2026-10-10T15:02:00Z"))
    assert failed["action"] == "dispatch"
    assert failed["state"]["round"] == 1

    passed = decide(failed["state"], verdict("PASS", SHA_B, "p1", "2026-10-10T15:03:00Z"))
    assert passed["action"] == "stop_pass"
    assert passed["dispatch"] is False
    assert passed["state"]["in_progress"] is False
    assert passed["state"]["stopped_reason"] == "pass"

    same_sha = decide(passed["state"], verdict("FAIL", SHA_B, "f2", "2026-10-10T15:04:00Z"))
    assert same_sha["action"] == "ignore"
    assert same_sha["reason"] == "waiting_for_human"
    assert same_sha["dispatch"] is False

    new_sha = decide(passed["state"], verdict("FAIL", SHA_E, "f3", "2026-10-10T15:05:00Z"))
    assert new_sha["action"] == "dispatch"
    assert new_sha["state"]["round"] == 2


def test_human_and_other_automation_do_not_start_a_fix():
    human = {
        "review": {
            "id": 7,
            "body": qa_body("FAIL"),
            "commit_id": SHA_A,
            "state": "COMMENTED",
            "submitted_at": "2026-10-10T15:01:00Z",
            "user": {"login": "The-sky1234", "type": "User"},
        },
        "pull_request": {"number": 1},
    }
    assert event_from_github(human, "pull_request_review") is None

    other = {
        "review": {
            "id": 8,
            "body": qa_body("FAIL", automation="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            "commit_id": SHA_A,
            "state": "COMMENTED",
            "submitted_at": "2026-10-10T15:01:00Z",
            "user": {"login": "cursor[bot]", "type": "Bot"},
        },
        "pull_request": {"number": 1},
    }
    assert event_from_github(other, "pull_request_review") is None


def test_github_approval_cannot_carry_a_fail_verdict():
    payload = {
        "review": {
            "id": 9,
            "body": qa_body("FAIL"),
            "commit_id": SHA_A,
            "state": "APPROVED",
            "submitted_at": "2026-10-10T15:01:00Z",
            "user": {"login": "cursor[bot]", "type": "Bot"},
        },
        "pull_request": {"number": 1},
    }
    event = event_from_github(payload, "pull_request_review")
    decision = decide(initial_state(1), event)
    assert decision["action"] == "ignore"
    assert decision["reason"] == "approval_conflicts_with_verdict"
    assert decision["dispatch"] is False


def test_observe_does_not_save_or_call_webhook_and_failed_post_does_not_advance():
    event = verdict("FAIL", SHA_A, "r1", "2026-10-10T15:01:00Z")
    observed = MemoryStore(1)
    observe_hook = MemoryWebhook()
    code, decision = execute("observe", event, observed, observe_hook)
    assert code == 0
    assert decision["dispatch"] is True
    assert observed.saves == []
    assert observe_hook.contexts == []

    armed = MemoryStore(1)
    failed_hook = MemoryWebhook(ok=False)
    code, _ = execute("armed", event, armed, failed_hook)
    assert code == 1
    assert armed.state["round"] == 0
    assert armed.state["in_progress"] is False
    assert armed.state["attempted_shas"] == []
    assert failed_hook.contexts == [decision["webhook_context"]]

    armed_ok = MemoryStore(1)
    ok_hook = MemoryWebhook(ok=True)
    code, armed_decision = execute("armed", event, armed_ok, ok_hook)
    assert code == 0
    assert armed_ok.saves[0]["round"] == 1
    assert armed_ok.saves[0]["in_progress"] is True
    assert "不要合并" in armed_decision["webhook_context"]
    assert "生产数据库" in armed_decision["webhook_context"]


def test_state_comment_roundtrip_and_corrupt_state_fails_closed():
    state = initial_state(1)
    state["round"] = 2
    state["attempted_shas"] = [SHA_A]
    restored = state_from_comment(render_state_comment(state), 1)
    assert restored["round"] == 2
    assert restored["attempted_shas"] == [SHA_A]
    with pytest.raises(VerdictError, match="state_corrupt"):
        state_from_comment("<!-- pdf-autofix-state\n{\"round\": 9}\n-->", 1)


def test_webhook_target_and_token_are_constrained():
    with pytest.raises(ValueError):
        validate_webhook_url("https://example.com/hook")
    assert validate_webhook_url(
        "https://api2.cursor.sh/automations/webhook/fdc3199e-c4b6-11f1-ac31-5e2d0494121f"
    )
    assert normalize_token("secret\n") == "secret"
    with pytest.raises(ValueError):
        normalize_token("sec ret")
    with pytest.raises(ValueError):
        normalize_token(" \n")
    hook = CursorWebhook("https://example.com/hook", "token")
    assert hook.post("context") is False


def test_workflow_is_dormant_and_cannot_merge():
    workflow = (ROOT / ".github/workflows/qa-autofix-bridge.yml").read_text(encoding="utf-8")
    readme = (ROOT / "automations/qa_loop/README.md").read_text(encoding="utf-8")
    developer = (ROOT / "automations/qa_loop/prompts/developer-autofix.md").read_text(encoding="utf-8")
    assert "gh pr merge" not in workflow
    assert "QA_AUTOFIX_MODE: armed" not in workflow
    assert "workflow_dispatch" in workflow
    assert "vars.QA_AUTOFIX_MODE == 'armed'" in workflow
    assert "不合并" in readme
    assert "保持关闭" in developer
    assert "真实 PDF" in developer


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_runner_idle_and_observe_do_not_post(monkeypatch, tmp_path):
    assert run_from_env({"GITHUB_EVENT_NAME": "pull_request_review"}) == 0

    payload = {
        "review": {
            "id": 11,
            "body": qa_body("PASS"),
            "commit_id": SHA_A,
            "state": "COMMENTED",
            "submitted_at": "2026-10-10T15:34:33Z",
            "user": {"login": "cursor[bot]", "type": "Bot"},
            "html_url": "https://github.com/The-sky1234/pdf-classification-9m/pull/1",
        },
        "pull_request": {"number": 1},
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload), encoding="utf-8")
    calls = []

    def opener(request: Request, timeout=30):
        calls.append((request.get_method(), request.full_url))
        if "comments?per_page=100&sort=created&direction=desc" in request.full_url:
            return _Response([])
        raise AssertionError(request.full_url)

    monkeypatch.setattr("automations.qa_loop.bridge.urllib.request.urlopen", opener)
    code = run_from_env(
        {
            "GITHUB_EVENT_NAME": "pull_request_review",
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_REPOSITORY": "The-sky1234/pdf-classification-9m",
            "GITHUB_TOKEN": "test-token",
            "QA_AUTOFIX_MODE": "observe",
        }
    )
    assert code == 0
    assert calls
    assert all(method == "GET" for method, _url in calls)


def test_runner_prints_observe_dispatch_without_network_post(monkeypatch, tmp_path, capsys):
    payload = {
        "review": {
            "id": 12,
            "body": qa_body("FAIL"),
            "commit_id": SHA_A,
            "state": "COMMENTED",
            "submitted_at": "2026-10-10T15:01:44Z",
            "user": {"login": "cursor[bot]", "type": "Bot"},
        },
        "pull_request": {"number": 1},
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload), encoding="utf-8")

    def opener(request: Request, timeout=30):
        assert request.get_method() == "GET"
        return _Response([])

    monkeypatch.setattr("automations.qa_loop.bridge.urllib.request.urlopen", opener)
    code = run_from_env(
        {
            "GITHUB_EVENT_NAME": "pull_request_review",
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_REPOSITORY": "The-sky1234/pdf-classification-9m",
            "GITHUB_TOKEN": "test-token",
            "QA_AUTOFIX_MODE": "observe",
            "CURSOR_DEVELOPER_WEBHOOK_URL": "https://api2.cursor.sh/automations/webhook/test",
            "CURSOR_DEVELOPER_WEBHOOK_TOKEN": "secret",
        }
    )
    captured = capsys.readouterr().out
    assert code == 0
    assert '"dispatch": true' in captured
    assert "observe only" in captured
    assert "secret" not in captured


def _comment(comment_id: int, body: str, login: str, user_type: str, created_at: str) -> dict:
    return {
        "id": comment_id,
        "body": body,
        "created_at": created_at,
        "user": {"login": login, "type": user_type},
    }


def _store(comments: list[dict], pr: int = 2) -> GithubCommentStore:
    def opener(request: Request, timeout=30):
        assert request.get_method() == "GET"
        return _Response(comments)

    return GithubCommentStore(
        "The-sky1234/pdf-classification-9m",
        pr,
        "test-token",
        opener=opener,
    )


def _stopped_state(pr: int) -> dict:
    state = initial_state(pr)
    state["round"] = 3
    state["stopped_reason"] = "max_rounds"
    state["attempted_shas"] = [SHA_A, SHA_B, SHA_C]
    return state


def _running_state(pr: int) -> dict:
    state = initial_state(pr)
    state["round"] = 1
    state["in_progress"] = True
    state["active_run_sha"] = SHA_A
    state["attempted_shas"] = [SHA_A]
    state["last_verdict"] = "FAIL"
    state["last_verdict_sha"] = SHA_A
    state["last_event_at"] = "2026-10-11T01:00:00Z"
    return state


def test_untrusted_state_comment_cannot_reset_or_hide_the_actions_comment():
    """A newer human comment must not replace or shadow the Actions bot state."""
    pr = 2
    comments = [
        _comment(
            201,
            "请看 pdf-autofix-state，不要手工改 JSON",
            "attacker",
            "User",
            "2026-10-11T02:00:03Z",
        ),
        _comment(
            200,
            render_state_comment(initial_state(pr)),
            "attacker",
            "User",
            "2026-10-11T02:00:02Z",
        ),
        _comment(
            100,
            render_state_comment(_stopped_state(pr)),
            "github-actions[bot]",
            "Bot",
            "2026-10-11T02:00:01Z",
        ),
    ]
    store = _store(comments, pr)
    loaded = store.load()
    assert loaded["round"] == 3
    assert loaded["stopped_reason"] == "max_rounds"
    assert store.comment_id == 100

    bare_actions = [
        _comment(
            202,
            "请看 pdf-autofix-state，不要手工改 JSON",
            "github-actions[bot]",
            "Bot",
            "2026-10-11T02:00:04Z",
        ),
        comments[2],
    ]
    skipped = _store(bare_actions, pr).load()
    assert skipped["round"] == 3


def test_corrupt_actions_state_fails_closed():
    pr = 2
    comments = [
        _comment(
            1,
            '<!-- pdf-autofix-state\n{"round": 9}\n-->',
            "github-actions[bot]",
            "Bot",
            "2026-10-11T02:00:02Z",
        ),
        _comment(
            2,
            render_state_comment(initial_state(pr)),
            "github-actions[bot]",
            "Bot",
            "2026-10-11T02:00:01Z",
        ),
    ]
    with pytest.raises(VerdictError, match="state_corrupt"):
        _store(comments, pr).load()


def test_recorded_cursor_done_marker_opens_the_lock_for_the_next_fail():
    pr = 4
    running = _running_state(pr)
    comments = [
        _comment(
            301,
            done_body(SHA_A, "pushed", SHA_B),
            "attacker",
            "User",
            "2026-10-11T02:30:00Z",
        ),
        _comment(
            300,
            done_body(SHA_A, "pushed", SHA_B),
            "cursor[bot]",
            "Bot",
            "2026-10-11T02:00:00Z",
        ),
        _comment(
            100,
            render_state_comment(running),
            "github-actions[bot]",
            "Bot",
            "2026-10-11T01:00:01Z",
        ),
    ]
    loaded = _store(comments, pr).load()
    assert loaded["in_progress"] is False
    assert loaded["active_run_sha"] is None
    decision = decide(
        loaded,
        verdict("FAIL", SHA_B, "r-next", "2026-10-11T03:00:00Z", pr=pr),
    )
    assert decision["action"] == "dispatch"
    assert decision["dispatch"] is True
    assert decision["state"]["round"] == 2

    human_only = [comments[0], comments[2]]
    stuck = _store(human_only, pr).load()
    assert stuck["in_progress"] is True
    deferred = decide(
        stuck,
        verdict("FAIL", SHA_B, "r-human", "2026-10-11T03:00:00Z", pr=pr),
    )
    assert deferred["action"] == "defer_in_progress"
    assert deferred["dispatch"] is False


def test_state_save_failure_does_not_call_the_webhook():
    class DenySave(MemoryStore):
        def save(self, state):
            raise OSError("denied")

    event = verdict("FAIL", SHA_A, "r1", "2026-10-10T15:01:00Z")
    store = DenySave(1)
    hook = MemoryWebhook(ok=True)
    code, _decision = execute("armed", event, store, hook)
    code_again, _again = execute("armed", event, store, hook)
    assert code == 1
    assert code_again == 1
    assert hook.contexts == []
    assert store.state["round"] == 0


def test_rejected_webhook_rolls_the_claimed_round_back():
    order = []

    class RecordingStore(MemoryStore):
        def save(self, state):
            order.append(("save", state["round"], state["in_progress"]))
            super().save(state)

    class RecordingHook(MemoryWebhook):
        def post(self, context):
            order.append(("post", self.ok))
            return super().post(context)

    event = verdict("FAIL", SHA_A, "r1", "2026-10-10T15:01:00Z")
    store = RecordingStore(1)
    hook = RecordingHook(ok=False)
    code, _decision = execute("armed", event, store, hook)
    assert code == 1
    assert store.state["round"] == 0
    assert store.state["in_progress"] is False
    assert store.state["attempted_shas"] == []
    assert order == [("save", 1, True), ("post", False), ("save", 0, False)]


class _CommentApi:
    def __init__(self, comments: list[dict]):
        self.comments = comments
        self.webhook_posts = 0

    def opener(self, request: Request, timeout=30):
        method = request.get_method()
        url = request.full_url
        if "api2.cursor.sh" in url:
            self.webhook_posts += 1
            return _Response({"success": True})
        if method == "GET" and "/comments" in url:
            return _Response(self.comments)
        payload = json.loads(request.data.decode())
        if method == "POST" and url.rstrip("/").endswith("/comments"):
            comment = _comment(
                900,
                payload["body"],
                "github-actions[bot]",
                "Bot",
                "2026-10-11T04:00:00Z",
            )
            self.comments.insert(0, comment)
            return _Response(comment)
        if method == "PATCH" and "/issues/comments/" in url:
            comment_id = int(url.rstrip("/").split("/")[-1])
            if comment_id == 200:
                raise OSError("forbidden")
            for comment in self.comments:
                if int(comment["id"]) == comment_id:
                    comment["body"] = payload["body"]
                    return _Response({"id": comment_id, "body": payload["body"]})
            raise OSError(f"missing comment {comment_id}")
        raise AssertionError(url)


def _run(monkeypatch, tmp_path, api: _CommentApi, payload: dict, event_name: str) -> int:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("automations.qa_loop.bridge.urllib.request.urlopen", api.opener)
    return run_from_env(
        {
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_REPOSITORY": "The-sky1234/pdf-classification-9m",
            "GITHUB_TOKEN": "test-token",
            "QA_AUTOFIX_MODE": "armed",
            "CURSOR_DEVELOPER_WEBHOOK_URL": "https://api2.cursor.sh/automations/webhook/test",
            "CURSOR_DEVELOPER_WEBHOOK_TOKEN": "secret",
        }
    )


def test_armed_runs_ignore_forged_state_and_a_later_done_marker(monkeypatch, tmp_path):
    pr = 2
    stopped = _comment(
        100,
        render_state_comment(_stopped_state(pr)),
        "github-actions[bot]",
        "Bot",
        "2026-10-11T02:00:01Z",
    )
    forged = _comment(
        200,
        render_state_comment(initial_state(pr)),
        "attacker",
        "User",
        "2026-10-11T02:00:02Z",
    )
    api = _CommentApi([forged, stopped])
    review = {
        "review": {
            "id": 50,
            "body": qa_body("FAIL", SHA_D),
            "commit_id": SHA_D,
            "state": "COMMENTED",
            "submitted_at": "2026-10-11T03:00:00Z",
            "user": {"login": "cursor[bot]", "type": "Bot"},
        },
        "pull_request": {"number": pr},
    }
    assert _run(monkeypatch, tmp_path, api, review, "pull_request_review") == 0
    assert _run(monkeypatch, tmp_path, api, review, "pull_request_review") == 0
    assert api.webhook_posts == 0

    pr = 4
    running = _comment(
        100,
        render_state_comment(_running_state(pr)),
        "github-actions[bot]",
        "Bot",
        "2026-10-11T01:00:01Z",
    )
    done = _comment(
        300,
        done_body(SHA_A, "pushed", SHA_B),
        "cursor[bot]",
        "Bot",
        "2026-10-11T02:00:00Z",
    )
    api = _CommentApi([done, running])
    human = {
        "action": "created",
        "issue": {"number": pr, "pull_request": {"url": "https://example.test/pull/4"}},
        "comment": {
            "id": 400,
            "body": "looks good",
            "created_at": "2026-10-11T02:10:00Z",
            "user": {"login": "attacker", "type": "User"},
        },
    }
    assert _run(monkeypatch, tmp_path, api, human, "issue_comment") == 0
    assert api.webhook_posts == 0
    saved = state_from_comment(running["body"], pr)
    assert saved["in_progress"] is False

    later_fail = {
        "review": {
            "id": 60,
            "body": qa_body("FAIL", SHA_B),
            "commit_id": SHA_B,
            "state": "COMMENTED",
            "submitted_at": "2026-10-11T03:00:00Z",
            "user": {"login": "cursor[bot]", "type": "Bot"},
        },
        "pull_request": {"number": pr},
    }
    assert _run(monkeypatch, tmp_path, api, later_fail, "pull_request_review") == 0
    assert api.webhook_posts == 1
    assert _run(monkeypatch, tmp_path, api, later_fail, "pull_request_review") == 0
    assert api.webhook_posts == 1
