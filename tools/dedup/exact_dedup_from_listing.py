#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从全量文件清单做字节级精确去重，不抽取 PDF 文本。

三步都在能跑代码的机器上执行：

1. load   只读 20 个 CSV，按文件大小找出可能重复的候选。不连存储机。
2. hash   只对候选算 SHA-256。默认让存储机本地算，网络只回哈希。
3. report 写出精确重复组。主件只做标记，不删除、不移动源文件。
4. masters 写出全部主件清单（每组留一份，其余文件全保留），交给阶段一 load。

大小不同的文件不会进入 hash。大小相同只说明「可能相同」，报告里的组必须哈希一致。
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import os
import queue
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

EMPTY_SHA = hashlib.sha256(b"").hexdigest()
PATH_KEYS = ("完整路径", "路径", "path", "full_path")
SIZE_KEYS = ("文件大小_字节", "文件大小", "size", "bytes", "file_size")
NAME_KEYS = ("文件名", "name", "filename")
EXT_KEYS = ("扩展名", "ext", "suffix")
MTIME_KEYS = ("修改时间", "mtime", "modified")

# 这段代码在存储机上执行。源码里不要出现单引号，方便远端 shell 包一层引号。
REMOTE_HASHER = r'''
import sys, base64, hashlib
tab = chr(9)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        path = base64.b64decode(line).decode("utf-8")
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                blob = fh.read(8388608)
                if not blob:
                    break
                digest.update(blob)
        sys.stdout.write("OK" + tab + digest.hexdigest() + tab + line + "\n")
    except Exception as exc:
        msg = str(exc).replace(tab, " ").replace("\n", " ")[:180]
        sys.stdout.write("ERR" + tab + type(exc).__name__ + ":" + msg + tab + line + "\n")
    sys.stdout.flush()
'''


def human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{n} B"


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-524288")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            name TEXT,
            ext TEXT,
            mtime TEXT,
            sha256 TEXT,
            hash_status TEXT,
            hash_error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            k TEXT PRIMARY KEY,
            v TEXT
        )
        """
    )
    conn.commit()


def resolve_field(fieldnames, keys):
    if not fieldnames:
        return None
    mapping = {(name or "").strip(): name for name in fieldnames}
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def open_text(path: str):
    """整份文件都能按 UTF-8 解开才用 UTF-8，否则按 GB18030。只看开头会在中途遇到 GBK 行时崩掉。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with open(path, "r", encoding=encoding, newline="") as probe:
                while probe.read(8 * 1024 * 1024):
                    pass
        except UnicodeDecodeError:
            continue
        return open(path, "r", encoding=encoding, newline=""), encoding
    raise UnicodeDecodeError("gb18030", b"", 0, 1, f"{path} 既不是 UTF-8 也不是 GB18030")


_MTIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
    "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日",
)


def normalize_mtime(raw: str) -> str:
    """统一成 YYYY-MM-DD HH:MM:SS，按字符串排序才等于按时间排序。认不出的原样保留并加 ? 前缀排到最后。"""
    import datetime as _dt

    value = (raw or "").strip()
    if not value:
        return ""
    if value.replace(".", "", 1).isdigit() and len(value.split(".")[0]) >= 9:
        return _dt.datetime.fromtimestamp(float(value), _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    head = value.split(".")[0].split("+")[0].rstrip("Z")
    for fmt in _MTIME_FORMATS:
        try:
            return _dt.datetime.strptime(head, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return "?" + value


def normalize_ext(ext: str, path: str) -> str:
    value = (ext or "").strip().lower()
    if not value:
        value = os.path.splitext(path)[1].lower()
    if value and not value.startswith("."):
        value = "." + value
    return value


def want_ext(ext: str, path: str, only_ext: str) -> bool:
    if not only_ext:
        return True
    return normalize_ext(ext, path) == only_ext


def rebuild_collisions(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS collision_size")
    conn.execute(
        """
        CREATE TABLE collision_size (
            size INTEGER PRIMARY KEY,
            n INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO collision_size(size, n)
        SELECT size, COUNT(*)
        FROM files
        WHERE COALESCE(hash_status, '') != 'PATH_CONFLICT'
        GROUP BY size
        HAVING COUNT(*) > 1
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_files_size ON files(size)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_files_sha ON files(sha256)")
    conn.commit()


def write_load_stats(
    conn: sqlite3.Connection,
    out_dir: str,
    rows_read: int,
    bad_rows: int,
    kept: int,
    filtered_ext: int,
    only_ext: str,
) -> str:
    unique_paths = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conflicts = conn.execute(
        "SELECT COUNT(*) FROM files WHERE hash_status = 'PATH_CONFLICT'"
    ).fetchone()[0]
    ext_rows = conn.execute(
        "SELECT ext, COUNT(*) AS n FROM files GROUP BY ext ORDER BY n DESC LIMIT 15"
    ).fetchall()
    unique_size_files = conn.execute(
        """
        SELECT COALESCE(SUM(n), 0) FROM (
            SELECT COUNT(*) AS n FROM files
            WHERE COALESCE(hash_status, '') != 'PATH_CONFLICT'
            GROUP BY size
            HAVING COUNT(*) = 1
        )
        """
    ).fetchone()[0]
    collision = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(n), 0), COALESCE(SUM(size * n), 0) FROM collision_size"
    ).fetchone()
    top = conn.execute(
        "SELECT size, n, size * n AS bytes FROM collision_size ORDER BY n DESC, bytes DESC LIMIT 20"
    ).fetchall()

    summary_path = os.path.join(out_dir, "collision_summary.csv")
    with open(summary_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["文件大小_字节", "文件数", "合计字节"])
        for row in conn.execute(
            "SELECT size, n, size * n AS bytes FROM collision_size ORDER BY n DESC, size"
        ):
            writer.writerow([row["size"], row["n"], row["bytes"]])

    lines = [
        "阶段：大小预筛（未读文件内容，未算 SHA-256）",
        f"扩展名过滤：{only_ext or '不过滤'}",
        f"清单数据行：{rows_read}",
        f"解析失败行：{bad_rows}",
        f"扩展名不符已跳过：{filtered_ext}",
        f"写入台账的路径行：{kept}",
        f"唯一路径：{unique_paths}",
        f"清单重复行（同一路径多出来的行）：{max(kept - unique_paths, 0)}",
        f"大小互相矛盾的路径：{conflicts}",
        f"大小唯一、不可能是精确重复的文件：{unique_size_files}",
        f"发生碰撞的大小种类：{collision[0]}",
        f"需要进一步哈希的文件：{collision[1]}",
        f"需要进一步哈希的字节：{collision[2]} ({human_bytes(collision[2] or 0)})",
        "",
        "扩展名 TOP15：",
    ]
    for row in ext_rows:
        lines.append(f"  {row['ext'] or '(空)'}  {row['n']}")
    lines.append("")
    lines.append("人数最多的 20 个同大小组（大小相同还不能当重复）：")
    for row in top:
        lines.append(
            f"  大小 {row['size']} 字节  文件 {row['n']}  合计 {human_bytes(row['bytes'])}"
        )
    lines.append("")
    lines.append("下一步只对「需要进一步哈希的文件」算 SHA-256。")
    text = "\n".join(lines) + "\n"
    stats_path = os.path.join(out_dir, "stats_size.txt")
    with open(stats_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(text)
    print(f"已写入 {stats_path}")
    print(f"已写入 {summary_path}")
    return stats_path


def cmd_load(args: argparse.Namespace) -> None:
    csv_dir = os.path.expanduser(args.csv_dir)
    out_dir = os.path.expanduser(args.out)
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, "dedup.sqlite")
    if os.path.exists(db_path):
        if not args.reset:
            sys.exit(f"已有 {db_path}。确认要重做预筛时再加 --reset。")
        os.remove(db_path)
        for suffix in ("-wal", "-shm"):
            leftover = db_path + suffix
            if os.path.exists(leftover):
                os.remove(leftover)

    names = sorted(
        name for name in os.listdir(csv_dir) if name.lower().endswith(".csv")
    )
    if not names:
        sys.exit(f"{csv_dir} 里没有 CSV")

    only_ext = (args.ext or "").strip().lower()
    if only_ext and not only_ext.startswith("."):
        only_ext = "." + only_ext

    conn = connect(db_path)
    ensure_schema(conn)
    rows_read = 0
    bad_rows = 0
    kept = 0
    filtered_ext = 0
    bad_samples = []
    batch = []

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        conn.executemany(
            """
            INSERT INTO files(path, size, name, ext, mtime)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                hash_status = CASE
                    WHEN files.size != excluded.size THEN 'PATH_CONFLICT'
                    ELSE files.hash_status
                END
            """,
            batch,
        )
        conn.commit()
        batch = []

    for name in names:
        full = os.path.join(csv_dir, name)
        handle, _encoding = open_text(full)
        with handle:
            reader = csv.DictReader(handle)
            path_key = resolve_field(reader.fieldnames, PATH_KEYS)
            size_key = resolve_field(reader.fieldnames, SIZE_KEYS)
            if not path_key or not size_key:
                sys.exit(f"{name} 缺少路径或大小列，表头是：{reader.fieldnames}")
            name_key = resolve_field(reader.fieldnames, NAME_KEYS)
            ext_key = resolve_field(reader.fieldnames, EXT_KEYS)
            mtime_key = resolve_field(reader.fieldnames, MTIME_KEYS)
            for row in reader:
                rows_read += 1
                path = (row.get(path_key) or "").strip()
                raw_size = (row.get(size_key) or "").strip().replace(",", "")
                try:
                    size = int(raw_size)
                    if size < 0 or not path:
                        raise ValueError
                except ValueError:
                    bad_rows += 1
                    if len(bad_samples) < 20:
                        bad_samples.append(f"{name}:{rows_read}:{path}:{raw_size}")
                    continue
                ext = row.get(ext_key) if ext_key else ""
                if not want_ext(ext, path, only_ext):
                    filtered_ext += 1
                    continue
                file_name = (row.get(name_key) or "").strip() if name_key else os.path.basename(path)
                mtime = normalize_mtime(row.get(mtime_key) or "") if mtime_key else ""
                batch.append((path, size, file_name, normalize_ext(ext, path), mtime))
                kept += 1
                if len(batch) >= 20000:
                    flush()
                    if kept % 200000 == 0:
                        print(f"已写入 {kept} 条路径", flush=True)
        print(f"读完 {name}，累计保留 {kept}", flush=True)
    flush()
    if bad_samples:
        with open(os.path.join(out_dir, "bad_rows.txt"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(bad_samples) + "\n")
    rebuild_collisions(conn)
    conn.execute(
        "INSERT INTO meta(k, v) VALUES ('rows_read', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (str(rows_read),),
    )
    conn.execute(
        "INSERT INTO meta(k, v) VALUES ('bad_rows', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (str(bad_rows),),
    )
    conn.commit()
    write_load_stats(conn, out_dir, rows_read, bad_rows, kept, filtered_ext, only_ext)
    conn.close()


def mark_empty_collisions(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        """
        UPDATE files
        SET sha256 = ?, hash_status = 'OK', hash_error = NULL
        WHERE size = 0
          AND COALESCE(hash_status, '') = ''
          AND size IN (SELECT size FROM collision_size)
        """,
        (EMPTY_SHA,),
    )
    conn.commit()
    return cur.rowcount


def pending_files(conn: sqlite3.Connection, limit: int):
    sql = """
        SELECT f.path, f.size
        FROM files f
        JOIN collision_size c ON c.size = f.size
        WHERE f.hash_status IS NULL OR f.hash_status = 'ERR'
        ORDER BY CASE WHEN f.hash_status IS NULL THEN 0 ELSE 1 END, f.size ASC, f.path
    """
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    return [(row["path"], row["size"]) for row in conn.execute(sql)]


def pack_batches(items, max_files: int, max_bytes: int):
    batch = []
    batch_bytes = 0
    for path, size in items:
        if batch and (len(batch) >= max_files or batch_bytes + size > max_bytes):
            yield batch
            batch = []
            batch_bytes = 0
        batch.append((path, size))
        batch_bytes += size
    if batch:
        yield batch


def encode_paths(paths) -> str:
    return "".join(base64.b64encode(path.encode("utf-8")).decode("ascii") + "\n" for path in paths)


def decode_key(token: str) -> str:
    return base64.b64decode(token.encode("ascii")).decode("utf-8")


def ssh_command(host: str, control_path: str) -> list[str]:
    remote = "python3 -u -c " + shlex.quote(REMOTE_HASHER)
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=4",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=30m",
        "-o",
        f"ControlPath={control_path}",
        host,
        remote,
    ]


def run_remote_batch(host: str, control_path: str, paths, timeout: int):
    proc = subprocess.Popen(
        ssh_command(host, control_path),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(encode_paths(paths), timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return {}, "TIMEOUT", "ssh 超时"
    parsed = {}
    for line in stdout.splitlines():
        status, _, rest = line.partition("\t")
        payload, _, key = rest.rpartition("\t")
        if not key:
            continue
        try:
            path = decode_key(key)
        except (ValueError, UnicodeDecodeError):
            continue
        parsed[path] = (status, payload)
    if proc.returncode != 0 and not parsed:
        err = (stderr or "").strip().replace("\n", " ")[:300]
        return {}, "SSH", err or f"ssh exit {proc.returncode}"
    return parsed, "", (stderr or "").strip()


def hash_local_batch(paths):
    parsed = {}
    for path in paths:
        try:
            parsed[path] = ("OK", hash_file(path))
        except OSError as exc:
            parsed[path] = ("ERR", f"{type(exc).__name__}:{exc}")
    return parsed


def hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            blob = handle.read(8 * 1024 * 1024)
            if not blob:
                break
            digest.update(blob)
    return digest.hexdigest()


def apply_results(conn: sqlite3.Connection, expected, parsed, fallback_status: str, fallback_error: str) -> None:
    rows = []
    for path, _size in expected:
        status, payload = parsed.get(path, (fallback_status, fallback_error))
        if status == "OK" and len(payload) == 64 and all(ch in "0123456789abcdef" for ch in payload):
            rows.append((payload, "OK", None, path))
        else:
            rows.append((None, "ERR", (payload or fallback_error or status)[:300], path))
    conn.executemany(
        "UPDATE files SET sha256 = ?, hash_status = ?, hash_error = ? WHERE path = ?",
        rows,
    )
    conn.commit()


def batch_timeout(items) -> int:
    total = sum(size for _path, size in items)
    return max(180, int(total / (20 * 1024 * 1024)) + 20 * len(items))


def cmd_hash(args: argparse.Namespace) -> None:
    out_dir = os.path.expanduser(args.out)
    db_path = os.path.join(out_dir, "dedup.sqlite")
    if not os.path.exists(db_path):
        sys.exit("还没有预筛库。先运行 load。")
    if args.mode == "ssh" and not args.ssh:
        sys.exit("ssh 模式需要 --ssh user@storage-host。只想试读本地文件时用 --mode local。")

    conn = connect(db_path)
    ensure_schema(conn)
    rebuild_collisions(conn)
    empty_marked = mark_empty_collisions(conn)
    if empty_marked:
        print(f"0 字节碰撞文件直接记为空文件哈希：{empty_marked}")

    items = pending_files(conn, args.limit)
    if not items:
        print("没有待哈希的碰撞文件。可以运行 report。")
        conn.close()
        return

    print(
        f"本轮待哈希 {len(items)} 个文件，并发 {args.workers}。"
        "存储机磁盘是瓶颈，先用 8，确认磁盘没打满再加大，不要开到 96。"
    )
    batches = list(pack_batches(items, args.batch_files, args.batch_bytes))
    work_q = queue.Queue(maxsize=max(1, args.workers) * 2)
    result_q = queue.Queue()

    def work(batch):
        paths = [path for path, _size in batch]
        if args.mode == "local":
            return batch, hash_local_batch(paths), "", ""
        parsed, status, error = run_remote_batch(
            args.ssh, args.control_path, paths, batch_timeout(batch)
        )
        missing = [item for item in batch if item[0] not in parsed]
        if missing and len(batch) > 1:
            for path, size in missing:
                one, one_status, one_error = run_remote_batch(
                    args.ssh, args.control_path, [path], batch_timeout([(path, size)])
                )
                if path in one:
                    parsed[path] = one[path]
                else:
                    parsed[path] = ("ERR", one_error or one_status or error or "UNKNOWN")
        elif missing and len(batch) == 1:
            parsed[batch[0][0]] = ("ERR", error or status or "UNKNOWN")
        return batch, parsed, status, error

    def worker() -> None:
        while True:
            batch = work_q.get()
            try:
                if batch is None:
                    return
                result_q.put(work(batch))
            except Exception as exc:
                result_q.put((batch, {}, "ERR", f"{type(exc).__name__}:{exc}"))
            finally:
                work_q.task_done()

    threads = []
    for _ in range(max(1, args.workers)):
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        threads.append(thread)

    def feed() -> None:
        for batch in batches:
            work_q.put(batch)
        for _ in threads:
            work_q.put(None)

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    done = 0
    done_bytes = 0
    for _ in batches:
        batch, parsed, status, error = result_q.get()
        apply_results(conn, batch, parsed, status or "ERR", error or "没有返回")
        done += len(batch)
        done_bytes += sum(size for _path, size in batch)
        print(f"进度 {done}/{len(items)}  已读 {human_bytes(done_bytes)}", flush=True)
    feeder.join()
    for thread in threads:
        thread.join()
    remaining = conn.execute(
        """
        SELECT COUNT(*)
        FROM files f JOIN collision_size c ON c.size = f.size
        WHERE f.hash_status IS NULL
        """
    ).fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM files WHERE hash_status = 'ERR'").fetchone()[0]
    print(f"本轮结束。尚未哈希 {remaining}，失败 {failed}。失败可直接重跑 hash，已成功的会跳过。")
    conn.close()


def cmd_report(args: argparse.Namespace) -> None:
    out_dir = os.path.expanduser(args.out)
    db_path = os.path.join(out_dir, "dedup.sqlite")
    if not os.path.exists(db_path):
        sys.exit("还没有预筛库。先运行 load。")
    conn = connect(db_path)
    groups_path = os.path.join(out_dir, "exact_groups.csv")
    errors_path = os.path.join(out_dir, "hash_errors.csv")

    with open(errors_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["完整路径", "文件大小_字节", "错误"])
        for row in conn.execute(
            """
            SELECT path, size, hash_error
            FROM files
            WHERE hash_status = 'ERR'
            ORDER BY path
            """
        ):
            writer.writerow([row["path"], row["size"], row["hash_error"] or ""])

    group_count = 0
    member_count = 0
    extra_count = 0
    extra_bytes = 0
    with open(groups_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["精确哈希", "组成员数", "是否主件", "文件大小_字节", "修改时间", "扩展名", "文件名", "完整路径"]
        )
        current = None
        bucket = []

        def emit(rows) -> None:
            nonlocal group_count, member_count, extra_count, extra_bytes
            if len(rows) < 2:
                return
            group_count += 1
            member_count += len(rows)
            extra_count += len(rows) - 1
            extra_bytes += sum(row["size"] for row in rows[1:])
            for index, row in enumerate(rows):
                writer.writerow(
                    [
                        row["sha256"],
                        len(rows),
                        "是" if index == 0 else "否",
                        row["size"],
                        row["mtime"] or "",
                        row["ext"] or "",
                        row["name"] or "",
                        row["path"],
                    ]
                )

        query = conn.execute(
            """
            SELECT sha256, path, size, mtime, ext, name
            FROM files
            WHERE hash_status = 'OK' AND sha256 IS NOT NULL
            ORDER BY sha256,
                     CASE WHEN mtime IS NULL OR mtime = '' OR mtime LIKE '?%' THEN 1 ELSE 0 END,
                     mtime,
                     path
            """
        )
        for row in query:
            if current != row["sha256"]:
                emit(bucket)
                bucket = []
                current = row["sha256"]
            bucket.append(row)
        emit(bucket)

    pending = conn.execute(
        """
        SELECT COUNT(*)
        FROM files f JOIN collision_size c ON c.size = f.size
        WHERE f.hash_status IS NULL
        """
    ).fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM files WHERE hash_status = 'ERR'").fetchone()[0]
    hashed = conn.execute("SELECT COUNT(*) FROM files WHERE hash_status = 'OK'").fetchone()[0]
    same_size_diff = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT sha256
            FROM files
            WHERE hash_status = 'OK'
            GROUP BY sha256
            HAVING COUNT(*) = 1
        )
        """
    ).fetchone()[0]
    lines = [
        "阶段：精确重复报告",
        "判定：SHA-256 完全一致才算同一组。主件 = 修改时间最早，时间相同则完整路径字典序最小。",
        "这份报告只标记重复关系，不删除源文件，不移动源文件，也不把副本写成低价值目录。",
        "专利副本即使字节相同，后续仍按专利优先，不在本报告里改分类。",
        f"已算出哈希的文件：{hashed}",
        f"精确重复组：{group_count}",
        f"组内文件：{member_count}",
        f"多余副本：{extra_count}",
        f"多余副本字节：{extra_bytes} ({human_bytes(extra_bytes)})",
        f"同大小但哈希不同的文件：{same_size_diff}",
        f"尚未哈希的碰撞文件：{pending}",
        f"哈希失败：{failed}",
    ]
    if pending or failed:
        lines.append("还有未完成或失败的文件，当前重复组不是最终全量结果。")
    else:
        lines.append("碰撞文件都已有结果，这份重复组可以作为精确去重结果。")
    text = "\n".join(lines) + "\n"
    report_path = os.path.join(out_dir, "stats_exact.txt")
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(text)
    print(f"已写入 {groups_path}")
    print(f"已写入 {errors_path}")
    print(f"已写入 {report_path}")
    conn.close()


MASTER_SQL = """
    WITH ranked AS (
        SELECT f.path, f.size, f.ext, f.hash_status, f.sha256,
               CASE WHEN c.size IS NULL THEN 0 ELSE 1 END AS collided,
               ROW_NUMBER() OVER (
                   PARTITION BY CASE WHEN f.hash_status = 'OK' AND c.size IS NOT NULL THEN f.sha256 ELSE f.path END
                   ORDER BY CASE WHEN f.mtime IS NULL OR f.mtime = '' OR f.mtime LIKE '?%' THEN 1 ELSE 0 END,
                            f.mtime, f.path
               ) AS rn,
               COUNT(*) OVER (
                   PARTITION BY CASE WHEN f.hash_status = 'OK' AND c.size IS NOT NULL THEN f.sha256 ELSE f.path END
               ) AS members
        FROM files f LEFT JOIN collision_size c ON c.size = f.size
    )
    SELECT path, size, ext, hash_status, collided, members
    FROM ranked
    WHERE rn = 1
    ORDER BY path
"""


def master_source(row) -> str:
    if row["hash_status"] == "PATH_CONFLICT":
        return "大小矛盾"
    if not row["collided"]:
        return "大小唯一"
    if row["hash_status"] == "OK":
        return "精确组主件" if row["members"] > 1 else "哈希唯一"
    if row["hash_status"] == "ERR":
        return "哈希失败"
    return "未哈希"


def cmd_masters(args: argparse.Namespace) -> None:
    """写出阶段一要处理的全部主件：每个精确重复组留一份，其余文件全部保留。"""
    out_dir = os.path.expanduser(args.out)
    db_path = os.path.join(out_dir, "dedup.sqlite")
    if not os.path.exists(db_path):
        sys.exit("还没有预筛库。先运行 load。")
    conn = connect(db_path)
    rebuild_collisions(conn)
    pending = conn.execute(
        "SELECT COUNT(*) FROM files f JOIN collision_size c ON c.size = f.size WHERE f.hash_status IS NULL"
    ).fetchone()[0]
    if pending and not args.allow_incomplete:
        sys.exit(f"还有 {pending} 个碰撞文件没算哈希。先跑完 hash；确实要先出清单时加 --allow-incomplete，"
                 "这些文件会以「未哈希」来源全部保留。")
    path = os.path.join(out_dir, "masters.csv")
    counts: dict[str, int] = {}
    with open(path + ".part", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["完整路径", "文件大小_字节", "扩展名", "来源"])
        for row in conn.execute(MASTER_SQL):
            source = master_source(row)
            counts[source] = counts.get(source, 0) + 1
            writer.writerow([row["path"], row["size"], row["ext"] or "", source])
    os.replace(path + ".part", path)
    total = sum(counts.values())
    print(f"主件 {total} 份，已写入 {path}")
    for key, value in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {key}: {value}")
    conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用文件清单做字节级精确去重，不读取 PDF 文本。")
    sub = parser.add_subparsers(dest="cmd", required=True)

    load = sub.add_parser("load", help="只读 CSV，按文件大小找出候选")
    load.add_argument("--csv-dir", required=True, help="放着 all_files_001.csv 到 020 的目录")
    load.add_argument("--out", required=True, help="输出目录，例如 ~/dedup_out")
    load.add_argument("--ext", default="", help="只保留一种扩展名，例如 pdf。默认全部文件")
    load.add_argument("--reset", action="store_true", help="删掉已有预筛库后重做")
    load.set_defaults(func=cmd_load)

    digest = sub.add_parser("hash", help="只给大小相同的文件算 SHA-256")
    digest.add_argument("--out", required=True)
    digest.add_argument("--mode", choices=("ssh", "local"), default="ssh")
    digest.add_argument("--ssh", default="", help="存储机，例如 user@10.0.0.8")
    digest.add_argument("--workers", type=int, default=8)
    digest.add_argument("--batch-files", type=int, default=40)
    digest.add_argument("--batch-bytes", type=int, default=1024 * 1024 * 1024)
    digest.add_argument("--limit", type=int, default=0, help="本轮最多哈希多少个文件，试通时用 50")
    digest.add_argument("--control-path", default="/tmp/dedup_cm_980w")
    digest.set_defaults(func=cmd_hash)

    report = sub.add_parser("report", help="写出精确重复组")
    report.add_argument("--out", required=True)
    report.set_defaults(func=cmd_report)

    masters = sub.add_parser("masters", help="写出全部主件清单 masters.csv，给阶段一 load 用")
    masters.add_argument("--out", required=True)
    masters.add_argument("--allow-incomplete", action="store_true", help="碰撞文件还没哈希完也先出清单")
    masters.set_defaults(func=cmd_masters)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
