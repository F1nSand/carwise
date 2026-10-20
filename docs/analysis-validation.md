# 首期共享分析与看板验收记录

日期：2026-10-09。按已批准计划完成六项共享分析、多维筛选看板与现有 Agent 接入。

## 交付与入口

- `/dashboard`：价格分布、里程与价格相关性、车系对比、品牌画像、城市价差、快照车龄与价格关系；均有数据表替代。
- 筛选支持品牌、品牌+车系、城市、预算、里程、生产年份、快照车龄和可用能源类型；草稿经应用后生效，URL 可恢复条件。
- API：`GET /v1/analysis/catalog`、`POST /v1/analysis/query`、`POST /v1/analysis/dashboard`。单位与字段见 `analysis-contract.md` / `analysis-api-contract.json`。
- Agent 与看板调用同一分析服务；报告支持 `analysisBlocks`，旧报告继续可读。看板直接调用 API，不触发模型与会话/task/报告持久化。
- 保留 Parquet + 缓存 pandas / SQLite 元数据架构。图表散点最多 2,000 个，统计仍使用完整有效样本。

## 自动化与集成检查

| 验证 | 结果 |
| --- | --- |
| 集成阶段后端全量回归 | 449 passed，1 条 Starlette 弃用警告 |
| 最后边界修复的后端定向回归 | 22 passed：analysis_edge_cases/service/api/shared_analysis_tools |
| Agent 定向回归 | 170 passed；含 fake model 的工具→证据→finalize→报告完整链路 |
| 前端全量回归 | 30 files / 159 passed |
| 最终前端生产构建 | 通过 |
| 四个旧工具完整基准 | price_compare / region_gap / price_decay / recommend 全字典一致 |
| 本地真实 HTTP 一致性 | 10 项通过；六项 query/dashboard/Agent 适配器统计与计数一致 |

最后的定向回归覆盖可选数值列缺失、全局/用户排除计数、无匹配为空态、单指标失败保留其他结果、异常车龄分箱在分配数组前拒绝。HTTP 还验证同一快照/范围、陈旧版本 409、任意 SQL 参数 422。Agent 验证使用可控 fake model，本次未调用远端真实模型。

复现检查（项目根目录）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-temp-analysis-validation
cd frontend
npm run test:run
npm run build
```

## 性能与资源

真实数据 143,793 条，Parquet 约 33.46 MB，缓存宽 DataFrame 估算约 303 MB。共享分析投影所需列，避免逐请求复制全部宽表。

20 次热态串行看板计算：中位 0.373 秒，P95 0.790 秒，最大 1.148 秒；最大 JSON 249,756 字节。通过串行 P95 ≤ 2 秒、JSON ≤ 2 MB 目标。基准期间后端回归测试同时运行，数值仅代表当前机器。

4 线程直接服务计算 P95 为 2.217 秒。HTTP 4 客户端突发 20 次：2 次 200、18 次 503，`Retry-After: 2`；验证每进程最多 2 个分析请求的准入限制，超额请求立即返回可重试忙碌态。仅 2 个成功 HTTP 样本，不据此推断稳定吞吐或 HTTP P95。前端提供忙碌原因和重试。

观测 RSS：加载后约 665 MB，串行结束约 545 MB，并发结束约 588 MB。重复串行采样未出现持续线性增长；有限采样不等同长期内存泄漏证明或全场景峰值。

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_analysis.py --iterations 20 --output artifacts/analysis-validation/benchmark-rerun.json --base-url http://127.0.0.1:8000
```

原始记录：`artifacts/analysis-validation/benchmark-final.json`、`http-consistency.json`、`baseline-parity.json`；旧工具基准刷新脚本：`artifacts/analysis-validation/update_baseline_parity.py`。

## 浏览器与视觉核对

真实 Chrome 验证桌面 1440px、平板 768px、手机 375px：页面无横向溢出；手机首次进入筛选区折叠。实际选择宝马品牌、宝马 3 系与 5 系并应用，六张图呈现同一范围 6,310 条车源。城市图限制为价差较大的前 10 个达标城市，表格保留完整城市。

主页普通区域鼠标事件后，主内容区不处于焦点态，计算样式 `outline: none`；保留交互控件和跳转链接的键盘焦点提示。浏览器摘要见 `artifacts/analysis-validation/browser-summary.json`。

浏览器发现的分位区间坐标轴裁切已修复：ECharts custom series 明确声明 `encode.y` 为 P25/中位数/P75 三列；目标测试与最终构建通过。品牌占比轴标题已缩短。布局截图：`dashboard-desktop.png`、`dashboard-tablet.png`、`dashboard-mobile.png`。`dashboard-results.png` 是坐标修复前诊断截图；最终结果截图重试超时，不作为最终视觉证据。

## 数据与后续范围

车龄使用快照字段，采集年份 2024 为推定信息，不提供市场月份趋势。能源字段覆盖率约 8.57%，缺失类型不推断。样本不足、缺字段、空结果与部分失败分别反馈；统计基于挂牌价。

机器学习估价、Agent 对话策略专项与 DuckDB 迁移按约定留到后续。当前仅本地验证，无远端部署、无提交。
