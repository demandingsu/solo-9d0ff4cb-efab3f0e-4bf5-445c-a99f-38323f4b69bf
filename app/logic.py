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
               s.offset_ms + (s.end_ms - s.start_ms) AS seg_final_end,
               s.start_ms AS sel_src_start, s.end_ms AS sel_src_end
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
        "review_marks": [dict(r) for r in conn.execute(
            "SELECT * FROM review_marks ORDER BY material_id, subtitle_no, src_start_ms, id")],
    }


# ---------- 核看登记：区间运算 ----------

def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """把若干 [start, end) 区间合并成不重叠、按起点排序的区间列表。"""
    merged: list[list[int]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def subtract_intervals(
    start: int, end: int, covered: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """[start, end) 中未被 covered（已合并排序）覆盖的部分，即缺口。"""
    gaps: list[tuple[int, int]] = []
    cur = start
    for s, e in covered:
        if e <= cur:
            continue
        if s > cur:
            gaps.append((cur, min(s, end)))
        cur = max(cur, e)
        if cur >= end:
            break
    if cur < end:
        gaps.append((cur, end))
    return gaps


def _visible_src_interval(row: sqlite3.Row) -> tuple[int, int]:
    """成片行在原片中露出的区间（含起点、不含终点）。

    用行重算时存下的原字幕起止快照与所在段切点求交；已提交状态下该快照
    与原字幕现值一致（校时与行重算在同一事务提交）。
    """
    return (
        max(row["src_start_ms"], row["sel_src_start"]),
        min(row["src_end_ms"], row["sel_src_end"]),
    )


def rows_review_status(conn: sqlite3.Connection) -> list[dict]:
    """成片行 + 核看状态：每行附带尚未核看的成片时间区间（缺口）。

    同一原字幕的多笔登记先合并成并集，再与该行的原片可见区间求差；
    缺口最后换算成本段成片时间。调用方需在只读事务内调用，保证行与
    缺口来自同一已提交状态。
    """
    rows = list_final_rows(conn)
    marks_by_sub: dict[tuple, list[tuple[int, int]]] = {}
    for m in conn.execute(
        "SELECT material_id, subtitle_no, src_start_ms, src_end_ms FROM review_marks"
    ):
        marks_by_sub.setdefault((m["material_id"], m["subtitle_no"]), []).append(
            (m["src_start_ms"], m["src_end_ms"])
        )
    versions = {
        (s["material_id"], s["subtitle_no"]): s["version"]
        for s in conn.execute("SELECT material_id, subtitle_no, version FROM source_subtitles")
    }

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        key = (r["material_id"], r["subtitle_no"])
        merged = merge_intervals(marks_by_sub.get(key, []))
        vis_s, vis_e = _visible_src_interval(r)
        if vis_s < vis_e:
            gaps_src = subtract_intervals(vis_s, vis_e, merged)
        else:
            gaps_src = []
        # 原片时刻 -> 本段成片时刻：final = 段成片起点 + (原片时刻 - 段原片起点)
        d["visible_src_start"] = vis_s
        d["visible_src_end"] = vis_e
        d["gaps"] = [
            [r["seg_final_start"] + (g0 - r["sel_src_start"]),
             r["seg_final_start"] + (g1 - r["sel_src_start"])]
            for g0, g1 in gaps_src
        ]
        d["all_reviewed"] = not gaps_src
        d["version"] = versions.get(key)
        d["mark_count"] = len(marks_by_sub.get(key, []))
        out.append(d)
    return out


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
    """整体替换原片字幕。原稿变了，所有核看登记一并清空（载入样例同）。"""
    conn.execute("DELETE FROM source_subtitles")
    conn.execute("DELETE FROM review_marks")
    conn.executemany(
        "INSERT INTO source_subtitles (material_id, subtitle_no, start_ms, end_ms, text)"
        " VALUES (?,?,?,?,?)",
        [(r["material_id"], r["subtitle_no"], r["start_ms"], r["end_ms"], r["text"]) for r in rows],
    )


def replace_selections(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """选段按给定顺序首尾相接，重算每段在成片中的偏移。

    核看登记挂在原字幕的原片区间上，与选段无关：重传选段后不清登记，
    新算出的成片行按原片区间自动沿用。
    """
    conn.execute("DELETE FROM selections")
    offset = 0
    for i, r in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO selections (seq, material_id, start_ms, end_ms, offset_ms)"
            " VALUES (?,?,?,?,?)",
            (i, r["material_id"], r["start_ms"], r["end_ms"], offset),
        )
        offset += r["end_ms"] - r["start_ms"]


# ---------- 核看登记：写入与查询 ----------

def add_review_mark(
    conn: sqlite3.Connection,
    row_id: int,
    expect_version: int,
    expect_final_start: int,
    expect_final_end: int,
) -> dict:
    """登记「已核看」：把该行在原片中露出的区间记到对应原字幕名下。

    页面读出后若成片行或原字幕已变（行消失、行区间或字幕版本不符），
    抛 Conflict 拒绝登记并提示刷新。调用方需在事务内调用，失败回滚则
    不留下任何登记。
    """
    row = conn.execute("SELECT * FROM final_rows WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise Conflict("这条成片行已不存在（可能重传选段后被重算），请刷新列表")
    if (row["final_start_ms"], row["final_end_ms"]) != (expect_final_start, expect_final_end):
        raise Conflict("页面所见的成片行已变化，请刷新列表后再登记")
    sel = conn.execute("SELECT * FROM selections WHERE seq = ?", (row["selection_seq"],)).fetchone()
    sub = conn.execute(
        "SELECT * FROM source_subtitles WHERE material_id = ? AND subtitle_no = ?",
        (row["material_id"], row["subtitle_no"]),
    ).fetchone()
    if sel is None or sub is None:
        raise Conflict("选段或原字幕已变化，请刷新列表")
    if sub["version"] != expect_version:
        raise Conflict("这条原字幕在您读取后已被修改，请刷新页面再登记")

    # 只登记该行在原片中露出的区间（含起点、不含终点），以服务端现算为准
    vis_start = max(sub["start_ms"], sel["start_ms"])
    vis_end = min(sub["end_ms"], sel["end_ms"])
    if vis_start >= vis_end:
        raise Conflict("该行在原片中已无可见区间，请刷新列表")
    cur = conn.execute(
        "INSERT INTO review_marks (material_id, subtitle_no, src_start_ms, src_end_ms)"
        " VALUES (?,?,?,?)",
        (sub["material_id"], sub["subtitle_no"], vis_start, vis_end),
    )
    return {
        "id": cur.lastrowid,
        "material_id": sub["material_id"],
        "subtitle_no": sub["subtitle_no"],
        "src_start_ms": vis_start,
        "src_end_ms": vis_end,
    }


def get_review_context(
    conn: sqlite3.Connection, material_id: str, subtitle_no: int
) -> Optional[dict]:
    """登记明细页：某条原字幕的全部登记（含该字幕现值）。"""
    sub = conn.execute(
        "SELECT * FROM source_subtitles WHERE material_id = ? AND subtitle_no = ?",
        (material_id, subtitle_no),
    ).fetchone()
    if sub is None:
        return None
    marks = conn.execute(
        "SELECT * FROM review_marks WHERE material_id = ? AND subtitle_no = ?"
        " ORDER BY src_start_ms, id",
        (material_id, subtitle_no),
    ).fetchall()
    return {"sub": sub, "marks": marks}


def delete_review_mark(conn: sqlite3.Connection, mark_id: int) -> Optional[sqlite3.Row]:
    """撤销单笔登记，返回被删的登记；不存在返回 None。

    只删这一笔：同一区间若仍被其他登记覆盖，覆盖状态不变。
    """
    mark = conn.execute("SELECT * FROM review_marks WHERE id = ?", (mark_id,)).fetchone()
    if mark is None:
        return None
    conn.execute("DELETE FROM review_marks WHERE id = ?", (mark_id,))
    return mark


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
    # 校时成功后该原字幕的原片区间已变，仅它的核看登记失效（同事务，失败一起回滚）
    cleared = conn.execute(
        "DELETE FROM review_marks WHERE material_id = ? AND subtitle_no = ?",
        (sub["material_id"], sub["subtitle_no"]),
    ).rowcount
    return {
        "material_id": sub["material_id"],
        "subtitle_no": sub["subtitle_no"],
        "start_ms": new_src_start,
        "end_ms": new_src_end,
        "cleared_marks": cleared,
    }
