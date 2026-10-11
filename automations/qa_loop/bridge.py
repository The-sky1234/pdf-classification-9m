"""Decide when a QA review may start one Developer autofix run.

The decision function is pure. GitHub and Cursor are contacted only by
``run_from_env``, and only the workflow calls that entry point.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

QA_AUTOMATION_ID = "fdc3199e-c4b6-11f1-ac31-5e2d0494121f"
DEVELOPER_AUTOMATION_ID = "7442224a-c4bd-11f1-ac31-5e2d0494121f"
MAX_AUTOFIX_ROUNDS = 3
MAX_COMMENT_PAGES = 50
WEBHOOK_HOST = "api2.cursor.sh"
WEBHOOK_PATH_RE = re.compile(r"/automations/webhook/[A-Za-z0-9-]+")
GITHUB_API = "https://api.github.com"
CURSOR_LOGINS = frozenset({"cursor[bot]", "cursor"})
ACTIONS_LOGIN = "github-actions[bot]"
RESULTS = frozenset({"PASS", "FAIL", "NOT VERIFIED"})
DONE_STATUSES = frozenset({"pushed", "no_change", "blocked"})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
AUTOMATION_RE = re.compile(
    r"CURSOR_AUTOMATION_ID:\s*([0-9a-fA-F-]{36})"
)
HEADING_RE = re.compile(
    r"^##\s*(?:QA 结论|审查结论)\s*[:：]\s*(PASS|FAIL|NOT VERIFIED)\s*$",
    re.MULTILINE,
)
VERDICT_RE = re.compile(
    r"<!--\s*pdf-qa-verdict:\s*(\{[^{}]*\})\s*-->",
    re.DOTALL,
)
DONE_RE = re.compile(
    r"<!--\s*pdf-autofix-done:\s*(\{[^{}]*\})\s*-->",
    re.DOTALL,
)
STATE_RE = re.compile(
    r"<!--\s*pdf-autofix-state\s*(\{.*\})\s*-->",
    re.DOTALL,
)

# Actions that must be stored before a later event can see them.
PERSIST_ACTIONS = frozenset(
    {
        "stop_pass",
        "pause_not_verified",
        "defer_in_progress",
        "stop_max_rounds",
        "clear_in_progress",
    }
)


class VerdictError(ValueError):
    """The review body claims to be a QA verdict but cannot be trusted."""


SAVE_ERRORS = (OSError, VerdictError, json.JSONDecodeError)


def initial_state(pr: int) -> dict:
    return {
        "schema": 1,
        "pr": pr,
        "round": 0,
        "max_rounds": MAX_AUTOFIX_ROUNDS,
        "in_progress": False,
        "active_run_sha": None,
        "attempted_shas": [],
        "pending_sha": None,
        "pending_result": None,
        "pending_review_id": None,
        "pending_event_at": None,
        "last_verdict": None,
        "last_verdict_sha": None,
        "last_review_id": None,
        "last_event_at": None,
        "stopped_reason": None,
        "dispatch_confirmed": True,
    }


def validate_webhook_url(url: str) -> str:
    """Return a Cursor automation webhook URL, or raise ValueError.

    The path must be a single webhook id. Dot segments, queries, and
    userinfo are rejected so a secret cannot be aimed at another route.
    """
    cleaned = url.strip()
    if cleaned != url or not cleaned:
        raise ValueError("webhook URL must be the Cursor automations endpoint")
    parts = urlsplit(cleaned)
    if (
        parts.scheme != "https"
        or parts.netloc != WEBHOOK_HOST
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise ValueError("webhook URL must be the Cursor automations endpoint")
    path = parts.path
    if "\\" in path or ".." in path.split("/") or not WEBHOOK_PATH_RE.fullmatch(path):
        raise ValueError("webhook URL path is not a single automation webhook")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def normalize_token(token: str) -> str:
    """Strip the trailing newline that makes GitHub curl send a bad body."""
    cleaned = token.strip()
    if not cleaned or any(char.isspace() for char in cleaned):
        raise ValueError("webhook token is empty or contains whitespace")
    return cleaned


def _outside_fences(body: str) -> str:
    """Drop fenced examples so a quoted format block cannot change the verdict."""
    return re.sub(r"```.*?```", "", body or "", flags=re.DOTALL)


def parse_qa_review(body: str) -> dict:
    """Extract the QA result and automation id from a review body.

    The GitHub event supplies the commit SHA. A machine-readable tag wins
    over the heading; if both exist they must agree.
    """
    visible = _outside_fences(body or "")
    automation_ids = AUTOMATION_RE.findall(visible)
    if QA_AUTOMATION_ID not in automation_ids:
        raise VerdictError("not_qa_automation")
    if any(item.lower() != QA_AUTOMATION_ID for item in automation_ids):
        raise VerdictError("mixed_automation")

    headings = HEADING_RE.findall(visible)
    heading_result = headings[0] if len(set(headings)) == 1 and headings else None
    if headings and heading_result is None:
        raise VerdictError("conflicting_headings")

    machine = VERDICT_RE.search(visible)
    machine_result = None
    machine_sha = None
    if machine:
        try:
            payload = json.loads(machine.group(1))
        except json.JSONDecodeError as exc:
            raise VerdictError("bad_verdict_json") from exc
        if "schema" in payload and payload["schema"] != 1:
            raise VerdictError("bad_verdict_schema")
        machine_result = payload.get("result")
        machine_sha = payload.get("sha")
        if machine_result not in RESULTS:
            raise VerdictError("bad_verdict_result")
        if machine_sha is not None and not SHA_RE.fullmatch(str(machine_sha)):
            raise VerdictError("bad_verdict_sha")
        if heading_result and heading_result != machine_result:
            raise VerdictError("verdict_conflict")

    result = machine_result or heading_result
    if result not in RESULTS:
        raise VerdictError("missing_verdict")
    return {
        "result": result,
        "automation_id": QA_AUTOMATION_ID,
        "machine_sha": machine_sha,
    }


def parse_done_marker(body: str) -> dict:
    """Extract a Developer completion marker. QA verdicts are not completions.

    Cursor prepends ``CURSOR_AUTOMATION_ID`` to the comment. Only the
    Developer automation id is a completion; the QA id and any other id are not.
    """
    visible = _outside_fences(body or "")
    if VERDICT_RE.search(visible):
        raise VerdictError("done_mixed_with_qa")
    automation_ids = [item.lower() for item in AUTOMATION_RE.findall(visible)]
    if any(item == QA_AUTOMATION_ID for item in automation_ids):
        raise VerdictError("done_mixed_with_qa")
    if not automation_ids:
        raise VerdictError("missing_developer_automation")
    if any(item != DEVELOPER_AUTOMATION_ID for item in automation_ids):
        raise VerdictError("foreign_developer_automation")
    match = DONE_RE.search(visible)
    if not match:
        raise VerdictError("missing_done_marker")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise VerdictError("bad_done_json") from exc
    if "schema" in payload and payload["schema"] != 1:
        raise VerdictError("bad_done_schema")
    status = payload.get("status")
    attempted = str(payload.get("attempted_sha") or "")
    pushed = payload.get("pushed_sha")
    if status not in DONE_STATUSES or not SHA_RE.fullmatch(attempted):
        raise VerdictError("bad_done_fields")
    if status == "pushed":
        if not isinstance(pushed, str) or not SHA_RE.fullmatch(pushed):
            raise VerdictError("bad_pushed_sha")
        if pushed == attempted:
            raise VerdictError("pushed_sha_unchanged")
    elif pushed not in (None, ""):
        raise VerdictError("unexpected_pushed_sha")
    return {
        "attempted_sha": attempted,
        "status": status,
        "pushed_sha": pushed if status == "pushed" else None,
    }


def load_state(payload: Mapping | None, pr: int) -> dict:
    """Return a validated state. Invalid stored JSON fails closed."""
    if payload is None:
        return initial_state(pr)
    if not isinstance(payload, Mapping):
        raise VerdictError("state_corrupt")
    state = initial_state(pr)
    try:
        round_no = int(payload.get("round", 0))
    except (TypeError, ValueError) as exc:
        raise VerdictError("state_corrupt") from exc
    if round_no < 0 or round_no > MAX_AUTOFIX_ROUNDS:
        raise VerdictError("state_corrupt")
    attempted = payload.get("attempted_shas", [])
    if not isinstance(attempted, list) or any(
        not isinstance(item, str) or not SHA_RE.fullmatch(item) for item in attempted
    ):
        raise VerdictError("state_corrupt")
    active = payload.get("active_run_sha")
    pending = payload.get("pending_sha")
    for name, value in (("active_run_sha", active), ("pending_sha", pending)):
        if value is not None and (not isinstance(value, str) or not SHA_RE.fullmatch(value)):
            raise VerdictError("state_corrupt")
    in_progress = payload.get("in_progress", False)
    if not isinstance(in_progress, bool):
        raise VerdictError("state_corrupt")
    if in_progress and active is None:
        raise VerdictError("state_corrupt")
    pending_result = payload.get("pending_result")
    if pending_result is not None and pending_result not in RESULTS:
        raise VerdictError("state_corrupt")
    if (pending is None) != (pending_result is None):
        raise VerdictError("state_corrupt")
    confirmed = payload.get("dispatch_confirmed", True)
    if not isinstance(confirmed, bool):
        raise VerdictError("state_corrupt")
    state.update(
        {
            "pr": pr,
            "round": round_no,
            "in_progress": in_progress,
            "active_run_sha": active,
            "attempted_shas": list(attempted),
            "dispatch_confirmed": confirmed,
            "pending_sha": pending,
            "pending_result": pending_result,
            "pending_review_id": payload.get("pending_review_id"),
            "pending_event_at": payload.get("pending_event_at"),
            "last_verdict": payload.get("last_verdict"),
            "last_verdict_sha": payload.get("last_verdict_sha"),
            "last_review_id": payload.get("last_review_id"),
            "last_event_at": payload.get("last_event_at"),
            "stopped_reason": payload.get("stopped_reason"),
        }
    )
    return state


def state_from_comment(body: str, pr: int) -> dict:
    match = STATE_RE.search(body or "")
    if not match:
        raise VerdictError("missing_state")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise VerdictError("state_corrupt") from exc
    return load_state(payload, pr)


def _is_actions_bot(user: Mapping | None) -> bool:
    """GitHub Actions' GITHUB_TOKEN comments as this login. Users cannot."""
    if not isinstance(user, Mapping) or user.get("login") != ACTIONS_LOGIN:
        return False
    return user.get("type") in (None, "Bot")


def _is_cursor_bot(user: Mapping | None) -> bool:
    if not isinstance(user, Mapping):
        return False
    return user.get("type") == "Bot" and user.get("login") in CURSOR_LOGINS


def _event_pr(event: Mapping) -> int | None:
    try:
        return int(event["pr"])
    except (KeyError, TypeError, ValueError):
        return None


def interpret_issue_comments(comments: list, pr: int) -> tuple[dict, int | None, list[dict]]:
    """Read trusted state and completion comments. Untrusted text is ignored.

    A comment that only mentions the state marker, without JSON, is skipped
    so it cannot hide an older Actions comment. Corrupt JSON from Actions
    still fails closed.
    """
    state = None
    comment_id = None
    done_events: list[dict] = []
    state_candidates = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        user = comment.get("user") if isinstance(comment.get("user"), Mapping) else {}
        body = comment.get("body") or ""
        if _is_actions_bot(user) and "pdf-autofix-state" in body:
            state_candidates.append(comment)
        if _is_cursor_bot(user) and "pdf-autofix-done" in body:
            try:
                parsed_done = parse_done_marker(body)
            except VerdictError:
                continue
            done_events.append(
                {
                    "kind": "autofix_done",
                    "pr": pr,
                    "review_id": f"comment-{comment.get('id')}",
                    "submitted_at": comment.get("created_at") or "",
                    **parsed_done,
                }
            )
    for comment in sorted(state_candidates, key=lambda item: int(item["id"]), reverse=True):
        body = comment.get("body") or ""
        try:
            parsed = state_from_comment(body, pr)
        except VerdictError as exc:
            if not exc.args or exc.args[0] != "missing_state":
                raise
            continue
        state = parsed
        comment_id = int(comment["id"])
        break
    if state is None:
        state = initial_state(pr)
    return state, comment_id, done_events


def _matching_done(state: Mapping, event: Mapping) -> bool:
    return (
        bool(state.get("in_progress"))
        and _event_pr(event) == state.get("pr")
        and event.get("attempted_sha") == state.get("active_run_sha")
    )


def apply_recorded_done(state: Mapping, done_events: list[dict]) -> dict:
    """Apply Cursor completion comments that match the active run, oldest first."""
    current = load_state(state, int(state["pr"]))
    ordered = sorted(done_events, key=_event_sort_key)
    for event in ordered:
        if not current["in_progress"]:
            break
        if not _matching_done(current, event):
            continue
        decision = decide(current, event)
        if decision["action"] == "ignore":
            continue
        current = decision["state"]
    return current


def render_state_comment(state: Mapping) -> str:
    encoded = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        "<!-- pdf-autofix-state\n"
        f"{encoded}\n"
        "-->\n"
        "此评论由 qa-autofix-bridge 维护，用于保证每个 PR 同时只有一个自动修复，"
        "并且整个 PR 最多 3 轮。请不要手工修改 JSON。\n"
    )


def _touch(state: dict, event: Mapping) -> None:
    state["last_review_id"] = event.get("review_id")
    state["last_event_at"] = event.get("submitted_at")
    state["last_verdict"] = event.get("result", state.get("last_verdict"))
    if event.get("sha"):
        state["last_verdict_sha"] = event["sha"]


def _clear_pending(state: dict) -> None:
    state["pending_sha"] = None
    state["pending_result"] = None
    state["pending_review_id"] = None
    state["pending_event_at"] = None


def _result(state: dict, action: str, reason: str, dispatch: bool) -> dict:
    context = None
    if dispatch:
        context = _webhook_context(state)
    return {
        "action": action,
        "reason": reason,
        "dispatch": dispatch,
        "webhook_context": context,
        "state": state,
    }


def _webhook_context(state: Mapping) -> str:
    return "\n".join(
        [
            "pdf-autofix-dispatch",
            f"pr: {state['pr']}",
            f"sha: {state['active_run_sha']}",
            f"round: {state['round']}",
            f"max_rounds: {state['max_rounds']}",
            "修复当前 PR 的头部分支并推送到原 PR。",
            "不要新开 PR，不要合并，不要批准。",
            "不要连接生产数据库，不要读取真实 PDF。",
            "推送前重新读取 pdf-autofix-state。若 stopped_reason 已变为 pass、"
            "not_verified 或 max_rounds，或 active_run_sha 已不是本任务的 sha，则不要推送。",
            "完成后发布 pdf-autofix-done 标记并停止。",
        ]
    )


def _event_time(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp. Fractional seconds sort after whole seconds."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_sort_key(event: Mapping):
    parsed = _event_time(event.get("submitted_at"))
    if parsed is None:
        return (1, str(event.get("submitted_at") or ""))
    return (0, parsed.timestamp())


def _is_earlier(submitted: object, previous: object) -> bool:
    left = _event_time(submitted)
    right = _event_time(previous)
    if left is None or right is None:
        return False
    return left < right


def _guard_event(state: dict, event: Mapping) -> dict | None:
    if event.get("pr") != state["pr"]:
        return _result(state, "ignore", "pr_mismatch", False)
    review_id = event.get("review_id")
    if review_id and review_id == state.get("last_review_id"):
        return _result(state, "ignore", "duplicate_review", False)
    if _is_earlier(event.get("submitted_at"), state.get("last_event_at")):
        return _result(state, "ignore", "stale_event", False)
    return None


def _remember_pending(state: dict, event: Mapping) -> None:
    state["pending_sha"] = event["sha"]
    state["pending_result"] = event["result"]
    state["pending_review_id"] = event.get("review_id")
    state["pending_event_at"] = event.get("submitted_at")


def _check_github_review_state(result: str, github_state: str | None) -> None:
    if github_state == "APPROVED" and result != "PASS":
        raise VerdictError("approval_conflicts_with_verdict")
    if github_state == "CHANGES_REQUESTED" and result != "FAIL":
        raise VerdictError("changes_requested_conflicts_with_verdict")


def _apply_verdict(state: dict, event: Mapping) -> dict:
    blocked = _guard_event(state, event)
    if blocked:
        return blocked
    result = event["result"]
    sha = event["sha"]
    if not SHA_RE.fullmatch(sha):
        return _result(state, "ignore", "bad_sha", False)
    try:
        _check_github_review_state(result, event.get("github_review_state"))
    except VerdictError as exc:
        return _result(state, "ignore", str(exc), False)
    machine_sha = event.get("machine_sha")
    if machine_sha and machine_sha != sha:
        return _result(state, "ignore", "sha_conflict", False)

    if result in {"PASS", "NOT VERIFIED"}:
        action = "stop_pass" if result == "PASS" else "pause_not_verified"
        reason = "qa_pass" if result == "PASS" else "not_verified"
        state["stopped_reason"] = "pass" if result == "PASS" else "not_verified"
        _clear_pending(state)
        _touch(state, event)
        if state["in_progress"]:
            # The Developer run is still the only one allowed to finish.
            # Opening the lock here let a later FAIL start a second run.
            return _result(state, action, "active_run_still_open", False)
        state["active_run_sha"] = None
        return _result(state, action, reason, False)

    if (
        state.get("stopped_reason") == "pass"
        and sha == state.get("last_verdict_sha")
    ):
        _touch(state, event)
        state["stopped_reason"] = "pass"
        state["last_verdict"] = "PASS"
        return _result(state, "ignore", "waiting_for_human", False)

    if sha in state["attempted_shas"]:
        if (
            state["in_progress"]
            and state.get("active_run_sha") == sha
            and state.get("dispatch_confirmed") is False
        ):
            _touch(state, event)
            return _result(state, "dispatch", "retry_unconfirmed", True)
        _touch(state, event)
        return _result(state, "ignore", "duplicate_sha", False)

    if state["in_progress"]:
        _touch(state, event)
        if state.get("stopped_reason") in {"pass", "not_verified"}:
            return _result(state, "defer_in_progress", "active_run_still_open", False)
        _remember_pending(state, event)
        return _result(state, "defer_in_progress", "one_fix_at_a_time", False)

    if state["round"] >= MAX_AUTOFIX_ROUNDS:
        state["stopped_reason"] = "max_rounds"
        _clear_pending(state)
        _touch(state, event)
        return _result(state, "stop_max_rounds", "max_rounds", False)

    _start_round(state, sha)
    _touch(state, event)
    return _result(state, "dispatch", "qa_fail", True)


def _start_round(state: dict, sha: str) -> None:
    state["round"] += 1
    state["in_progress"] = True
    state["active_run_sha"] = sha
    state["attempted_shas"] = [*state["attempted_shas"], sha]
    state["stopped_reason"] = None
    # A completed decision is confirmed. commit_decision clears the flag on the
    # claim it writes before the webhook, and sets it again only after success.
    state["dispatch_confirmed"] = True
    _clear_pending(state)


def _apply_done(state: dict, event: Mapping) -> dict:
    blocked = _guard_event(state, event)
    if blocked:
        return blocked
    if event.get("attempted_sha") != state.get("active_run_sha") or not state["in_progress"]:
        _touch_done(state, event)
        return _result(state, "ignore", "done_does_not_match_active_run", False)

    pending_event = None
    if state.get("pending_sha"):
        pending_event = {
            "kind": "qa_verdict",
            "pr": state["pr"],
            "sha": state["pending_sha"],
            "result": state["pending_result"],
            "review_id": event.get("review_id"),
            "submitted_at": event.get("submitted_at"),
            "github_review_state": "COMMENTED",
            "machine_sha": state["pending_sha"],
        }
    state["in_progress"] = False
    state["active_run_sha"] = None
    _clear_pending(state)
    if pending_event is None:
        _touch_done(state, event)
        return _result(state, "clear_in_progress", event.get("status") or "done", False)
    # Completion is newer than the deferred review, so apply that review as a
    # fresh event. Its original id remains stale against this later timestamp.
    return _apply_verdict(state, pending_event)


def _touch_done(state: dict, event: Mapping) -> None:
    state["last_review_id"] = event.get("review_id")
    state["last_event_at"] = event.get("submitted_at")


def decide(state: Mapping, event: Mapping) -> dict:
    """Return the next state and whether a Developer webhook may be sent."""
    current = load_state(state, int(event["pr"]))
    kind = event.get("kind")
    if kind == "qa_verdict":
        return _apply_verdict(current, event)
    if kind == "autofix_done":
        return _apply_done(current, event)
    return _result(current, "ignore", "unknown_event", False)


def event_from_github(payload: Mapping, event_name: str) -> dict | None:
    """Convert a GitHub delivery into a decision event.

    Returns None when the delivery is not a Cursor QA review or a Cursor
    Developer completion marker. Human comments never start a fix.
    """
    if event_name == "pull_request_review":
        review = payload.get("review") or {}
        pull = payload.get("pull_request") or {}
        user = review.get("user") or {}
        if not _is_cursor_bot(user):
            return None
        body = review.get("body") or ""
        if "pdf-autofix-done" in body and QA_AUTOMATION_ID not in body:
            try:
                parsed_done = parse_done_marker(body)
            except VerdictError:
                return None
            return {
                "kind": "autofix_done",
                "pr": int(pull["number"]),
                "review_id": str(review.get("id")),
                "submitted_at": review.get("submitted_at") or "",
                **parsed_done,
            }
        try:
            parsed = parse_qa_review(body)
        except VerdictError:
            return None
        return {
            "kind": "qa_verdict",
            "pr": int(pull["number"]),
            "sha": review.get("commit_id") or "",
            "result": parsed["result"],
            "machine_sha": parsed["machine_sha"],
            "review_id": str(review.get("id")),
            "submitted_at": review.get("submitted_at") or "",
            "github_review_state": review.get("state") or "COMMENTED",
            "html_url": review.get("html_url") or pull.get("html_url") or "",
        }

    if event_name == "issue_comment":
        comment = payload.get("comment") or {}
        issue = payload.get("issue") or {}
        if "pull_request" not in issue:
            return None
        user = comment.get("user") or {}
        if not _is_cursor_bot(user):
            return None
        body = comment.get("body") or ""
        if "pdf-autofix-done" not in body:
            return None
        try:
            parsed = parse_done_marker(body)
        except VerdictError:
            return None
        return {
            "kind": "autofix_done",
            "pr": int(issue["number"]),
            "review_id": f"comment-{comment.get('id')}",
            "submitted_at": comment.get("created_at") or "",
            **parsed,
        }
    return None


class MemoryStore:
    """In-memory stand-in for the PR state comment."""

    def __init__(self, pr: int, state: Mapping | None = None):
        self.pr = pr
        self.state = load_state(state, pr)
        self.saves: list[dict] = []

    def load(self) -> dict:
        return deepcopy(self.state)

    def save(self, state: Mapping) -> None:
        self.state = load_state(state, self.pr)
        self.saves.append(deepcopy(self.state))


class MemoryWebhook:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.contexts: list[str] = []

    def post(self, context: str) -> bool:
        self.contexts.append(context)
        return self.ok


def execute(mode: str, event: Mapping, store: MemoryStore, webhook: MemoryWebhook) -> tuple[int, dict]:
    """Apply one event. Observe mode never saves and never calls the webhook."""
    if mode not in {"observe", "armed"}:
        raise ValueError("mode must be observe or armed")
    decision = decide(store.load(), event)
    return commit_decision(mode, decision, store, webhook)


def commit_decision(mode: str, decision: Mapping, store: MemoryStore, webhook: MemoryWebhook) -> tuple[int, dict]:
    """Persist a round before posting. A rejected webhook rolls that round back.

    Saving first means a later run cannot post the same SHA again when the
    comment update fails. A failed POST never leaves the round in place.
    """
    if mode == "observe":
        return 0, decision
    if decision["dispatch"]:
        prior = store.load()
        claim = deepcopy(decision["state"])
        claim["dispatch_confirmed"] = False
        try:
            store.save(claim)
        except SAVE_ERRORS as exc:
            print(f"qa-autofix-bridge could not save state: {exc}")
            withheld = dict(decision)
            withheld["state"] = prior
            withheld["dispatch"] = False
            return 1, withheld
        if not webhook.post(decision["webhook_context"]):
            try:
                store.save(prior)
            except SAVE_ERRORS as exc:
                print(f"qa-autofix-bridge could not roll the round back: {exc}")
                # The claim stays unconfirmed so a later FAIL for this SHA can post again.
                stuck = dict(decision)
                stuck["state"] = store.load()
                return 1, stuck
            rolled = dict(decision)
            rolled["state"] = store.load()
            return 1, rolled
        confirmed = store.load()
        confirmed["dispatch_confirmed"] = True
        try:
            store.save(confirmed)
        except SAVE_ERRORS as exc:
            print(f"qa-autofix-bridge could not mark the webhook confirmed: {exc}")
            return 1, dict(decision)
        finished = dict(decision)
        finished["state"] = confirmed
        return 0, finished
    if decision["action"] in PERSIST_ACTIONS:
        try:
            store.save(decision["state"])
        except SAVE_ERRORS as exc:
            print(f"qa-autofix-bridge could not save state: {exc}")
            return 1, dict(decision)
    return 0, dict(decision)


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a webhook redirect. urllib would copy Authorization onto it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "redirect refused",
            headers,
            fp,
        )


_REAL_URLOPEN = urllib.request.urlopen


def _open_webhook(request: urllib.request.Request, timeout: int = 30):
    """Open a webhook POST. Tests replace urlopen; production refuses redirects."""
    if urllib.request.urlopen is not _REAL_URLOPEN:
        return urllib.request.urlopen(request, timeout=timeout)
    opener = urllib.request.build_opener(_RefuseRedirect)
    return opener.open(request, timeout=timeout)


def _post_webhook(url: str, token: str, context: str) -> bool:
    safe_url = validate_webhook_url(url)
    safe_token = normalize_token(token)
    body = json.dumps({"context": context}).encode("utf-8")
    request = urllib.request.Request(
        safe_url,
        data=body,
        headers={
            "Authorization": f"Bearer {safe_token}",
            "Content-Type": "application/json",
            "User-Agent": "pdf-qa-autofix-bridge",
        },
        method="POST",
    )
    try:
        with _open_webhook(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            status = getattr(response, "status", None)
            return status == 200 and parsed.get("success") is True
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return False
        return False
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False


class GithubCommentStore:
    """Load and save the one state comment on a pull request."""

    def __init__(self, repo: str, pr: int, token: str, opener=None):
        self.repo = repo
        self.pr = pr
        self.token = normalize_token(token)
        self.opener = opener or urllib.request.urlopen
        self.comment_id: int | None = None
        self.raw_state: dict | None = None
        self.done_events: list[dict] = []
        self.saves: list[dict] = []

    def load(self) -> dict:
        comments = self._list_comments()
        raw, comment_id, done_events = interpret_issue_comments(comments, self.pr)
        self.raw_state = raw
        self.comment_id = comment_id
        self.done_events = done_events
        return apply_recorded_done(raw, done_events)

    def _list_comments(self) -> list:
        """Read every issue-comment page. A failed page is not an empty PR.

        ``GET /issues/{n}/comments`` ignores ``sort`` and ``direction`` and
        returns comments in ascending id order. The next page comes from Link.
        """
        path = f"/repos/{self.repo}/issues/{self.pr}/comments?per_page=100"
        collected: list = []
        seen: set[str] = set()
        pinned_repo_id: str | None = None
        for _page in range(MAX_COMMENT_PAGES):
            if path in seen:
                raise VerdictError("state_unread")
            seen.add(path)
            body, headers = self._open("GET", path)
            if not isinstance(body, list):
                raise VerdictError("state_corrupt")
            collected.extend(body)
            nxt, pinned_repo_id = _next_comment_path(
                headers, self.repo, self.pr, pinned_repo_id
            )
            if nxt is None:
                return collected
            path = nxt
        raise VerdictError("state_unread")

    def save(self, state: Mapping) -> None:
        validated = load_state(state, self.pr)
        body = render_state_comment(validated)
        if self.comment_id is None:
            created = self._request(
                "POST",
                f"/repos/{self.repo}/issues/{self.pr}/comments",
                {"body": body},
            )
            self.comment_id = int(created["id"])
        else:
            self._request(
                "PATCH",
                f"/repos/{self.repo}/issues/comments/{self.comment_id}",
                {"body": body},
            )
        self.saves.append(validated)

    def _request(self, method: str, path: str, payload: Mapping | None = None):
        body, _headers = self._open(method, path, payload)
        return body

    def _open(self, method: str, path: str, payload: Mapping | None = None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{GITHUB_API}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "pdf-qa-autofix-bridge",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method=method,
        )
        with self.opener(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            body = json.loads(raw) if raw else {}
            headers = getattr(response, "headers", {}) or {}
            return body, headers


LINK_NEXT_RE = re.compile(
    r"<([^>]+)>\s*;\s*rel=(?:\"next\"|'next'|next)(?=[\s,;>]|$)",
    re.IGNORECASE,
)


def _header_value(headers, name: str) -> str:
    if not headers or not hasattr(headers, "get"):
        return ""
    return str(headers.get(name) or headers.get(name.lower()) or headers.get(name.title()) or "")


def _next_comment_path(
    headers, repo: str, pr: int, pinned_repo_id: str | None
) -> tuple[str | None, str | None]:
    """Return the next comments path, or None when this page is the last.

    GitHub ignores sort and returns ``Link`` URLs under
    ``/repositories/{id}/issues/{pr}/comments``. Other hosts, repos, and
    query keys are refused so a bad Link cannot be read as an empty history.
    """
    link = _header_value(headers, "Link")
    if not link:
        return None, pinned_repo_id
    match = LINK_NEXT_RE.search(link)
    if not match:
        if re.search(r"rel=(?:\"next\"|'next'|next)(?=[\s,;>]|$)", link, re.IGNORECASE):
            raise VerdictError("state_unread")
        return None, pinned_repo_id
    parts = urlsplit(match.group(1).strip())
    if (
        parts.scheme != "https"
        or parts.netloc != "api.github.com"
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise VerdictError("state_unread")
    path = parts.path.rstrip("/")
    if "\\" in path or any(segment == ".." for segment in path.split("/")):
        raise VerdictError("state_unread")
    expected = f"/repos/{repo}/issues/{pr}/comments"
    numeric = re.fullmatch(rf"/repositories/(\d+)/issues/{pr}/comments", path)
    if path == expected:
        repo_id = pinned_repo_id
    elif numeric:
        repo_id = numeric.group(1)
        if pinned_repo_id is not None and repo_id != pinned_repo_id:
            raise VerdictError("state_unread")
    else:
        raise VerdictError("state_unread")
    page = None
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "page":
            if not value.isdigit() or int(value) < 1:
                raise VerdictError("state_unread")
            page = str(int(value))
        elif key in {"per_page", "sort", "direction"}:
            continue
        else:
            raise VerdictError("state_unread")
    query = [("page", page)] if page is not None else []
    query.append(("per_page", "100"))
    return f"{path}?{urlencode(query)}", repo_id


class CursorWebhook:
    def __init__(self, url: str, token: str, poster=_post_webhook):
        self.url = url
        self.token = token
        self.poster = poster
        self.contexts: list[str] = []

    def configured(self) -> bool:
        try:
            validate_webhook_url(self.url)
            normalize_token(self.token)
        except ValueError:
            return False
        return True

    def post(self, context: str) -> bool:
        self.contexts.append(context)
        if not self.configured():
            return False
        return bool(self.poster(self.url, self.token, context))


def _print_decision(mode: str, decision: Mapping) -> None:
    print(json.dumps(
        {
            "mode": mode,
            "action": decision["action"],
            "reason": decision["reason"],
            "dispatch": decision["dispatch"],
            "round": decision["state"]["round"],
            "stopped_reason": decision["state"]["stopped_reason"],
            "in_progress": decision["state"]["in_progress"],
        },
        ensure_ascii=False,
        sort_keys=True,
    ))
    if mode == "observe" and decision["dispatch"]:
        print("observe only: webhook was not called and Developer stays disabled")


def run_from_env(environ: Mapping[str, str] | None = None) -> int:
    """GitHub Actions entry point. Defaults to a no-op unless mode is set."""
    env = os.environ if environ is None else environ
    event_name = env.get("GITHUB_EVENT_NAME", "")
    mode = "observe" if event_name == "workflow_dispatch" else env.get("QA_AUTOFIX_MODE", "")
    if mode not in {"observe", "armed"}:
        print("qa-autofix-bridge idle: QA_AUTOFIX_MODE is not observe or armed")
        return 0
    payload_path = env.get("GITHUB_EVENT_PATH", "")
    if not payload_path:
        print("qa-autofix-bridge missing GITHUB_EVENT_PATH")
        return 1
    with open(payload_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    try:
        event = _event_for_delivery(payload, event_name, env)
    except (VerdictError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"qa-autofix-bridge refused delivery: {exc}")
        return 1
    pr = _delivery_pr(payload, event_name, event)
    if pr is None:
        print(f"qa-autofix-bridge ignored {event_name}")
        return 0
    try:
        store = GithubCommentStore(
            env["GITHUB_REPOSITORY"],
            pr,
            env["GITHUB_TOKEN"],
        )
        store.load()
    except (VerdictError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"qa-autofix-bridge state unavailable: {exc}")
        return 1
    if store.raw_state is None:
        print("qa-autofix-bridge state unavailable: missing raw state")
        return 1
    holder = _LoadedStore(deepcopy(store.raw_state), store)
    events = _events_for_run(store.done_events, event)
    if not events:
        print(f"qa-autofix-bridge ignored {event_name}")
        return 0
    webhook = CursorWebhook(
        env.get("CURSOR_DEVELOPER_WEBHOOK_URL", ""),
        env.get("CURSOR_DEVELOPER_WEBHOOK_TOKEN", ""),
    )
    for item in events:
        if item.get("kind") == "autofix_done" and not _matching_done(holder.load(), item):
            continue
        try:
            code, decision = execute(mode, item, holder, webhook)
        except (VerdictError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
            print(f"qa-autofix-bridge state unavailable: {exc}")
            return 1
        if mode == "observe":
            holder.state = deepcopy(decision["state"])
        _print_decision(mode, decision)
        if code != 0:
            if decision["dispatch"] and decision["state"].get("in_progress"):
                print("qa-autofix-bridge did not start Developer; the claimed round could not be rolled back")
            else:
                print("qa-autofix-bridge did not start Developer; state was not advanced")
            return code
    return 0


def _delivery_pr(payload: Mapping, event_name: str, event: Mapping | None) -> int | None:
    if event is not None:
        return _event_pr(event)
    if event_name == "issue_comment":
        issue = payload.get("issue") or {}
        if "pull_request" not in issue:
            return None
        number = issue.get("number")
    elif event_name == "pull_request_review":
        number = (payload.get("pull_request") or {}).get("number")
    else:
        return None
    try:
        return int(number)
    except (TypeError, ValueError):
        return None


def _events_for_run(done_events: list[dict], trigger: Mapping | None) -> list[dict]:
    """Replay recorded completions, then the triggering event, in time order."""
    events = list(done_events)
    if trigger is not None and all(
        trigger.get("review_id") != item.get("review_id") for item in events
    ):
        events.append(dict(trigger))
    events.sort(key=_event_sort_key)
    return events


class _LoadedStore:
    """Store adapter whose first load is the comment already fetched."""

    def __init__(self, state: Mapping, backing: GithubCommentStore):
        self.state = state
        self.backing = backing
        self.saves: list[dict] = []

    def load(self) -> dict:
        return deepcopy(self.state)

    def save(self, state: Mapping) -> None:
        self.backing.save(state)
        self.state = load_state(state, self.backing.pr)
        self.saves.append(deepcopy(self.state))


def _event_for_delivery(payload: Mapping, event_name: str, env: Mapping[str, str]) -> dict | None:
    if event_name != "workflow_dispatch":
        return event_from_github(payload, event_name)
    inputs = payload.get("inputs") or {}
    pr = int(inputs["pr_number"])
    review_id = str(inputs.get("review_id") or "").strip()
    repo = env["GITHUB_REPOSITORY"]
    token = normalize_token(env["GITHUB_TOKEN"])
    if not review_id:
        raise ValueError("workflow_dispatch requires review_id")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls/{pr}/reviews/{review_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "pdf-qa-autofix-bridge",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        review = json.loads(response.read().decode("utf-8"))
    return event_from_github(
        {
            "review": review,
            "pull_request": {"number": pr, "html_url": review.get("html_url", "")},
        },
        "pull_request_review",
    )


def main() -> None:
    raise SystemExit(run_from_env())


if __name__ == "__main__":
    main()
