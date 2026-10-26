# 结构化观察审计事实 v0.1

详细设计 §20.3、附录 B 要求事件内容可用于检查状态与费用变化。新生成的 ObservationRecorded 和 ConflictingObservation 不再使用 Python repr 表达观察内容，改用版本化、规范 JSON 的事实字段。数据库 schema 仍为 v10，历史事件不迁移、不补写。

## 原始事实与有效费用

`avcl/observation-fact/0.1` 精确包含 format、observation_id、operation_id、request_digest、kind、cumulative_costs、final_cost、terminal_effect。布尔字段使用 JSON 布尔；费用采用规范非负十进制字符串；向量用 `{account,cumulative_cost}` 数组表达。字段不允许缺失、额外属性、重复 JSON 键或非规范编码。

原始费用数组保留提交顺序；重复账户不会在编码时被字典覆盖，因为同 ID 冲突可能包含这类证据。kind 允许当前 Execution 枚举，request_digest 在事实层保留受限文本：冲突记录需要保存非法业务绑定，不能只为让 schema 通过而丢弃它。事实格式校验不是业务接纳规则。

新 ObservationRecorded 的 details 精确包含 observation、fact、effective_costs。运行时仍先执行既有绑定、费用维度和终局效果检查，再编码事实并登记事件。effective_costs 是排序且账户唯一的完整费用向量，包含宿主依据委派链派生的本地额度账户；这些账户不伪装成远端回执提供的事实。原始费用和有效费用的同名账户必须一致。

结构化事实没有把事故成本重新限制到请求金额或签名回执的数值域；受信宿主已支持的较大超界成本仍可记录。整体继续受既有规范文档与存储大小限制，不能声称能够无界接收任意证据。编码在修改参考内核 observation 映射之前完成；持久化仍受原事务保护。

ObservationRecorded 只说明观察被保留供后续处理：它可能紧随 AssumptionBroken、ObservationSubsumed 或正常结算事件。不能据此单独推断费用已经入账、效果属实或操作已最终完成。

## 同 ID 冲突

新 ConflictingObservation 的 details 精确包含 observation、fact、prior_fact_digest。fact 保存此次冲突候选；prior_fact_digest 是先前已接纳 Observation 的规范事实在 `avcl/observation-fact-digest/0.1` 域下的 SHA-256 摘要。先前事实仍保留在持久观察表；冲突候选不会覆盖它或再次扣款，后续仍进入既有 Disputed/隔离处理。

这个摘要连接内容，不是日志事件 ID 或完整因果边。只有同时取得先前事实且独立核对摘要，才能检查该引用；本阶段的单页载荷检查仅验证摘要形状，不声称完成跨页引用或因果检查。对于旧日志，摘要依据已存储的 typed Observation 计算，绝不执行、解析或猜测旧 repr 文本。

## 离线检查和兼容性

[载荷配置](audit-payload-profile.md)演进为 `avcl/legacy-event-payload/0.2`，同时识别旧不透明形状和新结构化形状。新 ObservationRecorded 额外检查事实 ID/操作 ID 与外层一致、请求摘要形状、可接纳效果种类、终局标记、原始账户唯一、有效向量排序唯一及原始费用保持。冲突候选只检查事实结构和外层 ID 一致，保留其不合法的业务含义。

`--check-payloads` 报告新增 structured_observation_events 和 opaque_observation_events；空页均为 0。旧事件仍可读取、导出和做完整性检查，但被明确计入不透明数量，不能假定整段历史已经结构化。default 哈希模式不新增格式声明。

这仍不是完整事件封套或语义重放：没有补造 actor、可信时刻、操作版本、完整预算差额、政策上下文或完整因果链。仅凭本字段不能验证派生账户的授权来源，也不能验证远端 HMAC、事实真实性或费用与所有状态转移一致。认证回执仍由既有 ReceiptAuthenticated 与原始 receipt 存储路径负责，其安全边界未改变。

## 验证

单元测试检查原始顺序、同 ID 语义重试、错误绑定/重复账户冲突、部分结算→Final→晚到较弱证据、超界事故费用、委派费用展开、规范解码、外层绑定与费用保持、不透明历史计数，以及编码失败不改变参考状态。

真实 PostgreSQL 测试检查观察应用提交前崩溃后的恢复、一次性事实记录、冲突持久化且原观察/费用不变，以及旧 payload 字节在新追加后保持。快照演示另输出 observation-facts.json，供直接审阅结构化内容；该文件是演示投影，不是生产对象权限接口或独立真实性认证。
