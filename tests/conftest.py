import pytest

DEPLOY_ENV = ("SOURCE_ROOTS", "ARCHIVE_ROOT", "LOCK_DIR", "THRESHOLDS_FILE", "LLM_ENDPOINT", "LLM_MAX_CALLS",
              "MODEL_DEVICE", "OCR_USE_GPU")


@pytest.fixture(autouse=True)
def _no_deploy_env(monkeypatch):
    """服务器上的部署变量会改变波次行为（例如 SOURCE_ROOTS 不可读时 worker 一直等），测试里一律清掉。"""
    for name in DEPLOY_ENV:
        monkeypatch.delenv(name, raising=False)
