# -*- coding: utf-8 -*-
"""读取需求表（Excel / CSV），输出采集任务列表。"""
import csv
import re
from pathlib import Path

import core


def read_sources(path):
    """返回 [{no, title, url, site}]，no 从 1 开始，对应表里的顺序"""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"找不到需求表: {p}")
    rows = []
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        ws = openpyxl.load_workbook(p, data_only=True).worksheets[0]
        raw = [r for r in ws.iter_rows(min_row=1, values_only=True)]
    else:
        with p.open(encoding="utf-8-sig", newline="") as f:
            raw = [tuple(r) for r in csv.reader(f)]

    for r in raw:
        cells = [str(c).strip() if c is not None else "" for c in r]
        url = next((c for c in cells if re.search(r"https?://", c)), "")
        if not url:
            continue                       # 跳过表头和空行
        url = core.clean_url(url)
        title = next((c for c in cells if c and c != url and "http" not in c), "")
        rows.append({"no": len(rows) + 1, "title": title, "url": url,
                     "site": core.site_of(url)})
    return rows


def source_dirname(src):
    """每条来源一个子目录：0007_客家电视台-作客他乡-老挝"""
    name = src["title"] or src["url"].rsplit("/", 1)[-1]
    return f"{src['no']:04d}_{core.safe_name(name)}"
