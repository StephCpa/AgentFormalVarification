# PostgreSQL 持久事务规格

实现日期：2026-10-26。该适配器将现有退款状态转移落到真实数据库，不改变原设计文档。它是源代码工作区内运行的持久层原型；没有把 Python/SQL 与 Lean 的对应关系提升为精化证明。

## 环境与复现

固定 PostgreSQL 18.6、Python 3.12、psycopg 3.3.3；依赖见 `requirements-postgres.txt`。使用 PostgreSQL 官方 Windows 下载页指向的 EDB ZIP 分发，精确 URL 写入 `scripts/bootstrap_postgres.py`。按 Range 获取 bin/lib/share，通过 ZIP CRC，再保存逐文件 SHA-256。这里的 SHA-256 是本地冻结的供应物清单，没有声称已验证另行发布的厂商 SHA-256。完整验证重查这些文件，并将非秘密清单复制到证据目录。

新环境按以下顺序执行；已有 `.venv` 与数据目录时不要重新初始化：

```powershell
python -m venv .venv
H:\soft\uv\uv.exe pip install --python .venv\Scripts\python.exe -r requirements-postgres.txt
.\.venv\Scripts\python.exe scripts\bootstrap_postgres.py
.\.venv\Scripts\python.exe scripts\local_postgres.py start
.\verify.cmd --postgres
.\.venv\Scripts\python.exe scripts\local_postgres.py stop
```

若 uv 不在示例路径，使用已安装的 uv。启动程序仅在工作区 `.runtime/pg-data` 创建集群，以 SCRAM 密码认证并只监听 127.0.0.1，不安装服务、不修改 PATH、不修改现有 Docker。随机密码与连接串只存入被忽略的 `.runtime/local-postgres.json`。启动前核对目录归属，已有未知目录拒绝覆盖；停止仅作用于这个工作区集群。

测试要求实际 `server_version=180006`、`fsync=on`、`synchronous_commit=on`、`full_page_writes=on`。未启用 `--postgres` 的报告明确声明数据库未测试；启用后缺驱动或缺数据库均失败。数据库测试逐用例新建随机 schema，仅清理该用例自己创建的 schema。

## 事务和持久事实

每个写操作新开 SERIALIZABLE 事务，先锁 `authority_guard(tenant_id)`，再加载该租户的政策封套、授权、账本、操作、预留、防重与事件记录。业务语义复用 ReferenceKernel；提交将修改投影回关系表。序列化冲突或死锁从头重试整个事务，最多 8 次。连接丢失不自动假定提交失败，调用方以稳定请求键核对。

租户 guard 将该租户的预留、撤销、政策更新、准入、发送意图与回执应用排序。每次加载/保存还检查账本聚合、许可绑定、一次准入和事件序列；许可数据库状态必须与 operation 一致。当前按租户加载并写回全量状态，适合正确性验证，不声称具备大规模吞吐。

|事实|关系表与约束|
|---|---|
|权限顺序与本地签名键|authority_guard 租户主键；epoch、活动政策、隔离位与签名键|
|历史政策|policy_snapshot；不可 UPDATE/DELETE；恢复校验 HMAC 与生成检查|
|多账本承诺|budget_account、reservation；整数非负、外键；聚合一致性由内核复核|
|操作与一次许可|operation、permit；每操作唯一许可；绑定摘要复核|
|客户端/业务防重|request_dedup 的租户/主体/端点/请求键唯一约束；business_action 业务键唯一约束|
|原始观察|observation_receipt 的内容摘要主键；不可 UPDATE/DELETE|
|观察应用|observation_application 独立记录应用成功或拒绝及原因；observation 保存首个有效观察|
|待发送与发送意图|outbox 每操作/种类唯一；send_attempt 按代际记录工作者和请求绑定|
|未知核对义务|obligation 保存首次 due、owner、领取代际、Open/Claimed/Satisfied/Breached 及独立履行结果|
|审计|audit_event 单调序号、不可 UPDATE/DELETE|
|容量策略|capacity_policy 持久保存活动操作、未来义务和活动工作流限额；与准入共用 guard|
|委派|delegation_subject、delegation_grant、delegation_request；主体登记、冻结父链与单调撤销、幂等签发；额度仍复用 budget_account/reservation|

事故费用允许超过预算上界，数据库没有设置会阻止真实超额成本落盘的 `spent+pending<=limit` CHECK。真实超界先记账、设置 breached/quarantined，并保留 Disputed；不能用事务回滚掩盖现实成本。不可修改触发器不等于防数据库管理员篡改，当前测试角色拥有数据库管理权限。

历史封套按签发时点验签和重建，过期后的回执仍可结算；新的授权和发送使用单独持久化的当前健康状态。历史恢复不会把当前 Broken 自动改回 Healthy。政策更新保留旧操作引用的快照。

## 发送和崩溃边界

schema v8 的 [在线委派](delegation-profile.md) 与操作准入共享 tenant guard。委派不会预留整份子额度；实际消费时同时预留租户、订单和完整父链额度。远端回执只含业务成本，额度消耗由内核派生；撤销不妨碍原始回执恢复与真实成本入账。

1. Dispatch 在一个事务中消费准入许可、写事件并建立 Pending outbox。
2. claim_outbox 在事务中复检隔离位与原始合约的假设，写入发送意图并领取带 generation 的 InFlight 租约。
3. 外部 I/O 在事务外完成；数据库不声称能撤回已经发出的网络请求。
4. 领取者消失后，租约过期处理将操作置 Unknown、建立持久核对义务，任务置 NeedsReconcile，额度继续保留。不会把过期租约直接改回 Pending 并盲目重发。
5. 收到观察先单独提交原始记录，再在第二个事务中结算。中间崩溃留下可恢复的原始回执；重复应用不会再次扣费。
6. 错误回执的投影事务回滚，另记拒绝结果和审计事件；恢复队列继续处理其他回执。原始错误记录仍保留。

操作键使用 UUID，许可引用使用 256 位随机值，避免不同进程都从 op-1 开始。它们不是认证凭据。已领取但延迟的工作者仍可能发包，外部幂等契约仍是必要前提；[发送窗口规格](send-window-profile.md) 加入共同门禁与可选有限 TTL 模拟服务，[发送鉴权规格](send-envelope-profile.md) 与 [回执鉴权规格](receipt-envelope-profile.md) 加入独立 HMAC、同库密钥恢复和原始输入上下文保留。真实服务保留契约、生产身份和密钥隔离尚未验证。

## 已验证范围和剩余工作

集成测试包含独立连接竞争 70/60 预算 100、并发相同请求、多账本失败回滚、跨实例撤销、政策快照和现时健康、租户隔离、原始记录不可更新、真实成本超界，以及 prepare/dispatch/claim/observe 的子进程 `os._exit(97)` 故障点。数据库不由 mock 替代。具体测试数与退出码以最新完整证据为准。

这些故障测试终止应用子进程；没有模拟操作系统掉电、磁盘损坏、数据库服务器崩溃、主从切换或旧备份恢复。远端仍是父进程内独立模拟对象，其崩溃恢复不在本次范围。第一轮手动检查出现的两个错误来自测试辅助函数对无返回行的 UPDATE 调用 fetchall，修正后重新验证，不能将第一轮描述为全部通过。

schema v2 的 [持久监督器](supervision-profile.md) 已加入领取、超期记录、免费模拟查询与确认交接；v3 的 [持久工作流](durable-workflow-profile.md) 接通事务检查点；v4 的 [容量门禁](capacity-profile.md) 在新操作前预留未来义务槽位。下一步需增加公平调度和收费核对预算、限制数据库角色和宿主 API、加入真实身份与出口隔离，并验证远端去重保留窗口及恢复 epoch。签名键目前与测试数据共库保存；没有生产密钥隔离。默认时钟仍是 0 起始的逻辑时钟，不能直接连接真实时间服务并沿用默认 0..1000 的有效期。

来源：[PostgreSQL Windows 下载](https://www.postgresql.org/download/windows/)、[EDB ZIP 分发](https://www.enterprisedb.com/download-postgresql-binaries)、[PostgreSQL 18 事务隔离](https://www.postgresql.org/docs/18/transaction-iso.html)。
