# 二手车智能分析前端

Vue 3 + TypeScript + Vite 前端，通过 HTTP/SSE 连接 FastAPI Agent；`/dashboard` 直接调用共享统计 API，无需模型密钥。

## 本地运行

要求 Node.js 22+、Python 3.12+。在项目根目录运行 `npm run setup`，然后 `npm run dev`。

只启动前端：

```powershell
npm ci
npm run dev
```

另开终端，在项目根目录启动后端：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.service:app --host 127.0.0.1 --port 8000
```

开发服务器将 `/v1`、`/health` 代理至 `http://127.0.0.1:8000`。生产推荐使用同站 HTTPS 网关；跨站 `VITE_API_BASE_URL` 需要配套 CORS、HTTPS 与 Cookie 配置。不要把 API Key 或后端访问 token 写入任何前端变量。

## 验证与构建

前端统一 npm 与 package-lock.json，仅保留 vite.config.ts：

```powershell
npm run test:run
npm run build
```

## 部署

多阶段 Docker 镜像通过 Node 构建、Nginx 提供 SPA 与 API 代理。运行时要求同容器网络有可解析的 backend 服务；单独构建镜像不提供后端。推荐使用根目录 Compose 的 Caddy HTTPS 与认证入口，见[部署说明](../docs/deployment.md)。Nginx 提供 history fallback 与 `/healthz`。

## 页面与数据边界

- `/`、`/chat`、`/chat/:id`：会话工作区、Agent 执行与参数确认。
- `/dashboard`：多维筛选、共享统计与图表/数据表。
- `/analysis/:id`：证据化报告。
- `/history`：重定向至会话列表。
- `/about`：数据与分析限制。

conversationAdapter 负责会话与消息分页，analysisAdapter 负责运行、SSE 与报告，dashboardAdapter 负责共享统计。后端负责 system prompt、工具定义、消息历史、工具执行与统计；前端不重新计算价格或排序。

资源归属通过后端签名的 HttpOnly owner Cookie 隔离。清 Cookie、换设备或长期过期后无法通过原匿名身份恢复历史。展示内容是挂牌行情参考，不是成交估价、车辆鉴定或质量保证。
