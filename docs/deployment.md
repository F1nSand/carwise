# 单节点生产部署

要求 Docker Compose、可解析到服务器的域名、可访问的 80/443 端口和足够的磁盘/内存。此配置保持单节点 SQLite；不要把 runtime 数据库置于网络共享文件系统。

构建镜像前，须按根目录 README 准备有权使用的真实数据文件 `data/clean/dcd_clean.parquet`。仓库不分发车源数据；CI 使用的空文件仅用于镜像构建检查，不能用于业务分析。

## 准备配置

从 `.env.example` 创建服务器 `.env`。填写模型密钥与随机 `OWNER_COOKIE_SECRET`，再添加以下部署字段：

```dotenv
APP_DOMAIN=analysis.example.com
APP_USERNAME=analyst
APP_PASSWORD_HASH='$2a$...由下面命令生成的完整哈希...'
```

交互生成网关密码哈希，密码不会出现在命令参数里：

```sh
docker run --rm -it caddy:2-alpine caddy hash-password
```

哈希含 `$`，在 `.env` 中使用单引号保留原值。用户通过浏览器网关认证后访问应用；后端与前端服务不绑定宿主端口。Caddy 自动申请与续期 HTTPS 证书，并将 HTTP 重定向至 HTTPS。首次签发要求域名、DNS 和端口已就绪。

可选 `APP_PUBLIC_ACCESS_TOKEN` 用于后端 API 额外校验：网关认证通过后在内部请求中注入该 Bearer token，前端无需存储秘密。直连 API 客户端也须携带该 token。不要把此配置写入任何 `VITE_*` 变量。

```sh
docker compose -f docker-compose.cloud.yml up --build -d
docker compose -f docker-compose.cloud.yml ps
```

默认入口为 `https://你的域名/`。确认登录后创建会话、刷新页面、读取同一会话与报告；Cookie 必须包含 Secure/HttpOnly/SameSite=Lax。后端 Docker 健康检查在内部运行，网关凭据不会影响它。

## 接单与 token 额度

Compose 默认最多 128 个未结束任务、每个匿名 owner 最多 16 个，包含 queued/running/waiting_user；每日 token 核算上限默认 10,000,000。分别通过 `RUNTIME_MAX_PENDING_GLOBAL`、`RUNTIME_MAX_PENDING_PER_OWNER`、`RUNTIME_DAILY_TOKEN_LIMIT` 调整；0 表示关闭该项。超限返回 429，幂等重放仍返回原接受结果，不重复扣额。网关认证与全局上限防止单靠重建匿名身份绕开保护。

每个新逻辑任务按配置允许的最大模型调用次数 ×（最大输入 + 最大输出）保守预留。当前配置为 12 × (524,288 + 65,536) = 7,077,888 token；因此默认每日额度允许一个这样的最大预算任务占用名额，正常完成后释放未使用余额。希望更多任务并发时，应结合上下文预算和模型调用上限设置日额度，不只增加 worker 数。

日额度按任务接受日（UTC）记账；暂停、续接与同键重放不会刷新额度。retry 是新任务；worker_lost 恢复只对后续剩余调用预留，但丢失进程无法证明的用量按原预留上界计入扣额，恢复完成不会退还这部分。它是保守的服务准入核算，不是供应商账单金额保证。

SSE 本进程通知即时唤醒；通知丢失或跨进程时默认每 2 秒轻量检查状态游标，可用 `RUNTIME_SSE_POLL_INTERVAL_SECONDS` 调整。只读数据快照缓存默认上限 512 MiB 的 DataFrame deep 估算值，可用 `DATASET_SNAPSHOT_CACHE_MAX_BYTES` 调整；该上限不等于进程总 RSS。

## 数据库与身份配置

进程环境变量优先于本地 `.env`。默认数据库为 `.cache/runtime.sqlite3`；`RUNTIME_DATABASE_PATH` 可指定绝对路径，或相对项目根目录的路径。容器自定义路径必须处于持久化卷内。

entrypoint 从与 API 相同的 Python 配置读取数据库路径，迁移标记保存在该数据库旁。切换数据库会使用不同标记，数据库不存在时旧标记失效。迁移导入本身幂等。迁移前停止旧 writer，并备份 SQLite 数据库、WAL/SHM 或使用 SQLite 在线备份接口；不要仅复制活跃数据库文件。

不要执行 `docker compose down -v` 来升级，该命令会删除持久卷。升级前备份，完成后再次检查历史会话、数据版本及 owner 连续性。

## 依赖与验证

后端在线安装锁定的运行依赖，无需开发机私有 wheelhouse；镜像包含四个项目 Skills。CI 构建前后端镜像并验证 Skills 可发现和加载。Python 运行与测试依赖分开，前端统一 npm ci、Node 22。

本地没有 Docker 时，可完成 pytest/Vitest/build 与 Compose 静态检查，但这些不等于实际镜像、TLS签发与公网访问验收；应在具备 Docker 的 CI/服务器完成上述步骤。
