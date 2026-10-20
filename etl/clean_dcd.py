# -*- coding: utf-8 -*-
"""
M1 数据清洗管道 — clean_dcd.py
===============================
对 14.4 万条原始二手车挂牌数据做「一键清洗 + 质量报告」，并导出 parquet/csv。

设计依据（docs/02-数据集说明与数据质量.md）：
  1) 原始文件表头 20 列，但 csv.reader 实测每行 100% 为 21 字段 =
     表头前 16 列 + 一个表头未声明的 0/1 冗余列(索引16) + 档案详情/相关参数/用车成本/公司名称；
  2) 正确修复 = 剔除冗余 0/1 列并对齐 20 列（注意：不是"截断前 20 列"，那会丢掉公司名称）；
  3) 「品牌列出现里程/日期」是 pandas 直接读原文件时的解析伪影，
     —— 本管道严禁直接 read_csv 原始文件，必须先过 csv.reader 文本层修复。

阶段（--stage，运行到指定阶段为止，前序阶段自动执行）：
  scan        csv.reader 逐行统计字段数分布 + 唯一 id（验收线 1）
  repair      剔除 0/1 冗余列 → 20 列对齐表头 → 写 data/tmp/dcd_repaired.csv（验收线 2）
  unpack      ast.literal_eval 展开 3 个嵌套 JSON 字段（暂无→空，验收线 3）
  standardize 价格→元/里程→公里/年份→int/车龄"X年Y个月"→数值年限 + 异常统计（验收线 4）
  report      输出 reports/field_metrics.csv + clean_report.md（论文实验一素材，验收线 5）
  export      导出 data/clean/*.csv|.parquet 并核对行数/唯一 id/抽样（验收线 6）

CSV 及 Parquet 消费方应通过同目录的 dcd_clean.manifest.json 核对批次 ID 和文件 SHA-256；
本项目服务端只读 Parquet，并在 manifest 存在时拒绝尚未完成发布的批次。

用法：
  python etl/clean_dcd.py --stage all        # 全链
  python etl/clean_dcd.py --stage scan       # 只跑前序+scan
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import hashlib
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 原始表头 20 列（与 dcd.csv 首行一致，勿改）
HEADER_20 = [
    "id", "车辆照片链接", "汽车发动机名称", "汽车名称", "品牌名称", "里程数",
    "汽车第一次注时间", "汽车年龄", "价格", "标题", "副标题", "详情链接",
    "汽车生产年份", "车源城市名称", "固定价格", "原产地价格", "档案详情",
    "相关参数", "用车成本", "公司名称",
]

# 品牌列不允许出现的模式（错位/污染特征，同 locate_shift.py）
BRAND_BAD_RE = re.compile(r"万公里|公里$|^[\d.]+万$|^\d{4}-\d{2}-\d{2}")
NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")

# --------------------------------------------------------------------------
# 基础解析函数（纯函数，可单测）
# --------------------------------------------------------------------------

def build_missing_pred(patterns: list[str]):
    """构造缺失判定函数：空白 或 值整体命中 '暂无'/'--' 等模式前缀。"""
    lows = tuple(p.lower() for p in patterns if p)
    def _is_missing(s) -> bool:
        if s is None:
            return True
        t = str(s).strip()
        if not t:
            return True
        low = t.lower()
        return low.startswith(lows)
    return _is_missing


def parse_first_number(s) -> float | None:
    """取字符串里第一个数字（兜底解析）。"""
    if s is None:
        return None
    m = NUM_RE.search(str(s).replace(",", "").replace("，", ""))
    return float(m.group()) if m else None


def _is_missing_or_signed_number(s, is_missing) -> bool:
    """A leading minus is a numeric sign, not the standalone missing marker '-'."""
    if s is not None and re.match(r"^\s*-\s*\d", str(s)):
        return False
    return is_missing(s)


def parse_price_yuan(s, is_missing) -> float | None:
    """价格→元。'55.80万'→558000.0；含'元'/纯数字兜底；缺失→None。"""
    if _is_missing_or_signed_number(s, is_missing):
        return None
    s = str(s).strip().replace(",", "").replace("，", "")
    if "万" in s:
        v = parse_first_number(s)
        return v * 10000 if v is not None else None
    v = parse_first_number(s)
    return v  # 其余按"元"处理


def parse_mileage_km(s, is_missing) -> float | None:
    """里程→公里。'4.20万公里'→42000.0；'8.6万'→86000；'40000公里'→40000。"""
    if _is_missing_or_signed_number(s, is_missing):
        return None
    s = str(s).strip().replace(",", "").replace("，", "")
    v = parse_first_number(s)
    if v is None:
        return None
    return v * 10000 if "万" in s else v


def parse_year_int(s, is_missing) -> int | None:
    """年份→int。'2022'→2022。"""
    if is_missing(s):
        return None
    m = re.search(r"(19|20)\d{2}", str(s))
    return int(m.group()) if m else None


def parse_age_years(s, is_missing) -> float | None:
    """车龄→数值年限。'2年9个月'→2.75；'1年'→1.0；'6个月'→0.5；缺失→None。"""
    if _is_missing_or_signed_number(s, is_missing):
        return None
    s = str(s).strip()
    m_y = re.search(r"([-+]?\d+(?:\.\d+)?)\s*\u5e74", s)
    m_m = re.search(r"([-+]?\d+(?:\.\d+)?)\s*\u4e2a\u6708", s)
    if not m_y and not m_m:
        return None
    y = float(m_y.group(1)) if m_y else 0.0
    mo = float(m_m.group(1)) if m_m else 0.0
    return round(y + mo / 12.0, 4)


def parse_axis_to_mm(s, is_missing) -> int | None:
    """轴距'2800mm'→2800；'2.8m'→2800。"""
    if is_missing(s):
        return None
    v = parse_first_number(s)
    if v is None:
        return None
    return int(round(v * 1000)) if "m" in str(s).lower() and "mm" not in str(s).lower() else int(round(v))


def parse_power_kw(s, is_missing) -> float | None:
    """最大功率→kW。'135kW'→135；'184马力/184Ps'→184*0.7355≈135.3；其余取首个数字当 kW。"""
    if is_missing(s):
        return None
    v = parse_first_number(s)
    if v is None:
        return None
    low = str(s).lower()
    if "马力" in low or "ps" in low or "匹" in low:
        return round(v * 0.7355, 2)
    return v


def parse_float_any(s, is_missing) -> float | None:
    """通用数值：'6.9s'→6.9；'7.1L'→7.1；'5328元'→5328。"""
    if is_missing(s):
        return None
    return parse_first_number(s)


def parse_json_to_dict(s, is_missing):
    """安全解析 JSON 列表字符串 → {name: value}。
    返回 (dict|None, status)：status ∈ ok / missing / parse_error。
    '暂无***'/空白 → (None, 'missing')；解析失败 → (None, 'parse_error')。"""
    if is_missing(s):
        return None, "missing"
    s = str(s).strip()
    try:
        obj = ast.literal_eval(s)
    except Exception:
        return None, "parse_error"
    if not isinstance(obj, list):
        return None, "parse_error"
    out: dict[str, str] = {}
    for item in obj:
        if isinstance(item, dict) and "name" in item and item["name"] is not None:
            out[str(item["name"]).strip()] = item.get("value")
    return out, "ok"


def to_na_if_missing(v, is_missing):
    """把缺失/暂无的值统一转 NaN（用于展开后的子键值）。"""
    if v is None or is_missing(v):
        return np.nan
    t = str(v).strip()
    return t if t else np.nan


def repair_data_row(row, extra_pos: int, allowed: set[str], keep_positions: list[int]):
    """把 csv.reader 读出的一行原始字段修复成标准 20 列记录。
    返回 (aligned20|None, status, msg)。"""
    if len(row) == 20:
        aligned = list(row)
    elif len(row) == 21 and row[extra_pos].strip() in allowed:
        aligned = row[:extra_pos] + row[extra_pos + 1:]
    else:
        return None, "unusual", f"len={len(row)} extra={row[extra_pos] if len(row) > extra_pos else 'N/A'}"
    return [aligned[i] for i in keep_positions], "ok", ""


# --------------------------------------------------------------------------
# 阶段实现
# --------------------------------------------------------------------------

class Pipeline:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        root = PROJECT_ROOT
        p = cfg["paths"]
        self.raw_path = root / p["raw_csv"]
        self.repaired_path = root / p["repaired_csv"]
        self.clean_csv_path = root / p["clean_csv"]
        self.clean_pq_path = root / p["clean_parquet"]
        self.metrics_csv_path = root / p["field_metrics_csv"]
        self.report_md_path = root / p["clean_report_md"]

        drop_cols = cfg["layout"]["drop_columns_from_header"]
        self.keep_header = [c for c in HEADER_20 if c not in drop_cols]
        # 对齐后 20 列里要保留的位置（去掉被剔除的"固定价格"列）
        self.keep_positions = [i for i, c in enumerate(HEADER_20) if c not in drop_cols]
        self.extra_pos = cfg["layout"]["row_extra_col_idx"]
        self.extra_allowed = set(cfg["layout"]["extra_col_allowed"])

        sc = cfg["standardize"]
        self.current_year = sc.get("current_year") or pd.Timestamp.now().year
        self.year_max = self.current_year + 1
        a = sc["anomaly"]
        self.price_min, self.price_max = a["price_min_yuan"], a["price_max_yuan"]
        self.mileage_max = a["mileage_max_km"]
        self.year_min = a["year_min"]
        self.is_missing = build_missing_pred(sc["text_missing_patterns"])

        self.json_cfg = cfg["json_fields"]
        self.sample_n = cfg["report"]["sample_rows_for_compare"]
        self.stats: dict = {}
        self.df: pd.DataFrame | None = None

    # ---------- stage: scan ----------
    def stage_scan(self) -> dict:
        counts = Counter()
        header_len = 0
        empty_id = 0
        ids: set[str] = set()
        unusual_sample = []
        with open(self.raw_path, "r", encoding="utf-8", newline="") as f:
            rd = csv.reader(f)
            header = next(rd)
            header_len = len(header)
            for i, row in enumerate(rd):
                counts[len(row)] += 1
                if row and row[0].strip():
                    ids.add(row[0].strip())
                else:
                    empty_id += 1
                if len(row) != 21 and len(unusual_sample) < 5:
                    unusual_sample.append((i + 2, len(row)))
        st = {
            "total_rows": sum(counts.values()),
            "field_dist": dict(sorted(counts.items(), reverse=True)),
            "header_len": header_len,
            "uniq_id": len(ids),
            "empty_id": empty_id,
            "unusual_sample": unusual_sample,
        }
        if header_len != self.cfg["layout"]["total_header_cols"]:
            raise ValueError(f"表头字段数不是 {self.cfg['layout']['total_header_cols']}，中止！")
        if st["total_rows"] != 143793:
            raise ValueError(f"总行数不是 143793：{st['total_rows']}")
        if set(counts) != {21}:
            raise ValueError(f"字段数分布不是 100% 21 字段：{counts}")
        self.stats["scan"] = st
        print("[scan] 表头字段数 =", header_len)
        print("[scan] 总数据行 =", st["total_rows"], "| 字段数分布 =", st["field_dist"])
        print("[scan] 唯一 id =", st["uniq_id"], "| 空 id 行 =", st["empty_id"])
        return st

    # ---------- stage: repair ----------
    def stage_repair(self) -> dict:
        if "scan" not in self.stats:          # 避免 scan 被重复执行（setdefault 会急切求值）
            self.stage_scan()
        extra_cnt = Counter()
        semantic_bad = 0
        semantic_sample = []
        unusual_cnt = 0
        unusual_sample = []
        before_samples, after_samples = [], []
        self.repaired_path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with open(self.raw_path, "r", encoding="utf-8", newline="") as f, \
                open(self.repaired_path, "w", encoding="utf-8", newline="") as g:
            rd = csv.reader(f)
            _ = next(rd)
            w = csv.writer(g)
            w.writerow(self.keep_header)
            for i, row in enumerate(rd):
                n += 1
                if len(row) > self.extra_pos:
                    extra_cnt[row[self.extra_pos].strip()] += 1
                aligned, status, msg = repair_data_row(
                    row, self.extra_pos, self.extra_allowed, self.keep_positions)
                if status == "unusual":
                    unusual_cnt += 1
                    if len(unusual_sample) < 5:
                        unusual_sample.append((i + 2, msg))
                    continue
                # 行级语义校验：品牌列不得出现里程/日期；公司列应像商家
                brand = aligned[4]
                if BRAND_BAD_RE.search(brand):
                    semantic_bad += 1
                    if len(semantic_sample) < 5:
                        semantic_sample.append((i + 2, brand))
                if len(before_samples) < self.sample_n:
                    before_samples.append((i + 2, row))
                    after_samples.append((i + 2, aligned))
                w.writerow(aligned)
        st = {
            "repaired_rows": n,
            "unusual_rows": unusual_cnt,
            "unusual_sample": unusual_sample,
            "semantic_bad_rows": semantic_bad,
            "semantic_sample": semantic_sample,
            "extra_col_dist": dict(extra_cnt),
            "before_sample": before_samples,
            "after_sample": after_samples,
        }
        self.stats["repair"] = st
        print(f"[repair] 处理 {n} 行 → 写出 {self.repaired_path.name}")
        print(f"[repair] 0/1 冗余列分布 = {st['extra_col_dist']}")
        print(f"[repair] 非常规行 = {unusual_cnt} | 品牌语义异常行 = {semantic_bad}")
        return st

    # ---------- stage: unpack ----------
    def stage_unpack(self) -> pd.DataFrame:
        if "repair" not in self.stats:
            self.stage_repair()
        # 只允许读修复后的中间件（严禁直接读原始 CSV，防 pandas 解析伪影）
        df = pd.read_csv(self.repaired_path, dtype=str, keep_default_na=False,
                         encoding="utf-8")
        if list(df.columns) != self.keep_header:
            raise ValueError("修复中间件列与预期不符！")
        if len(df) != self.stats["scan"]["total_rows"]:
            raise ValueError("修复后行数与 scan 不一致！")
        im = self.is_missing

        json_stats: dict[str, Counter] = {}
        subkey_stats: dict[str, Counter] = {}
        for field, spec in self.json_cfg.items():
            col = f"{field}_raw"
            status_cnt = Counter()
            sub_cnt: Counter = Counter()
            rows = []
            extras = []
            for raw in df[field].astype(str):
                d, status = parse_json_to_dict(raw, self.is_missing)
                status_cnt[status] += 1
                if d is None:
                    rows.append({})
                    extras.append(None)
                    continue
                keep = {}
                extra = {}
                for k, v in d.items():
                    if k in spec["keys"]:
                        keep[k] = to_na_if_missing(v, self.is_missing)
                    else:
                        extra[k] = v
                    sub_cnt[k] += 1
                rows.append(keep)
                extras.append(extra if extra else None)
            # 宽表展开（按配置的 type 做真正的数值解析）
            keys = spec["keys"]
            sub = pd.DataFrame(rows)
            for k, meta in keys.items():
                name, typ = meta["name"], meta["type"]
                if k in sub:
                    raw_series = sub[k]
                    if typ == "int":
                        if k == "轴距":  # '2800mm'/'2.8m'
                            series = raw_series.map(lambda v: parse_axis_to_mm(v, im) if not pd.isna(v) else np.nan)
                        else:
                            series = raw_series.map(lambda v: parse_first_number(v) if not pd.isna(v) else np.nan)
                    elif typ == "float":
                        if k == "最大功率":  # '135kW'/'184马力'
                            series = raw_series.map(lambda v: parse_power_kw(v, im) if not pd.isna(v) else np.nan)
                        else:  # '6.9s'/'7.1'/'5328元'
                            series = raw_series.map(lambda v: parse_float_any(v, im) if not pd.isna(v) else np.nan)
                    else:
                        series = raw_series
                else:
                    series = pd.Series(np.nan, index=df.index)
                df[name] = series.values
            # 原文留存列 + 未入 schema 的键
            df[col] = df[field].astype(str).where(
                ~df[field].astype(str).map(self.is_missing), "")
            df["_extra_" + field] = [json.dumps(e, ensure_ascii=False) if e else "" for e in extras]
            json_stats[field] = status_cnt
            subkey_stats[field] = sub_cnt
        self.stats["json"] = {
            "parse_status": json_stats,
            "subkey_hits": subkey_stats,
        }
        print("[unpack] JSON 解析状态：")
        for f, c in json_stats.items():
            print(f"         {f}: {dict(c)}")
        self.stats["df"] = df                      # unpack 产物缓存，避免 standardize 重跑
        return df

    # ---------- stage: standardize ----------
    def stage_standardize(self) -> pd.DataFrame:
        df = self.stats.get("df")
        if df is None:
            df = self.stage_unpack()
        self.stats["df"] = df
        im = self.is_missing

        def _clean(col):
            return df[col].map(lambda v: np.nan if im(v) else str(v).strip())

        # 原始文本列去空白 + 暂无→NaN（标识/链接类字段仅去空白，不做缺失归一化，
        # 避免 id 恰为 'nan'/'None' 之类文本时被误判为空）
        text_keep_raw = {"id", "车辆照片链接", "详情链接"}
        for c in self.keep_header:
            if c in text_keep_raw:
                df[c] = df[c].map(lambda v: str(v).strip())
            else:
                df[c] = _clean(c)
        for f in self.json_cfg:
            df[f"{f}_raw"] = df[f"{f}_raw"].map(lambda v: str(v).strip())

        # ---- 标准化派生列（一律 to_numeric coerce，避免 object/None 比较问题） ----
        df["brand_name"] = df["品牌名称"]
        df["car_name"] = df["汽车名称"]
        df["city_name"] = df["车源城市名称"]

        price = pd.to_numeric(df["价格"].map(lambda s: parse_price_yuan(s, im)), errors="coerce")
        df["price_yuan"] = price
        df["orig_price_yuan"] = pd.to_numeric(
            df["原产地价格"].map(lambda s: parse_price_yuan(s, im)), errors="coerce")
        df["mileage_km"] = pd.to_numeric(
            df["里程数"].map(lambda s: parse_mileage_km(s, im)), errors="coerce")
        df["production_year"] = pd.to_numeric(
            df["汽车生产年份"].map(lambda s: parse_year_int(s, im)), errors="coerce")

        reg = pd.to_datetime(df["汽车第一次注时间"], errors="coerce")
        reg_str = reg.dt.strftime("%Y-%m-%d").where(reg.notna())
        df["registration_date"] = reg_str
        df["registration_year"] = reg.dt.year
        df["vehicle_age_years"] = pd.to_numeric(
            df["汽车年龄"].map(lambda s: parse_age_years(s, im)), errors="coerce")

        # JSON 展开列的数值化已在 unpack 完成；校验存档字段文本清洗
        # ---- 异常标记（只统计不删行） ----
        py = df["production_year"]
        age = df["vehicle_age_years"]
        p_bad = price.isna() | (price <= 0) | (price >= self.price_max)
        p_parse_fail = df["价格"].notna() & price.isna()
        m_parse_fail = df["里程数"].notna() & df["mileage_km"].isna()
        m_bad = df["mileage_km"].isna() | (df["mileage_km"] <= 0) | (df["mileage_km"] >= self.mileage_max)
        y_bad = py.isna() | (py < self.year_min) | (py > self.year_max)
        json_fail = pd.Series(False, index=df.index)
        for f in self.json_cfg:
            s = df[f]
            json_fail |= s.notna() & ~s.map(lambda v: self._json_ok(v, f))
        reg_early = reg.dt.year.notna() & py.notna() & (reg.dt.year < py)  # 注时早于生产年份

        # ---- 行级质量分（0~100） ----
        core_missing = pd.concat([
            df["品牌名称"].isna(), df["价格"].isna(), df["里程数"].isna(),
            df["汽车生产年份"].isna(), df["车源城市名称"].isna(),
        ], axis=1).sum(axis=1)
        extra_penalty = (p_bad.astype(int) + m_bad.astype(int) + y_bad.astype(int)
                         + df["vehicle_age_years"].isna().astype(int))
        df["data_quality_score"] = np.clip(100 - 10 * (core_missing + extra_penalty), 0, 100)

        self.stats["anomaly"] = {
            "price_parse_fail": int(p_parse_fail.sum()),
            "mileage_parse_fail": int(m_parse_fail.sum()),
            "price_bad": int(p_bad.sum()),
            "mileage_bad": int(m_bad.sum()),
            "year_bad": int(y_bad.sum()),
            "reg_early_than_prod": int(reg_early.sum()),
            "json_parse_fail_rows": int(json_fail.sum()),
            "score_dist": df["data_quality_score"].value_counts().sort_index(ascending=False).head(8).to_dict(),
        }
        # 车龄交叉核验：推断挂牌基准日 = 上牌年份 + 文本车龄（供报告讨论，不用于修正）
        implied = df["registration_year"] + df["vehicle_age_years"]
        ii = implied.dropna()
        if len(ii):
            med = float(ii.median())
            iqr_lo, iqr_hi = float(ii.quantile(0.25)), float(ii.quantile(0.75))
            self.stats["age_crosscheck"] = {
                "pairs": int(len(ii)),
                "median_implied_listing_year": round(med, 2),
                "iqr": [round(iqr_lo, 2), round(iqr_hi, 2)],
                "dev_gt_2y": int(((ii - med).abs() > 2).sum()),
            }
        print("[standardize] 异常统计：", self.stats["anomaly"])
        return df

    def _json_ok(self, raw, field) -> bool:
        _, status = parse_json_to_dict(raw, self.is_missing)
        return status == "ok"

    # ---------- stage: report ----------
    def stage_report(self):
        df = self.stats.get("df")
        if df is None:
            df = self.stage_standardize()
        self.stats["df"] = df
        im = self.is_missing

        # 字段可用性指标表
        rows = []
        for c in df.columns:
            if c.startswith("_extra_"):
                continue
            s = df[c]
            nonnull = int(s.notna().sum())
            miss = int(s.isna().sum())
            uniq = int(s.nunique(dropna=True))
            first_val = ""
            for v in s.dropna():
                if str(v).strip():
                    first_val = str(v)[:40]
                    break
            group = "原始" if c in self.keep_header else ("派生" if c.startswith("_extra_") else "派生")
            if c.endswith("_raw"):
                group = "原始(JSON原文)"
            rows.append({
                "字段": c, "分组": group, "非空数": nonnull, "缺失数": miss,
                "缺失率": round(miss / len(df), 4), "可用率": round(nonnull / len(df), 4),
                "唯一数": uniq, "首个非空样例": first_val,
            })
        metrics = pd.DataFrame(rows).sort_values(["分组", "可用率"], ascending=[True, False])
        self.metrics_df = metrics

        # 输出 CSV
        self.metrics_csv_path.parent.mkdir(parents=True, exist_ok=True)
        metrics.to_csv(self.metrics_csv_path, index=False, encoding="utf-8-sig")

        # JSON 子键覆盖表
        sub_rows = []
        for f, cnt in self.stats["json"]["subkey_hits"].items():
            for k, v in cnt.items():
                sub_rows.append({"字段": f, "子键": k, "出现行数": v,
                                 "覆盖率": round(v / len(df), 4)})
        sub_df = pd.DataFrame(sub_rows).sort_values(["字段", "覆盖率"], ascending=[True, False])

        # 生成 MD 报告
        md = self._build_markdown(metrics, sub_df)
        self.report_md_path.write_text(md, encoding="utf-8")
        print(f"[report] 已写出 {self.metrics_csv_path.name} / {self.report_md_path.name}")

    def _build_markdown(self, metrics, sub_df) -> str:
        st = self.stats
        scan, rep = st["scan"], st["repair"]
        anom = st["anomaly"]
        jst = st["json"]["parse_status"]
        df = self.stats["df"]
        L = len(df)
        cur = self.current_year
        dup_id = int(df["id"].duplicated().sum())
        dup_samples = df.loc[df["id"].duplicated(), "id"].head(5).tolist()

        lines = []
        A = lines.append
        A("# M1 数据清洗质量报告")
        A("")
        A(f"- 报告生成日期：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}")
        A(f"- 原始文件：`{self.raw_path.name}`")
        A(f"- 清洗程序：`etl/clean_dcd.py`（复用 outputs/项目内容整理/ 探查脚本结论）")
        A("")
        A("## 1. 行数与唯一 id 核对（验收线）")
        A("")
        A("| 指标 | 原始 CSV | 清洗后 | 是否一致 |")
        A("|---|---|---|---|")
        A(f"| 数据行数（不含表头） | {scan['total_rows']} | {L} | {'✔' if scan['total_rows'] == L else '✘'} |")
        A(f"| 唯一 id 数 | {scan['uniq_id']} | {int(df['id'].nunique())} | {'✔' if scan['uniq_id'] == df['id'].nunique() else '✘'} |")
        A(f"| 重复 id 数 | — | {dup_id} | " + ("✔（无重复）" if dup_id == 0 else f"样例：{dup_samples}") + " |")
        A("")
        A("## 2. 错位修复说明（csv.reader 全量实证）")
        A("")
        A(f"- 表头 20 列；**数据行 100%（{scan['total_rows']} 行）为 21 字段**，无 20 字段混行。")
        A("- 21 字段构成 = 表头前 16 列 + **一个表头未声明的 0/1 冗余列** + 档案详情/相关参数/用车成本/公司名称（整体右移 1 位）。")
        A(f"- 0/1 冗余列分布：{rep['extra_col_dist']}。经相关性分析（flag=1 仅 498 行，品牌分布/字段完整度与整体一致）"
          "判定为采集端无业务含义字段 → **剔除**，记录于此供论文说明。")
        A("- 旧探查结论「品牌列出现里程/日期」为 **pandas 直接读原文件的解析伪影**；"
          "修复后中间件由 csv.reader 文本层标准转义写出，后续 pandas 读取不再错位。")
        A(f"- 修复结果：非常规行 {rep['unusual_rows']} 行（预期 0）"
          f"{'✔' if rep['unusual_rows'] == 0 else '！'}；品牌列语义异常行 {rep['semantic_bad_rows']} 行。")
        A("- 另：表头 `固定价格` 列 100% 为「暂无报价」（可用率 0%），随冗余列一同剔除。")
        A("")
        A("### 修复前后抽样对照（前 %d 行）" % len(rep["before_sample"]))
        A("")
        A("| 源行 | 阶段 | id | 品牌 | 里程 | 价格 | 生产年份 | 城市 | 公司名称(末字段) |")
        A("|---|---|---|---|---|---|---|---|---|")
        for (ln, before), (_, after) in zip(rep["before_sample"], rep["after_sample"]):
            def cell(v): return str(v).replace("|", "/")[:26] or "∅"
            # before 是 21 字段原始行
            b_id, b_brand, b_km, b_price, b_year, b_city = before[0], before[4], before[5], before[8], before[12], before[13]
            b_co = before[20] if len(before) > 20 else ""
            a_id, a_brand, a_km, a_price, a_year, a_city, a_co = (
                after[0], after[4], after[5], after[8], after[12], after[13], after[-1])
            A(f"| {ln} | 修复前(21列) | {cell(b_id)} | {cell(b_brand)} | {cell(b_km)} | {cell(b_price)} | {cell(b_year)} | {cell(b_city)} | {cell(b_co)} |")
            A(f"| {ln} | 修复后(20列) | {cell(a_id)} | {cell(a_brand)} | {cell(a_km)} | {cell(a_price)} | {cell(a_year)} | {cell(a_city)} | {cell(a_co)} |")
        A("")
        A("## 3. 嵌套字段解析率（暂无→空）")
        A("")
        A("| 字段 | 解析成功 | 暂无/缺失 | 解析失败 |")
        A("|---|---|---|---|")
        for f, c in jst.items():
            A(f"| {f} | {c.get('ok', 0)} | {c.get('missing', 0)} | {c.get('parse_error', 0)} |")
        A("")
        A("子键覆盖率（前 20）：")
        A("")
        A("| 字段 | 子键 | 出现行数 | 覆盖率 |")
        A("|---|---|---|---|")
        for r in sub_df.head(20).itertuples():
            A(f"| {r.字段} | {r.子键} | {r.出现行数} | {r.覆盖率:.1%} |")
        A("")
        A("## 4. 字段可用性（论文实验一素材）")
        A("")
        A(f"共 {len(metrics)} 个输出字段（原始列 + 派生/展开列），完整表见 `field_metrics.csv`。"
          "字段可用率/缺失率将作为创新点①可比性框架的阈值设计弹药。")
        A("")
        A("| 字段 | 分组 | 非空数 | 缺失率 | 可用率 | 唯一数 |")
        A("|---|---|---|---|---|---|")
        for r in metrics.head(28).itertuples():
            A(f"| {r.字段} | {r.分组} | {r.非空数} | {r.缺失率:.2%} | {r.可用率:.2%} | {r.唯一数} |")
        A("")
        A("## 5. 异常统计（只统计不删行）")
        A("")
        A("| 检查项 | 数量 | 口径 |")
        A("|---|---|---|")
        A(f"| 价格解析失败 | {anom['price_parse_fail']} | 价格列非空但无法转元 |")
        A(f"| 里程解析失败 | {anom['mileage_parse_fail']} | 里程列非空但无法转公里 |")
        A(f"| 价格异常 | {anom['price_bad']} | 缺失 / ≤0 / ≥{self.price_max/10000:.0f}万 |")
        A(f"| 里程异常 | {anom['mileage_bad']} | 缺失 / ≤0 / ≥{self.mileage_max/10000:.0f}万公里 |")
        A(f"| 生产年份异常 | {anom['year_bad']} | 缺失 / <{self.year_min} / >{self.year_max} |")
        A(f"| JSON 解析失败行 | {anom['json_parse_fail_rows']} | 3 个嵌套字段任一解析失败 |")
        A(f"| 注时早于生产年份 | {anom['reg_early_than_prod']} | 登记(上牌)日期 < 生产年份 |")
        A(f"| 价格中位数/范围 | {df['price_yuan'].median():,.0f} 元 / "
          f"{df['price_yuan'].min():,.0f}~{df['price_yuan'].max():,.0f} 元 | |")
        A(f"| 里程中位数 | {df['mileage_km'].median():,.0f} 公里 | |")
        A(f"| 生产年份范围 | {df['production_year'].min():.0f}~{df['production_year'].max():.0f} | |")
        A(f"| 品牌数 / 城市数 | {df['brand_name'].nunique()} / {df['city_name'].nunique()} | |")
        A(f"| 质量分分布(高→低前8档) | {anom['score_dist']} | 满分 100，见程序内口径 |")
        A("")
        A("## 6. 车龄交叉核验（文本车龄 vs 日期推算）")
        A("")
        ac = st.get("age_crosscheck")
        if ac:
            A(f"- 依据「上牌年份 + 文本车龄」反推挂牌基准日：中位数 **{ac['median_implied_listing_year']} 年**"
              f"（IQR {ac['iqr'][0]}~{ac['iqr'][1]}），样本 {ac['pairs']} 对。")
            A(f"- 偏离中位数 >2 年的行数：**{ac['dev_gt_2y']}**。数据整体采自 {round(ac['median_implied_listing_year'])} 年前后"
              "（与报告日不同的挂牌基准），后续按基准日口径计算车龄时以本结果为准。")
        else:
            A("- 可交叉核验样本不足。")
        A("")
        A("## 7. 导出物")
        A("")
        A("- `data/clean/dcd_clean.csv`：标量列 + `*_raw` 原文列（UTF-8-SIG，Excel 友好）")
        A("- `data/clean/dcd_clean.parquet`：额外含未入展开键表的 `_extra_*` 列（JSON 文本）")
        A("- 说明：CSV 与 parquet 均为 **19 个原始列 + 派生列**；原始列保留原文便于溯源，"
          "标准化数值在 `price_yuan/mileage_km/production_year/registration_year/vehicle_age_years` 等派生列。")
        return "\n".join(lines)

    # ---------- stage: export ----------
    def stage_export(self):
        df = self.stats.get("df")
        if df is None:
            df = self.stage_standardize()
        self.stats["df"] = df

        # Validate in memory before touching the published files. These checks
        # must remain active under ``python -O``.
        scan = self.stats["scan"]
        if len(df) != scan["total_rows"]:
            raise ValueError("行数核对失败")
        if int(df["id"].nunique()) != scan["uniq_id"]:
            raise ValueError("唯一 id 核对失败")

        # 抽样断言（用已知的第一条记录做精确锚点）
        row0 = df[df["id"] == "aaaGJ13976215"]
        if len(row0):
            r = row0.iloc[0]
            expect = dict(brand_name="宝马", price_yuan=558000.0, production_year=2022,
                          mileage_km=42000.0, vehicle_age_years=2.75, city_name="昆明",
                          registration_year=2021)
            for k, v in expect.items():
                got = r[k]
                if got != v:
                    raise ValueError(f"锚点行 {k} 期望 {v} 实际 {got}")
        brand_clean = df["品牌名称"].fillna("").astype(str)
        if brand_clean.str.contains("万公里|公里$", regex=True).any():
            raise ValueError("品牌列仍含里程！")

        csv_cols = [c for c in df.columns if not c.startswith("_extra_")]
        manifest_path = self.clean_csv_path.with_name(f"{self.clean_csv_path.stem}.manifest.json")
        outputs = [self.clean_csv_path, self.clean_pq_path, manifest_path]
        temporary: list[Path] = []
        backups: dict[Path, Path | None] = {}
        preserve_backups: set[Path] = set()

        def temp_path(parent: Path, suffix: str) -> Path:
            handle, name = tempfile.mkstemp(prefix=".dcd-publish-", suffix=suffix, dir=parent)
            os.close(handle)
            path = Path(name)
            temporary.append(path)
            return path

        try:
            for output in outputs:
                output.parent.mkdir(parents=True, exist_ok=True)
            csv_tmp = temp_path(self.clean_csv_path.parent, ".csv")
            pq_tmp = temp_path(self.clean_pq_path.parent, ".parquet")
            df[csv_cols].to_csv(csv_tmp, index=False, encoding="utf-8-sig")
            df.to_parquet(pq_tmp, index=False)

            # Re-read both staged artifacts and verify the same row identities.
            csv_check = pd.read_csv(csv_tmp, encoding="utf-8-sig", usecols=["id"])
            pq_check = pd.read_parquet(pq_tmp, columns=["id"])
            expected_ids = df["id"].astype(str).tolist()
            if csv_check["id"].astype(str).tolist() != expected_ids or pq_check["id"].astype(str).tolist() != expected_ids:
                raise ValueError("CSV/Parquet 暂存文件记录 ID 与验收批次不一致")

            def sha256(path: Path) -> str:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                return digest.hexdigest()

            csv_hash, pq_hash = sha256(csv_tmp), sha256(pq_tmp)
            batch_id = hashlib.sha256(f"{csv_hash}:{pq_hash}".encode()).hexdigest()
            manifest = {
                "format_version": 1, "batch_id": batch_id,
                "row_count": int(len(df)), "unique_id_count": int(df["id"].nunique()),
                "artifacts": {
                    self.clean_csv_path.name: {"sha256": csv_hash, "rows": int(len(csv_check))},
                    self.clean_pq_path.name: {"sha256": pq_hash, "rows": int(len(pq_check))},
                },
            }
            manifest_tmp = temp_path(manifest_path.parent, ".manifest.json")
            manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            # Keep restorable copies until the manifest has been published last.
            for output in outputs:
                if output.exists():
                    backup = temp_path(output.parent, ".backup")
                    shutil.copy2(output, backup)
                    backups[output] = backup
                else:
                    backups[output] = None
            published: list[Path] = []
            try:
                for staged, output in ((csv_tmp, outputs[0]), (pq_tmp, outputs[1]), (manifest_tmp, outputs[2])):
                    os.replace(staged, output)
                    published.append(output)
            except Exception:
                rollback_errors = []
                for output in reversed(published):
                    backup = backups[output]
                    try:
                        if backup is None:
                            output.unlink(missing_ok=True)
                        else:
                            os.replace(backup, output)
                            temporary.remove(backup)
                    except OSError as rollback_error:
                        if backup is not None:
                            preserve_backups.add(backup)
                        rollback_errors.append(f"{output}: {rollback_error}")
                if rollback_errors:
                    raise RuntimeError(
                        "发布失败且回滚不完整；请从保留的 .backup 文件恢复："
                        + "; ".join(rollback_errors)
                    )
                raise
        finally:
            for path in temporary:
                if path not in preserve_backups:
                    path.unlink(missing_ok=True)

        print(f"[export] 写出 {self.clean_csv_path.name} ({len(df)} 行 × {len(csv_cols)} 列)")
        print(f"[export] 写出 {self.clean_pq_path.name} ({len(df)} 行 × {len(df.columns)} 列)")
        print(f"[export] CSV/Parquet 同批次 manifest 已发布：{manifest_path.name}")
        return True

    # ---------- 总调度 ----------
    def run(self, stage: str):
        order = ["scan", "repair", "unpack", "standardize", "report", "export"]
        assert stage in order + ["all"], f"未知阶段：{stage}"
        end = order.index(stage) if stage != "all" else len(order) - 1
        for s in order[:end + 1]:
            print(f"\n===== stage: {s} =====")
            getattr(self, f"stage_{s}")()
        print("\n===== 全部完成 =====")


def main():
    ap = argparse.ArgumentParser(description="M1 二手车数据清洗管道")
    ap.add_argument("--stage", default="all",
                    choices=["scan", "repair", "unpack", "standardize", "report", "export", "all"],
                    help="运行到指定阶段为止（前序自动执行）")
    ap.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    Pipeline(cfg).run(args.stage)


if __name__ == "__main__":
    main()
