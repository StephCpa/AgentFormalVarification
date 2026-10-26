# 受限运行时 API 与凭据绑定

schema v9 为通过路由/输入检查且凭据认证成功的请求增加[持久拒绝记录](durable-rejection-profile.md)。业务 Reject 在 savepoint 内回滚，事件与配额在同一 guard 下提交；审计容量或存储失败返回 503，不宣称原始拒绝已记录。未经认证及解析阶段错误仍不在此范围。

依据详细设计第 19 章，PostgreSQL schema v5 增加服务端凭据、冻结 checked_ref 和 API 请求防重。当前 HTTP 服务只允许绑定 `127.0.0.1`，用于独立进程集成验证；未作为面向网络的生产服务器发布。

## 身份与引用

宿主通过 `GatewayAuthority.issue(subject, scopes, expires)` 签发 256 位随机不透明 bearer token。数据库只保存带域分离的 SHA-256 摘要，以及独立 credential ID、租户、主体、作用域、过期时点和撤销标志。明文仅在签发时返回宿主；不能从 token 正文自报租户或权限。

服务端先用摘要定位租户，再取得该租户 authority guard，并在同一业务事务中复检凭据未撤销、未过期、包含所需作用域。撤销也取得该 guard；已查到凭据但尚未进入事务的竞态不能绕过复检。撤销后的同请求幂等重试仍拒绝，不会先返回缓存。凭据撤销不释放已经预留或在途的预算，也不等同于撤销全部业务能力。

`GatewayAuthority.register_checked(request, evidence_ref, expires)` 是宿主入口，要求请求与既有可信 evidence 摘要一致、租户一致、有效期不超过 evidence。服务端以不可修改的 checked_request 保存完整请求和主体；此动作本身不授予退款权限。schema v6 增加公开 candidates/validate 入口，依据宿主登记的订单快照签发固定资格命题，见 [候选验证规格](candidate-validation-profile.md)。真实上游身份和通用验证器接入仍待完成。

Prepare 只能传 checked_ref、capability_ref、一个 approval_ref、权威业务键和客户端请求键。金额、目的地、tenant_id、subject、服务端状态等额外字段一律拒绝。后续 Dispatch 只接受原 permit_ref 与请求键，不接受覆盖参数。

## 当前 HTTP 表面

|接口|用途|成功响应|
|---|---|---|
|POST /v1/candidates/validate|依据有权访问的快照校验退款候选；需 validate scope|200，checked_ref 与 evidence_refs；无授权|
|POST /v1/operations/prepare|冻结 checked_ref 对应请求的授权和预留|200，操作与不透明 permit_ref|
|POST /v1/operations/{id}/dispatch|准入并提交 outbox；不直接调用远端|202，当前操作投影|
|GET /v1/operations/{id}|只查询凭据主体拥有的操作|200，结果/结算/预留/义务引用|
|POST /v1/operations/{id}/reconcile|持久请求核对；Dispatched 转 Unknown 后保留预留|202，不承诺已获终局|
|POST /v1/operations/{id}/cancel|仅支持 cancel_kind=before_dispatch|200，准入前取消；已准入拒绝释放|
|POST /v1/tasks/{task_ref}/close|需 close scope；按宿主冻结范围判断任务完成|200，当前任务评估；详见 [任务关闭](task-close-profile.md)|
|GET /v1/tasks/{task_ref}|需 get scope；只访问本主体任务|200，最新评估、结算与义务计数|

管理面政策/权限撤销、凭据签发、Observation、adapter.send、delegation 和任意执行入口均未挂载。GET 不返回许可、原始请求、批准、签名键或数据库连接信息。跨主体、跨租户与不存在的操作返回相同 OPERATION_NOT_FOUND，不通过错误消息泄露对象存在性。

准备请求形状为：

```json
{
  "client_request_id": "prepare-001",
  "checked_ref": "checked-<host-issued-reference>",
  "capability_ref": "cap-<host-issued-reference>",
  "approval_refs": ["approval-<host-issued-reference>"],
  "business_action_key": "approved-refund-case"
}
```

变更请求以租户、主体、端点和 client_request_id 防重，规范化正文与路径共同进入摘要。同键不同输入拒绝，同输入返回已有操作的当前投影。防重记录与操作转移同事务提交，服务进程退出不会创建第二次预留或准入。

错误封套包含服务端 trace_id，以及 code、request_id、operation_ref、retry_advice、requirement_ids、detail_ref。未授权错误不返回原始异常、SQL 或请求对象。401 表示凭据无效，403 表示作用域/授权拒绝，404 表示不可访问，409 表示状态/绑定/额度冲突，422 表示不支持的保证，503 表示依赖不可用。客户端不能把 202 当业务成功，也不能把 5xx 当远端无效果。

## 传输和启动

入口为 `scripts/runtime_api.py --config PRIVATE_CONFIG --ready-file PRIVATE_READY_FILE`。配置包含 dsn、schema、clock_file，放在被忽略的 `.runtime`，不写入证据。clock_file 为宿主整数逻辑测试时钟，例如 `{"now":0}`；ready 文件只包含回环地址、随机端口和实际 PID，并原子发布。数据库需先迁移及宿主初始化。集成测试负责启动、终止并核对实际服务进程。

固定路由只接受 GET/POST，POST 要求 application/json；正文最多 64 KiB，拒绝重复 JSON 键、数字编码、重复关键 HTTP 头和 Transfer-Encoding。Host 必须匹配实际回环地址与端口，带 Origin 的请求被拒绝；不配置 CORS、cookie 身份或 query-token。响应为 no-store，禁止嗅探。日志不记录请求正文或 Authorization。

传输采用本地标准库测试服务器，没有 TLS、生产反向代理信任配置、连接级速率限制、连接并发硬配额或远程监听选项。正式身份接入、密钥保管、真实 UTC/可信时间区间和服务加固仍需实现。当前整数序号/金额使用十进制字符串；尚未完成设计要求的完整 UTC 时间网络协议。

## 验证边界

真实 HTTP 子进程测试覆盖缺失/伪造/过期/撤销凭据、scope、客户端身份/参数覆盖、checked_ref 跨主体、跨租户读取、幂等输入冲突、伪造许可、核对请求、准入后取消拒绝、管理/适配器路由不可达、协议形状拒绝、容量门禁和 API 进程重启。另以真实数据库在摘要定位后、事务前插入撤销，验证事务内复检。

首轮手动测试因测试客户端错误使用 HTTPConnection 上下文管理、Windows 启动器清理时文件句柄未释放而失败，不能视为通过。修正后直接启动实际 Python 进程，核对 ready PID，并显式关闭连接；旧失败测试的空操作 schema 已清理，随后重新验证。

首次全量回归记录在 `evidence/20261025T172352.095942Z`：78 项集成测试中 1 项失败。该测试把新库还原为 v3 时遗留 v5 表，迁移报 DuplicateTable；已修正测试夹具以还原真实旧版本结构，生产迁移仍保留对重复对象的拒绝。失败记录保留，最终通过状态以随后生成的 verification.json 为准。

这证明受限网络 API 的请求边界，不证明完整 OS 沙箱或全中介。服务仍用测试数据库管理身份；同一 Windows 用户若持有主机文件/执行权限，仍可能读取配置或绕过 API。生产数据库角色、独立服务账户、工具凭据隔离和网络出口策略尚未落实。完整语言命令、通用验证器治理和管理 API 仍未完成。

schema v8 增加 `POST /v1/capabilities/delegate`，要求 delegate scope，创建绑定已登记子主体的衰减能力；协议、共享额度与撤销语义见 [委派规格](delegation-profile.md)。此路由不提供主体登记、根权限开通或凭据签发功能。

## 需求诊断补充

错误响应的 `requirement_ids` 按实际拒绝门禁与失败条款生成，详见 [运行拒绝与需求追踪](requirement-diagnostics.md)。不再为所有错误填写固定 R01/R02/R16。字段形状、状态码与授权/事务行为保持原协议；这些编号不证明整个需求已经满足，也不代替持久拒绝审计日志。
