# QualityFlow 可编辑业务请求体控制台设计

## 目标

在现有 Run 控制台上补齐“测试工具”需要的输入能力：测试人员先选择测试类型和已注册套件，再自行编辑该套件允许的业务 JSON 请求体，通过页面按钮完成校验、提交、查询、刷新、查看与下载证据。首个真实接入对象是 `restful-booker-api`。

## 核心区分

- **测试类型**回答“测什么”：接口功能测试或性能测试。
- **套件**回答“由哪组可信测试代码执行”：`demo-api`、`demo-load`、`restful-booker-api`。
- **运行参数**回答“怎么跑”：例如 `scenario=ok`，仍使用服务端白名单下拉框。
- **业务请求体**回答“拿什么数据测”：使用可编辑 JSON，不再用有限下拉框枚举业务字段。

平台仍只执行预注册套件，不允许用户输入命令、脚本路径或任意目标 URL。

## 套件契约

`config/suites.yaml` 为每个套件声明：

- `test_type`: `api` 或 `performance`；
- 可选 `request_body`：包含 `required`、标准 JSON Schema 和安全示例。

`GET /api/v1/suites` 公开测试类型、参数白名单以及请求体的 schema/example，但继续隐藏 argv、工作目录和环境变量。没有 `request_body` 声明的套件不会显示 JSON 编辑器。

首版只有 `restful-booker-api` 声明业务请求体。它接受 Restful Booker 创建 Booking 所需字段，并拒绝缺字段、类型错误、额外字段、超长内容和非对象 JSON。为兼容已有 CI/调用方，该请求体在 API 层允许省略；页面选择该套件时自动填入示例，正常页面提交会携带用户编辑后的内容。

## 数据与执行链路

```text
页面选择 test_type 和 suite
  -> 编辑并本地解析 JSON
  -> POST /api/v1/runs
  -> Pydantic 校验外层结构
  -> SuiteRegistry 按该套件 JSON Schema 校验业务请求体
  -> PostgreSQL 将 request_body 与 Run 一起事务保存
  -> Outbox / Redis / Celery 保持原有异步投递
  -> RunWorker 读取 request_body
  -> Runner 以 QUALITY_FLOW_REQUEST_BODY_JSON 注入可信套件子进程
  -> Restful Booker fixture 使用它覆盖 valid_booking
  -> 原有 pytest 用例执行并归档结果
```

新增 `runs.request_body` JSON 可空列，并使用 Alembic 迁移。Run 详情 API 返回该请求体，便于回看本次到底使用了什么数据；页面以格式化只读 JSON 展示。由于它可能含测试数据，控制台明确提醒不要填写真实密码或生产隐私数据。

## 页面交互

创建页调整为：

1. 选择测试类型；
2. 选择该类型下的注册套件；
3. 选择有限运行参数；
4. 对支持的套件编辑业务 JSON；
5. 点击“校验 JSON”获得即时结果；
6. 点击“提交 Run”调用创建接口。

页面提供“加载示例”和“格式化”按钮。提交前必须再次解析 JSON；服务端仍是最终可信校验者。现有“运行记录、刷新、查看详情、服务检查、查看文件、下载文件”按钮继续分别封装查询类 API，不要求用户手写 URL、Header 或 Docker 命令。

## 安全与边界

- 业务请求体最大 64 KiB；
- 仅接受 JSON object；
- 仅接受套件注册时声明的 schema；
- JSON 使用紧凑序列化后放入子进程环境，不参与命令拼接；
- `subprocess` 保持 `shell=False`；
- 请求体不写入 Outbox 或 Redis，队列仍只传标识符；
- API 保持仅绑定本机回环地址；
- 不支持任意接口调试、任意 URL、Header/Token 编辑、脚本上传或动态代码执行。

## 错误处理

- JSON 语法错误：页面阻止提交并定位为格式问题；
- schema 不匹配：API 返回 422，错误包含字段路径，不包含内部路径；
- 套件不接受请求体却收到请求体：返回 422；
- 请求体过大：返回 422；
- Worker 读取到非法快照：按现有 `worker_setup` 基础设施失败处理；
- Restful Booker 未传请求体：继续使用版本库内 YAML 默认数据，保持向后兼容。

## 验证标准

- Registry 能解析、冻结并校验请求体契约；
- 创建 Run 时合法 payload 被保存，非法 payload 不产生 Run/Outbox；
- Worker 将 JSON 原样、安全地注入子进程；
- Restful Booker fixture 能读取自定义 payload，并在未提供时回退 YAML；
- 页面能按测试类型过滤套件，编辑、格式化、校验和提交 JSON；
- Run 详情能回显本次请求体；
- 原有单元、集成、E2E 与 Compose 路径继续通过；
- 真实浏览器完成一次自定义 Restful Booker payload 的提交和结果查看（公网可用时）；若公网不稳定，则至少使用本地确定性测试证明数据端到端传递。

