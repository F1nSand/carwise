# -*- coding: utf-8 -*-
"""
命令行演示 —— 输入一句人话，观察单 Agent 多轮 tool_calls 全链路

用法：
  python backend/run_demo.py                       # 交互模式（循环输入）
  python backend/run_demo.py "2022奥迪A4L多少钱"     # 单次查询
"""
from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path

# 让脚本能 import backend.*（把项目根加入 sys.path）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import DEEPSEEK_MODEL  # noqa: E402
from backend.core.orchestrate import run_query  # noqa: E402
from backend.deepseek_client import get_client  # noqa: E402
from backend.agent_runtime.session_store import SessionStore  # noqa: E402


def _print_fact(fact: dict) -> None:
    """打印单个工具的真实数据。"""
    tool = fact.get("tool")
    if not fact.get("ok"):
        print(f"    (无结果) {fact.get('error') or fact.get('note')}")
        return

    if tool == "region_gap":
        print(f"    车系       : {fact.get('series')}  年份: {fact.get('year')}")
        print(f"    全国中位   : {fact.get('national_median_wan')} 万（基于 {fact.get('raw_count')} 条车源）")
        print(f"    纳入城市   : {fact.get('n_cities_included')} 个（门槛 ≥{fact.get('min_city_n')} 台）")
        for c in (fact.get("cheapest_cities") or [])[:1]:
            print(f"    最便宜     : {c['city']} {c['median_wan']} 万（{c['diff_pct']}%）")
        for p in (fact.get("priciest_cities") or [])[:1]:
            print(f"    最贵       : {p['city']} {p['median_wan']} 万（+{p['diff_pct']}%）")
        print(f"    可信度     : {fact.get('confidence')}")

    elif tool == "price_decay":
        yr = fact.get("year_range") or ["?", "?"]
        print(f"    车系       : {fact.get('series')}  城市: {fact.get('city') or '全国'}")
        print(f"    可比年款   : {yr[0]}~{yr[1]}（{fact.get('n_years')} 个年份，门槛 ≥{fact.get('min_year_n')} 台）")
        print(f"    年均折旧率 : {fact.get('annual_depreciation_pct')}%（每老一个年款平均便宜约该比例）")
        print(f"    总变化     : {fact.get('total_change_pct')}%（最老→最新年款）")
        pts = fact.get("points") or []
        if pts:
            head, tail = pts[0], pts[-1]
            print(f"    {head['year']}款    : {head['median_wan']} 万")
            print(f"    {tail['year']}款    : {tail['median_wan']} 万")
        print(f"    可信度     : {fact.get('confidence')}")

    elif tool == "recommend":
        bmin = fact.get("budget_min_wan") or 0
        bmax = fact.get("budget_max_wan")
        scope = f"{bmin:g}~{bmax:g} 万" if bmin else f"{bmax:g} 万以内"
        extra = []
        if fact.get("brand"):
            extra.append(f"品牌={fact['brand']}")
        if fact.get("series"):
            extra.append(f"车系={fact['series']}")
        if fact.get("city"):
            extra.append(f"城市={fact['city']}")
        print(f"    预算       : {scope}" + (f"  ({'，'.join(extra)})" if extra else ""))
        print(f"    候选车系   : {fact.get('n_candidates')} 个（预算内车源 {fact.get('budget_count')} 条）")
        for c in (fact.get("recommendations") or []):
            print(f"      - {c['series']:<8} 中位 {c['median_wan']} 万 "
                  f"({c['p25_wan']}~{c['p75_wan']})  样本 {c['sample_size']}  "
                  f"中位年款 {c['median_year']}  得分 {c['score']}")
        print(f"    可信度     : {fact.get('confidence')}")

    elif tool == "csv_query":
        print(f"    检索命中   : {fact.get('match_count')} 条")
        print(f"    过滤条件   : {'；'.join(fact.get('filters_applied') or []) or '无'}")
        for row in (fact.get("rows") or [])[:5]:
            print(
                f"      - {row.get('series') or row.get('car_name') or '未知车系'}"
                f" | {row.get('city_name') or '未知城市'}"
                f" | {row.get('production_year') or '未知年款'}"
                f" | {row.get('price_wan') or '?'} 万"
                f" | {row.get('mileage_km') or '?'} km"
            )
        print(f"    说明       : {fact.get('note') or ''}")

    else:  # price_compare
        p = fact.get("price") or {}
        print(f"    车系       : {fact.get('series')}  年份: {fact.get('year')}  城市: {fact.get('city')}")
        print(f"    样本量     : {fact.get('sample_size')}（原始 {fact.get('raw_count')}，剔除异常 {fact.get('excluded_count')}）")
        print(f"    中位价     : {p.get('median_wan')} 万")
        print(f"    主流区间   : {p.get('p25_wan')} ~ {p.get('p75_wan')} 万")
        print(f"    可信度     : {fact.get('confidence')}")
        if (fact.get("dispersion") or {}).get("iqr_ratio") is not None:
            print(f"    离散度     : IQR/中位 = {fact['dispersion']['iqr_ratio']}")


def _print_result(res: dict) -> None:
    print("\n" + "=" * 62)
    print(f"用户输入：{res['input']}")
    trace_id = res.get("trace_id")
    flags = (res.get("state") or {}).get("flags") or {}
    if trace_id:
        print(f"运行轨迹：{trace_id}")
    if res.get("session_id"):
        print(f"会话 ID：{res['session_id']}")
    if flags.get("prefix_hash"):
        print(
            f"静态前缀：{flags['prefix_hash'][:16]}...；"
            f"本地复用命中：{bool(flags.get('prefix_cache_hit'))}"
        )
    print(
        f"执行步数：{flags.get('steps', 0)}；工具调用：{len(res.get('tool_results') or [])}；"
        f"成功调用：{flags.get('successful_tool_count', 0)}"
    )

    tool_results = res.get("tool_results") or (res.get("state") or {}).get("tool_results") or []
    print("\n[1] Agent 工具调用轨迹")
    if not tool_results:
        print("    本次没有调用工具。")
    for i, item in enumerate(tool_results, 1):
        output = item.get("output") or {}
        print(
            f"  ── 调用 {i}/{len(tool_results)} [{item.get('tool_name')}] "
            f"status={item.get('status')} duration={item.get('duration_ms', 0)}ms ──"
        )
        print(f"    call_id   : {item.get('tool_call_id')}")
        print(f"    参数      : {json.dumps(item.get('input') or {}, ensure_ascii=False)}")
        _print_fact(output)

    print(f"\n[2] 最终 Agent 回答（来源：{res.get('summary_source', 'agent')}）")
    print(f"    {res.get('summary')}")
    print("=" * 62)


def _handle(res: dict) -> None:
    """展示一次 Agent 查询结果。"""
    _print_result(res)


def main() -> None:
    # Windows 终端可能使用本地代码页；统一使用 UTF-8，避免 Agent 的中文轨迹和数据摘要乱码。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="二手车智能分析单 Agent CLI")
    parser.add_argument("--session-id", default=None, help="复用同一会话的安全 ID")
    parser.add_argument("--rollback-message-id", default=None, help="回滚到该消息之前，不启动模型")
    parser.add_argument("--rollback-revision", type=int, default=None, help="回滚时校验会话 revision")
    parser.add_argument("question", nargs="*", help="一次性问题")
    args = parser.parse_args()

    if args.rollback_message_id:
        if not args.session_id:
            parser.error("--rollback-message-id 必须和 --session-id 一起使用")
        snapshot = SessionStore().rollback(
            args.session_id,
            args.rollback_message_id,
            expected_revision=args.rollback_revision,
        )
        print(
            f"会话 {args.session_id} 已回滚；revision={snapshot.revision}；"
            f"active_head_id={snapshot.active_head_id or '(空)'}"
        )
        return

    client = get_client()
    print(f">> Agent 模式：原生 tool_calls 多轮循环，模型：{DEEPSEEK_MODEL}")
    if client.is_available():
        print(">> DeepSeek 已就绪，Agent 可以自主决定是否调用工具。")
    else:
        print(f">> DeepSeek 未启用（{client.reason}），本次无法启动模型 Agent。")

    # 命令行单次查询
    if args.question:
        _handle(run_query(" ".join(args.question), session_id=args.session_id))
        return

    # 交互模式
    print(">> 输入问题（如：2022奥迪A4L多少钱 / 15万买什么车），输入 q 退出。")
    while True:
        try:
            text = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.lower() in {"q", "quit", "exit"}:
            break
        if not text:
            continue
        _handle(run_query(text, session_id=args.session_id))


if __name__ == "__main__":
    main()
