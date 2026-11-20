"""CSV 解析：原片字幕 CSV 与选段 CSV。

原片字幕 CSV 表头（需含表头行，支持中/英文列名）：
    material_id, subtitle_no, start_ms, end_ms, text
    素材号,      字幕号,      开始毫秒, 结束毫秒, 正文
选段 CSV 表头（按成片顺序逐行列出，允许同一素材重复出现）：
    material_id, start_ms, end_ms
    素材号,      开始毫秒, 结束毫秒
"""
from __future__ import annotations

import csv
import io

ALIASES_SOURCE = {
    "material_id": {"material_id", "material", "素材号", "素材"},
    "subtitle_no": {"subtitle_no", "subtitle", "no", "字幕号", "字幕编号"},
    "start_ms": {"start_ms", "start", "开始毫秒", "起点毫秒", "起"},
    "end_ms": {"end_ms", "end", "结束毫秒", "终点毫秒", "止"},
    "text": {"text", "正文", "内容"},
}
ALIASES_SELECTION = {
    "material_id": ALIASES_SOURCE["material_id"],
    "start_ms": ALIASES_SOURCE["start_ms"],
    "end_ms": ALIASES_SOURCE["end_ms"],
}


class CsvError(Exception):
    pass


def _column_indexes(header: list[str], aliases: dict, kind: str) -> dict:
    normalized = [h.strip().lower() for h in header]
    indexes = {}
    for canon, names in aliases.items():
        for pos, cell in enumerate(normalized):
            if cell in names:
                indexes[canon] = pos
                break
    missing = [c for c in aliases if c not in indexes]
    if missing:
        raise CsvError(
            f"{kind} CSV 缺少必需列: {', '.join(missing)}；"
            f"读到的表头为: {', '.join(header) or '(空)'}"
        )
    return indexes


def _parse(text: str, aliases: dict, kind: str, with_text: bool) -> list[dict]:
    try:
        raw = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    except csv.Error as e:
        raise CsvError(f"{kind} CSV 解析失败: {e}") from e
    if not raw:
        raise CsvError(f"{kind} CSV 为空")
    idx = _column_indexes(raw[0], aliases, kind)

    out, seen = [], set()
    for lineno, r in enumerate(raw[1:], start=2):
        try:
            material = r[idx["material_id"]].strip()
            start_ms = int(r[idx["start_ms"]].strip())
            end_ms = int(r[idx["end_ms"]].strip())
        except (IndexError, ValueError):
            raise CsvError(f"第 {lineno} 行格式错误（应为整数毫秒）: {','.join(r)}") from None
        if not material:
            raise CsvError(f"第 {lineno} 行素材号为空")
        if start_ms < 0 or end_ms < 0:
            raise CsvError(f"第 {lineno} 行时间不能为负")
        if start_ms >= end_ms:
            raise CsvError(f"第 {lineno} 行起点必须小于终点（{start_ms} >= {end_ms}）")
        if with_text:
            try:
                subtitle_no = int(r[idx["subtitle_no"]].strip())
                content = r[idx["text"]]
            except (IndexError, ValueError):
                raise CsvError(f"第 {lineno} 行字幕号/正文格式错误: {','.join(r)}") from None
            key = (material, subtitle_no)
            if key in seen:
                raise CsvError(f"第 {lineno} 行与前面重复: 素材 {material} 字幕号 {subtitle_no}")
            seen.add(key)
            out.append({
                "material_id": material, "subtitle_no": subtitle_no,
                "start_ms": start_ms, "end_ms": end_ms, "text": content,
            })
        else:
            out.append({"material_id": material, "start_ms": start_ms, "end_ms": end_ms})
    if not out:
        raise CsvError(f"{kind} CSV 没有数据行")
    return out


def parse_source(text: str) -> list[dict]:
    return _parse(text, ALIASES_SOURCE, "原片字幕", with_text=True)


def parse_selections(text: str) -> list[dict]:
    return _parse(text, ALIASES_SELECTION, "选段", with_text=False)
