# QualityFlow V1 证据矩阵

表中命令必须在所述依赖已准备的隔离环境运行。`GitHub Actions` 行的远程执行证据来自提交 `63d012f` 的 [run #2](https://github.com/liuyaohui666/Quality-flow/actions/runs/32006807495)，证据只适用于该提交及表中声明的学生规模边界。

| Claim | Implementation | Verification command | Evidence artifact | Limitation |
| --- | --- | --- | --- | --- |
| 逻辑幂等提交 | `RunService`、PostgreSQL 唯一键、API 冲突回读 | `python -m pytest tests/e2e/test_quality_flow.py::test_duplicate_submission_has_one_effective_attempt_and_terminal_event -q` | 两次 202、同一 run_id、单 Attempt/case/gate/terminal event | 不保证物理 exactly-once |
| Run/Outbox 原子受理 | UoW 中同时写 Run、RunEvent、Outbox | `python -m pytest tests/integration/test_run_persistence.py::test_commit_constraint_failure_rolls_back_run_event_and_outbox -q` | 约束失败后三表均无部分记录 | 依赖真实 PostgreSQL |
| Outbox 恢复 | `OutboxDispatcher`、publish attempts、未确认继续 pending | `python -m pytest tests/unit/test_dispatcher.py -q`；`python -m pytest tests/integration/test_worker_lifecycle.py::test_real_postgres_publish_failure_keeps_outbox_pending_and_counted -q` | 固定 event/run ID、失败后 pending Outbox 与递增 attempts、可见日志 | Redis 非权威；at-least-once |
| 重复 delivery 围栏 | 指定 run_id 条件领取、数据库 Run 状态与 lease | `python -m pytest tests/integration/test_worker_lifecycle.py -k concurrent_duplicate_delivery -q` | 一个有效 Attempt 和一份终态聚合 | 不能证明进程从未物理启动两次 |
| 租约过期恢复 | heartbeat CAS、lease token、`LeaseReconciler` | `python -m pytest tests/integration/test_worker_lifecycle.py -k reconciler -q` | abandoned Attempt、`infra_failed/unknown` Run/event | V1 不自动重试 |
| 超时分类 | `SafeSubprocessExecutor`、PytestRunner、进程树回收 | `python -m pytest tests/unit/test_subprocess_runner.py -k posix_timeout -q`；`python -m pytest tests/e2e -k slow -q` | `timed_out/unknown`、Linux child-reap 结果、无伪造 JUnit | 受控本地进程，不是恶意代码沙箱 |
| pytest 功能门禁 | JUnit parser、functional gate | `python -m pytest tests/unit/test_pytest_runner.py -q`；`python -m pytest tests/e2e -k registered_demo_scenarios -q` | ok/error case summary、Attempt、gate、JUnit 元数据 | API 只返回 case 汇总，不返回逐 CaseResult |
| Locust 性能门禁 | CSV parser、请求数/错误率/P95 policy | `python -m pytest tests/unit/test_locust_runner.py -q`；`python -m pytest tests/e2e -k registered_demo_scenarios -q` | metrics、performance gate、`p95_ms` reason | 单用户、本地靶场；无多节点压测 |
| Artifact 隔离 | staging 验证、`FileArtifactStore`、Attempt namespace | `python -m pytest tests/unit/test_artifacts.py -q`；`python -m pytest tests/e2e/test_quality_flow.py::test_artifacts_are_owned_by_distinct_attempts -q` | 不同 Artifact/Attempt ID、SHA-256、大小/MIME、API 无路径 | 本地 named volume；无下载/删除/GC |
| 注册套件信任边界 | Registry、参数白名单、固定 argv、`shell=False` | `python -m pytest tests/unit/test_suite_registry.py tests/unit/test_subprocess_runner.py -q` | 非法参数/路径/保留参数被拒 | 只隔离可信套件；不是恶意代码安全沙箱 |
| API 最小暴露面 | Pydantic response allowlist | `python -m pytest tests/unit/test_api_runs.py -q` | Run/event/Artifact 响应无 URI/path/任意 payload | 不提供 Artifact 文件下载接口 |
| Compose 隔离拓扑 | Dockerfile allowlist、8 服务、非 root、health gates、限额 tmpfs scratch | `python -m pytest tests/unit/test_compose_contract.py tests/unit/test_healthcheck.py -q`；`docker compose -p quality-flow-check config --quiet` | 服务/卷/端口/health 契约；同容器重启后 workspace/staging 为空 | 单主机；镜像未按 digest 锁定 |
| CI 退出码 | `scripts/ci_gate.py` | live stack 上分别执行 `demo-api / ok` 与 `demo-api / error` | `pass=0`、`expected_quality_failure=1` | 非零类别不能全部等同为质量失败 |
| 五场景闭环 | Demo Target、pytest/Locust Runner、API E2E | `$env:QUALITY_FLOW_API_URL='http://127.0.0.1:18000'; python -m pytest tests/e2e -q` | 五组终态、events、case/metrics、gate、Artifact 元数据 | 只验证确定性本地靶场 |
| CI 诊断保留安全 | evidence allowlist + `audit_ci_evidence.py` | `python -m pytest tests/unit/test_ci_evidence_audit.py -q` | safe bundle 通过；secret URL/header/canary/link 被拒且不回显 | 规则扫描不是通用秘密检测平台 |
| GitHub CI | SHA-pinned workflow：quality/integration/e2e | `python -m pytest tests/unit/test_delivery_contracts.py -q`；检查 [GitHub Actions run #2](https://github.com/liuyaohui666/Quality-flow/actions/runs/32006807495) | 三个 Job 全绿；三份经审计的 JUnit/Compose 日志 evidence artifact | 单次托管 Runner 证据不等于生产持续稳定性 |
