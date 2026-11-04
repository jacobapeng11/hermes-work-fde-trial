# 个人知识库单元测试

## 模块目标

验证个人知识库与默认 Profile Desired 状态的一致性，重点覆盖删除知识库时
Profile revision、能力投影和后续 Skill 安装重放之间的存储契约。

## 目录职责

- 使用 SQLite 内存库和最小 fake Provider/ACL 隔离外部知识服务和权限目录。
- 不连接生产 MySQL、真实 Runtime、PVC 或外部知识 Provider。
- 真实端到端上传和运行态收敛属于生产验收或独立 Runtime 冒烟，不在本目录伪造。

## 关键场景

- 删除个人知识库前推进默认 Profile Desired head。
- 删除后新 revision 不再引用不存在知识库。
- 后续安装个人 Skill 时完整 Desired 重放仍成功。
- WeKnora 创建/删除请求头、路径、响应结构和失败语义。
- WeKnora RAG `/knowledge-search` 请求体、match_count、响应列表校验和脱敏失败语义。
- Filebase/WeKnora 混合 Provider 路由、平台文件引用映射、实时 ACL 和整次失败边界。
- RAG/Wiki 能力快照、state 兼容默认值、revision 变化和精确 dataset ID 回填。
- Wiki 搜索/读页契约、slug 安全、页面正文、多个来源文件映射和能力/404 错误。
- RAG/Wiki 混合检索的一次授权、能力拆分、并行执行、范围缩小和任一路失败 fail closed。
- 真实 smoke 脚本的 provider 选择、RAG/Wiki 平台引用和撤权拒绝契约；脚本本身不连接真实环境。
- 创建失败时的同 Provider 补偿删除。
- WeKnora 文件 multipart 上传、外部文档 ID、解析状态同步和文件流契约。
- 上传本地落库失败的远端补偿、重复上传和缺失映射时的 fail closed。
- 单文件删除接受、远端已删除、删除中核对及 Filebase OSS 兼容。
- MySQL 文件字段迁移的幂等计划；不连接真实 MySQL 或执行 DDL。

## 运行命令

```bash
cd backend
.venv/bin/pytest -q tests/unit/personal_knowledge/
```
