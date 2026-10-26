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
| 本轮进展小结 | [PROGRESS.md](PROGRESS.md) |
| 基线审阅报告 | [AVCL_Repository_Review_3e5c63f.docx](AVCL_Repository_Review_3e5c63f.docx) |
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

可选 [SMT 验证](avcl/spec/smt-profile.md) 使用锁定的 Z3，从现有纯谓词 IR 与生成指令导出局部查询及反例回放。按 `avcl/requirements-smt.txt` 安装后执行 `.\verify.cmd --smt`；默认验证明确不包含 SMT。

最近一次包含 PostgreSQL、SMT 和 M1 定向检查的完整验证记录为 [20261026T155147.862720Z](avcl/evidence/20261026T155147.862720Z/verification.json)：276 项单元测试、257 项 PostgreSQL 集成测试、38 项 SMT 测试、24 项 M1 定向检查，28 个验证阶段通过。M1 定向用例与其他测试存在覆盖重叠，不应将这些数量相加解释为独立场景数。报告绑定 178 个源码文件摘要；本次提交前已重新核对一致。各轮覆盖情况以 `verification.json` 为准，历史失败记录与后续修复证据一并保留。

针对提交 `3e5c63f` 审阅中的 F1—F4，已修复终局回执合并、工作流发送所有权、累计费用表示域及历史合约回放，见[修复规格](avcl/spec/review-fixes-profile.md)与[验证索引](avcl/evidence/20261025T213748.983131Z/review-fix-summary.json)。修复前的缺陷探针、失败断言日志和修复后证据一并保存；M1 发布仍需按原设计逐项验收。

仓库保留设计文档、源码、测试及冻结证据；本地虚拟环境、数据库集群、运行时凭据和构建缓存不纳入版本控制。
