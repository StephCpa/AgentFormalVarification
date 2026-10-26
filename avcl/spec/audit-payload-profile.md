# 当前事件载荷的离线严格校验 v0.2

详细设计附录 B 要求按事件类型判别载荷。本阶段提供 `avcl/legacy-event-payload/0.2`，描述当前原型的 53 种事件，作为快照完整性校验的可选附加检查。0.2 同时识别新结构化观察载荷和旧不透明形状；它不是附录 B 的完整 `avcl.event/0.1` 封套，也不是状态转移或预算语义重放。

## 校验规则

`avcl/audit_payload.py` 的显式目录按 kind 枚举合法的精确字段集合，拒绝缺失、未知和重复字段；操作引用分别要求存在、不存在或允许二者。底层仍使用不可变 Event、排序唯一字符串 details 和严格存储解码。

- binding、请求、政策、IR、合约、封套、工作流及内容摘要必须为 `sha256:` 加 64 位小写十六进制；回执拒绝的 raw_digest 另允许 `unavailable`。
- 数量字段采用无前导零、无符号、无指数的十进制字符串，最大为有符号 64 位整数上界。配额、容量和发送尝试另按当前实现的具体界限校验。此通用范围不替代各业务 API 的更窄边界或跨事件约束。
- requirement_ids 必须是规范 JSON 字符串数组，排序、唯一、标识符有效；允许用户定义的合约编号，不把合法编号解释为已履行需求。
- evaluation_work 必须符合当前工作量模型，精确包含 model/limit/used/exhausted，used 不大于 limit，成功准入事件的 exhausted 为 false；不接受未知模型或额外属性。
- SendAttemptAdmitted 区分 direct/workflow 和有无持久工作者字段四种形状；workflow 必须带主体、run、workflow_digest、node，worker/generation 必须成对出现。SendBlocked 保留当前两种真实生成形状。
- ActionRejected 的 operation_revision 与操作引用是否存在相匹配；当前拒绝事务只接受空 budget_deltas、causal_parents、evidence_refs。新增真实因果数据需显式演进本配置，不能把非空数据悄悄忽略。
- Task 状态、假设健康状态、发送 profile、归属、布尔字符串及固定结算/观察原因按当前种类校验。

其他文本仅做既有字符串、非空和 UTF-8/长度检查；不证明外部标识符实际存在。新 ObservationRecorded/ConflictingObservation 使用[结构化事实](observation-fact-profile.md)并检查内外层 ID、费用和相关字段。旧 ObservationRecorded.content 与旧 ConflictingObservation.observation 保持不透明 repr 文本，不解析、不执行，也不据其推导费用或状态。

## 使用与结论

```powershell
.\.venv\Scripts\python.exe scripts/audit_snapshot.py verify page.json --tenant demo --check-payloads
.\.venv\Scripts\python.exe scripts/audit_snapshot.py verify page.json --tenant demo --check-payloads --expected-checkpoint checkpoint.json
```

Python 接口为 `verify_bundle(..., check_payloads=True)`。通过后额外返回 payload_schema，包含 profile、status:"Conforms"、checked_events，以及 structured_observation_events/opaque_observation_events 两种观察数量。空页计数均为 "0"，不说明历史内容已被检查。该结果只覆盖本页事件。

未知 kind 返回 `UNSUPPORTED_AUDIT_EVENT_KIND`，已知 kind 不符合规则返回 `INVALID_AUDIT_PAYLOAD`；两者映射 R14。格式错误的旧日志不会被改写或自动降级，严格模式明确拒绝。调用者可另外使用默认哈希模式获取内容完整性结论；它不返回载荷符合性声明。未知宿主扩展事件也适用这一规则。

退出码保持原义：0 为符合调用者提供的末端检查点，2 为缺少末端锚定的内部/延续一致性，1 为拒绝或 I/O 不可用。载荷格式通过不会把 2 提升为 0；检查点仍须由调用者独立保管并确认可信来源。

严格校验不改变运行时事件生成、数据库迁移、业务准入或既有日志格式；旧日志仍可加载、导出和做哈希比对。完整事件封套、时钟来源、跨事件因果引用、投影权限、动作授权与预算重放仍待实现。

## 验证与复现

```powershell
.\.venv\Scripts\python.exe scripts/local_postgres.py start
.\verify.cmd --audit --smt --m1
.\.venv\Scripts\python.exe scripts/local_postgres.py stop
```

`--audit` 冻结定向 PostgreSQL 审计导出、结构化观察测试与真实快照演示，不运行全量数据库集成测试；与 `--postgres` 同时使用不会重复执行。verification.json 明确区分定向和全量范围。

测试覆盖真实准入/发送/观察/结算事件、四种发送形状、重算链仍不符合载荷规则的反例、未知类型、操作引用、非法摘要/数字/需求列表/求值报告、拒绝事务、CLI 返回码及不透明文本。静态检查对照当前所有 `_event` 字面量调用与目录，并要求将来新增动态种类显式审阅；这不代表穷尽验证每种事件的所有运行路径。

数据库测试另覆盖重启后真实拒绝事件、持久工作流与认证回执、旧事件不改写和只读状态保持。演示对真实数据库生成的整页、分页和空尾页执行严格载荷检查，同时保持哈希篡改对照。
