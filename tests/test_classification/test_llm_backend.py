"""LLMHttpBackend 对着本地假服务跑：正常回答、思考模型、服务暂时 503、预算用完。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from classifier.backends import LLMHttpBackend

OPTIONS = ["L1_AQSC", "L1_FZZJ", "L1_YJJY"]


@pytest.fixture()
def server():
    state = {"replies": [], "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"body": body, "auth": self.headers.get("Authorization")})
            status, content = state["replies"].pop(0) if state["replies"] else (200, "UNCERTAIN")
            payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_port}/v1"
    yield state
    httpd.shutdown()


def _llm(server, **kw):
    kw.setdefault("max_calls", 10)
    return LLMHttpBackend(endpoint=server["url"], model="qwen", api_key="k", retry_wait_s=0, **kw)


def test_picks_option_and_sends_prompt(server):
    server["replies"] = [(200, "L1_FZZJ")]
    res = _llm(server).choose("防汛物资储备通知", OPTIONS, descriptions={"L1_FZZJ": "防灾减灾救灾"})
    assert res == {"choice": "L1_FZZJ", "reason": "llm"}
    req = server["requests"][0]
    assert req["auth"] == "Bearer k" and req["body"]["model"] == "qwen"
    assert req["body"]["temperature"] == 0 and req["body"]["max_tokens"] == 32
    prompt = req["body"]["messages"][0]["content"]
    assert "1. L1_AQSC\n2. 防灾减灾救灾\n3. L1_YJJY" in prompt and "只输出一个数字（1-3）" in prompt


@pytest.mark.parametrize("reply,choice", [("2", "L1_FZZJ"), ("答案：3", "L1_YJJY"), ("1。", "L1_AQSC"),
                                          ("0", "UNCERTAIN"), ("7", "7"), ("L1_YJJY", "L1_YJJY")])
def test_numbered_answers(server, reply, choice):
    server["replies"] = [(200, reply)]
    assert _llm(server).choose("x", OPTIONS)["choice"] == choice


def test_thinking_model_answer_after_think_block(server):
    server["replies"] = [(200, "<think>像是防汛……</think>\nL1_FZZJ"), (200, "<think>还没想完")]
    llm = _llm(server)
    assert llm.choose("x", OPTIONS)["choice"] == "L1_FZZJ"
    assert llm.choose("x", OPTIONS) == {"choice": "UNCERTAIN", "reason": "llm_thinking_truncated"}


def test_retries_while_service_starts(server):
    server["replies"] = [(503, ""), (503, ""), (200, "L1_AQSC")]
    assert _llm(server).choose("x", OPTIONS)["choice"] == "L1_AQSC"
    assert len(server["requests"]) == 3


def test_gives_up_and_raises(server):
    server["replies"] = [(503, "")] * 5
    with pytest.raises(RuntimeError, match="llm call failed"):
        _llm(server, retries=1).choose("x", OPTIONS)
    assert len(server["requests"]) == 2


def test_client_error_not_retried(server):
    server["replies"] = [(400, "")]
    with pytest.raises(RuntimeError):
        _llm(server).choose("x", OPTIONS)
    assert len(server["requests"]) == 1


def test_off_list_and_budget(server):
    server["replies"] = [(200, "L9_MADE_UP")]
    llm = _llm(server, max_calls=1)
    assert llm.choose("x", OPTIONS)["reason"] == "llm_off_list"
    assert llm.choose("x", OPTIONS) == {"choice": "UNCERTAIN", "reason": "llm_disabled_or_budget_exhausted"}
    assert len(server["requests"]) == 1


def test_disabled_without_model_or_budget(server):
    assert not LLMHttpBackend(endpoint=server["url"], model="", max_calls=10).enabled
    assert not LLMHttpBackend(endpoint=server["url"], model="qwen", max_calls=0).enabled


def test_cooldown_after_outage_and_budget_refund(server):
    server["replies"] = [(503, "")] * 2 + [(200, "L1_AQSC")]
    llm = _llm(server, retries=1, cooldown_s=60, max_calls=1)
    with pytest.raises(RuntimeError, match="llm call failed"):
        llm.choose("x", OPTIONS)
    with pytest.raises(RuntimeError, match="cooling down"):
        llm.choose("x", OPTIONS)
    assert len(server["requests"]) == 2 and llm.calls == 0
    llm._down_until = 0
    assert llm.choose("x", OPTIONS)["choice"] == "L1_AQSC"
