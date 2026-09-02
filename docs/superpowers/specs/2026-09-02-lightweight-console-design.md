# QualityFlow 轻量控制台与结果访问设计

## 目标

为现有 QualityFlow 增加一个不依赖 Node.js 构建链的轻量 Web 控制台，让测试人员不再通过长 Docker、PowerShell 或 HTTP 命令完成日常操作。初版覆盖以下闭环：

1. 用统一短命令启动平台并自动打开控制台；
2. 在页面选择已注册套件和白名单参数并创建 Run；
3. 查看 Run 历史、状态、Attempt、用例、指标、门禁和事件；
4. 在线查看或下载平台归档的 stdout、stderr、JUnit XML 和 Locust CSV；
5. 用统一短命令查看服务状态、汇总日志和停止平台。

本功能改善可用性与结果可见性，但不改变 QualityFlow 的执行、可靠投递、重试或质量判定语义。

## 范围边界

### 初版包含

- 以 Run 为中心的单页控制台；
- 安全的套件目录、Run 列表、用例结果和 Artifact 内容接口；
- 运行中 Run 的定时轮询；
- Windows PowerShell 统一启动入口；
- Docker Compose 中 API 对 Artifact 卷的只读挂载；
- API、Compose、启动脚本和浏览器主路径的自动化验证；
- README 使用说明。

### 初版不包含

- 用户登录、RBAC 和多租户；
- WebSocket/SSE；
- 任意宿主机路径浏览；
- 直接访问 PostgreSQL 或 Docker 卷；
- 手动重试、取消、删除、定时执行；
- 图形化测试编排、跨 Run DAG；
- React/Vue/Node.js 工程；
- 完整 Allure 替代品。

## 用户体验

测试人员在 Windows 项目目录运行：

```powershell
.\qualityflow.ps1 start
```

脚本检查 Docker CLI 与 Docker Engine，调用固定 Compose 项目名启动服务，等待 `/health/ready`，然后打开 `http://127.0.0.1:18000/ui/`。页面后续承担全部日常操作。

其他维护命令：

```powershell
.\qualityflow.ps1 status
.\qualityflow.ps1 logs
.\qualityflow.ps1 stop
```

`stop` 只停止服务并保留 PostgreSQL、Redis 和 Artifact 卷；初版不提供删除卷的快捷命令。

## 页面结构

单页控制台使用左侧导航与右侧内容区：

- **运行记录**：默认页，列出最近 Run，可按状态和套件筛选；
- **创建测试**：从套件目录生成表单，参数只能从服务端返回的白名单中选择；
- **Run 详情**：展示状态、Outcome、时间、Attempt、用例、指标、门禁、事件和 Artifact；
- **服务状态**：展示 API 当前 live/ready 状态。

页面每两秒轮询一次处于 `queued` 或 `running` 的 Run。到达终态、离开详情页或浏览器标签隐藏后停止或降低轮询，避免无意义请求。

前端不拼接 Docker 路径、不读取数据库，也不接受任意命令。创建 Run 时由浏览器生成新的 UUID 作为 `Idempotency-Key`；用户重复点击提交时禁用按钮，避免同一次操作发出多个逻辑请求。

## API 设计

### `GET /api/v1/suites`

返回可公开的套件目录：

- `suite_id`
- `runner_type`
- `allowed_parameters`

不返回 `working_directory`、`argv`、环境变量或内部路径。

### `GET /api/v1/runs`

返回最近 Run，默认 50 条，最大 100 条，按 `created_at`、`run_id` 倒序。初版支持可选的 `status` 和 `suite_id` 精确筛选。列表只返回页面需要的摘要：Run ID、套件、状态、Outcome、创建/开始/结束时间和最新 Attempt 编号。

### `GET /api/v1/runs/{run_id}/cases`

返回最新 Attempt 的逐用例结果：

- `case_result_id`
- `attempt_id`
- `node_id`
- `status`
- `duration_ms`
- `message`
- `created_at`

初版不公开任意 `details` 字典，以避免意外暴露请求体、令牌或内部路径。

### `GET /api/v1/runs/{run_id}/artifacts/{artifact_id}/content`

按 Run ID 和 Artifact ID 查数据库，确认归属关系后，使用数据库保存的服务端 URI 调用 `FileArtifactStore.resolve()`。客户端永远不能提交 URI 或文件路径。

响应使用数据库保存的 MIME，并提供由 `artifact_type` 映射出的安全文件名。查询不到、Artifact 不属于该 Run、文件丢失或路径校验失败时统一返回不泄露内部路径的 `404`。

默认 `Content-Disposition: inline`，传入 `?download=true` 时使用 `attachment`。浏览器可直接显示文本、XML 与 CSV，也可以下载。

### `GET /ui/`

返回控制台 HTML；静态 CSS 和 JavaScript 从 `/ui/assets/` 提供。根路径 `/` 重定向至 `/ui/`，OpenAPI 文档仍保留 `/docs`。

## 后端结构

现有 `RunReader` 扩展为面向查询的只读接口，提供：

- `list_runs(...)`
- `get_run(run_id)`
- `get_cases(run_id)`
- `get_artifact(run_id, artifact_id)`

`ApiDependencies` 继续承载依赖，但新增安全 Artifact 解析能力与套件目录读取能力。所有 SQLAlchemy 查询各自创建短生命周期 Session，不复用可变 Session。

前端资产放在 `src/quality_flow/api/static/`，由现有 FastAPI 容器直接提供，不增加第九个 Compose 服务，也不新增 npm 依赖。

## Artifact 安全边界

- API 容器只读挂载 `quality-flow-artifacts:/runtime/artifacts:ro`；
- Worker 保持读写挂载；
- Artifact 必须先通过数据库里的 `(run_id, artifact_id)` 归属查询；
- 只使用数据库中的不透明 URI，不接受客户端路径；
- 继续使用 `FileArtifactStore.resolve()` 的 UUID、目录边界、普通文件和链接校验；
- 错误响应与日志不暴露真实磁盘路径；
- 初版只允许平台已保存的 Artifact 类型，不提供目录列表或通用文件服务。

## 错误处理

- API 未就绪：页面显示“平台未就绪”及重试按钮，不显示虚假空列表；
- 创建参数不合法：展示后端返回的可读错误，不自动修改参数；
- Run 不存在：详情页显示 404 状态并返回列表；
- Artifact 数据库记录存在但文件丢失：返回安全 404，页面标记“文件不可用”；
- 轮询临时失败：保留上一次已知状态并采用有限退避，不把 Run 标成失败；
- 启动脚本失败：返回非零退出码并给出下一步诊断命令，不执行全局 prune 或删除卷。

## 验证策略

### 单元测试

- 套件接口不泄露 argv 与路径；
- Run 列表排序、限制和筛选；
- 用例接口只返回最新 Attempt 且隐藏 details；
- Artifact 必须属于目标 Run；
- 下载接口拒绝未知 ID、缺失文件与不安全 URI；
- UI 路由和静态资源可访问；
- PowerShell 脚本只有固定 Compose 项目范围，`stop` 不带 `-v`，且没有 prune。

### 集成与端到端

- API 通过真实 PostgreSQL 读取 Run 历史和逐用例结果；
- Worker 写入 Artifact 后，API 只读挂载可以下载相同字节；
- 通过页面所使用的 API 创建 `demo-api` Run，轮询到终态并读取 case、gate、event 和 Artifact；
- 现有 quality、integration、e2e 工作流继续通过。

### 界面验证

- 桌面宽屏和较窄窗口均可使用；
- 键盘焦点、颜色对比、加载/空/错误状态明确；
- 浏览器中实际完成一次 `demo-api / ok` 的提交和结果查看；
- 页面不要求用户理解 Docker、Celery 或内部文件路径。

## 成功标准

一名只知道套件业务含义的测试人员，可以只运行一次短启动命令，随后完全通过网页完成：选择 `demo-api / ok`、提交、观察状态变化、确认用例与门禁通过、查看或下载 stdout/JUnit，并安全停止平台。整个过程中不需要手写 Docker Compose、HTTP 请求或卷导出命令。
