# 审计快照与离线完整性校验 v0.1

依据详细设计 §20.3 和附录 B，提供宿主只读导出、分页哈希链及离线检查点校验。它保留当前 Event 存储格式，不冒充附录 B 的完整事件封套，也不执行预算/状态转移语义重放。

## 来源与链

`PostgresTrusted.export_audit(after=0,limit=100,expected_previous=None)` 在既有 SERIALIZABLE/tenant guard 内加载和核对计数、事件序列及形状，再生成固定页。不会更新业务状态、计数、可信时钟或日志。只接受宿主数据库访问，不新增 HTTP 导出路由；终端用户按对象权限读取的审计投影仍待实现。

导出保留全租户原始记录，不自动脱敏；宿主读取权限不能解释为终端用户有权读取全部对象。

每条记录保留数据库中的原始 payload 文本，以 UTF-8 字节计算 SHA-256。链根为 `avcl/audit-genesis/0.1` 域下 tenant 的规范摘要；链节为 `avcl/audit-link/0.1` 域下 tenant、sequence、previous 和 payload_digest 的规范摘要。沿用项目的“域名、零字节、规范 JSON”摘要规则。链在导出时计算，不新增数据库链头，也不修改旧事件。

页格式 `avcl/audit-page/0.1` 严格限定 format、tenant、after_sequence、previous_digest、records、checkpoint。每条记录仅含 sequence、payload、digest。检查点仅含 tenant、sequence、digest。序号使用规范十进制字符串，摘要必须为 `sha256:` 加 64 位小写十六进制。字段不允许重复或未知键。页上限 256 条、16 MiB；单条原始 payload 仍受 64 KiB 存储编码限制。超限拒绝，不静默截断。

分页从 after_sequence 之后开始；expected_previous 可钉住该位置已保存的检查点，源历史前缀改变会拒绝继续导出。页面可来自不同提交时刻，后续追加事件允许自然延续；没有据此宣称整个多页导出冻结于同一全局时间。空页具有明确检查点，不代表永久不会再产生事件。调用者须保存预期范围，不能把一段后缀当作全部历史。

## 校验结论

离线校验要求预期租户和起始位置，并验证事件连续性、表述序号与 payload 内序号一致、完整链节及末尾检查点。它区分三种结果：

- InternallyConsistent：仅检查输入自洽。攻击者重写内容并重算链仍可得到此结果。
- ContinuationConsistent：链正确接上调用者给出的起点检查点；新增尾部仍未由可信末端检查点约束。
- VerifiedAgainstCheckpoint：重算结果符合调用者给出的末端检查点。在该检查点独立、可信且 SHA-256 安全的前提下，支持本范围内容完整性。

把同一不可信文件中的 checkpoint 再作为“预期检查点”传入，不会创造信任。工具不认证检查点的获取方式，不声称抵抗同时改写数据库、快照和检查点的攻击者。校验报告明确给出 `semantic_replay=not_performed` 与 `event_truth=not_verified`；哈希完整性不证明事件观察真实、动作已授权或实际费用正确。

## 历史检查加固

加载及保存检查 Event 的精确整数序号、字符串 kind/operation_id，以及有序、唯一、字符串键值的不可变 details。存储层 kind 只检查标识符形状；离线校验可通过 `--check-payloads` 额外启用[当前 53 种载荷的严格配置](audit-payload-profile.md)。它不改变存储兼容性，也不为缺少 actor、政策、因果父事件或真实时间的旧事件补造字段。

历史前缀改用冻结的规范编码比较，拒绝 `True == 1` 等 Python 值相等却编码/类型不同的替换。原始数据库文本单独保留用于导出；规范编码用于比较内存内容，因此旧 JSON 空白风格不会被改写。非法或可变事件结构会停止加载，不能继续生成可信快照。

## CLI 与运行边界

```powershell
# 宿主导出；profile 含数据库连接信息，勿发布该文件
.\.venv\Scripts\python.exe scripts/audit_snapshot.py export --schema avcl --tenant demo --output page.json

# 离线检查：未提供可信末端检查点时只给内部一致性结论
.\.venv\Scripts\python.exe scripts/audit_snapshot.py verify page.json --tenant demo

# checkpoint.json 应由调用者此前独立保存并确认来源
.\.venv\Scripts\python.exe scripts/audit_snapshot.py verify page.json --tenant demo --expected-checkpoint checkpoint.json
```

下一页可用 `--previous-checkpoint`，未显式传 --after 时从该检查点取得起点。export 不自动迁移、初始化数据库或覆盖已有输出。verify 不连接数据库；退出码 0 表示符合给定末端检查点，2 表示内部一致/延续一致但缺乏末端锚定，1 表示拒绝或 I/O 不可用。export 成功写文件返回 0，含义是本地导出成功，不是真实性验证通过。异常不打印 DSN、密码或异常 repr。

输出是普通本地文件，不是 WORM/外部只追加归档，没有断电持久性承诺。写入中断留下的文件必须重新校验，不可当作完成快照。全量历史加载和前缀哈希仍有线性成本；页大小限制不构成全服务内存或延迟保证。完整封套、因果引用核查、业务语义重放、外部检查点保管/归档和数据库角色隔离仍待完成。

## 验证

单元测试覆盖篡改、重排、重复、删除、截断、重算整链、起止/租户绑定、分页/空页、重复键、JSON 数字、大小、非法事件及类型相等漏洞。数据库测试覆盖只读和原始文本保持、追加续页、跨租户检查点、有效形状的源历史改写、CLI 往返/防覆盖/错误信息脱敏。

完整验证增加 `audit-snapshot-demo`：真实 PostgreSQL 中完成模拟退款，导出整页和分页并保存检查点、校验报告与产物摘要；同时证明改写后重算的链可内部一致，但与原检查点不符。示例检查点来自同一次演示，不代表已部署独立可信检查点服务。
