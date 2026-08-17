# Restful Booker Suite Integration Design

## Goal

将独立仓库 `restful-booker-api-automation` 中已经验证的 API Client、pytest fixture、YAML 数据、断言、日志和 CRUD 用例，作为一个预注册可信 pytest 套件接入 QualityFlow。QualityFlow 负责受理、投递、隔离执行、JUnit 解析、质量门禁和 Artifact 归档；公开 Restful Booker 服务继续作为独立被测系统运行。

## Scope and provenance

- 套件 ID 固定为 `restful-booker-api`。
- 选择性复用原仓库已提交 revision `387568c`，不读取或覆盖 `D:\New_project` 的未提交修改。
- 只接入运行所需的 Client、fixture、配置、数据、公共工具、CRUD 用例和离线单测。
- 不复制原仓库的 Dockerfile、GitHub Actions、README、Allure 结果、日志、缓存、编辑器配置和本地脚本。
- 不把 Restful Booker 后端源码放入 QualityFlow；测试通过 HTTPS 调用公开部署。

## Layout and registration

套件放入 `demo_suites/restful_booker/`，复用现有 Dockerfile 对 `demo_suites` 的运行时复制规则。`config/suites.yaml` 注册：

- `runner_type: pytest`
- `working_directory: demo_suites/restful_booker`
- 固定 argv：`python -m pytest tests/test_booking_crud.py -q --strict-markers`
- `timeout_seconds: 120`
- `allowed_parameters: {}`
- 功能门禁：`min_pass_rate: 1.0`、`max_failures: 0`
- `source_revision: restful-booker@387568c`

客户端通过 `POST /api/v1/runs` 提交 `suite_id=restful-booker-api` 和空 `parameters`。V1 不使用普通参数传递账号、密码或任意 base URL，因为参数会进入数据库快照，且任意 URL 会扩大 SSRF 与秘密暴露边界。

## Runtime behavior

Worker 从不可变 Run snapshot 读取套件定义，把套件复制到 Attempt 专属 workspace，再由 `PytestRunner` 追加受控的 `--capture=no` 和 `--junitxml`。平台解析 JUnit 为 case summary 和 gate，保存 stdout、stderr、JUnit 与终态聚合，最后清理临时 workspace。

独立 Restful Booker 项目继续保留 Allure。接入 QualityFlow 的副本移除 Allure 装饰器和附件调用，避免为平台中不会被持久化的第二套报告链路增加依赖。请求/响应证据由结构化断言、stderr 日志和 JUnit 提供。

## Security and failure handling

- 请求日志继续对 `password` 等敏感字段递归脱敏。
- 鉴权响应中的 `token`、`cookie`、`authorization` 和 `password` 必须在日志输出前递归脱敏；离线测试证明原值不会进入日志。
- 平台 clean environment 不放行 `RESTFUL_BOOKER_USERNAME/PASSWORD`，接入版只使用公开演示账号默认值。未来如需私有环境，必须单独设计不落库的 Secret 注入通道。
- 公开服务的 DNS、网络、限流、共享数据或服务故障会反映为该 Run 的测试/基础设施结论；不会自动重试，也不会伪装成平台成功。

## CI strategy

push/PR 的必跑 `quality` Job 增加套件离线单测和注册/Runner 契约验证，证明代码可导入、请求封装正确、配置可解析、token 被脱敏、JUnit/gate/Artifact 契约成立。Docker integration/e2e 继续只依赖确定性的本地 `demo_target`。

公开 Restful Booker CRUD 不进入必跑 CI，不影响平台代码的绿色门禁。合并前单独执行一次真实公网 CRUD，并通过 QualityFlow Runner 做一次真实套件执行，记录它与核心 CI 证据的边界。

## Acceptance criteria

1. `restful-booker-api` 能被 Registry 加载，拒绝未知参数。
2. 离线套件单测经真实 `PytestRunner` 执行后为 `PASSED`，产生可信 JUnit、stdout、stderr 和通过 gate。
3. 鉴权 token 不进入日志或持久化测试证据。
4. 公网 CRUD 在验证时七个 case 全部通过；若公网不可用，明确记录外部阻塞，不放宽断言。
5. 现有 QualityFlow unit/integration/e2e 无回归。
6. GitHub Actions 最新 main revision 的 quality/integration/e2e 三个 Job 全绿。

## Non-goals

- 不本地部署 Restful Booker 后端。
- 不为公网失败增加盲目重试或 `continue-on-error`。
- 不扩展 `ci_gate.py` 的通用参数 CLI。
- 不新增 Allure 平台、Artifact 下载接口、凭据管理系统或任意 URL 执行能力。
