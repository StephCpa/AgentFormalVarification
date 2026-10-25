# 候选退款验证与快照依赖

依据详细设计第 7、19 章，schema v6 将公开候选验证接入已有 checked_ref、能力、批准和操作账本。当前实现固定退款验证器 `order-service / RefundEligible / sim/0.1`，不接受客户端上传的验证程序或自报命题。

## 请求与结果

`POST /v1/candidates/validate` 要求服务端凭据具有 `validate` 作用域，正文严格包含以下四项：

```json
{
  "client_request_id": "validate-001",
  "validator_id": "order-service",
  "context_refs": ["context-<host-issued-reference>"],
  "candidate": {
    "task": "task-a",
    "business_key": "approved-refund-case",
    "order": "a",
    "order_version": "v1",
    "destination": "original-a",
    "amount_minor": "70",
    "max_fee_minor": "0",
    "currency": "CNY",
    "tool": "refund",
    "adapter_version": "sim/0.1",
    "schema_version": "refund/0.1"
  }
}
```

租户与主体来自已认证凭据，不能出现在候选正文。候选可以提出金额和目的地，但这些值必须与可信订单快照及允许范围相符。客户端不能提供 remaining、issuer、predicate_id、expires、allow、evidence_refs 或已验证状态。

成功返回 checked_ref、evidence_refs、predicate_id、验证器 ID/版本、`RuntimeSatisfied`、限定范围和 `not_after_logical`。此状态仅表示当前注册快照支持本次资格谓词，不是全局证明、未来状态保证或执行授权。验证不创建操作、不预留资金、不签发 Capability、不创建或消费 Approval。后续 Prepare 仍必须提交独立有效能力和匹配批准。

## 快照来源与纯验证

宿主调用 `ValidationAuthority.register_order_snapshot(subject, order_id, order, fee, expires)` 登记快照。`RefundService.read_order` 提供当前独立模拟服务的不可变订单值与费用；注册步骤属于受信宿主，不向 HTTP 客户端开放。快照存储订单版本、原路目的地、剩余额度、费用、主体和有效区间。

公开验证只读取已登记快照，不调用模型或远端工具。它检查当前合约确实注册了指定验证器三元组，然后核对订单、版本、目的地、本金上界和手续费上界。未知或不同主体/租户的 context_ref 均被拒绝。结构正确、任意名为“验证器”的字符串或客户端填写 true 都不能签发 RefundEligible。

快照真实性仍依赖宿主获取来源正确的订单事实。生产受控 Read 操作、收费读取预算、上游签名与真实订单服务认证尚未实现；这里没有把普通 JSON 提升成可信事实。

## 持久绑定与失效

新增三张表：

|表|职责|
|---|---|
|order_snapshot|不可修改的订单事实、主体与有效区间|
|order_context_head|每租户每订单当前可用快照引用；撤销后为 NULL|
|candidate_validation|输入摘要、幂等键、checked/evidence/context 引用、验证器版本与签发时段|

candidate_validation 与快照禁止更新或删除；引用通过外键约束。证据、checked 请求、验证记录与审计事件在同一个 SERIALIZABLE/tenant guard 事务提交，相关外键延迟到提交检查。有效期取快照有效期、政策有效期与签发时点加 300 个逻辑时间单位的最小值。

宿主登记新快照会替换该订单的 head，即使版本文本未变也使旧快照失效；这是保守的刷新语义。快照 ACL 当前仅支持一个主体；为其他主体登记同一订单的新快照也会使旧引用失效。旧事实与验证记录仍保留审计。

准备与派发准入在数据库事务内检查公开验证证据的依赖：快照仍是 head、主体一致、时段有效，且当前合约支持原验证器。检查接入参考内核的授权步骤，因此直接调用 PostgreSQL 内核及持久工作流也无法绕开此检查。旧的宿主手工注册 Evidence 没有这些绑定，仍由宿主承担有效性责任；该兼容入口不是公开 API。

快照失效后，已经 Prepared 的操作不能继续准入，预留仍保留，可按已有规则在准入前取消。已经准入的操作可能已在远端发生，不因快照更新释放预算或撤回已提交事实；本实现不把快照撤销声称为远端取消。

## 防重与恢复

验证按租户、主体、client_request_id 防重，摘要覆盖整个规范化正文。相同输入返回同一 checked/evidence 引用；同键异参返回 IDEMPOTENCY_CONFLICT。每次重试先复检凭据、scope 和依赖，过期或撤销后不返回过时缓存。若需基于新快照重新验证，应使用新请求键。

测试在验证提交前后强制终止实际子进程：前者不能遗留一半 Evidence/checked/validation，后者通过 HTTP 重试复用已有引用与一次审计事件。另覆盖 API 进程重启、并发同键、拒绝身份/事实注入、跨主体/租户、权限与批准独立性、条件不满足、时钟回退、快照过期/刷新/撤销、记录不可改写，以及 v5 旧凭据迁移后继续使用。

测试数与通过状态以本轮完整 verification.json 为准。初次定向测试曾因模块全局导入 TestCase 别名而重复收集原 API 用例；已消除重复，不能将当时的 27 次执行计为 27 项新增测试。

补充的两项崩溃测试首次运行在启动子进程前失败：测试传入字典，而内部持久编码器刻意不支持任意字典。已改为传递 JSON 文本、在测试子进程使用严格解析器还原请求；没有放宽持久编码器。该次 16 项中 2 项错误的运行不能作为崩溃验证通过证据。

## 保留的设计缺口

这里补齐的是退款请求的公开资格验证入口。完整 Evidence 类型与签名协议、可治理的多验证器注册/撤销、通用事实依赖图、任意候选 IR/工作流的公开验证、propose 的受控模型调用、真实 UTC 区间和生产快照来源认证仍未完成。验证历史与快照历史也尚无独立归档配额；本机测试服务不作为生产容量或安全隔离保证。

## 工作流 v0.6 Validate

持久工作流现在可以从 Candidate 与 Context 生成 Evidence，再直接用于 Prepare，无需宿主预先为请求生成资格证据。静态输入种类 Context 表示有主体/租户绑定的服务器快照引用；Validate 输出的 Evidence 仍不能充当 Capability、Approval、Permit 或 Context。

工作流启动时检查候选的租户/主体，以及新运行的 Context 可访问性和有效期。Validate 执行时再次检查政策版本、逻辑时钟、注册验证器、快照 head、有效区间和订单事实。失败会停止在 Validate 节点，不创建退款操作。成功只产生证据，不补足缺失的能力或批准。

验证请求键由 run_id、工作流摘要和节点 ID 派生。Evidence、checked_request、candidate_validation、节点轨迹、引用环境中的 e 和 pc 同一事务提交。提交前进程退出不遗留半份验证产物；提交后退出恢复下一节点，不重新签发证据。多个 worker 争用同一节点仍只提交一次。

生成证据后并不冻结外部世界。Prepare 与 Dispatch 仍复检证据依赖：若快照在两步之间更新或撤销，前者不能消费批准/预留，后者保留已准备的预算并阻止新准入。对不同 Candidate 复用证据会因请求摘要不匹配被拒绝。

已经完成或停止的相同运行重放返回原检查点，不因 Context 过期而重新验证或延长 Evidence。尚未执行的 Validate 节点、以及后续 Prepare/Dispatch 继续按当前有效性检查；历史重放不是重新获得权限。

静态效果包含 Read/ResourceConsume，覆盖所有分支；这里的 Read 是读取本地可信快照，未隐藏远端查询或模型调用。v0.4/v0.5 不接受 Context 或 Validate；固定旧版本样本验证字节和摘要保持。内存执行器缺少此注册快照服务，会在任何动作前明确拒绝，而不会执行到中途才忽略验证。

三条 close-workflow-demo 轨迹已经从零 Evidence 开始，经 Validate → Prepare → Dispatch → Send → Close，必要时再 Reconcile → Close。每条轨迹同时检查只有一次验证、一次发送，以及独立模拟远端成本与账本余额。通用验证器和内存验证后端仍是后续工作。
