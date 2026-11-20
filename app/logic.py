"""核心业务：时间轴换算、成片行重算、单行校时。

区间约定：所有区间含起点、不含终点 [start, end)。
选段在成片里首尾相接：第 i 段成片区间 = [offset_i, offset_i + 时长_i)。
原字幕与选段的可见交集 = [max(字幕起, 段起), min(字幕止, 段止))，非空才生成成片行。
"""
from __future__ import annotations

import sqlite3
from typing import Optional


class ValidationError(Exception):
    """输入不合法（越界、起止颠倒、行不存在等），保存被拒绝。"""


class Conflict(Exception):
    """原字幕在该行读出后被改过，拒绝保存并提示刷新。"""


# ---------- 查询 ----------

def list_final_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT fr.*, s.offset_ms AS seg_final_start,
               s.offset_ms + (s.end_ms - s.start_ms) AS seg_final_end
        FROM final_rows fr
        JOIN selections s ON s.seq = fr.selection_seq
        ORDER BY fr.final_start_ms, fr.id
        """
    ).fetchall()


def get_edit_context(conn: sqlite3.Connection, row_id: int) -> Optional[dict]:
    """编辑页所需的全部现值（裁切标记、可见区间都按当前库内状态实时换算）。"""
    row = conn.execute("SELECT * FROM final_rows WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        return None
    sel = conn.execute("SELECT * FROM selections WHERE seq = ?", (row["selection_seq"],)).fetchone()
    sub = conn.execute(
        "SELECT * FROM source_subtitles WHERE material_id = ? AND subtitle_no = ?",
        (row["material_id"], row["subtitle_no"]),
    ).fetchone()
    if sel is None or sub is None:
        return None
    start_clipped = sub["start_ms"] < sel["start_ms"]
    end_clipped = sub["end_ms"] > sel["end_ms"]
    vis_src_start = max(sub["start_ms"], sel["start_ms"])
    vis_src_end = min(sub["end_ms"], sel["end_ms"])
    return {
        "row": row,
        "sel": sel,
        "sub": sub,
        "version": sub["version"],
        "start_clipped": start_clipped,
        "end_clipped": end_clipped,
        "final_start": sel["offset_ms"] + (vis_src_start - sel["start_ms"]),
        "final_end": sel["offset_ms"] + (vis_src_end - sel["start_ms"]),
        "seg_final_start": sel["offset_ms"],
        "seg_final_end": sel["offset_ms"] + (sel["end_ms"] - sel["start_ms"]),
    }


def snapshot(conn: sqlite3.Connection) -> dict:
    """给 API/自测用的只读全量状态。"""
    return {
        "subtitles": [dict(r) for r in conn.execute(
            "SELECT * FROM source_subtitles ORDER BY material_id, subtitle_no")],
        "selections": [dict(r) for r in conn.execute(
            "SELECT * FROM selections ORDER BY seq")],
        "final_rows": [dict(r) for r in conn.execute(
            "SELECT * FROM final_rows ORDER BY final_start_ms, id")],
    }


# ---------- 重算 ----------

def _visible_interval(sel: sqlite3.Row, sub: sqlite3.Row) -> Optional[dict]:
    """原字幕在该选段里的可见部分；不可见返回 None。区间含起点不含终点。"""
    overlap_start = max(sub["start_ms"], sel["start_ms"])
    overlap_end = min(sub["end_ms"], sel["end_ms"])
    if overlap_start >= overlap_end:  # 贴边不算可见
        return None
    return {
        "final_start_ms": sel["offset_ms"] + (overlap_start - sel["start_ms"]),
        "final_end_ms": sel["offset_ms"] + (overlap_end - sel["start_ms"]),
        "start_clipped": int(sub["start_ms"] < sel["start_ms"]),
        "end_clipped": int(sub["end_ms"] > sel["end_ms"]),
    }


def recompute_subtitle(conn: sqlite3.Connection, material_id: str, subtitle_no: int) -> None:
    """只重算这一条原字幕在各段中的成片行：补新行、撤下不再可见的行。

    仍然可见的行就地更新（保持行 id 稳定），这样“行读出后原字幕被改”
    能在保存时被版本检查拦下，而不是表现为行丢失。
    """
    existing = {
        r["selection_seq"]: r
        for r in conn.execute(
            "SELECT * FROM final_rows WHERE material_id = ? AND subtitle_no = ?",
            (material_id, subtitle_no),
        )
    }
    sub = conn.execute(
        "SELECT * FROM source_subtitles WHERE material_id = ? AND subtitle_no = ?",
        (material_id, subtitle_no),
    ).fetchone()

    visible_seqs: set[int] = set()
    if sub is not None:
        sels = conn.execute(
            "SELECT * FROM selections WHERE material_id = ? ORDER BY seq", (material_id,)
        ).fetchall()
        for sel in sels:
            vis = _visible_interval(sel, sub)
            if vis is None:
                continue
            visible_seqs.add(sel["seq"])
            if sel["seq"] in existing:  # 仍在可见：就地更新
                conn.execute(
                    """
                    UPDATE final_rows
                       SET final_start_ms = ?, final_end_ms = ?,
                           src_start_ms = ?, src_end_ms = ?,
                           start_clipped = ?, end_clipped = ?, text = ?
                     WHERE id = ?
                    """,
                    (vis["final_start_ms"], vis["final_end_ms"],
                     sub["start_ms"], sub["end_ms"],
                     vis["start_clipped"], vis["end_clipped"], sub["text"],
                     existing[sel["seq"]]["id"]),
                )
            else:  # 新出现的行：补上
                conn.execute(
                    """
                    INSERT INTO final_rows
                        (selection_seq, material_id, subtitle_no,
                         final_start_ms, final_end_ms, src_start_ms, src_end_ms,
                         start_clipped, end_clipped, text)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (sel["seq"], material_id, subtitle_no,
                     vis["final_start_ms"], vis["final_end_ms"],
                     sub["start_ms"], sub["end_ms"],
                     vis["start_clipped"], vis["end_clipped"], sub["text"]),
                )

    # 撤下不再可见的行
    for seq, row in existing.items():
        if seq not in visible_seqs:
            conn.execute("DELETE FROM final_rows WHERE id = ?", (row["id"],))


def rebuild_all(conn: sqlite3.Connection) -> None:
    """导入 CSV 后全量重算。"""
    conn.execute("DELETE FROM final_rows")
    subs_by_material: dict[str, list[sqlite3.Row]] = {}
    for sub in conn.execute("SELECT * FROM source_subtitles"):
        subs_by_material.setdefault(sub["material_id"], []).append(sub)
    for sel in conn.execute("SELECT * FROM selections ORDER BY seq"):
        for sub in subs_by_material.get(sel["material_id"], []):
            vis = _visible_interval(sel, sub)
            if vis is None:
                continue
            conn.execute(
                """
                INSERT INTO final_rows
                    (selection_seq, material_id, subtitle_no,
                     final_start_ms, final_end_ms, src_start_ms, src_end_ms,
                     start_clipped, end_clipped, text)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (sel["seq"], sub["material_id"], sub["subtitle_no"],
                 vis["final_start_ms"], vis["final_end_ms"],
                 sub["start_ms"], sub["end_ms"],
                 vis["start_clipped"], vis["end_clipped"], sub["text"]),
            )


def replace_source(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.execute("DELETE FROM source_subtitles")
    conn.executemany(
        "INSERT INTO source_subtitles (material_id, subtitle_no, start_ms, end_ms, text)"
        " VALUES (?,?,?,?,?)",
        [(r["material_id"], r["subtitle_no"], r["start_ms"], r["end_ms"], r["text"]) for r in rows],
    )


def replace_selections(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """选段按给定顺序首尾相接，重算每段在成片中的偏移。"""
    conn.execute("DELETE FROM selections")
    offset = 0
    for i, r in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO selections (seq, material_id, start_ms, end_ms, offset_ms)"
            " VALUES (?,?,?,?,?)",
            (i, r["material_id"], r["start_ms"], r["end_ms"], offset),
        )
        offset += r["end_ms"] - r["start_ms"]


# ---------- 单行校时 ----------

def adjust_row(
    conn: sqlite3.Connection,
    row_id: int,
    new_final_start: Optional[int],
    new_final_end: Optional[int],
    expect_version: int,
) -> dict:
    """把选中行在成片上的新起止换算回原字幕，并只重算这条原字幕的成片行。

    被切点裁掉的一端忽略入参、保留原片时刻；未裁的一端必须落在本段范围内。
    版本不符抛 Conflict；任何校验失败抛 ValidationError。
    调用方需在事务内调用，失败回滚则原字幕与成片行都保留上次结果。
    """
    row = conn.execute("SELECT * FROM final_rows WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise ValidationError("这条成片行已不存在（可能被重算移除），请刷新列表")
    sel = conn.execute("SELECT * FROM selections WHERE seq = ?", (row["selection_seq"],)).fetchone()
    sub = conn.execute(
        "SELECT * FROM source_subtitles WHERE material_id = ? AND subtitle_no = ?",
        (row["material_id"], row["subtitle_no"]),
    ).fetchone()
    if sel is None or sub is None:
        raise ValidationError("选段或原字幕已变化，请刷新列表")

    if sub["version"] != expect_version:
        raise Conflict("这条原字幕在您读取后已被修改，请刷新页面再试")

    start_clipped = sub["start_ms"] < sel["start_ms"]
    end_clipped = sub["end_ms"] > sel["end_ms"]
    if start_clipped and end_clipped:
        raise ValidationError("本行两端都被切点裁掉，没有可调整的端点")

    # 成片时刻 -> 原片时刻：src = 段原片起点 + (成片时刻 - 段成片起点)
    if start_clipped:
        new_src_start = sub["start_ms"]  # 被裁的一端不能调整，保留原片时刻
    else:
        if new_final_start is None:
            raise ValidationError("请填写新的成片起点")
        new_src_start = sel["start_ms"] + (new_final_start - sel["offset_ms"])
        if not sel["start_ms"] <= new_src_start <= sel["end_ms"]:
            raise ValidationError(
                f"起点超出本段范围（本段原片 {sel['start_ms']}–{sel['end_ms']} ms）"
            )

    if end_clipped:
        new_src_end = sub["end_ms"]
    else:
        if new_final_end is None:
            raise ValidationError("请填写新的成片终点")
        new_src_end = sel["start_ms"] + (new_final_end - sel["offset_ms"])
        if not sel["start_ms"] <= new_src_end <= sel["end_ms"]:
            raise ValidationError(
                f"终点超出本段范围（本段原片 {sel['start_ms']}–{sel['end_ms']} ms）"
            )

    if new_src_start >= new_src_end:
        raise ValidationError("起点必须小于终点")

    # 乐观并发控制：版本不匹配则更新不到行，拒绝保存
    cur = conn.execute(
        "UPDATE source_subtitles SET start_ms = ?, end_ms = ?, version = version + 1"
        " WHERE material_id = ? AND subtitle_no = ? AND version = ?",
        (new_src_start, new_src_end, sub["material_id"], sub["subtitle_no"], expect_version),
    )
    if cur.rowcount != 1:
        raise Conflict("这条原字幕在您读取后已被修改，请刷新页面再试")

    recompute_subtitle(conn, sub["material_id"], sub["subtitle_no"])
    return {
        "material_id": sub["material_id"],
        "subtitle_no": sub["subtitle_no"],
        "start_ms": new_src_start,
        "end_ms": new_src_end,
    }
