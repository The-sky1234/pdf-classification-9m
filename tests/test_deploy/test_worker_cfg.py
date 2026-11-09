"""部署文件的静态检查：编排、镜像、依赖版本和代码对得上，不需要 Docker。"""
import os
import re

import pytest

yaml = pytest.importorskip("yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY = os.path.join(ROOT, "deploy")


def _read(name):
    with open(os.path.join(DEPLOY, name), encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(_read("docker-compose.yml"))


def test_services_and_gpu_profiles(compose):
    services = compose["services"]
    assert {"postgres", "api", "cpu-worker", "ocr-worker", "classify-worker"} <= set(services)
    assert "redis" not in services and "mineru-worker" not in services
    for name, profile in (("ocr-worker", "ocr"), ("classify-worker", "classify")):
        svc = services[name]
        assert svc["profiles"] == [profile], "GPU 波次默认不启动，由 ops.sh wave 切换"
        devices = svc["deploy"]["resources"]["reservations"]["devices"]
        assert devices[0]["capabilities"] == ["gpu"]
        assert svc["environment"]["LOCK_DIR"] == "/data/locks"
        assert any(v.endswith(":/data/locks") for v in svc["volumes"]), "两个 GPU 波次要共用同一把文件锁"
    assert "deploy" not in services["cpu-worker"] and "deploy" not in services["api"]


def test_workers_share_ledger_and_paths(compose):
    for name in ("cpu-worker", "ocr-worker", "classify-worker"):
        svc = compose["services"][name]
        env = svc["environment"]
        assert env["DATABASE_URL"].startswith("postgresql+psycopg2://")
        mounted = any(v.startswith("${SOURCE_ROOT:?}:") for v in svc["volumes"])
        if env["SOURCE_ROOTS"]:
            assert mounted, f"{name} 检查源目录却没挂载，会一直等「源目录不可读」"
    assert compose["services"]["ocr-worker"]["environment"]["SOURCE_ROOTS"] == "${SOURCE_ROOT:?}"
    cpu = compose["services"]["cpu-worker"]
    assert "${SOURCE_ROOT:?}:${SOURCE_ROOT:?}:ro" in cpu["volumes"], "源目录按原路径只读挂载"
    assert "${ARCHIVE_ROOT:?}:${ARCHIVE_ROOT:?}" in cpu["volumes"]
    assert cpu["command"][:3] == ["python", "-m", "phase1"]
    assert {"cpu", "archive"} <= set(cpu["command"])


def test_commands_match_cli(compose):
    from phase1 import ledger

    for name in ("cpu-worker", "ocr-worker", "classify-worker"):
        cmd = compose["services"][name]["command"]
        stages = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--stage"]
        assert stages and set(stages) <= set(ledger.WORK_STAGES)


def test_database_and_api_not_public(compose):
    assert compose["services"]["postgres"]["ports"] == ["127.0.0.1:${PG_PORT:-5432}:5432"]
    assert compose["services"]["api"]["ports"][0].startswith("${API_BIND:-127.0.0.1}:")


def test_env_example_covers_compose_variables():
    text = _read("docker-compose.yml")
    used = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", text))
    example = {line.split("=", 1)[0] for line in _read(".env.example").splitlines()
               if "=" in line and not line.startswith("#")}
    assert used - {"PG_PORT", "PROM_PORT"} <= example
    for line in _read(".env.example").splitlines():
        if line.startswith(("POSTGRES_PASSWORD=", "API_KEY=", "LLM_API_KEY=")):
            assert line.endswith("="), "示例文件里不能带真实密钥"


@pytest.mark.parametrize("name", ["requirements-common.txt", "requirements-api.txt",
                                  "requirements-ocr.txt", "requirements-infer.txt"])
def test_requirements_pinned(name):
    for line in _read(name).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-r "):
            continue
        assert "==" in line or line == "setuptools<75", f"{name}: {line} 没固定版本"


@pytest.mark.parametrize("name", ["Dockerfile.api", "Dockerfile.ocr", "Dockerfile.infer"])
def test_dockerfiles_copy_existing_paths(name):
    for line in _read(name).splitlines():
        if line.startswith("COPY "):
            for src in line.split()[1:-1]:
                assert os.path.exists(os.path.join(ROOT, src)), f"{name}: {src} 不存在"


def test_ops_script_uses_known_services(compose):
    text = _read("ops.sh")
    for svc in re.findall(r"\b([a-z]+-worker)\b", text):
        assert svc in compose["services"], svc
    assert "down -v" not in text
