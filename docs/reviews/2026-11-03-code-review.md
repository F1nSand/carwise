# 代码与功能审查：2026-11-03

范围：分析 API、六维看板服务、筛选边界、数据快照、浏览器 SSE 生命周期、Markdown 渲染、身份隔离和默认 CI。此次聚焦错误恢复、信息暴露和连接资源，不声称覆盖全部生产环境行为。

## 已修复的问题

| 等级 | 位置 | 触发与影响 | 优化 |
| --- | --- | --- | --- |
| Medium | `backend/analysis_api.py` 的 catalog/query/dashboard | catalog 直接返回底层异常文本；query/dashboard 数据加载失败成为普通内部错误，用户无法得到一致的重试语义 | 数据加载失败统一返回脱敏的 `503 dataset_unavailable`、`retryable: true` 和 `Retry-After`；详细异常只进入服务端日志；保留 409、422 和并发上限 |
| Medium | `backend/analysis/service.py` 的 `run_dashboard` | 单项分析异常原文进入卡片 limitations，可能暴露路径、查询或内部诊断 | 返回固定提示并记录异常；继续保留其他卡片和 `partial_success` |
| Medium | `frontend/src/services/sse.ts` 的 `streamRunEvents` | JSON 解析或用户回调失败后只释放 reader 锁，开放流仍可继续占用网络资源；连接回调失败还会绕过清理 | 三种异常路径都取消读取并释放锁；取消本身失败时保留最初异常；保留正常 EOF 和主动取消行为 |
| Low | `.github/workflows/agent-eval.yml` | 默认后端 CI 没有运行分析 API/服务边界测试，以上问题无法被持续回归检测 | 新增四个使用隔离测试数据的测试文件，保持 CI 不需要真实数据集 |

| Low | `pytest.ini`（新增） | 默认 pytest 递归收集本地 artifacts 中的旧源码，产生 62 个同名模块导入冲突 | 将默认收集限定到正式 `tests/`，保留显式指定测试路径的能力 |

## 已检查但未扩大修改的路径

- 看板已有 AbortController 与请求序号，避免旧响应覆盖新筛选；已有路由离开和版本恢复回归。
- Markdown 用 Vue 文本插值渲染 token，审查路径未使用 v-html 执行模型内容。
- 筛选已拒绝未知字段、非有限数字、范围倒置和重复车系身份；数据快照已有并发合并和缓存预算。
- 身份 Cookie 使用 HMAC 签名，已有过期、续签和不同 owner 隔离测试。

## 验证

新增错误路径回归先在旧实现上失败，修复后通过。验证结果：后端完整 `pytest -q` 为 532 passed；前端 `npm run test:run` 为 204 passed；`npm run build` 通过；定向分析错误回归为 14 passed。后端仅出现现有 Starlette/AnyIO 弃用警告。默认 CI 同时覆盖 Windows/Linux 后端、前端测试/构建和容器构建。

未调用真实付费模型；生产域名、TLS、实际浏览器长连接压力和服务器部署不在本次验证范围内。本地完整后端测试使用已有车源与基线，不将它们上传仓库。

审查范围内的修复后质量评价：8/10。主要保留项是进一步扩展无需私有数据的端到端场景，以及用真实负载测量缓存和并发配置；未取得性能测量前不作性能提升承诺。
