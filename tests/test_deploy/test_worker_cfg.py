"""ISSUE-02复现：classify-worker必须携带API_KEY/API_URL；缺失即启动失败。"""
import pytest


def compose_text():
    return open("deploy/docker-compose.yml", encoding="utf-8").read()


def test_classify_worker_has_auth_env():
    text = compose_text()
    seg = text.split("classify-worker:")[1].split("prometheus:")[0]
    assert "API_KEY" in seg and "API_URL" in seg


def test_save_without_key_fails_fast(monkeypatch):
    import os, sys
    sys.path.insert(0, os.path.abspath("deploy"))
    monkeypatch.delenv("API_KEY", raising=False)
    import worker_main
    with pytest.raises(SystemExit):
        worker_main.require_env("API_KEY")
