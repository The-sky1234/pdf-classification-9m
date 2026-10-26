"""P0-01C：在隔离 PostgreSQL 上验证迁移链与约束。

只接受 127.0.0.1:15432 / pdf_classification_test。
密码来自环境变量 PDFCLS_TEST_PGPASSWORD，不写进本文件。
业务插入在事务中回滚。迁移本身会提交到该测试库。
"""
from __future__ import annotations

import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2 import sql as pysql

from backend.migrate import apply_migrations, split_sql

SHA_A = "a" * 64
SHA_B = "b" * 64
FILE_A = "018f3b2c-7d3e-7a11-8b22-aaaaaaaaaaaa"
FILE_B = "018f3b2c-7d3e-7a11-8b22-bbbbbbbbbbbb"
FILE_C = "018f3b2c-7d3e-7a11-9abc-dddddddddddd"
TASK_V7 = "018f3b2c-7d3e-7c44-8d55-cccccccccccc"
LEGACY = "0123456789abcdef0123456789abcdef"

ALLOWED_TABLES = {
    "doc_tasks",
    "classification_results",
    "archive_decisions",
    "archive_executions",
    "processing_batches",
    "file_registry",
    "file_content_versions",
    "processing_tasks",
    "review_records",
    "dedup_members",
    "schema_migrations",
}
REQUIRED_TABLES = ALLOWED_TABLES
BUSINESS_TABLES = ALLOWED_TABLES - {"schema_migrations"}


def test_split_sql_keeps_quotes_and_do_blocks():
    parts = split_sql("SELECT 'a;b';\nSELECT 2;\n")
    assert parts == ["SELECT 'a;b'", "SELECT 2"]
    block = "DO $$\nBEGIN\n  PERFORM 1;\nEND $$;\nSELECT 3;\n"
    parts = split_sql(block)
    assert len(parts) == 2
    assert parts[0].startswith("DO $$")
    assert "PERFORM 1;" in parts[0]
    assert parts[1] == "SELECT 3"
    assert split_sql("-- only comment\n") == []


def _settings():
    host = os.environ.get("PDFCLS_TEST_PGHOST", "")
    port = os.environ.get("PDFCLS_TEST_PGPORT", "")
    database = os.environ.get("PDFCLS_TEST_PGDATABASE", "")
    user = os.environ.get("PDFCLS_TEST_PGUSER", "")
    password = os.environ.get("PDFCLS_TEST_PGPASSWORD", "")
    if database == "app_test" or user == "app_user":
        pytest.fail("拒绝连接云端开发库 app_test / app_user")
    if (host, port, database) != ("127.0.0.1", "15432", "pdf_classification_test"):
        pytest.fail(
            "拒绝连接：只允许 127.0.0.1:15432/pdf_classification_test，"
            f"当前是 {host}:{port}/{database}"
        )
    if not user or password == "":
        pytest.fail("缺少 PDFCLS_TEST_PGUSER 或 PDFCLS_TEST_PGPASSWORD")
    return host, int(port), database, user, password


def _public_tables(cur):
    cur.execute(
        """
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY 1
        """
    )
    return [row[0] for row in cur.fetchall()]


def _count(cur, table):
    cur.execute(pysql.SQL("SELECT count(*) FROM {}").format(pysql.Identifier(table)))
    return cur.fetchone()[0]


@pytest.fixture(scope="session")
def proof():
    host, port, database, user, password = _settings()
    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
        connect_timeout=5,
        application_name="p001c-migrate-test",
    )
    conn.set_client_encoding("UTF8")
    info = {
        "client_host": host,
        "client_port": port,
        "client_database": database,
        "client_user": user,
    }
    try:
        with conn.cursor() as cur:
            dsn = conn.get_dsn_parameters()
            info["dsn_host"] = dsn.get("host")
            info["dsn_port"] = dsn.get("port")
            info["dsn_dbname"] = dsn.get("dbname")
            if (info["dsn_host"], str(info["dsn_port"]), info["dsn_dbname"]) != (
                "127.0.0.1",
                "15432",
                "pdf_classification_test",
            ):
                raise RuntimeError(f"DSN 不是隔离测试库: host={info['dsn_host']} port={info['dsn_port']} db={info['dsn_dbname']}")
            cur.execute(
                """
                SELECT current_database(), current_user,
                       inet_server_addr()::text, inet_server_port()
                """
            )
            dbname, dbuser, server_addr, server_port = cur.fetchone()
            info.update(
                database=dbname,
                db_user=dbuser,
                server_addr=server_addr,
                server_port=server_port,
            )
            if dbname == "app_test" or dbuser == "app_user":
                raise RuntimeError(
                    f"拒绝清库：current_database()={dbname} current_user={dbuser}"
                )
            if dbname != "pdf_classification_test":
                raise RuntimeError(f"current_database()={dbname}")
            cur.execute(
                """
                SELECT e.extname
                FROM pg_extension e
                JOIN pg_namespace n ON n.oid = e.extnamespace
                WHERE n.nspname = 'public'
                """
            )
            extensions = [row[0] for row in cur.fetchall()]
            if extensions:
                raise RuntimeError(f"public 中有扩展，停止重建: {extensions}")
            tables = _public_tables(cur)
            unexpected = sorted(set(tables) - ALLOWED_TABLES)
            counts = {table: _count(cur, table) for table in tables}
            info["tables_before"] = tables
            info["counts_before"] = counts
            dirty = {name: n for name, n in counts.items() if name != "schema_migrations" and n}
            if unexpected or dirty:
                raise RuntimeError(
                    f"停止重建。意外表={unexpected} 非空表={dirty} 重建前行数={counts}"
                )
            print("PROOF_BEFORE", info)
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cur.execute("CREATE SCHEMA public AUTHORIZATION CURRENT_USER")
            cur.execute("GRANT ALL ON SCHEMA public TO PUBLIC")
        conn.commit()
        info["applied_first"] = apply_migrations(conn)
        info["applied_second"] = apply_migrations(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version FROM schema_migrations ORDER BY id"
            )
            info["versions"] = [row[0] for row in cur.fetchall()]
        print(
            "MIGRATION_FIRST",
            info["applied_first"],
            "MIGRATION_SECOND",
            info["applied_second"],
            "VERSIONS",
            info["versions"],
        )
    except Exception:
        conn.rollback()
        conn.close()
        raise
    info["conn"] = conn
    yield info
    conn.rollback()
    conn.close()


@pytest.fixture
def tx(proof):
    conn = proof["conn"]
    conn.rollback()
    yield conn
    conn.rollback()


def test_migration_chain_on_isolated_postgres(proof):
    assert proof["database"] == "pdf_classification_test"
    assert proof["dsn_host"] == "127.0.0.1"
    assert str(proof["dsn_port"]) == "15432"
    business_before = {
        name: count
        for name, count in proof["counts_before"].items()
        if name != "schema_migrations"
    }
    assert all(count == 0 for count in business_before.values())
    print("COUNTS_BEFORE", proof["counts_before"])
    assert proof["applied_first"] == [
        "001_init.sql",
        "002_archive.sql",
        "003_registry.sql",
        "004_phase1_loop.sql",
    ]
    assert proof["applied_second"] == []
    assert proof["versions"] == proof["applied_first"]
    third = apply_migrations(proof["conn"])
    assert third == []
    print("MIGRATION_THIRD", third)


def test_required_tables_exist(tx):
    with tx.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY 1
            """
        )
        tables = [row[0] for row in cur.fetchall()]
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.views
            WHERE table_schema = 'public'
            ORDER BY 1
            """
        )
        views = [row[0] for row in cur.fetchall()]
    print("TABLES", tables)
    print("VIEWS", views)
    assert REQUIRED_TABLES <= set(tables)
    assert "v_legacy_results" in views


def test_constraints_and_indexes(tx):
    with tx.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, con.conname, con.contype, pg_get_constraintdef(con.oid)
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
            ORDER BY 1, 2
            """
        )
        constraints = cur.fetchall()
        cur.execute(
            """
            SELECT tablename, indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
            ORDER BY 1, 2
            """
        )
        indexes = cur.fetchall()
    print("CONSTRAINTS")
    for row in constraints:
        print(" | ".join(str(part) for part in row))
    print("INDEXES")
    for row in indexes:
        print(" | ".join(str(part) for part in row))
    names = {row[1] for row in constraints} | {row[1] for row in indexes}
    required = {
        "uq_file_registry_source_path",
        "fk_file_registry_batch",
        "chk_file_registry_file_id_form",
        "chk_file_registry_source_sha256",
        "chk_file_registry_content_version",
        "pk_file_content_versions",
        "fk_file_content_versions_file",
        "fk_file_content_versions_batch",
        "chk_file_content_versions_ver",
        "chk_file_content_versions_sha",
        "uq_processing_tasks_file_stage",
        "fk_processing_tasks_file",
        "fk_processing_tasks_content",
        "chk_processing_tasks_task_form",
        "chk_processing_tasks_new_task_ne_file",
        "uq_processing_batches_batch_no",
        "chk_processing_batches_roots",
        "chk_processing_batches_status",
        "uq_archdec_fv",
        "one_active_archive",
        "one_final_completed",
        "chk_completed",
        "uq_archdec_id_file_cv",
        "fk_archdec_content",
        "fk_archexec_decision",
        "fk_archexec_decision_file_cv",
        "uq_archexec_decision_content_completed",
        "chk_classification_results_content_version",
    }
    missing = sorted(required - names)
    assert not missing, missing
    sha_index = next(row for row in indexes if row[1] == "ix_file_registry_source_sha256")
    assert "UNIQUE" not in sha_index[2].upper()
    active = next(row for row in indexes if row[1] == "one_active_archive")
    assert "WHERE" in active[2].upper() and "COMPLETED" in active[2]


def _insert_batch(cur, batch_no):
    cur.execute(
        """
        INSERT INTO processing_batches (batch_no, source_roots)
        VALUES (%s, %s)
        RETURNING id
        """,
        (batch_no, '["/isolation-test/src"]'),
    )
    return cur.fetchone()[0]


def _insert_file(cur, batch_id, file_id, path, sha, origin="V7", content_version=None, nbytes=10):
    cur.execute(
        """
        INSERT INTO file_registry (
          file_id, source_path, source_sha256, "bytes", mtime,
          batch_id, content_version, id_origin
        ) VALUES (%s, %s, %s, %s, now(), %s, %s, %s)
        """,
        (file_id, path, sha, nbytes, batch_id, content_version, origin),
    )


def _insert_version(cur, batch_id, file_id, content_version, sha, nbytes=10):
    cur.execute(
        """
        INSERT INTO file_content_versions (
          file_id, content_version, source_sha256, "bytes", mtime, batch_id, observed_at
        ) VALUES (%s, %s, %s, %s, now(), %s, now())
        """,
        (file_id, content_version, sha, nbytes, batch_id),
    )


def _insert_decision(cur, file_id, version, content_version, sha):
    cur.execute(
        """
        INSERT INTO archive_decisions (
          file_id, version, content_version, archive_type, decision_status,
          approved, source_sha256, target_dir
        ) VALUES (%s, %s, %s, 'PATENT', 'APPROVED', 1, %s, '01_专利/')
        RETURNING id
        """,
        (file_id, version, content_version, sha),
    )
    return cur.fetchone()[0]


def _seed_archived(cur, batch_no, file_id, path, sha=SHA_A):
    batch_id = _insert_batch(cur, batch_no)
    _insert_file(cur, batch_id, file_id, path, None, content_version=None, nbytes=10)
    _insert_version(cur, batch_id, file_id, 1, sha, nbytes=10)
    cur.execute(
        """
        UPDATE file_registry
        SET source_sha256 = %s, content_version = 1, "bytes" = 10
        WHERE file_id = %s
        """,
        (sha, file_id),
    )
    decision_id = _insert_decision(cur, file_id, 1, 1, sha)
    return batch_id, decision_id


def _reject(cur, statement, params):
    with pytest.raises(psycopg2.Error) as caught:
        cur.execute(statement, params)
    err = caught.value
    first = (err.pgerror or str(err)).splitlines()[0]
    name = err.diag.constraint_name
    print(f"REJECTED {type(err).__name__} constraint={name} {first}")
    return name, first


def test_same_sha256_different_paths(tx):
    with tx.cursor() as cur:
        batch_id = _insert_batch(cur, "p001c-same-sha")
        _insert_file(cur, batch_id, FILE_A, "/isolation-test/src/a.pdf", SHA_A)
        _insert_file(cur, batch_id, FILE_B, "/isolation-test/src/b.pdf", SHA_A)
        cur.execute(
            """
            SELECT file_id, source_path, source_sha256
            FROM file_registry
            ORDER BY source_path
            """
        )
        rows = cur.fetchall()
    print("SAME_SHA_ROWS", rows)
    assert len(rows) == 2
    assert rows[0][2].strip() == SHA_A
    assert rows[1][2].strip() == SHA_A
    assert rows[0][1] != rows[1][1]


def test_duplicate_canonical_path_rejected(tx):
    with tx.cursor() as cur:
        batch_id = _insert_batch(cur, "p001c-dup-path")
        _insert_file(cur, batch_id, FILE_A, "/isolation-test/src/a.pdf", SHA_A)
        name, _message = _reject(
            cur,
            """
            INSERT INTO file_registry (
              file_id, source_path, source_sha256, "bytes", mtime,
              batch_id, content_version, id_origin
            ) VALUES (%s, %s, %s, 10, now(), %s, NULL, 'V7')
            """,
            (FILE_C, "/isolation-test/src/a.pdf", SHA_B, batch_id),
        )
    assert name == "uq_file_registry_source_path"


def test_path_hash_rejected_as_source_sha256(tx):
    with tx.cursor() as cur:
        batch_id = _insert_batch(cur, "p001c-path-hash")
        name, _message = _reject(
            cur,
            """
            INSERT INTO file_registry (
              file_id, source_path, source_sha256, "bytes", mtime,
              batch_id, content_version, id_origin
            ) VALUES (%s, %s, %s, 10, now(), %s, NULL, 'V7')
            """,
            (FILE_A, "/isolation-test/src/a.pdf", "ab" * 16, batch_id),
        )
    assert name == "chk_file_registry_source_sha256"


def test_content_version_keeps_old_archive_pointer(tx):
    with tx.cursor() as cur:
        batch_id, decision_id = _seed_archived(
            cur, "p001c-version", FILE_A, "/isolation-test/src/a.pdf", SHA_A
        )
        cur.execute(
            """
            INSERT INTO archive_executions (
              decision_id, file_id, content_version, final_path,
              source_sha256, target_sha256, target_bytes,
              bytes_ok, sha_ok, src_unchanged, status
            ) VALUES (%s, %s, 1, %s, %s, %s, 10, 1, 1, 1, 'COMPLETED')
            RETURNING id
            """,
            (decision_id, FILE_A, "/isolation-test/archive/a-v1.pdf", SHA_A, SHA_A),
        )
        execution_id = cur.fetchone()[0]
        _insert_version(cur, batch_id, FILE_A, 2, SHA_B, nbytes=11)
        cur.execute(
            """
            UPDATE file_registry
            SET source_sha256 = %s, content_version = 2, "bytes" = 11, mtime = now()
            WHERE file_id = %s
            """,
            (SHA_B, FILE_A),
        )
        cur.execute(
            """
            SELECT content_version, source_sha256
            FROM archive_decisions
            WHERE id = %s
            """,
            (decision_id,),
        )
        decision = cur.fetchone()
        cur.execute(
            """
            SELECT content_version, final_path, status
            FROM archive_executions
            WHERE id = %s
            """,
            (execution_id,),
        )
        execution = cur.fetchone()
        cur.execute(
            """
            SELECT content_version, source_sha256
            FROM file_content_versions
            WHERE file_id = %s
            ORDER BY content_version
            """,
            (FILE_A,),
        )
        versions = cur.fetchall()
        cur.execute(
            "SELECT content_version, source_sha256 FROM file_registry WHERE file_id = %s",
            (FILE_A,),
        )
        current = cur.fetchone()
        print("OLD_DECISION", decision)
        print("OLD_EXECUTION", execution)
        print("VERSION_ROWS", versions)
        print("REGISTRY_CURRENT", current)
        assert decision[0] == 1 and decision[1].strip() == SHA_A
        assert execution[0] == 1 and execution[2] == "COMPLETED"
        assert [row[0] for row in versions] == [1, 2]
        assert versions[0][1].strip() == SHA_A
        assert versions[1][1].strip() == SHA_B
        assert current[0] == 2 and current[1].strip() == SHA_B
        name, message = _reject(
            cur,
            "UPDATE archive_decisions SET content_version = 2 WHERE id = %s",
            (decision_id,),
        )
    assert name is None
    assert "immutable" in message


def test_legacy_non_uuid_id_inserts(tx):
    with tx.cursor() as cur:
        batch_id = _insert_batch(cur, "p001c-legacy")
        _insert_file(
            cur,
            batch_id,
            LEGACY,
            "/isolation-test/src/legacy.pdf",
            None,
            origin="LEGACY",
            content_version=None,
            nbytes=4,
        )
        cur.execute(
            """
            INSERT INTO processing_tasks (
              task_id, file_id, stage, status, retry, content_version, task_origin
            ) VALUES (%s, %s, 'SCAN', 'PENDING', 0, NULL, 'LEGACY')
            """,
            (LEGACY, LEGACY),
        )
        _insert_version(cur, batch_id, LEGACY, 1, SHA_A, nbytes=4)
        decision_id = _insert_decision(cur, LEGACY, 1, 1, SHA_A)
        cur.execute(
            """
            SELECT file_id, id_origin FROM file_registry WHERE file_id = %s
            """,
            (LEGACY,),
        )
        registered = cur.fetchone()
        cur.execute(
            "SELECT task_id, file_id, task_origin FROM processing_tasks WHERE task_id = %s",
            (LEGACY,),
        )
        task = cur.fetchone()
        cur.execute(
            "SELECT file_id, content_version FROM archive_decisions WHERE id = %s",
            (decision_id,),
        )
        decision = cur.fetchone()
        print("LEGACY_FILE", registered)
        print("LEGACY_TASK", task)
        print("LEGACY_DECISION", decision)
        assert registered[0] == LEGACY and registered[1] == "LEGACY"
        assert task[0] == task[1] == LEGACY
        assert decision[0] == LEGACY and decision[1] == 1
        _insert_file(cur, batch_id, FILE_A, "/isolation-test/src/v7.pdf", None, origin="V7")
        name, _message = _reject(
            cur,
            """
            INSERT INTO processing_tasks (
              task_id, file_id, stage, status, retry, task_origin
            ) VALUES (%s, %s, 'PARSE', 'PENDING', 0, 'V7')
            """,
            (FILE_A, FILE_A),
        )
    assert name == "chk_processing_tasks_new_task_ne_file"


def _completed_params(decision_id, final_path, target_bytes, target_sha):
    return (
        decision_id,
        FILE_A,
        final_path,
        SHA_A,
        target_sha,
        target_bytes,
    )


COMPLETED_SQL = """
INSERT INTO archive_executions (
  decision_id, file_id, content_version, final_path,
  source_sha256, target_sha256, target_bytes,
  bytes_ok, sha_ok, src_unchanged, status
) VALUES (%s, %s, 1, %s, %s, %s, %s, 1, 1, 1, 'COMPLETED')
"""


def test_completed_missing_path_rejected(tx):
    with tx.cursor() as cur:
        _, decision_id = _seed_archived(cur, "p001c-no-path", FILE_A, "/isolation-test/src/a.pdf")
        name, _message = _reject(cur, COMPLETED_SQL, _completed_params(decision_id, None, 10, SHA_A))
    assert name == "chk_completed"


def test_completed_missing_size_rejected(tx):
    with tx.cursor() as cur:
        _, decision_id = _seed_archived(cur, "p001c-no-size", FILE_A, "/isolation-test/src/a.pdf")
        name, _message = _reject(
            cur,
            COMPLETED_SQL,
            _completed_params(decision_id, "/isolation-test/archive/a.pdf", None, SHA_A),
        )
    assert name == "chk_completed"


def test_completed_missing_sha256_rejected(tx):
    with tx.cursor() as cur:
        _, decision_id = _seed_archived(cur, "p001c-no-sha", FILE_A, "/isolation-test/src/a.pdf")
        name, _message = _reject(
            cur,
            COMPLETED_SQL,
            _completed_params(decision_id, "/isolation-test/archive/a.pdf", 10, None),
        )
    assert name == "chk_completed"


def test_completed_update_without_facts_rejected(tx):
    with tx.cursor() as cur:
        _, decision_id = _seed_archived(cur, "p001c-upd", FILE_A, "/isolation-test/src/a.pdf")
        cur.execute(
            """
            INSERT INTO archive_executions (
              decision_id, file_id, content_version, status
            ) VALUES (%s, %s, 1, 'QUEUED')
            RETURNING id
            """,
            (decision_id, FILE_A),
        )
        execution_id = cur.fetchone()[0]
        assert execution_id
        name, _message = _reject(
            cur,
            "UPDATE archive_executions SET status = 'COMPLETED' WHERE id = %s",
            (execution_id,),
        )
    assert name == "chk_completed"


def test_completed_with_path_size_sha256_inserts(tx):
    with tx.cursor() as cur:
        _, decision_id = _seed_archived(cur, "p001c-ok", FILE_A, "/isolation-test/src/a.pdf")
        cur.execute(
            COMPLETED_SQL + " RETURNING id, status, final_path, target_bytes, target_sha256",
            _completed_params(decision_id, "/isolation-test/archive/a.pdf", 10, SHA_A),
        )
        row = cur.fetchone()
    print("VALID_COMPLETED", row)
    assert row[1] == "COMPLETED"
    assert row[2] == "/isolation-test/archive/a.pdf"
    assert row[3] == 10
    assert row[4].strip() == SHA_A


def test_archive_idempotency_second_completed_rejected(tx):
    with tx.cursor() as cur:
        _, decision_id = _seed_archived(cur, "p001c-idem", FILE_A, "/isolation-test/src/a.pdf")
        cur.execute(
            COMPLETED_SQL,
            _completed_params(decision_id, "/isolation-test/archive/a.pdf", 10, SHA_A),
        )
        cur.execute(
            """
            SELECT count(*) FROM archive_executions
            WHERE decision_id = %s AND content_version = 1 AND status = 'COMPLETED'
            """,
            (decision_id,),
        )
        assert cur.fetchone()[0] == 1
        name, _message = _reject(
            cur,
            COMPLETED_SQL,
            _completed_params(decision_id, "/isolation-test/archive/a-again.pdf", 10, SHA_A),
        )
    assert name in {"one_active_archive", "uq_archexec_decision_content_completed"}


def test_version_row_cannot_be_overwritten(tx):
    with tx.cursor() as cur:
        batch_id, _decision_id = _seed_archived(
            cur, "p001c-append", FILE_A, "/isolation-test/src/a.pdf", SHA_A
        )
        _insert_version(cur, batch_id, FILE_A, 2, SHA_B, nbytes=11)
        name, message = _reject(
            cur,
            """
            UPDATE file_content_versions
            SET source_sha256 = %s
            WHERE file_id = %s AND content_version = 1
            """,
            (SHA_B, FILE_A),
        )
    assert name is None
    assert "append-only" in message


def test_business_rows_rolled_back(tx):
    with tx.cursor() as cur:
        counts = {table: _count(cur, table) for table in sorted(BUSINESS_TABLES)}
        cur.execute("SELECT count(*) FROM schema_migrations")
        versions = cur.fetchone()[0]
    print("COUNTS_AFTER", counts, "SCHEMA_MIGRATIONS", versions)
    assert versions == 4
    assert all(n == 0 for n in counts.values()), counts
