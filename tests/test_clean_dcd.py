# -*- coding: utf-8 -*-
"""M1 清洗管道关键函数单元测试。

运行：cd 项目根 && .venv/Scripts/python -m pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))          # 项目根
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "etl"))  # 可直接 import clean_dcd

import pytest
import pandas as pd

import clean_dcd as cd

# 与 config.yaml 一致的缺失模式
MISS_PATTERNS = ["暂无", "无", "--", "-", "None", "nan", "N/A", "NA", "未填写"]
im = cd.build_missing_pred(MISS_PATTERNS)


class TestMissing:
    def test_empty_and_blank(self):
        assert im(None) and im("") and im("   ") and im("NaN")

    def test_wuzan_prefix(self):
        assert im("暂无报价") and im("暂无档案相关信息") and im("无")

    def test_normal_not_missing(self):
        assert not im("55.80万") and not im("宝马") and not im("[{'name': 'x'}]")


class TestRepairRow:
    HEADER = list(cd.HEADER_20)
    # 21 字段样例 = 表头前 16 列内容 + 冗余 '0' + 档案详情/相关参数/用车成本/公司名称
    row21 = [f"v{i}" for i in range(16)] + ["0", "dang", "param", "cost", "公司A"]
    keep_pos = [i for i, c in enumerate(HEADER) if c != "固定价格"]

    def test_drop_extra_and_align(self):
        aligned, status, _ = cd.repair_data_row(
            self.row21, extra_pos=16, allowed={"0", "1"}, keep_positions=self.keep_pos)
        assert status == "ok"
        assert len(aligned) == 19                      # 20 列表头 - 固定价格
        assert aligned[14] == "v15"                    # 原产地价格（固定价格列已被跳过）
        assert aligned[15] == "dang"                   # 档案详情落位
        assert aligned[16] == "param"                  # 相关参数
        assert aligned[17] == "cost"                   # 用车成本
        assert aligned[18] == "公司A"                  # 公司名称保留（不被截断丢弃）
        assert len(self.keep_pos) == 19

    def test_unusual_row_rejected(self):
        bad = list(self.row21)
        bad[16] = "x"                                  # 冗余位不是 0/1
        _, status, _ = cd.repair_data_row(bad, extra_pos=16, allowed={"0", "1"},
                                          keep_positions=self.keep_pos)
        assert status == "unusual"


class TestPrice:
    def test_wan_to_yuan(self):
        assert cd.parse_price_yuan("55.80\u4e07", im) == 558000.0
        assert cd.parse_price_yuan("7.98\u4e07", im) == 79800.0

    def test_yuan_and_plain(self):
        assert cd.parse_price_yuan("128000\u5143", im) == 128000.0
        assert cd.parse_price_yuan("128000", im) == 128000.0

    def test_missing(self):
        assert cd.parse_price_yuan("\u6682\u65e0\u62a5\u4ef7", im) is None
        assert cd.parse_price_yuan("", im) is None

    def test_negative_price_is_not_cleaned_into_positive(self):
        assert cd.parse_price_yuan("-3\u4e07", im) == -30000.0
        assert cd.parse_price_yuan("3-5\u4e07", im) == 30000.0

class TestMileage:
    def test_wan_km(self):
        assert cd.parse_mileage_km("4.20\u4e07\u516c\u91cc", im) == 42000.0
        assert cd.parse_mileage_km("8.6\u4e07", im) == 86000.0

    def test_km_only(self):
        assert cd.parse_mileage_km("40000\u516c\u91cc", im) == 40000.0

    def test_missing(self):
        assert cd.parse_mileage_km("\u6682\u65e0", im) is None

    def test_negative_mileage_is_preserved(self):
        assert cd.parse_mileage_km("-2\u4e07\u516c\u91cc", im) == -20000.0

class TestAgeAndYear:
    def test_age_ym(self):
        assert cd.parse_age_years("2\u5e749\u4e2a\u6708", im) == 2.75
        assert cd.parse_age_years("1\u5e74", im) == 1.0
        assert cd.parse_age_years("6\u4e2a\u6708", im) == pytest.approx(0.5)
        assert cd.parse_age_years("3\u5e7410\u4e2a\u6708", im) == pytest.approx(3 + 10 / 12, abs=1e-3)

    def test_age_missing(self):
        assert cd.parse_age_years("\u6682\u65e0", im) is None
        assert cd.parse_age_years("-2\u5e74", im) == -2.0

    def test_year(self):
        assert cd.parse_year_int("2022", im) == 2022
        assert cd.parse_year_int("2019\u5e74", im) == 2019
        assert cd.parse_year_int("\u6682\u65e0", im) is None

class TestParams:
    def test_wheelbase(self):
        assert cd.parse_axis_to_mm("2800mm", im) == 2800
        assert cd.parse_axis_to_mm("2.8m", im) == 2800

    def test_power_kw(self):
        assert cd.parse_power_kw("135kW", im) == 135.0
        assert cd.parse_power_kw("184马力", im) == pytest.approx(184 * 0.7355, abs=0.01)

    def test_float_any(self):
        assert cd.parse_float_any("6.9s", im) == 6.9
        assert cd.parse_float_any("7.1L", im) == 7.1
        assert cd.parse_float_any("5328元", im) == 5328.0


class TestJsonParse:
    def test_ok(self):
        s = "[{'name': '排放标准', 'value': '国6'}, {'name': '能源类型', 'value': '汽油'}]"
        d, status = cd.parse_json_to_dict(s, im)
        assert status == "ok"
        assert d == {"排放标准": "国6", "能源类型": "汽油"}

    def test_ok_with_unit(self):
        s = "[{'value': '7.1', 'unit': 'L', 'name': '百公里油耗'}]"
        d, status = cd.parse_json_to_dict(s, im)
        assert status == "ok" and d == {"百公里油耗": "7.1"}

    def test_missing(self):
        assert cd.parse_json_to_dict("暂无用车成本相关信息", im) == (None, "missing")

    def test_parse_error(self):
        assert cd.parse_json_to_dict("[{'name': 'x'", im)[1] == "parse_error"
        assert cd.parse_json_to_dict("not-a-list", im)[1] == "parse_error"


def _export_pipeline(tmp_path):
    pipeline = object.__new__(cd.Pipeline)
    pipeline.clean_csv_path = tmp_path / "dcd_clean.csv"
    pipeline.clean_pq_path = tmp_path / "dcd_clean.parquet"
    pipeline.df = pd.DataFrame({"id": ["id-1", "id-2"], "品牌名称": ["甲", "乙"],
                                "price_yuan": [10000.0, 20000.0]})
    pipeline.stats = {"df": pipeline.df, "scan": {"total_rows": 2, "uniq_id": 2}}
    return pipeline


@pytest.mark.parametrize("fail_on", [2, 3])
def test_export_publish_failure_restores_previous_csv_and_parquet_and_manifest(tmp_path, monkeypatch, fail_on):
    pipeline = _export_pipeline(tmp_path)
    manifest = pipeline.clean_csv_path.with_name("dcd_clean.manifest.json")
    targets = [pipeline.clean_csv_path, pipeline.clean_pq_path, manifest]
    original = [b"old csv", b"old parquet", b"old manifest"]
    for path, contents in zip(targets, original):
        path.write_bytes(contents)
    real_replace = cd.os.replace
    calls = 0

    def fail_second_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == fail_on:
            raise OSError("injected publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(cd.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="injected publish failure"):
        pipeline.stage_export()

    assert [path.read_bytes() for path in targets] == original


@pytest.mark.parametrize("failure_stage", ["csv", "parquet", "manifest"])
def test_export_staging_failure_leaves_all_published_files_untouched(tmp_path, monkeypatch, failure_stage):
    pipeline = _export_pipeline(tmp_path)
    manifest = pipeline.clean_csv_path.with_name("dcd_clean.manifest.json")
    targets = [pipeline.clean_csv_path, pipeline.clean_pq_path, manifest]
    original = [b"old csv", b"old parquet", b"old manifest"]
    for path, contents in zip(targets, original):
        path.write_bytes(contents)

    def injected_failure(*args, **kwargs):
        raise OSError(f"injected {failure_stage} staging failure")

    if failure_stage == "csv":
        monkeypatch.setattr(pd.DataFrame, "to_csv", injected_failure)
    elif failure_stage == "parquet":
        monkeypatch.setattr(pd.DataFrame, "to_parquet", injected_failure)
    else:
        real_write_text = Path.write_text

        def fail_manifest(path, *args, **kwargs):
            if path.name.endswith(".manifest.json"):
                injected_failure()
            return real_write_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail_manifest)

    with pytest.raises(OSError, match=f"injected {failure_stage} staging failure"):
        pipeline.stage_export()

    assert [path.read_bytes() for path in targets] == original


def test_successful_export_publishes_csv_and_parquet_manifest_for_one_batch(tmp_path):
    import json

    pipeline = _export_pipeline(tmp_path)

    assert pipeline.stage_export() is True
    manifest = json.loads(pipeline.clean_csv_path.with_name("dcd_clean.manifest.json").read_text(encoding="utf-8"))
    assert manifest["row_count"] == manifest["unique_id_count"] == 2
    assert manifest["artifacts"]["dcd_clean.csv"]["rows"] == 2
    assert manifest["artifacts"]["dcd_clean.parquet"]["rows"] == 2
    assert len(manifest["batch_id"]) == 64


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
