"""端到端自测：重复选段下的单行校时。

场景：素材 M1 在成片里被选了两次（第 1 段 [0,4000)、第 3 段 [2000,7000)），
同一条原字幕 M1#1 [1000,3000) 因此在成片里出现两行。
"""
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APP_DB", ":memory:")  # 兜底；fixture 里会换成临时文件

from app.main import app  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")


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


def sub(state, material, no):
    matches = [s for s in state["subtitles"]
               if s["material_id"] == material and s["subtitle_no"] == no]
    assert len(matches) == 1
    return matches[0]


def save(client, row_id, version, start=None, end=None):
    data = {"version": str(version),
            "final_start_ms": "" if start is None else str(start),
            "final_end_ms": "" if end is None else str(end)}
    return client.post(f"/rows/{row_id}/save", data=data, follow_redirects=False)


# ---------- 样例与全量重算 ----------

def test_sample_layout_with_duplicate_selection(loaded):
    s = state(loaded)
    assert [sel["offset_ms"] for sel in s["selections"]] == [0, 4000, 6500]
    rows = s["final_rows"]
    assert len(rows) == 6
    # 第 1 段 M1 [0,4000)：#1 完整可见，#2 尾部被裁
    r1 = find_row(rows, 1, "M1", 1)
    assert (r1["final_start_ms"], r1["final_end_ms"]) == (1000, 3000)
    assert (r1["start_clipped"], r1["end_clipped"]) == (0, 0)
    r2 = find_row(rows, 1, "M1", 2)
    assert (r2["final_start_ms"], r2["final_end_ms"]) == (3500, 4000)
    assert r2["end_clipped"] == 1
    # 第 2 段 M2
    r3 = find_row(rows, 2, "M2", 1)
    assert (r3["final_start_ms"], r3["final_end_ms"]) == (4000, 6000)
    # 第 3 段 M1 [2000,7000)（重复选段）：#1 头部被裁，#3 尾部被裁
    r4 = find_row(rows, 3, "M1", 1)
    assert (r4["final_start_ms"], r4["final_end_ms"]) == (6500, 7500)
    assert r4["start_clipped"] == 1
    r6 = find_row(rows, 3, "M1", 3)
    assert (r6["final_start_ms"], r6["final_end_ms"]) == (10500, 11500)
    assert r6["end_clipped"] == 1


# ---------- 校时：改一处，重复选段同步 ----------

def test_adjust_propagates_to_duplicate_selection(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    # 剪辑师：这句出得太早/太长，把第 1 段里这行终点从 3000 改到 2800
    r = save(loaded, row["id"], v, start=1000, end=2800)
    assert r.status_code == 303, r.text
    s2 = state(loaded)
    # 原字幕被换算回原片时刻
    assert (sub(s2, "M1", 1)["start_ms"], sub(s2, "M1", 1)["end_ms"]) == (1000, 2800)
    assert sub(s2, "M1", 1)["version"] == v + 1
    # 本段行更新
    r1 = find_row(s2["final_rows"], 1, "M1", 1)
    assert (r1["final_start_ms"], r1["final_end_ms"]) == (1000, 2800)
    # 重复选段（第 3 段）里的同一句也跟着更新：6500 + (2800-2000) = 7300
    r4 = find_row(s2["final_rows"], 3, "M1", 1)
    assert (r4["final_start_ms"], r4["final_end_ms"]) == (6500, 7300)
    # 其它字幕的行不受影响
    assert find_row(s2["final_rows"], 1, "M1", 2)["final_end_ms"] == 4000


def test_clipped_end_is_kept(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 3, "M1", 1)  # 起点被裁
    assert row["start_clipped"] == 1
    v = sub(s, "M1", 1)["version"]
    # 即使提交了新的起点 6300，也应被忽略；只应用终点 7600 -> 原片 3100
    r = save(loaded, row["id"], v, start=6300, end=7600)
    assert r.status_code == 303, r.text
    s2 = state(loaded)
    assert (sub(s2, "M1", 1)["start_ms"], sub(s2, "M1", 1)["end_ms"]) == (1000, 3100)
    r4 = find_row(s2["final_rows"], 3, "M1", 1)
    assert (r4["final_start_ms"], r4["final_end_ms"]) == (6500, 7600)
    r1 = find_row(s2["final_rows"], 1, "M1", 1)
    assert (r1["final_start_ms"], r1["final_end_ms"]) == (1000, 3100)


def test_clipped_end_only_submission_rejected(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 3, "M1", 1)  # 起点被裁、终点可调
    v = sub(s, "M1", 1)["version"]
    before = state(loaded)
    r = save(loaded, row["id"], v, start=6300, end=None)  # 起点被裁忽略，终点没填
    assert r.status_code == 400
    assert state(loaded) == before  # 保存失败，原字幕和成片行都保留上次结果


# ---------- 校验失败：事务回滚 ----------

def test_out_of_segment_rejected_and_nothing_changes(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    before = state(loaded)
    r = save(loaded, row["id"], v, start=1000, end=4500)  # 本段成片只到 4000
    assert r.status_code == 400
    assert "本段" in r.text
    assert state(loaded) == before


def test_start_after_end_rejected_and_nothing_changes(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    before = state(loaded)
    r = save(loaded, row["id"], v, start=1000, end=500)
    assert r.status_code == 400
    assert state(loaded) == before


# ---------- 行的出现与消失 ----------

def test_row_disappears_and_reappears_in_other_segment(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    # 终点改到 1500：字幕 [1000,1500) 与第 3 段 [2000,7000) 不再相交 -> 该行撤下
    assert save(loaded, row["id"], v, start=1000, end=1500).status_code == 303
    s2 = state(loaded)
    assert len(s2["final_rows"]) == 5
    assert not [r for r in s2["final_rows"] if r["selection_seq"] == 3 and r["subtitle_no"] == 1]
    # 再改回 2500：与第 3 段重新相交 -> 补上新出现的行 [6500, 7000)
    row = find_row(s2["final_rows"], 1, "M1", 1)
    v = sub(s2, "M1", 1)["version"]
    assert save(loaded, row["id"], v, start=1000, end=2500).status_code == 303
    s3 = state(loaded)
    assert len(s3["final_rows"]) == 6
    r4 = find_row(s3["final_rows"], 3, "M1", 1)
    assert (r4["final_start_ms"], r4["final_end_ms"]) == (6500, 7000)


# ---------- 乐观并发 ----------

def test_stale_version_rejected_with_refresh_hint(loaded):
    s = state(loaded)
    row = find_row(s["final_rows"], 1, "M1", 1)
    v = sub(s, "M1", 1)["version"]
    assert save(loaded, row["id"], v, start=1000, end=2800).status_code == 303
    before = state(loaded)
    # 同一行按读出的旧版本再保存一次 -> 拒绝并提示刷新
    r = save(loaded, row["id"], v, start=1000, end=2600)
    assert r.status_code == 409
    assert "刷新" in r.text
    assert state(loaded) == before


# ---------- 上传 ----------

def test_bad_csv_keeps_previous_data(loaded):
    before = state(loaded)
    r = loaded.post("/upload/source",
                    files={"file": ("bad.csv", b"material_id,subtitle_no,start_ms,end_ms,text\nM1,1,abc,2000,x\n")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "err" in r.headers["location"]
    assert state(loaded) == before


def test_upload_csv_replaces_and_rebuilds(loaded):
    src = b"material_id,subtitle_no,start_ms,end_ms,text\nA,1,0,1000,hello\n"
    sel = b"material_id,start_ms,end_ms\nA,0,500\nA,500,1500\n"
    assert loaded.post("/upload/source", files={"file": ("s.csv", src)},
                       follow_redirects=False).status_code == 303
    assert loaded.post("/upload/selections", files={"file": ("s.csv", sel)},
                       follow_redirects=False).status_code == 303
    s = state(loaded)
    # 字幕 [0,1000) 被两段 [0,500) [500,1500) 各裁一半：成片 [0,500) 与 [500,1000)
    assert [(r["final_start_ms"], r["final_end_ms"]) for r in s["final_rows"]] == [(0, 500), (500, 1000)]
    assert s["final_rows"][0]["end_clipped"] == 1
    assert s["final_rows"][1]["start_clipped"] == 1


def test_pages_render(loaded):
    assert loaded.get("/rows").status_code == 200
    row_id = state(loaded)["final_rows"][0]["id"]
    assert loaded.get(f"/rows/{row_id}").status_code == 200
    assert loaded.get("/upload").status_code == 200
