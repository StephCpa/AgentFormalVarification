# 认证网关拒绝记录 v0.1

对应详细设计 §2、§3.3、§20 和[事务方案](durable-rejection-plan.md)。schema v9 已实现认证网关请求的持久拒绝事件；这不是全系统审计或 M1 发布声明。

## 入口与信任

请求通过路由、规范 JSON 和字段形状检查后，在 SERIALIZABLE 事务中取得 tenant authority_guard、加载状态，再复核凭据主体、有效期和撤销状态。复核成功才生成宿主 AuthenticatedContext；scope 不足属于已认证拒绝，可以记录。业务仍执行原有权限检查。

上下文携带 actor、action、trace_id、请求摘要和可选目标引用。摘要沿用 `avcl/api-request/0.1` 对 method/path/body 的规范摘要，格式为 `sha256:` 加 64 位十六进制。事件不保存 bearer token、批准引用、请求全文或异常 detail。上下文不提供 HTTP 构造接口，也不防御任意 Python/SQL 宿主访问。

范围包括已解析请求在 validate/delegate/prepare/dispatch/reconcile/cancel/close/get/task-get 执行中抛出的 Reject。返回业务报告的路径沿用自己的事件协议。凭据无效/撤销/过期、路由/HTTP/JSON/字段格式拒绝、未知租户、加载及 guard 前失败不在此范围。直接 Python 调用、控制面、工作流执行器和监督器尚未接入；不能称为全系统覆盖。

## 事务与事件

认证后用 savepoint 包含业务回调、状态检查和保存。Reject 回滚其中 SQL，同时丢弃已修改的内存内核，从同一 guard 保护下的回滚后状态重新加载。随后只插入 ActionRejected、更新拒绝配额和可信时钟，外层提交后才返回原 Reject。数据库故障、序列化错误或进程退出不冒充已记录的业务拒绝。

拒绝不调用全状态 `_save`，不修改业务账本、操作 revision、许可、批准、防重记录、outbox 或义务。已提交拒绝将 authority_guard.last_clock 推进到事务有效可信时间；这是明确允许的变化，不能说数据库完全不变。authority_guard.revision 不因拒绝增加。失败业务产生的事件和序号随 savepoint 回滚。

在 guard 内使用[事务计数行](audit-counter-profile.md)分配 sequence，以 INSERT 写入 audit_event，不用 upsert 忽略冲突。序列化/死锁重试重新认证，未提交事件、序号和配额一起回滚。提交后响应丢失再重试可能新增拒绝事件，也可能合法成功，不承诺跨 HTTP 重试仅一条记录，不写失败业务的永久幂等墓碑。

schema v10 已替换最初的历史长度分配方式。保存点内的分配随业务回滚，拒绝事件从回滚后的计数行取得序号；计数、行号及载荷不一致时明确拒绝，完整历史仍用于一致性检查。

沿用 Event(sequence,kind,operation_id,details)。details 包含 event_id、actor、action、request_digest、trace_id、operation_revision、policy_digest、epoch、code、requirement_ids、time、budget_deltas、causal_parents、evidence_refs。租户由表键绑定，sequence 表达 tenant_sequence；政策和 epoch 来自当次 guard。仅属于当前主体的既有操作包含 operation_id/revision，其余为空。预算变化为 `{}`；无可核验引用时因果和证据列表为 `[]`，不从用户输入推断链路。

details 延续字符串值协议，数组和对象为 JSON 字符串；缺失 revision 为 `"null"`。HTTP 错误结构不变，不公开内部事件 ID/报告/revision。其他主体对象与不存在对象同样返回 OPERATION_NOT_FOUND，审计也不含其他主体操作引用/revision。宿主通过 events() 读取，没有新增终端用户导出接口。数据库角色隔离、外部不可篡改归档和完整因果链仍未完成。

## 容量与故障

rejection_audit_policy 默认每租户最多 10,000 条新协议拒绝，每主体自首条起 60 个可信秒的固定窗口最多 100 条，每条序列化记录最多 16 KiB。rejection_audit_subject 每主体一行，不随窗口追加；主体行数受已保存事件总数约束。这只限制拒绝存储增长，不是全服务 CPU/延迟保证。

宿主 `trusted.configure_rejection_audit(total_limit,subject_limit,window_seconds)` 在 guard 内配置，范围分别为 1—1,000,000、1—1,000,000、1—86,400。总限额不能低于已保存数，不清零计数或删除历史。调整窗口沿用已有起点和计数。配置变更也有事件。

额度耗尽返回 HTTP 503 / AUDIT_CAPACITY_EXCEEDED，记录过大为 AUDIT_RECORD_TOO_LARGE，缺失策略为 AUDIT_UNAVAILABLE，数据库失败为 DEPENDENCY_UNAVAILABLE。均不声称原始拒绝已记录，业务仍回滚。配额失败不更新时间和计数；成功业务不消耗拒绝额度，仍可执行。没有自动删除或采样；达到总上限需宿主处置，归档续容协议尚未实现。

## 验证边界

integration/test_rejection_audit.py 使用真实数据库覆盖：业务 SQL/内存/事件回滚、预算拒绝后同键成功、主体/租户限额、scope 与凭据撤销、对象遮蔽、policy/epoch/clock、审计写入失败、提交前后进程终止、序列化重试、两个独立进程竞争业务预算及最后一个审计名额、过大事件和非法上下文。显式检查重连读取和连续序号。写入故障用数据库触发器注入，另有序列化错误定点注入，不代表真实磁盘故障或任意崩溃时刻已覆盖。

v1—v8 升级保留已有操作和审计事件，拒绝计数从零开始，不将旧事件补认成新协议拒绝。原有直接调用原子性测试保留；网关分别断言业务不变和审计新增，不弱化业务保证。

首轮开发测试因摘要验证器漏掉已有 `sha256:` 前缀，18 项网关测试中 14 项失败；已按既有协议修正，没有修改摘要算法。新夹具初次导入测试类导致额外运行 3 项旧用例，已改为模块引用。最终结果以冻结 verification.json 为准。

完整回归 `evidence/20261026T134103.597517Z` 保留一次 v5 重建夹具错误：它删除旧版本后续表时遗漏 v9 两张新表，升级报 DuplicateTable。已补齐该测试自身的 v5 重建步骤，未将正式迁移改成忽略重复表，也未改变业务实现；后续重跑结果单独留存。
