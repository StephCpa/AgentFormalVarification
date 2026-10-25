# 合约 v0.3 与政策更新实现规格

本轮落实详细设计第 4.2、6—8、14 章中的动作合约与发布门禁。发布、权限和预算均在参考内核内存中，不包含生产 key management、PostgreSQL 事务或部署认证。

## 不可变合约

`avcl/contract/0.3` 包含 module_id、递增 policy_version、tools、validators、assumptions、clauses、account_limits 和 source_map。未知字段、重复定义、未知依赖、需求编号不一致或不支持的条款类型在构建/加载时拒绝。最多 200 条款，整个编码仍受 64 KiB 限制。每个条款引用同一纯谓词 IR v0.2，生成检查器不另写业务条件。

工具声明绑定协议版本与效果集合。本地注册表必须与声明一致；不能把 Write/Disclose/ResourceConsume 改名为 Read 通过发布。当前退款声明包含 Read、Write、Disclose、ResourceConsume。注册验证器绑定 issuer、predicate_id 和版本；这只是依赖检查，远端快照获取、遥测和重试的完整效果工作流仍待实现。

条款包含 id、kind、rule、tool、events、requirement_ids、assumptions、frozen、predicate。safety 的 requires/invariant 要求表达式为真，forbid 要求为假；允许事件为 prepare/dispatch。每个声明工具必须分别有覆盖 prepare 和 dispatch 的安全条款。completion 只允许 success 与 close，不参与动作准入，也不能冒充冻结安全条款。close 当前是独立的合约求值入口，任务关闭持久协议尚未实现。

bounded_obligation、assumption 形式的表达式条款和 statistical 后端尚未建立，因此明确返回 UnsupportedClauseKind；外部协议假设暂由独立 assumptions 声明表达，不能默默忽略未实现条款。工作流 v0.4 已提供有限命令的仿射许可与分支效果合流；完整工作流后端、标签与持久监控尚未实现。

## 假设与分项判定

Assumption 声明 id、authority、scope、valid_from、expires、monitor。宿主 Health 记录必须匹配 authority/scope，且声明与健康记录都在有效区间、状态为 Healthy。Broken、Unknown、缺记录和过期均阻止依赖该假设的动作；客户端上下文不能自行传入一个 true 替代宿主健康记录。

每个匹配条款分别输出 clause_id、requirement_ids、kind、event、status、passed、reason、contract_digest。结果为 RuntimeSatisfied、Violated、Unknown 或 AssumptionBroken。没有匹配条款不能获得允许；一个通过条款不覆盖另一个失败条款。错误诊断不回显业务参数值。

默认模拟合约使用时钟区间 `[0,1000)` 的幂等性假设。该数字属于测试时钟，不能当作实际长期有效的供应商保证。健康记录由可信宿主更新，假设声明目前随冻结条款固定；声明续期和生产事故解除需要后续明确治理流程。

## 发布封套与宿主审阅

`avcl/signed-contract/0.3` 封套绑定完整合约及来源映射、每个条款的生成 checks、编译器版本、签发/到期时间、需求审阅集合、获准运行检查集合和共同允许见证。采用域分离 HMAC-SHA256，加载先验签，再重新编译并逐项比较检查产物。未知格式、签名损坏、使用另一 authority 密钥或过期均拒绝。

签发需要三个独立条件：

1. 宿主显式提交与合约完全一致的需求审阅集合。
2. 每个硬条款都有获准的运行检查，不能用缺失、Unknown 或外部声明的 ProvedUnder 跳过门禁。
3. 每个工具提供一个在当前假设下同时通过 prepare 与 dispatch 全部条款的输入见证；分别展示互不相容的单条款见证不够。

这个见证只证明业务谓词交集非空，不证明主体有真实权限、账本足够、任务完成可达或整条操作轨迹安全。见证使用合成输入，实际动作仍重新检查授权、账本与所有门禁。离线枚举得到的 Unknown 继续保留，不会因签名成功而升级为 ProvedUnder。

HMAC 密钥由每个 ReferenceKernel 随机生成，只保存在宿主内存。签名证明这个本地域的来源，不是公钥发布者身份或真实人工审阅证据。`TrustedPlane.seal_policy` 必须处于可信边界，Agent 不能调用；持有同一 Python 进程权限的人可绕过此边界。密钥持久化、轮换、分离签名服务和审阅者认证未实现。

## 政策激活事务

`activate_policy` 与 dispatch 共用内核 RLock。在锁内依次核验：签名与当前有效期；工具/验证器及假设；同一模块的严格递增版本；旧 frozen 条款和其依赖假设不变；当前退款执行器能提供所有字段；新预算逐账户不低于既有 spent+pending，且账户未 breach。

全部检查通过后才一起提交账户限额、活动快照、policy 标识、epoch 和 PolicyActivated 事件。任一检查失败保持这些状态不变。没有恢复旧 epoch 的回滚捷径；希望回滚行为必须使用新的政策版本，同时保留冻结条款。当前内核不自动修改被冻结的硬条件。

政策激活后，旧 Prepared 操作保留原承诺和摘要，dispatch 返回 StalePermit；用户可显式取消其未派发预留，但不会自动重置批准或业务键。已准入操作保留原政策快照并继续接收核对观察。新 prepare 需要新 epoch 的能力，并使用新合约。许可有效期不超过发布封套到期时间；假设仍在 dispatch 再检查，过期不会自动释放旧承诺。

这仅建立内存串行顺序。进程故障下的签名状态、预算/epoch 一致提交、恢复限制以及实际发送端的适配器隔离尚无持久证据，不能推导 R06/R14/R16 全部完成。

## 验证入口

`tests/test_contract_policy.py` 覆盖合约解析、签名/产物篡改、需求与动态门禁缺失、矛盾条款、效果少报、预算更新回滚、历史快照、冻结条款、过期/未知假设、旧许可和激活/派发并发。

`examples/policy_lifecycle.py` 留下真实运行的 contract.ir.json、signed-release.json、activation-report.json：70 的旧准备在政策更新后无法准入；显式取消后按新版本完成 60 的退款；随后把预算降到 59 的更新失败且不改变任何状态。密钥不导出，因此保存的封套是本轮验签/激活证据，不能在新进程直接当作可恢复生产发布。
