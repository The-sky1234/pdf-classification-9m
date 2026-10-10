"""Guards for the dormant QA-to-Developer bridge."""

import json
from pathlib import Path
from urllib.request import Request

import pytest

from automations.qa_loop.bridge import (
    QA_AUTOMATION_ID,
    CursorWebhook,
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
    assert armed.saves == []
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
