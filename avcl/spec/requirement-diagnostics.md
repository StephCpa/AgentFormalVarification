# 运行拒绝与需求追踪

依据详细设计 §2、§4.3、附录 B。需求编号标明某次检查所对应的设计约束，不表示该需求已经被完整证明或达到发布条件。

## 诊断来源

`requirements.py` 是已审阅错误代码映射的唯一实现来源。`Reject.requirement_ids` 是排序、去重的不可变元组，保留原有 `code`、异常类型及字符串表示。调用点可补充更具体的需求，例如委派账户超额同时关联 R04 和 R11。没有已审阅对应关系的扩展代码返回空元组；HTTP 协议格式和部分宿主配置错误也明确归类为空，不借用 R01/R02/R16 制造追踪。

|实际拒绝原因|需求编号|范围|
|---|---|---|
|普通账户 BUDGET_EXCEEDED|R04|当前失败的预算门禁；不表示授权也失败|
|委派账户或新委派额度超额|R04、R11|共享预算及权限衰减|
|POLICY_DENIED|R09|内置纯谓词返回 false|
|CONTRACT_DENIED|R09 与失败条款的 requirement_ids|通过条款不纳入此次失败诊断|
|ASSUMPTION_BROKEN|R14，若来自条款则另加失败条款编号|保留与普通谓词违反不同的错误代码|
|BINDING_MISMATCH / APPROVAL_MISMATCH|R02|参数、许可或批准绑定|
|STALE_PERMIT|R06、R08|政策/epoch/时效与许可准入|
|UNTRUSTED_REFERENCE|R01|未登记引用不能创造权限|

纯 IR 的 `Diagnostic` 同时保留源 `requirement_id`、`node_id` 和通用 R09 分类。合约拒绝从当前求值报告提取未通过条款的编号；报告中经过允许的自定义标识符也保留，不要求所有业务条款都使用 R01—R16。

## HTTP 与持久事件

网关返回的字段形状不变：仍是 `code/request_id/operation_ref/retry_advice/requirement_ids/detail_ref`。错误代码被公开边界遮蔽时保留实际门禁的编号。例如未登记许可引用公开为 AUTH_DENIED，仍关联 R01。缺失操作和无权访问操作继续经过相同遮蔽路径，不依据编号区分对象是否存在。响应不导出完整条款报告、请求值或凭据。

`Prepared` 和 `DispatchAdmitted` 的事件详情新增 `requirement_ids`，覆盖本次准入检查的 R01/R02/R04/R06/R08/R09/R14、实际匹配的合约条款编号，以及适用时的 R11。沿用 Event 的字符串值存储契约：该字段是一段规范 JSON 数组字符串；消费者先解析事件详情，再解析这个字段。旧 `requirement_id=R09` 作为兼容的主谓词标签保留，不能据此替代完整列表。合约摘要和绑定仍随事件保存。幂等返回不产生新准入事件。

不修改历史事件，也不将新字段补写入旧证据。schema v9 的[认证网关拒绝记录](durable-rejection-profile.md)在 savepoint 中回滚业务，在同一 guard 下提交事件、配额和单调时钟。直接调用和未认证入口不在此范围；HTTP 错误响应本身不能证明审计已提交，容量/存储失败返回不可用。全系统覆盖、归档和后续语言扩展仍需完善。

## 验收与剩余工作

`tests/test_requirement_trace.py` 覆盖真实预算/委派拒绝与状态不变、失败条款选择、假设失效、IR 源编号、准入幂等事件和新增错误代码未审阅时的失败门禁。静态扫描覆盖 `avcl/*.py` 中名为 Reject、Diagnostic、WorkflowDiagnostic、error、fail 的调用，其首参数是字符串常量或条件表达式常量分支；不声称覆盖所有动态代码路径。`scripts/diagnostic_inventory.py` 输出逐调用点、已审阅映射、源文件摘要和未解析动态调用清单，由验证脚本单独冻结。

`integration/test_gateway.py` 通过独立 HTTP 服务进程验证预算拒绝、引用遮蔽、合约失败编号、事务回滚，以及新连接重读准入事件。完整数据库与历史回归的结果由新的 `evidence/*/verification.json` 记录。

M1 仍未发布：[运行工作量预算](evaluation-budget-profile.md)已实现超限 Unknown/拒绝传播；[持久拒绝记录](durable-rejection-profile.md)已接入认证网关，完整系统覆盖仍未实现。未来语言扩展和动态错误须持续审阅。准确编号不解决全部需求的可验证性。

## 编译与加载来源

`diagnostics.describe(error)` 提供本地数据格式 `avcl/diagnostic/0.1`：code、requirement_ids、component、node_id、source_ref、location、clause_id。它不导出异常 detail、输入值或凭据，不扩展 HTTP 公共响应。错误类型和原有错误代码保持不变。

谓词、检查产物、工作流及合约加载器保留最内层诊断；外层只补充缺失信息及所属条款需求。工作流合并错误代码和命令的需求编号，避免 Prepare 等命令覆盖 E-AFFINE-201 的 R08。合约仅加入发生错误的条款编号，不把其余条款算作失败。

location 是 component 内的结构指针，例如 predicate 的 `/root/args/1`、workflow 的 `/body/1/then/0`、contract 的 `/clauses/0`。它不是文本行列位置；无法定位时使用 `/`，不伪造精确节点。source_ref 是调用者或源码映射提供的有界 UTF-8 标签，不是认证文件名，也不应未经转义展示。嵌套谓词保留自己的 component 和来源。无效源码映射仍拒绝，诊断中的来源标签本身不授予权限。

`tests/test_diagnostic_provenance.py` 覆盖常量/节点篡改、畸形 JSON 和类型字段、检查产物不匹配、工作流仿射冲突、嵌套条件、合约条款、非法需求容器和诊断导出边界。该实现没有新增文本语法解析器；详细设计中的全文语言与准确文本 span 仍需后续工作。

首次全量验证 `evidence/20261026T113500.596518Z` 中 212 项数据库测试有一项初始化错误：Windows 暂时拒绝读取已经原子发布的 HTTP 就绪文件，业务测试尚未执行。失败记录保留。夹具改为在拥有的子进程仍存活时限时重试 FileNotFoundError/PermissionError，损坏 JSON 与进程退出仍立即失败；清理在启动前登记。新增读锁/截止/终止/坏 JSON 探针及真实子进程初始化失败后的进程、文件、随机 schema 清理检查。该夹具修复不改变运行授权规则。
