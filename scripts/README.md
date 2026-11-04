# 脚本入口

部署顺序和环境要求见[部署指南](../docs/deployment.md)。脚本从仓库根目录调用；所有可能修改数据库、镜像、Kubernetes 或服务状态的命令都应先核对客户环境和目标资源。

| 目的 | 入口 | 主要副作用 |
| --- | --- | --- |
| 检查环境 | `bash scripts/check-env.sh prod` | 只读 |
| 首次建库 | `bash scripts/init-db.sh` | 建库、建表、种子数据 |
| 构建 Web | `bash scripts/build-public-prod.sh` | 发布静态资源到 `PUBLIC_RELEASE_ROOT` |
| 启动/重启 Backend | `bash scripts/start-public.sh prod` | 重启 Backend，不切换 Runtime |
| 查看状态 | `bash scripts/status-public-prod.sh` | 只读 |
| 安装管理端资源视图 RBAC | `bash scripts/install-admin-kubernetes-access.sh` | 创建集群权限和专用 kubeconfig；先核对 `deploy/kubernetes/admin-resource-reader-rbac.yaml` |
| 构建完整 Runtime 镜像 | `bash scripts/build-runtime-v3-image-dev.sh` | 镜像、环境配置和可能的集群副作用；先阅读目标脚本 |
| Runtime 维护 | `scripts/reload-*`、`scripts/rebuild-*` | 需要冻结、KService reset、旧 Pod 归零与 PVC 单写门禁 |

企业知识 Data Service 的内容资产未包含在试用包内；该功能接入客户自己的服务后再使用相应脚本。旧环境迁移与旧部署脚本不属于本指南推荐入口。
