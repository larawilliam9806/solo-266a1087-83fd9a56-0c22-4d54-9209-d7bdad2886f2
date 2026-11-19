"""SQLite 连接管理与建表/恢复逻辑。

设计要点：
- WAL + busy_timeout + 每次写事务使用 IMMEDIATE，保证多 gunicorn worker 并发安全；
- 标识映射（UID/PatientID）全局唯一且只插入、不更新，保证跨批次一致与重试不变号；
- 实例按 (原 SOP UID, 原文件 sha256) 去重，字节不同直接冲突，由上层整批拒绝。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS uid_mappings (
    role          TEXT NOT NULL CHECK (role IN ('study','series','sop')),
    original_uid  TEXT NOT NULL,
    new_uid       TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (role, original_uid)
);

CREATE TABLE IF NOT EXISTS patient_mappings (
    original_patient_id TEXT PRIMARY KEY,
    new_patient_id      TEXT NOT NULL UNIQUE,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS instances (
    sop_uid        TEXT PRIMARY KEY,           -- 原 SOPInstanceUID
    sha256         TEXT NOT NULL,
    new_sop_uid    TEXT NOT NULL UNIQUE,
    object_path    TEXT NOT NULL,              -- 处理结果在对象存储中的相对路径
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batches (
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL CHECK (status IN ('processing','ready','failed')),
    original_count   INTEGER NOT NULL DEFAULT 0,
    zip_path         TEXT,
    error            TEXT,
    source_zip_sha256 TEXT NOT NULL DEFAULT '',       -- 介质来源 ZIP 的 SHA-256（普通上传为空）
    source_series_uid TEXT NOT NULL DEFAULT '',      -- 发布所选自序列 UID（普通上传为空）
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at      TEXT
);

CREATE TABLE IF NOT EXISTS batch_files (
    batch_id              TEXT NOT NULL REFERENCES batches(id),
    sop_uid               TEXT NOT NULL,                  -- 原 SOP UID
    orig_name             TEXT NOT NULL,
    original_patient_id   TEXT NOT NULL DEFAULT '',
    original_study_uid    TEXT NOT NULL DEFAULT '',
    original_series_uid   TEXT NOT NULL DEFAULT '',
    media_path            TEXT NOT NULL DEFAULT '',      -- 实例在来源介质内的路径
    PRIMARY KEY (batch_id, sop_uid)
);

CREATE INDEX IF NOT EXISTS idx_batch_files_sop ON batch_files(sop_uid);
CREATE INDEX IF NOT EXISTS idx_instances_sha  ON instances(sha256);

-- 影像介质 ZIP 上传登记：ZIP 按 SHA-256 内容寻址，同一份介质只落盘一次
CREATE TABLE IF NOT EXISTS media_uploads (
    zip_sha256        TEXT PRIMARY KEY,
    zip_path          TEXT NOT NULL,                 -- 对象存储中的相对路径
    filename          TEXT NOT NULL DEFAULT '',      -- 上传时的原始文件名（仅展示）
    dicomdir_member   TEXT NOT NULL DEFAULT '',
    image_count       INTEGER NOT NULL DEFAULT 0,    -- DICOMDIR 中的 IMAGE 记录数
    ignored_count     INTEGER NOT NULL DEFAULT 0,    -- 被忽略的非 IMAGE 叶子记录数
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 介质序列发布登记：(介质, 序列) 唯一，保证同一份 ZIP 的同一序列只发布一次。
-- 并发确认在 IMMEDIATE 事务内 INSERT，唯一约束兜底：后来者直接复用已登记批次。
CREATE TABLE IF NOT EXISTS series_publications (
    zip_sha256    TEXT NOT NULL,
    series_uid    TEXT NOT NULL,
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    status        TEXT NOT NULL CHECK (status IN ('processing','ready','failed')),
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (zip_sha256, series_uid)
);

CREATE INDEX IF NOT EXISTS idx_publications_batch ON series_publications(batch_id);
"""

# 旧库增量迁移：早期批次表没有介质来源字段，用 ALTER 补齐（新库由建表语句覆盖）。
_COLUMN_MIGRATIONS = {
    "batches": [
        ("source_zip_sha256", "TEXT NOT NULL DEFAULT ''"),
        ("source_series_uid", "TEXT NOT NULL DEFAULT ''"),
    ],
    "batch_files": [
        ("media_path", "TEXT NOT NULL DEFAULT ''"),
    ],
}


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.get_db_path(),
        timeout=config.SQLITE_BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # 手动事务
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """开启 IMMEDIATE 事务，避免写-写竞争下的 deferred 死锁。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db() -> None:
    config.ensure_dirs()
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        _apply_column_migrations(conn)
    finally:
        conn.close()


def recover_interrupted_batches() -> list[str]:
    """进程启动时把上次中断遗留的 processing 批次标记为 failed。

    已成功入库的 instances/映射一律保留（它们本身是有效的、可被后续批次复用）；
    没有生成 zip 的批次绝不可能以 ready 状态出现，因此不存在可下载的半批。
    介质发布登记同步置为 failed，允许数据员重试（同介质同序列复用同一登记行）。
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id FROM batches WHERE status='processing'"
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            conn.execute(
                "UPDATE batches SET status='failed', error='服务重启，批次中断', "
                "finished_at=datetime('now') WHERE status='processing'"
            )
            conn.execute(
                "UPDATE series_publications SET status='failed', "
                "error='服务重启，发布中断', updated_at=datetime('now') "
                "WHERE status='processing'"
            )
        return ids
    finally:
        conn.close()
