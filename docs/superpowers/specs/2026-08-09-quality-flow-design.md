# QualityFlow 第一版设计规格

## 1. 项目定位

QualityFlow 是一个面向小型测试团队的持续测试执行与质量门禁系统。它接收预注册的可信测试套件，异步执行测试，保存结构化结果与诊断产物，并向 CI 返回明确的通过、测试失败或基础设施失败结论。

项目的重点不是测试用例管理页面，也不是普通业务 CRUD，而是测试执行基础设施中的可靠性问题：任务如何不丢失、重复消息如何保持幂等、测试与平台故障如何区分、执行产物如何隔离、性能退化如何触发质量门禁。

第一版目标是学生可以完整理解、运行和维护的单机系统。它不声称达到任何公司的生产规模，但通过稳定的领域模型和可替换接口，为后续多 Worker、对象存储和容器级执行隔离保留演进边界。

## 2. 目标用户与核心场景

目标用户是拥有 pytest 自动化资产、希望统一执行和留存结果的小型测试团队。

第一版支持以下场景：

1. 用户通过 API 提交一个预注册测试套件，立即获得 `run_id`。
2. 相同 `Idempotency-Key` 的并发或重复提交只创建一个逻辑 Run。
3. 数据库事务先持久化 Run 与 Outbox，再由 Dispatcher 可靠投递到 Redis/Celery。
4. Worker 在独立 Attempt 工作目录中运行 pytest 或 Locust。
5. 系统区分测试断言失败、性能门禁失败、执行器错误和执行超时。
6. JUnit、Locust 指标、stdout、stderr 等产物按 Run/Attempt 隔离并登记元数据。
7. 用户可以查询运行状态、用例结果、状态事件、质量门禁和产物。
8. CI 脚本可以提交任务、轮询结果，并根据质量门禁返回退出码。
9. 本地受控靶场可以稳定制造成功、5xx、慢响应和性能退化。
10. Worker 失联后，Reconciler 将过期 Attempt 标记为 `ABANDONED`，Run 标记为基础设施失败，而不是永久停留在运行中。

## 3. 第一版非目标

第一版不实现：

- 任意 Git 仓库或任意 shell 命令执行；
- 不可信代码安全沙箱；
- 自动重试、任务取消、优先级和定时任务；
- 多租户、注册登录、SSO、RBAC 和审批流；
- 大型前端管理系统或低代码用例编排；
- Kubernetes、Kafka、Elasticsearch、ClickHouse；
- 多机分布式压测；
- AI 用例生成、失败归因或日志摘要；
- 自动接入现有 Restful Booker 与 SauceDemo 仓库；
- 高可用、灾备、Exactly Once 或海量并发承诺。

这些能力只有在核心闭环经过验证后才考虑。

## 4. 总体架构

第一版采用模块化单体控制面与独立执行进程：

```text
API / CI Client
      |
      v
FastAPI Control Plane
      |
      +--> PostgreSQL
      |      Run / Attempt / CaseResult / Metric
      |      Artifact / GateEvaluation / RunEvent / Outbox
      |
      v
Outbox Dispatcher --> Redis Broker --> Celery Worker
                                          |
                                          v
                                    Runner Adapter
                                    pytest / Locust
                                          |
                                          v
                               Local ArtifactStore Volume

Reconciler --> PostgreSQL stale lease scan
Demo Target --> deterministic success/error/slow endpoints
```

API、Dispatcher、Worker 与 Reconciler 共享同一 Python 代码库，但以独立进程运行。PostgreSQL 是唯一事实来源；Redis 只负责传递任务。

## 5. 技术选型及职责

| 技术 | 第一版职责 |
| --- | --- |
| Python 3.12 | 唯一主要开发语言 |
| FastAPI | 创建 Run、查询状态和健康检查 |
| SQLAlchemy + Alembic | 领域数据访问、约束和数据库迁移 |
| PostgreSQL | Run、Attempt、结果、状态事件和 Outbox 的事实来源 |
| Redis | Celery Broker，不保存权威运行状态 |
| Celery | 小团队规模的异步任务传递与 Worker 运行 |
| pytest + JUnit XML | 功能测试执行和结构化结果 |
| Locust CSV | 单机性能场景和质量阈值输入 |
| 本地 ArtifactStore | 保存日志、JUnit 和性能原始结果；数据库只保存 URI 和元数据 |
| Docker Compose | 一键启动 API、Dispatcher、Worker、Reconciler、PostgreSQL、Redis 和靶场 |
| GitHub Actions | 验证平台代码和完整端到端闭环 |

Celery 是任务运输实现，不拥有领域状态。未来替换 Broker 或执行后端时，Run、Attempt、Runner 和质量门禁语义不变。

## 6. 安全与信任边界

第一版只执行仓库内预注册、可信测试套件。

- API 只接受 `suite_id`、`Idempotency-Key` 和经过白名单校验的结构化参数。
- 套件命令由服务端 Suite Registry 生成，不接受原始命令。
- 子进程使用参数数组和 `shell=False`。
- 工作目录由平台生成并校验在固定根目录内。
- 子进程只继承白名单环境变量。
- 密码、Token、Authorization 和 Cookie 不写入 Git、数据库明文或日志。
- stdout、stderr 和 Artifact 设置大小上限。
- 子进程运行设置总超时；超时后终止整个进程组。
- 每个 Attempt 拥有独立工作目录和 Artifact 命名空间。

第一版的进程和目录隔离用于防止可信任务互相污染，不宣称能够防御恶意测试代码。

## 7. 领域模型

### 7.1 SuiteDefinition

SuiteDefinition 存放在版本控制内的 YAML 文件，不开放 CRUD API。字段包括：

- `suite_id`
- `runner_type`
- `working_directory`
- 固定命令模板
- 参数白名单
- 超时时间
- 环境变量白名单
- Artifact 规则
- 质量门禁策略
- 源码版本标识

创建 Run 时将解析后的执行规格和门禁规则保存为不可变快照。

### 7.2 Run

Run 表示用户发起的一次逻辑测试：

- UUID `run_id`
- `suite_id`
- 唯一 `idempotency_key`
- `status`
- `outcome`
- 不可变执行快照
- 创建、开始、结束时间
- 乐观锁版本号

Run 状态：

```text
QUEUED -> RUNNING -> COMPLETED
                  -> INFRA_FAILED
                  -> TIMED_OUT
```

Run Outcome：

```text
UNKNOWN | PASSED | FAILED
```

`COMPLETED + FAILED` 表示测试或质量门禁失败；`INFRA_FAILED + UNKNOWN` 表示平台没有得到可信测试结论。

### 7.3 RunAttempt

Attempt 表示一次物理执行：

- UUID `attempt_id`
- `run_id`
- 唯一 `attempt_no`
- `status`
- `worker_id`
- `lease_token`
- `heartbeat_at`
- `lease_expires_at`
- 退出码和失败原因
- 开始、结束时间

Attempt 状态：

```text
DISPATCHED -> RUNNING -> PASSED
                      -> TEST_FAILED
                      -> INFRA_FAILED
                      -> TIMED_OUT
                      -> ABANDONED
```

第一版不自动重试，但保留 Run/Attempt 分离，为后续恢复和人工重跑保留数据边界。

### 7.4 其他实体

- `CaseResult`：用例 node id、状态、耗时和失败摘要；
- `Metric`：性能指标名称、数值和单位；
- `Artifact`：类型、URI、SHA-256、大小、MIME 和所属 Attempt；
- `GateEvaluation`：门禁是否通过及结构化原因；
- `RunEvent`：追加式状态迁移审计记录；
- `OutboxEvent`：待投递、已投递和投递失败次数。

## 8. 一致性与可靠性语义

### 8.1 幂等提交

- API 要求 `Idempotency-Key`；
- PostgreSQL 对该字段建立唯一约束；
- 相同键重复提交返回已有 Run；
- 并发竞争由数据库唯一约束决定，而不是依赖 Redis 锁。

### 8.2 Transactional Outbox

创建 Run 与 OutboxEvent 在同一事务完成。Dispatcher 轮询未发送事件，向 Celery 投递成功后再标记 `SENT`。重复投递由 Worker 的数据库条件领取处理。

### 8.3 Worker 领取与租约

- Worker 只有在 Run 仍为 `QUEUED` 时才能原子领取；
- 领取时创建 Attempt，并把 Run 改为 `RUNNING`；
- Worker 在执行期间更新心跳和租约；
- Reconciler 将租约过期的 Attempt 标记为 `ABANDONED`，并把 Run 标记为 `INFRA_FAILED`；
- 终态不可被旧 Worker 覆盖。

第一版不承诺测试进程绝不物理执行两次，只保证重复消息不会产生第二份有效业务结果。

## 9. Runner 契约

Runner 对控制层暴露统一接口：

```text
run(execution_spec, workspace) -> RunnerOutcome
```

RunnerOutcome 包含：

- Runner 终态；
- 退出码；
- 开始和结束时间；
- 用例结果；
- 性能指标；
- 产物清单；
- 失败分类和摘要。

### 9.1 PytestRunner

- 使用 `python -m pytest` 与固定参数数组；
- 强制生成 JUnit XML；
- 退出码 0 表示测试执行完成且用例通过；
- 退出码 1 表示测试执行完成但存在失败；
- 其他无法解析结果的退出码视为基础设施失败；
- JUnit 缺失或无法解析时不能标记为成功。

### 9.2 LocustRunner

- 仅支持固定并发和持续时间的单机 headless 模式；
- 强制输出 CSV；
- 解析请求数、失败率、吞吐量、平均响应时间和 P95；
- 根据 SuiteDefinition 中的 P95 与错误率阈值判定门禁；
- 性能任务在第一版限制为单并发执行；
- 不对任何公开第三方服务进行压测。

## 10. ArtifactStore

业务代码只依赖：

```text
put(source_path, artifact_metadata) -> artifact_uri
open(artifact_uri)
delete(artifact_uri)
```

第一版实现本地文件系统：

- 路径由平台根据 Run/Attempt 生成；
- 先写临时文件，完成后原子替换；
- 保存 SHA-256、大小和 MIME；
- 下载接口只接受 Artifact ID，不接受任意磁盘路径；
- 设置单文件和单 Run 容量上限；
- 后续可替换为 MinIO/S3。

## 11. 质量门禁

第一版支持：

### 功能测试门禁

- 最低通过率；
- 最大失败数；
- 测试错误视为失败；
- JUnit 缺失视为基础设施失败而不是门禁失败。

### 性能测试门禁

- 最大错误率；
- 最大 P95 响应时间；
- 最少请求数，避免空结果通过。

门禁结果写入 GateEvaluation。CI Client 对 `PASSED` 返回 0，对 `FAILED`、`INFRA_FAILED` 和 `TIMED_OUT` 返回非零退出码。

## 12. 可观测性

第一版采用 JSON 结构化日志。所有关键日志包含：

- `run_id`
- `attempt_id`
- `suite_id`
- `worker_id`
- `component`

健康检查区分：

- `/health/live`：进程是否存活；
- `/health/ready`：PostgreSQL、Redis 和 Suite Registry 是否可用。

第一版记录队列等待时间、执行耗时、结果数量、重复提交、Outbox 投递失败、过期租约和 Artifact 错误。Prometheus/Grafana 不进入第一版，指标接口在后续加入。

## 13. 平台自身测试策略

### 单元测试

- 状态迁移合法性；
- Suite Registry 参数校验；
- JUnit 与 Locust 结果解析；
- 功能和性能门禁；
- Artifact 安全路径与哈希；
- 日志脱敏。

### 集成测试

- PostgreSQL 唯一约束和事务回滚；
- 相同 Idempotency-Key 并发提交；
- Run 与 Outbox 同事务；
- Redis 暂时不可用后 Outbox 恢复；
- Worker 条件领取和重复消息；
- 租约过期与 Reconciler；
- 结果、门禁和 Artifact 元数据一致性。

### 端到端测试

在 Docker Compose 中验证：

1. 成功 pytest Run；
2. 断言失败 Run；
3. 执行超时 Run；
4. Locust 正常基线通过；
5. Locust 性能退化被门禁拦截；
6. 相同 Idempotency-Key 返回同一 Run；
7. 给定 run_id 能查询结果、事件和产物。

## 14. 第一版验收标准

第一版完成必须同时满足：

1. 新电脑只需 Git 与 Docker 即可按 README 启动；
2. 数据库迁移自动执行；
3. API 提交测试后快速返回 `202` 与 `run_id`；
4. Worker 异步执行，API 请求不等待测试结束；
5. 重复提交不创建第二个 Run；
6. PostgreSQL 是最终状态来源；
7. 成功、测试失败、基础设施失败和超时可区分；
8. pytest 与 Locust 至少各有一条通过和一条失败演示；
9. 日志和产物按 Run/Attempt 隔离；
10. CI Client 能以退出码表达质量结论；
11. 核心单元、集成和端到端测试通过；
12. README 能解释架构、执行流、故障边界、验证命令和已知限制；
13. 不包含虚构的生产规模、性能收益或缺陷数量。

## 15. 演进边界

第一版稳定后，可按实际需求演进：

1. 对象存储实现；
2. 每 Attempt 独立容器；
3. 多 Worker 能力路由；
4. 定时任务、Webhook 和极简页面；
5. Flaky 历史分析；
6. OpenAPI 契约 Runner；
7. 接入现有 API/UI 自动化仓库；
8. Kubernetes Job 执行后端；
9. SSO、RBAC 和资源配额；
10. OpenTelemetry 和集中日志。

核心 Run、Attempt、Runner、Artifact、结果协议和质量门禁语义保持不变。

## 16. 简历声明边界

只有经过自动化验证的能力才能进入简历。第一版完成后可以描述为：

> 设计并实现 Python 持续测试执行与质量门禁系统，使用 PostgreSQL 与 Redis/Celery构建异步任务链路，通过幂等键、Transactional Outbox、Run/Attempt 状态模型和租约识别处理重复提交与 Worker 失联；统一解析 pytest 与 Locust 结果，归档日志与测试产物，并将功能通过率、错误率和 P95 阈值转化为 CI 质量门禁。

不能声称生产级分布式平台、Exactly Once、高可用、任意代码安全沙箱或已在真实企业生产落地。
