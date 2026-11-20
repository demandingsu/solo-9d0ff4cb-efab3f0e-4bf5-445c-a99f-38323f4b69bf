"""SQLite 连接与事务管理。

库结构：
- source_subtitles: 原片字幕（素材号, 字幕号 为主键），version 用于乐观并发控制
- selections:       选段，按 seq 首尾相接拼成成片，offset_ms 为该段在成片中的起点
- final_rows:       已保存的成片字幕行（页面读取的就是这张表）
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_subtitles (
    material_id  TEXT    NOT NULL,
    subtitle_no  INTEGER NOT NULL,
    start_ms     INTEGER NOT NULL,
    end_ms       INTEGER NOT NULL,
    text         TEXT    NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (material_id, subtitle_no),
    CHECK (start_ms >= 0 AND start_ms < end_ms)
);

CREATE TABLE IF NOT EXISTS selections (
    seq         INTEGER PRIMARY KEY,   -- 成片顺序，从 1 开始
    material_id TEXT    NOT NULL,
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER NOT NULL,
    offset_ms   INTEGER NOT NULL,      -- 本段在成片时间轴上的起点
    CHECK (start_ms >= 0 AND start_ms < end_ms)
);

CREATE TABLE IF NOT EXISTS final_rows (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    selection_seq  INTEGER NOT NULL REFERENCES selections(seq) ON DELETE CASCADE,
    material_id    TEXT    NOT NULL,
    subtitle_no    INTEGER NOT NULL,
    final_start_ms INTEGER NOT NULL,
    final_end_ms   INTEGER NOT NULL,
    src_start_ms   INTEGER NOT NULL,   -- 重算时原字幕的起止（展示用快照）
    src_end_ms     INTEGER NOT NULL,
    start_clipped  INTEGER NOT NULL DEFAULT 0,  -- 起点被本段切点裁掉
    end_clipped    INTEGER NOT NULL DEFAULT 0,  -- 终点被本段切点裁掉
    text           TEXT    NOT NULL,
    UNIQUE (selection_seq, material_id, subtitle_no)
);
"""


def db_path() -> str:
    return os.environ.get("APP_DB", os.path.join("data", "app.db"))


def connect() -> sqlite3.Connection:
    path = db_path()
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # 自动提交，事务显式控制
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction():
    """写事务：任一一步失败整体回滚，原字幕和成片行都保留上次结果。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
