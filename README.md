# carwise

二手车智能分析 Agent：基于清洗后的车源数据提供行情查询、价格比较、区域差异、价格衰减和预算推荐，并支持交互式分析看板。

## 技术栈

- 后端：Python 3.12、FastAPI、pandas、PyArrow、SQLite
- 前端：Vue 3、TypeScript、Vite
- 测试：pytest、Vitest

## 安装与数据准备

需要 Git、Node.js 22+、Python 3.12+。克隆并安装依赖：

```sh
git clone https://github.com/F1nSand/carwise.git
cd carwise
npm run setup
```

车源原始数据不随项目分发。请自行准备有权使用且兼容本项目格式的 CSV，并放到 `config.yaml` 中 `paths.raw_csv` 指定的位置。然后在项目根目录运行清洗流程：

```powershell
.venv\Scripts\python.exe etl/clean_dcd.py --stage all
```

Linux/macOS 的虚拟环境解释器路径为 `.venv/bin/python`，相应命令是：

```sh
.venv/bin/python etl/clean_dcd.py --stage all
```

也可以自行准备符合应用数据格式的 `config.yaml` 所指定的 parquet 文件 `data/clean/dcd_clean.parquet`。没有可用数据文件时，应用无法提供车源分析。

## 密钥与启动

`npm run setup` 会在根目录创建 `.env`（若文件尚不存在）。编辑它并设置你的模型 API 密钥：

```dotenv
DEEPSEEK_API_KEY=你的API密钥
```

然后启动前后端：

```sh
npm run dev
```

打开前端 <http://127.0.0.1:5173>；后端 API 位于 <http://127.0.0.1:8000>，健康检查为 <http://127.0.0.1:8000/health>。不要提交 `.env` 或将密钥放入前端配置。

## 测试

CI 后端 job 只运行不依赖本地车源文件和分析基线的自包含测试。本地完整 `pytest` 与 trajectory 评估需要准备真实兼容数据；部分分析回归还依赖 `artifacts/analysis-validation/baseline.json`，该基线需自行生成。

```sh
.venv/bin/python -m pytest -q
cd frontend
npm run test:run
npm run build
```

Windows 后端测试请将 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe`。

## 部署

生产部署、HTTPS 网关、身份密钥和数据持久化配置见[部署说明](docs/deployment.md)。
