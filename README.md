# Agent 形式化验证语言研究与设计

本项目包含 Agent 形式化验证语言的研究设计、AVCL 详细设计，以及以退款业务为例的可执行参考原型。

**当前阶段：M1 实现中，尚未达到 M1 发布门槛。** 现有形式化证明覆盖显式假设下的抽象模型；生产身份与密钥隔离、真实适配器验证、Python/SQL 实现精化等仍待完成。

## 项目入口

| 内容 | 位置 |
| --- | --- |
| 原始研究设计 | [Agent形式化验证语言研究与设计.docx](Agent形式化验证语言研究与设计.docx) |
| 详细设计文档 | [AVCL形式化验证语言与执行系统详细设计_v0.1.docx](AVCL形式化验证语言与执行系统详细设计_v0.1.docx) |
| 详细设计源稿 | [.design-work/design.md](.design-work/design.md) |
| 原型说明与运行方法 | [avcl/README.md](avcl/README.md) |
| 实施状态与缺口 | [avcl/spec/implementation-status.md](avcl/spec/implementation-status.md) |
| Python 参考内核 | [avcl/avcl](avcl/avcl) |
| Lean 证明与有限状态模型 | [avcl/formal/lean](avcl/formal/lean)、[avcl/models](avcl/models) |
| 历史验证证据 | [avcl/evidence](avcl/evidence) |

## 快速验证

准备 Python 3.12+ 与项目固定的 Lean 工具链后，在 Windows PowerShell 中执行：

```powershell
Set-Location avcl
.\verify.cmd
```

PostgreSQL 集成验证需要单独配置本地测试数据库及驱动，见[持久层规格](avcl/spec/postgres-profile.md)。基础验证明确不包含 PostgreSQL；缺失工具不会按验证成功处理。

最近一次完整验证记录为 [20261025T202705.069248Z](avcl/evidence/20261025T202705.069248Z/verification.json)：208 项单元测试、195 项 PostgreSQL 集成测试、21 个验证阶段通过。这是对应源码快照的历史结果，验证范围以报告为准。历史失败记录与后续修复证据一并保留。

仓库保留设计文档、源码、测试及冻结证据；本地虚拟环境、数据库集群、运行时凭据和构建缓存不纳入版本控制。
