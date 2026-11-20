"""端到端自测：核看登记（已核看按钮、缺口、撤销、沿用与失效）。

关键场景：
- 同一原字幕在重复选段里的多行共享登记（按原片区间合并计算覆盖）；
- 换段（重传选段）后登记按原片区间沿用，缺口重算；
- 撤销重叠登记后，仍被其他登记覆盖的部分算已核看；
- 校时成功仅该原字幕登记失效；重传原稿/载入样例清空登记；
- 页面所见行或原字幕已变时拒绝登记并提示刷新（409），状态不变。
"""
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APP_DB", ":memory:")  # 兜底；fixture 里会换成临时文件

from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def loaded(client):
    r = client.post("/load_sample", follow_redirects=False)
    assert r.status_code == 303
    return client


def state(client):
    return client.get("/api/state").json()


def find_row(rows, seq, material, no):
    matches = [r for r in rows if r["selection_seq"] == seq
               and r["material_id"] == material and r["subtitle_no"] == no]
    assert len(matches) == 1, f"expected 1 row for seg{seq} {material}#{no}, got {matches}"
    return matches[0]


def mark(client, row, version=None):
    """按页面表单原样提交「已核看」。"""
    return client.post(
        f"/rows/{row['id']}/reviewed",
        data={
            "version": str(row["version"] if version is None else version),
            "final_start_ms": str(row["final_start_ms"]),
            "final_end_ms": str(row["final_end_ms"]),
        },
        follow_redirects=False,
    )


def save(client, row_id, version, start=None, end=None):
    data = {"version": str(version),
            "final_start_ms": "" if start is None else str(start),
            "final_end_ms": "" if end is None else str(end)}
    return client.post(f"/rows/{row_id}/save", data=data, follow_redirects=False)


def upload(client, path, csv_text):
    return client.post(path, files={"file": ("x.csv", csv_text.encode())},
                       follow_redirects=False)


# ---------- 登记与跨段覆盖 ----------

def test_mark_covers_same_subtitle_in_duplicate_selection(loaded):
    s = state(loaded)
    row1 = find_row(s["rows"], 1, "M1", 1)  # 第 1 段可见原片 [1000,3000)
    row3 = find_row(s["rows"], 3, "M1", 1)  # 第 3 段可见原片 [2000,3000)
    assert row1["gaps"] == [[1000, 3000]] and not row1["all_reviewed"]
    assert row3["gaps"] == [[6500, 7500]]

    r = mark(loaded, row1)
    assert r.status_code == 303, r.text
    s2 = state(loaded)
    # 只登记了一笔：该行在原片露出的区间 [1000,3000)
    assert [(m["src_start_ms"], m["src_end_ms"]) for m in s2["review_marks"]] == [(1000, 3000)]
    assert all(m["material_id"] == "M1" and m["subtitle_no"] == 1 for m in s2["review_marks"])
    # 第 1 段行无缺口；第 3 段行可见原片 [2000,3000) 被同一登记覆盖，也无缺口
    assert find_row(s2["rows"], 1, "M1", 1)["all_reviewed"] is True
    assert find_row(s2["rows"], 3, "M1", 1)["all_reviewed"] is True
    # 其它字幕不受影响
    assert find_row(s2["rows"], 1, "M1", 2)["gaps"] == [[3500, 4000]]


def test_gaps_after_selection_change_and_marks_carried_over(client):
    # 字幕 [0,1000)，选段 [0,500) [500,1500)：同一字幕被切成两行
    assert upload(client, "/upload/source",
                  "material_id,subtitle_no,start_ms,end_ms,text\nA,1,0,1000,hello\n").status_code == 303
    assert upload(client, "/upload/selections",
                  "material_id,start_ms,end_ms\nA,0,500\nA,500,1500\n").status_code == 303
    s = state(client)
    row1 = find_row(s["rows"], 1, "A", 1)
    assert mark(client, row1).status_code == 303  # 登记原片 [0,500)
    s = state(client)
    assert find_row(s["rows"], 1, "A", 1)["all_reviewed"] is True
    # 第 2 段行可见原片 [500,1000) 未覆盖 -> 缺口即整行成片区间
    assert find_row(s["rows"], 2, "A", 1)["gaps"] == [[500, 1000]]

    # 换段：重传选段 [0,400) [400,1500)，登记按原片区间沿用（不清空）
    assert upload(client, "/upload/selections",
                  "material_id,start_ms,end_ms\nA,0,400\nA,400,1500\n").status_code == 303
    s = state(client)
    assert [(m["src_start_ms"], m["src_end_ms"]) for m in s["review_marks"]] == [(0, 500)]
    # 新第 1 段行可见 [0,400) 仍被 [0,500) 覆盖
    assert find_row(s["rows"], 1, "A", 1)["all_reviewed"] is True
    # 新第 2 段行可见原片 [400,1000)，缺口 [500,1000) 换算成片仍是 [500,1000)
    assert find_row(s["rows"], 2, "A", 1)["gaps"] == [[500, 1000]]


# ---------- 撤销与重叠覆盖 ----------

def test_undo_overlapping_marks_keeps_covered_part(loaded):
    s = state(loaded)
    # 同一原字幕两笔重叠登记：[1000,3000)（第 1 段行）与 [2000,3000)（第 3 段行）
    assert mark(loaded, find_row(s["rows"], 1, "M1", 1)).status_code == 303
    assert mark(loaded, find_row(s["rows"], 3, "M1", 1)).status_code == 303
    s = state(loaded)
    assert len(s["review_marks"]) == 2
    assert find_row(s["rows"], 1, "M1", 1)["all_reviewed"] is True

    # 撤销第一笔 [1000,3000)：第 1 段行露出缺口 [1000,2000)，
    # [2000,3000) 仍被第二笔覆盖 -> 第 3 段行依然全部已核看
    first = min(s["review_marks"], key=lambda m: m["src_start_ms"])
    r = loaded.post(f"/reviews/{first['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    s = state(loaded)
    assert len(s["review_marks"]) == 1
    assert find_row(s["rows"], 1, "M1", 1)["gaps"] == [[1000, 2000]]
    assert find_row(s["rows"], 3, "M1", 1)["all_reviewed"] is True

    # 再撤销第二笔：缺口恢复为整行
    last = s["review_marks"][0]
    assert loaded.post(f"/reviews/{last['id']}/delete", follow_redirects=False).status_code == 303
    s = state(loaded)
    assert s["review_marks"] == []
    assert find_row(s["rows"], 1, "M1", 1)["gaps"] == [[1000, 3000]]
    assert find_row(s["rows"], 3, "M1", 1)["gaps"] == [[6500, 7500]]


def test_duplicate_marks_undo_one_still_covered(loaded):
    s = state(loaded)
    row = find_row(s["rows"], 1, "M1", 1)
    assert mark(loaded, row).status_code == 303
    assert mark(loaded, row).status_code == 303  # 同一区间重复登记
    s = state(loaded)
    assert len(s["review_marks"]) == 2
    # 撤销其中一笔，另一笔仍覆盖 -> 仍算已核看
    assert loaded.post(f"/reviews/{s['review_marks'][0]['id']}/delete",
                       follow_redirects=False).status_code == 303
    s = state(loaded)
    assert len(s["review_marks"]) == 1
    assert find_row(s["rows"], 1, "M1", 1)["all_reviewed"] is True


def test_undo_missing_mark_shows_error(loaded):
    r = loaded.post("/reviews/999/delete", follow_redirects=False)
    assert r.status_code == 303
    assert "err" in r.headers["location"]


# ---------- 失效与清空 ----------

def test_adjust_invalidates_only_that_subtitle(loaded):
    s = state(loaded)
    assert mark(loaded, find_row(s["rows"], 1, "M1", 1)).status_code == 303
    assert mark(loaded, find_row(s["rows"], 1, "M1", 2)).status_code == 303
    assert len(state(loaded)["review_marks"]) == 2

    # 校时 M1#1：终点 3000 -> 2800，成功后仅 M1#1 的登记失效
    s = state(loaded)
    row = find_row(s["rows"], 1, "M1", 1)
    assert save(loaded, row["id"], row["version"], start=1000, end=2800).status_code == 303
    s = state(loaded)
    assert [(m["material_id"], m["subtitle_no"]) for m in s["review_marks"]] == [("M1", 2)]
    # M1#1 各行缺口按新区间恢复为整行；M1#2 仍全部已核看
    assert find_row(s["rows"], 1, "M1", 1)["gaps"] == [[1000, 2800]]
    assert find_row(s["rows"], 1, "M1", 2)["all_reviewed"] is True


def test_source_upload_and_sample_load_clear_marks(loaded):
    assert mark(loaded, find_row(state(loaded)["rows"], 1, "M1", 1)).status_code == 303
    assert len(state(loaded)["review_marks"]) == 1
    # 重传原稿（哪怕内容相同）清空登记
    src = ("material_id,subtitle_no,start_ms,end_ms,text\n"
           "M1,1,1000,3000,第一句台词：我们得走了\n")
    assert upload(loaded, "/upload/source", src).status_code == 303
    assert state(loaded)["review_marks"] == []
    # 再登记，载入样例也清空
    assert mark(loaded, find_row(state(loaded)["rows"], 1, "M1", 1)).status_code == 303
    assert loaded.post("/load_sample", follow_redirects=False).status_code == 303
    assert state(loaded)["review_marks"] == []


# ---------- 页面状态已变：拒绝并提示刷新 ----------

def test_stale_row_rejected_with_refresh_hint(loaded):
    s = state(loaded)
    row = find_row(s["rows"], 1, "M1", 1)
    # 校时改变了行区间与字幕版本，页面那一份已过期
    assert save(loaded, row["id"], row["version"], start=1000, end=2800).status_code == 303
    before = state(loaded)
    r = mark(loaded, row)  # 旧 version + 旧区间
    assert r.status_code == 409
    assert "刷新" in r.text
    assert state(loaded) == before  # 写入失败保留原状


def test_stale_version_rejected_even_if_row_looks_same(loaded):
    s = state(loaded)
    row = find_row(s["rows"], 1, "M1", 1)
    v = row["version"]
    # 校时两次又回到原区间：行看起来一样，但版本已变
    assert save(loaded, row["id"], v, start=1000, end=2800).status_code == 303
    assert save(loaded, row["id"], v + 1, start=1000, end=3000).status_code == 303
    before = state(loaded)
    r = mark(loaded, find_row(before["rows"], 1, "M1", 1), version=v)  # 最初的旧版本
    assert r.status_code == 409
    assert "刷新" in r.text
    assert state(loaded) == before


def test_row_gone_after_selections_reupload_rejected(loaded):
    s = state(loaded)
    row = find_row(s["rows"], 1, "M1", 1)
    # 重传选段后成片行全部重算，旧行 id 失效
    assert upload(loaded, "/upload/selections",
                  "material_id,start_ms,end_ms\nM1,0,4000\n").status_code == 303
    before = state(loaded)
    r = mark(loaded, row)
    assert r.status_code == 409
    assert "刷新" in r.text
    assert state(loaded) == before


# ---------- 页面 ----------

def test_review_pages_render(loaded):
    assert mark(loaded, find_row(state(loaded)["rows"], 1, "M1", 1)).status_code == 303
    r = loaded.get("/rows")
    assert r.status_code == 200
    assert "未核看区间" in r.text and "已全部核看" in r.text
    r = loaded.get("/reviews/M1/1")
    assert r.status_code == 200
    assert "1000" in r.text and "3000" in r.text and "撤销" in r.text
    # 撤销后明细页同步
    mark_id = state(loaded)["review_marks"][0]["id"]
    assert loaded.post(f"/reviews/{mark_id}/delete", follow_redirects=False).status_code == 303
    r = loaded.get("/reviews/M1/1")
    assert r.status_code == 200 and "暂无核看登记" in r.text
    # 字幕不存在 -> 回列表并提示
    r = loaded.get("/reviews/M1/99", follow_redirects=False)
    assert r.status_code == 303 and "err" in r.headers["location"]
