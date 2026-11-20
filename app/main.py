"""FastAPI 入口：页面路由 + 上传 + 校时保存。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import csvio, db, logic

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.environ.get("SAMPLES_DIR", os.path.join(os.path.dirname(BASE_DIR), "samples"))


def fmt_ms(ms) -> str:
    if ms is None:
        return ""
    ms = int(ms)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    yield


app = FastAPI(title="成片字幕校时", lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.filters["fmt_ms"] = fmt_ms
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def redirect(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    if err:
        url += ("&" if "?" in url else "?") + "err=" + quote(err)
    return RedirectResponse(url, status_code=303)


# ---------- 页面 ----------

@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse("/rows", status_code=303)


@app.get("/rows", response_class=HTMLResponse)
def rows_page(request: Request, msg: str = "", err: str = ""):
    conn = db.connect()
    try:
        rows = logic.list_final_rows(conn)
        counts = {
            "subtitles": conn.execute("SELECT COUNT(*) c FROM source_subtitles").fetchone()["c"],
            "selections": conn.execute("SELECT COUNT(*) c FROM selections").fetchone()["c"],
        }
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "rows.html",
        {"rows": rows, "counts": counts, "msg": msg, "err": err},
    )


@app.get("/rows/{row_id}", response_class=HTMLResponse)
def edit_page(request: Request, row_id: int, err: str = ""):
    conn = db.connect()
    try:
        ctx = logic.get_edit_context(conn, row_id)
    finally:
        conn.close()
    if ctx is None:
        return redirect("/rows", err="这条成片行已不存在（可能被重算移除），请刷新列表")
    return templates.TemplateResponse(request, "edit.html", {"err": err, **ctx})


@app.post("/rows/{row_id}/save", response_class=HTMLResponse)
def save_row(
    request: Request,
    row_id: int,
    version: int = Form(...),
    final_start_ms: str = Form(""),
    final_end_ms: str = Form(""),
):
    def parse_opt(raw: str, label: str) -> Optional[int]:
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            v = int(raw)
        except ValueError:
            raise logic.ValidationError(f"{label}必须是整数毫秒")
        if v < 0:
            raise logic.ValidationError(f"{label}不能为负")
        return v

    try:
        new_start = parse_opt(final_start_ms, "成片起点")
        new_end = parse_opt(final_end_ms, "成片终点")
        with db.transaction() as conn:
            result = logic.adjust_row(conn, row_id, new_start, new_end, version)
    except logic.Conflict as e:
        # 原字幕读出后被改过：拒绝保存（事务已回滚），提示刷新
        conn = db.connect()
        try:
            ctx = logic.get_edit_context(conn, row_id)
        finally:
            conn.close()
        if ctx is None:
            return redirect("/rows", err=str(e))
        return templates.TemplateResponse(
            request, "edit.html", {"err": str(e), **ctx}, status_code=409
        )
    except logic.ValidationError as e:
        conn = db.connect()
        try:
            ctx = logic.get_edit_context(conn, row_id)
        finally:
            conn.close()
        if ctx is None:
            return redirect("/rows", err=str(e))
        return templates.TemplateResponse(
            request, "edit.html", {"err": str(e), **ctx}, status_code=400
        )

    return redirect(
        "/rows",
        msg=(
            f"已保存：原字幕 {result['material_id']} #{result['subtitle_no']} "
            f"改为 {result['start_ms']}–{result['end_ms']} ms，各段成片行已同步重算"
        ),
    )


# ---------- 上传 ----------

@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, msg: str = "", err: str = ""):
    conn = db.connect()
    try:
        counts = {
            "subtitles": conn.execute("SELECT COUNT(*) c FROM source_subtitles").fetchone()["c"],
            "selections": conn.execute("SELECT COUNT(*) c FROM selections").fetchone()["c"],
            "final_rows": conn.execute("SELECT COUNT(*) c FROM final_rows").fetchone()["c"],
        }
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "upload.html", {"counts": counts, "msg": msg, "err": err}
    )


def _read_upload(file: UploadFile) -> str:
    data = file.file.read()
    if len(data) > 10 * 1024 * 1024:
        raise csvio.CsvError("文件超过 10MB 上限")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise csvio.CsvError("文件不是有效的 UTF-8 编码") from None


@app.post("/upload/source", response_class=HTMLResponse)
def upload_source(file: UploadFile):
    try:
        rows = csvio.parse_source(_read_upload(file))
        with db.transaction() as conn:
            logic.replace_source(conn, rows)
            logic.rebuild_all(conn)
    except csvio.CsvError as e:
        return redirect("/upload", err=f"原片字幕 CSV 未导入：{e}")
    return redirect("/upload", msg=f"已导入原片字幕 {len(rows)} 条，成片行已全量重算")


@app.post("/upload/selections", response_class=HTMLResponse)
def upload_selections(file: UploadFile):
    try:
        rows = csvio.parse_selections(_read_upload(file))
        with db.transaction() as conn:
            logic.replace_selections(conn, rows)
            logic.rebuild_all(conn)
    except csvio.CsvError as e:
        return redirect("/upload", err=f"选段 CSV 未导入：{e}")
    return redirect("/upload", msg=f"已导入选段 {len(rows)} 段，成片行已全量重算")


@app.post("/load_sample", response_class=HTMLResponse)
def load_sample():
    try:
        with open(os.path.join(SAMPLES_DIR, "source_subtitles.csv"), encoding="utf-8-sig") as f:
            src = csvio.parse_source(f.read())
        with open(os.path.join(SAMPLES_DIR, "selections.csv"), encoding="utf-8-sig") as f:
            sel = csvio.parse_selections(f.read())
    except (OSError, csvio.CsvError) as e:
        return redirect("/upload", err=f"样例载入失败：{e}")
    with db.transaction() as conn:
        logic.replace_source(conn, src)
        logic.replace_selections(conn, sel)
        logic.rebuild_all(conn)
    return redirect("/rows", msg="已载入内置样例（含重复选段），可直接校时")


# ---------- 只读 API（调试/自测用） ----------

@app.get("/api/state")
def api_state():
    conn = db.connect()
    try:
        return logic.snapshot(conn)
    finally:
        conn.close()
