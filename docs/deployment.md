# 客户隔离环境部署指南

本指南适用于 Linux 主机上的单环境试用。平台由 Web/Backend 控制面与 Kubernetes/Knative Runtime 执行面组成；没有可独立完成全部基础设施安装的一键脚本。先准备隔离的目标环境，再按以下顺序部署。所有命令在**客户试用仓库根目录**执行。

## 1. 环境与网络

| 组件 | 要求 |
| --- | --- |
| 主机 | Linux amd64、Python 3.12+、Node.js 20+、npm、Git、Nginx、MySQL client |
| 数据 | MySQL 8、Redis；为试用平台单独建库和账号 |
| Runtime | Kubernetes、Knative Serving、Kourier、可写持久卷、容器镜像仓库 |
| 外部 | HTTPS 域名、标准 OAuth2 身份源、可访问的模型 Provider |
| 桌面构建 | Windows/macOS 构建机，Node.js 与对应系统的 Electron 打包依赖 |

公网只暴露 HTTPS 入口。Backend/MySQL/Redis、Runtime 内部端口和 Kourier 入口应放在受控网络。OAuth2 身份源必须允许 `https://<客户域名>/api/v1/auth/oauth2/callback`；Runtime Pod 需要能访问 `PUBLIC_API_BASE` 和已配置的模型 Provider。

## 2. 配置

```bash
cp backend/env.example backend/.env
chmod 600 backend/.env
```

逐项替换 `REPLACE_*` 和 `example.com`。至少确认：

1. `PUBLIC_WEB_BASE`、`PUBLIC_API_BASE` 为 Runtime Pod 可达的真实 HTTPS 入口；`PUBLIC_ADMIN_BASE` 为同域 `/admin`。
2. MySQL/Redis 地址、账号、TLS 模式及专用库；不要使用示例密码。
3. OAuth2 client、授权/令牌/userinfo URL、用户 ID/工号字段及回调地址。`COMPANY_DIRECTORY_NAMES` 用客户 HR 的真实 `com_id → 展示名` JSON 映射；`DEFAULT_ORG_ID` 与客户组织数据一致。`ADMIN_INITIAL_WORK_NOS` 填客户首批管理员工号，多个工号用逗号分隔；初始化时仅为这些工号授予管理权限，未配置则不自动授权。
4. 为 JWT、加密、Runtime Token、下载签名分别生成独立的随机密钥。
5. Runtime namespace、节点、镜像、镜像 manifest/config identity、持久卷和 kubeconfig 与目标集群一致。
6. `PUBLIC_RELEASE_ROOT` 与 Nginx 静态文件根一致；`PUBLIC_NGINX_CONFIG` 指向已启用的配置文件。

`backend/.env` 是私密本地文件，绝不提交。Python Settings 从运行进程的当前目录读取 `.env`；Backend 命令须先 `cd backend`。Shell 生命周期脚本从仓库根调用，自行读取 `backend/.env`。

## 3. Nginx 与 Runtime 路由

`deploy/nginx/customer.conf.example` 保留了 Backend API、Runtime Edge WebSocket、Runtime 文件代理及静态 Web 路由的现行结构。复制到目标 Nginx 配置目录后，必须按客户环境修改：

- `app.example.com`、`listen 127.0.0.1:8080` 与外层 TLS 网关；如 Nginx 直接终止 TLS，配置证书与 443 监听。
- `/opt/hermes/current` 与 `PUBLIC_RELEASE_ROOT` 一致。
- `127.0.0.1:8011` 是 Backend；`127.0.0.1:30364` 是示例 Kourier 入口，需按实际 Service/NodePort 修改。
- 保持 `/_runtime-edge-auth` 与 `/_runtime-file-auth` 为 `internal`，不要让浏览器直接选择 Runtime 上游。

先运行 `nginx -t`，再加载配置。`PUBLIC_NGINX_LISTEN_HOST`、`PUBLIC_NGINX_WEB_LISTEN_PORT`、`PUBLIC_NGINX_API_LISTEN_HOST`、`PUBLIC_NGINX_API_LISTEN_PORT` 必须与已启用配置的 `listen` 一致。

## 4. 安装依赖并初始化控制面

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
npm --prefix frontend/user-web ci
npm --prefix frontend/admin-web ci
npm --prefix hermes-desktop ci
```

确认 `backend/.env` 已指向**客户新建的专用 MySQL 库**后，首次部署执行：

```bash
bash scripts/init-db.sh
```

此脚本会建库、建表并运行种子数据，只在确认目标库身份后执行。现有库升级应先备份并按迁移清单处理，不能把首次初始化命令当作升级命令。

## 5. Runtime 镜像与集群

`Dockerfile.runtime-supervisor-overlay` 从固定 Hermes 父镜像构建完整 Runtime；`Dockerfile.runtime-code-overlay` 只适用于后续 Supervisor 代码更新。首次部署应使用完整镜像路径，并在构建前核对基础镜像、平台插件和 Skill 的授权。通过 `scripts/build-runtime-v3-image-dev.sh` 对应的构建入口生成镜像与身份，填入 `HERMES_RUNTIME_IMAGE`、`HERMES_RUNTIME_IMAGE_MANIFEST_DIGEST` 和 `HERMES_RUNTIME_IMAGE_CONFIG_ID`。该脚本会写镜像仓库/环境配置，运行前阅读脚本和参数，明确目标 registry 与集群。

创建 namespace、Runtime ServiceAccount、PV/PVC、Knative/Kourier 入口并核对 `HERMES_RUNTIME_STORAGE_TARGET` 的设备、挂载、权限和单写者约束。管理端资源视图所需的 RBAC 示例在 `deploy/kubernetes/admin-resource-reader-rbac.yaml`；先核对权限，将其中的 namespace 调整为客户的 `HERMES_RUNTIME_NAMESPACE`，再使用 `scripts/install-admin-kubernetes-access.sh` 安装。不要直接复用其他环境的 `runtime_root_host_path`、PVC 或旧 KService。首次登录后，应核对 Binding、KService、Pod Ready、live imageID、会话目录和首个 Run。

## 6. 构建并启动 Web/Backend

标准 OAuth2 模式下，前端构建从 `HERMES_AUTH_MODE=oauth2` 选择登录流程；登录密钥只保留在 Backend。

```bash
bash scripts/check-env.sh prod
bash scripts/build-public-prod.sh
bash scripts/start-public.sh prod
curl -fsS http://127.0.0.1:8011/api/v1/readyz
```

`build-public-prod.sh` 发布静态资源到 `PUBLIC_RELEASE_ROOT`；`start-public.sh prod` 只启动/重启 Backend，不初始化数据库、不切换 Runtime 镜像。两者都要求在新的 Git 仓库中有当前提交 SHA。若检查失败，按报错修正客户配置；不要改为连接原项目环境。

## 7. 桌面端

桌面包在构建时固定客户平台 origin。构建机显式设置：

```bash
export COWORK_SERVER_ORIGIN=https://app.example.com
export OAUTH2_DESKTOP_ALLOWED_ORIGINS='["https://idp.example.com"]'
npm --prefix hermes-desktop run build
```

将示例域名改为真实域名。打包用 `npm --prefix hermes-desktop run package:win` 或 `package:mac`；签名、更新源和分发地址由客户自行配置。打包后检查构建产物里的平台 origin 与允许跳转域，不向客户分发含示例域名的安装包。

## 8. 验收

- OAuth2 登录、登出与管理权限；浏览器和桌面端各做一次。
- 管理端发布一个客户可用模型，用户创建个人 Runtime 并发起首问、追问、取消和恢复。
- 浏览器 WebSocket 与文件上传/下载通过 Nginx 和 Kourier；Backend 不承载实时 Token。
- 看板、专家/技能、个人知识、应用 Agent 按已配置的客户依赖逐项验收。
- 首问和热 Session 的 TTFT 在目标网络实测；构建通过不等于 TTFT 验收。

企业知识 Data Service 与业务知识资产不在本试用包内。需要企业语义查询时，先由客户建立自己的资产、权限与服务，再配置 `KNOWLEDGE_DATA_SERVICE_BASE_URL` 和对应断言密钥。
