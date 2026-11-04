"""离职员工账号全量资源删除指导脚本。

按 docs/runbooks/2026-10-30-001-离职员工账号资源全量删除指导.md 的分步流程,
对单个工号执行:只读盘点、备份、跨用户解绑、应用 Agent 生命周期删除、
个人 Runtime K8s/宿主机清理、DB mass delete 和事后验证。

用法(必须在 backend 目录下用仓库 venv 运行,Settings 按当前目录解析 .env):

    cd backend
    .venv/bin/python scripts/offboard_user_cleanup.py <工号> inventory
    .venv/bin/python scripts/offboard_user_cleanup.py <工号> backup [--out-dir DIR]
    .venv/bin/python scripts/offboard_user_cleanup.py <工号> unbind-shared [--apply]
    .venv/bin/python scripts/offboard_user_cleanup.py <工号> delete-app-agents [--apply]
    .venv/bin/python scripts/offboard_user_cleanup.py <工号> delete-runtime-infra [--apply]
    .venv/bin/python scripts/offboard_user_cleanup.py <工号> db-delete [--apply]
    .venv/bin/python scripts/offboard_user_cleanup.py <工号> verify

安全设计:
- inventory/verify 只读;backup 只写指定输出目录。
- 其余子命令默认 dry-run,必须显式 --apply,且执行前要求输入工号二次确认。
- kubectl/宿主机目录只处理从 DB binding 或锚定正则解析出的精确对象名,
  删除前逐项打印;宿主机目录删除前做 ROOT 前缀与符号链接校验。
- 凭证不落盘、不回显;mysqldump 密码仅经 MYSQL_PWD 环境变量传递。
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import text  # noqa: E402

from app.config import Settings  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402

# ---------------------------------------------------------------------------
# 公共子查询:全部以目标用户为锚点动态推导,不携带任何硬编码 ID。
# ---------------------------------------------------------------------------

WS_SUB = "(SELECT id FROM cloud_hermes_profile WHERE user_id = :u)"
AGENTS_SUB = "(SELECT id FROM application_agent WHERE created_by_user_id = :u)"
OP_SUB = f"(SELECT id FROM runtime_agent_profile WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB})"
OE_SUB = "(SELECT id FROM expert_package WHERE owner_user_id = :u)"
OV_SUB = f"(SELECT id FROM expert_package_version WHERE expert_package_id IN {OE_SUB})"
OS_SUB = "(SELECT id FROM skill WHERE owner_user_id = :u)"
OM_SUB = "(SELECT id FROM mcp WHERE owner_user_id = :u)"
OKB_SUB = "(SELECT id FROM knowledge_base WHERE owner_user_id = :u)"
BINDINGS_SUB = f"(SELECT id FROM hermes_runtime_binding WHERE runtime_workspace_id IN {WS_SUB})"
RT_IDS_SUB = f"(SELECT runtime_id FROM hermes_runtime_binding WHERE runtime_workspace_id IN {WS_SUB})"
ST_SUB = f"(SELECT id FROM scheduled_task WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB})"
CH_SUB = (
    f"(SELECT id FROM im_channel WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB} "
    f"OR application_agent_id IN {AGENTS_SUB})"
)
INB_SUB = (
    f"(SELECT id FROM im_inbound_message WHERE channel_id IN {CH_SUB} "
    f"OR sender_platform_user_id = :u OR application_agent_id IN {AGENTS_SUB})"
)
# EXT = 仍引用目标用户资产、但不属于目标用户的 Profile(unbind 后通常只剩 disabled 空壳)。
EXT_SUB = (
    "(SELECT DISTINCT p.id FROM runtime_agent_profile p "
    "WHERE (p.user_id IS NULL OR p.user_id <> :u) AND ("
    f"p.id IN (SELECT runtime_agent_profile_id FROM installed_expert_binding WHERE expert_package_id IN {OE_SUB}) "
    f"OR p.id IN (SELECT runtime_agent_profile_id FROM profile_mcp_binding WHERE mcp_id IN {OM_SUB}) "
    f"OR p.id IN (SELECT runtime_agent_profile_id FROM profile_knowledge_binding WHERE knowledge_base_id IN {OKB_SUB})"
    "))"
)
TPL_SUB = (
    "(SELECT t.id FROM plaza_template t WHERE t.visibility = 'private' AND t.id IN "
    f"(SELECT template_id FROM template_version WHERE source_expert_package_id IN {OE_SUB}))"
)


async def q(db, sql: str, params: dict | None = None) -> list[dict]:
    result = await db.execute(text(sql), params or {})
    keys = result.keys()
    return [dict(zip(keys, row)) for row in result.all()]


async def q_scalar(db, sql: str, params: dict | None = None):
    return (await db.execute(text(sql), params or {})).scalar()


async def table_exists(db, name: str) -> bool:
    row = await db.execute(
        text("SELECT COUNT(*) FROM information_schema.tables "
             "WHERE table_schema = DATABASE() AND table_name = :n"),
        {"n": name},
    )
    return row.scalar() > 0


async def column_exists(db, table: str, column: str) -> bool:
    row = await db.execute(
        text("SELECT COUNT(*) FROM information_schema.columns "
             "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"),
        {"t": table, "c": column},
    )
    return row.scalar() > 0


async def resolve_target(db, code: str, *, allow_missing: bool = False) -> dict | None:
    """解析工号 → sys_user + user + workspace;不唯一或缺失时直接报错退出。

    链路说明:sys_user.user_id 是外部 HR 系统 ID,与 user.id 无关;
    账号真实关联是 user.user_code = sys_user.code(兜底 hrm_user_id)。
    """
    sys_rows = await q(db, "SELECT user_id, code, name FROM sys_user WHERE code = :c", {"c": code})
    if not sys_rows:
        if allow_missing:
            return None
        sys.exit(f"[abort] sys_user 中不存在工号 {code}(可能已删除);如需事后核验请用 verify")
    if len(sys_rows) > 1:
        sys.exit(f"[abort] sys_user 工号 {code} 命中 {len(sys_rows)} 行,拒绝在歧义身份上执行")
    sysu = sys_rows[0]
    user_rows = await q(db, "SELECT id, org_id FROM user WHERE user_code = :c", {"c": code})
    if not user_rows:
        user_rows = await q(
            db, "SELECT id, org_id FROM user WHERE hrm_user_id = :h", {"h": sysu["user_id"]}
        )
    if not user_rows:
        if allow_missing:
            return None
        sys.exit(f"[abort] user 表按 user_code/hrm_user_id 均未命中工号 {code}")
    if len(user_rows) > 1:
        sys.exit(f"[abort] user 表工号 {code} 命中 {len(user_rows)} 行,拒绝在歧义身份上执行")
    u = user_rows[0]
    workspaces = await q(db, "SELECT id, name FROM cloud_hermes_profile WHERE user_id = :u", {"u": u["id"]})
    return {
        "code": code,
        "sysu": sysu,
        "user_id": u["id"],
        "org_id": u["org_id"],
        "workspaces": workspaces,
    }


async def gather(db, target: dict) -> dict:
    """只读收集全部资源与计数。"""
    p = {"u": target["user_id"], "c": target["code"]}
    g: dict = {"target": target}
    g["bindings"] = await q(
        db,
        f"SELECT id, runtime_id, status, k8s_namespace, knative_service, "
        "runtime_root_pvc, runtime_root_pvc_uid, runtime_root_host_path "
        f"FROM hermes_runtime_binding WHERE runtime_workspace_id IN {WS_SUB}",
        p,
    )
    g["app_agents"] = await q(
        db,
        "SELECT id, name, status, lifecycle_status FROM application_agent "
        "WHERE created_by_user_id = :u",
        p,
    )
    agent_ids = tuple(a["id"] for a in g["app_agents"])
    g["app_bindings"] = (
        await q(
            db,
            "SELECT application_agent_id, id, k8s_namespace, knative_service, "
            "runtime_root_pvc, runtime_root_host_path FROM application_agent_runtime_binding "
            "WHERE application_agent_id IN :a",
            {**p, "a": agent_ids},
        )
        if agent_ids
        else []
    )
    g["experts"] = await q(db, f"SELECT id, name FROM expert_package WHERE owner_user_id = :u OR origin_runtime_workspace_id IN {WS_SUB}", p)
    g["skills"] = await q(db, "SELECT id, name FROM skill WHERE owner_user_id = :u", p)
    g["mcps"] = await q(db, "SELECT id, name FROM mcp WHERE owner_user_id = :u", p)
    g["kbs"] = await q(db, "SELECT id, name, provider, ragflow_id AS dataset_id FROM knowledge_base WHERE owner_user_id = :u", p)
    g["plaza_private"] = await q(db, f"SELECT id, name, visibility FROM plaza_template WHERE id IN {TPL_SUB}", p)
    g["plaza_public_refs"] = await q(
        db,
        "SELECT DISTINCT t.id, t.name FROM plaza_template t JOIN template_version v ON v.template_id = t.id "
        f"WHERE t.visibility <> 'private' AND (v.source_expert_package_id IN {OE_SUB})",
        p,
    )
    g["expert_installs_by_others"] = await q(
        db,
        "SELECT b.id, b.user_id AS installer_user_id, b.runtime_workspace_id, "
        "b.runtime_agent_profile_id, b.expert_package_id, p.org_id AS installer_org_id "
        "FROM installed_expert_binding b "
        "JOIN runtime_agent_profile p ON p.id = b.runtime_agent_profile_id "
        f"WHERE b.expert_package_id IN {OE_SUB} AND b.user_id <> :u",
        p,
    )
    g["mcp_refs_by_others"] = await q(
        db,
        "SELECT pb.id, pb.runtime_agent_profile_id, pb.mcp_id, p.user_id AS profile_user_id "
        "FROM profile_mcp_binding pb JOIN runtime_agent_profile p ON p.id = pb.runtime_agent_profile_id "
        f"WHERE pb.mcp_id IN {OM_SUB} AND (p.user_id IS NULL OR p.user_id <> :u)",
        p,
    )
    g["kb_bound_external_profiles"] = await q(
        db,
        "SELECT pb.knowledge_base_id, pb.runtime_agent_profile_id, p.profile_kind, p.status, p.user_id "
        "FROM profile_knowledge_binding pb JOIN runtime_agent_profile p ON p.id = pb.runtime_agent_profile_id "
        f"WHERE pb.knowledge_base_id IN {OKB_SUB} AND (p.user_id IS NULL OR p.user_id <> :u)",
        p,
    )
    artifact_uris = await q(
        db,
        f"SELECT a.storage_uri FROM expert_version_artifact a "
        f"WHERE a.expert_package_version_id IN {OV_SUB}",
        p,
    )
    marker = "expert-release-artifacts"
    prefixes: set[str] = set()
    for row in artifact_uris:
        parts = Path(str(row["storage_uri"])).parts
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                prefixes.add(parts[idx + 1])
    g["artifact_dirs"] = sorted(prefixes)
    g["counts"] = {
        "runtime_projection_receipt": await q_scalar(db, f"SELECT COUNT(*) FROM runtime_projection_receipt WHERE runtime_id IN {RT_IDS_SUB}", p),
        "runtime_admission_audit": await q_scalar(db, f"SELECT COUNT(*) FROM runtime_admission_audit WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}", p),
        "model_usage_log": await q_scalar(db, f"SELECT COUNT(*) FROM model_usage_log WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}", p),
        "chat_log": await q_scalar(db, f"SELECT COUNT(*) FROM chat_log WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}", p),
        "runtime_run": await q_scalar(db, f"SELECT COUNT(*) FROM runtime_run WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}", p),
        "runtime_session": await q_scalar(db, f"SELECT COUNT(*) FROM runtime_session WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}", p),
        "admin_work_no_permissions": await q_scalar(db, "SELECT COUNT(*) FROM admin_work_no_permissions WHERE work_no = :c", p),
    }
    return g


def print_inventory(g: dict) -> None:
    t = g["target"]
    print(f"工号 {t['code']}  sys_user.user_id={t['sysu']['user_id']}  name={t['sysu']['name']!r}")
    print(f"user.id={t['user_id']}  org_id={t['org_id']}  workspaces={[w['id'] for w in t['workspaces']]}")
    print("\n== 个人 Runtime bindings ==")
    for b in g["bindings"]:
        print(f"  binding {b['id']}  {b['runtime_id']}  ksvc={b['knative_service']}  ns={b['k8s_namespace']}")
        print(f"    pvc={b['runtime_root_pvc']}  uid={b['runtime_root_pvc_uid']}")
        print(f"    host={b['runtime_root_host_path']}  status={b['status']}")
    print("\n== 应用 Agent ==")
    for a in g["app_agents"]:
        print(f"  agent {a['id']}  {a['name']!r}  status={a['status']}  lifecycle={a['lifecycle_status']}")
    for b in g["app_bindings"]:
        print(f"    binding {b['id']}  ksvc={b['knative_service']}  pvc={b['runtime_root_pvc']}  host={b['runtime_root_host_path']}")
    print("\n== 资产 ==")
    print(f"  expert_package: {[e['id'] for e in g['experts']]}")
    print(f"  skill: {[s['id'] for s in g['skills']]}")
    print(f"  mcp: {[m['id'] for m in g['mcps']]}")
    print(f"  knowledge_base: {[(k['id'], k['provider'], k['dataset_id']) for k in g['kbs']]}")
    print(f"  plaza 私有模板(整删): {[x['id'] for x in g['plaza_private']]}")
    print(f"  plaza 公共模板(仅解除 expert 引用,保留): {[x['id'] for x in g['plaza_public_refs']]}")
    print(f"  expert 产物目录前缀: {g['artifact_dirs']}")
    print("\n== 跨用户引用(必须先解除) ==")
    for b in g["expert_installs_by_others"]:
        print(f"  expert {b['expert_package_id']} 安装于 user {b['installer_user_id']} profile {b['runtime_agent_profile_id']}")
    for b in g["mcp_refs_by_others"]:
        print(f"  mcp {b['mcp_id']} 引用于 profile {b['runtime_agent_profile_id']}(user {b['profile_user_id']})")
    for b in g["kb_bound_external_profiles"]:
        print(f"  kb {b['knowledge_base_id']} 绑定于 profile {b['runtime_agent_profile_id']}(user {b['profile_user_id']}, {b['profile_kind']}/{b['status']})")
    print("\n== 大量表计数(预估删除量) ==")
    for k, v in g["counts"].items():
        print(f"  {k}: {v}")


def confirm_apply(args: argparse.Namespace, target: dict, action: str) -> None:
    if not args.apply:
        print(f"[dry-run] 以上为 {action} 的计划;确认无误后加 --apply 执行")
        sys.exit(0)
    typed = input(f"确认{action}:输入工号 {target['code']} 并回车 > ")
    if typed.strip() != target["code"]:
        sys.exit("[abort] 工号不匹配,已取消")


def run(cmd: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=capture, text=True)


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------

def _mysql_settings() -> dict:
    s = Settings()
    url = getattr(s, "database_url", None) or getattr(s, "DATABASE_URL")
    from sqlalchemy.engine import make_url

    u = make_url(url)
    return {
        "host": u.host or "127.0.0.1",
        "port": u.port or 3306,
        "user": u.username or "root",
        "password": u.password or "",
        "db": u.database,
    }


def k8s_object_names(g: dict) -> dict:
    """按锚定正则解析目标用户当前及历史代的精确 K8s 对象名。"""
    out = {"ksvc": [], "secret": [], "pvc": [], "pv": [], "namespace": set()}
    bases = set()
    pvc_bases = set()
    for b in g["bindings"]:
        out["namespace"].add(b["k8s_namespace"])
        bases.add(re.sub(r"-g\d+$", "", b["knative_service"]))
        if b["runtime_root_pvc"]:
            pvc_bases.add(re.sub(r"-g\d+$", "", b["runtime_root_pvc"]))
    for ns in out["namespace"]:
        r = run(["kubectl", "get", "ksvc", "-n", ns, "-o", "name"])
        for line in (r.stdout or "").splitlines():
            name = line.split("/")[-1]
            if any(re.fullmatch(re.escape(base) + r"(-g\d+)?", name) for base in bases):
                out["ksvc"].append((ns, name))
        r = run(["kubectl", "get", "pvc", "-n", ns, "-o", "name"])
        for line in (r.stdout or "").splitlines():
            name = line.split("/")[-1]
            if any(re.fullmatch(re.escape(base) + r"(-g\d+)?", name) for base in pvc_bases):
                out["pvc"].append((ns, name))
                out["pv"].append((None, f"{name}-pv"))
        r = run(["kubectl", "get", "secret", "-n", ns, "-o", "name"])
        for line in (r.stdout or "").splitlines():
            name = line.split("/")[-1]
            if any(
                re.fullmatch(re.escape(base) + r"(-g\d+)?-(api-key|platform-control)", name)
                for base in bases
            ):
                out["secret"].append((ns, name))
    # 兜底:binding 记录的当前对象即使 kubectl 已查不到也保留(用于备份 YAML 的 --ignore-not-found)
    have_ksvc = {n for _, n in out["ksvc"]}
    have_pvc = {n for _, n in out["pvc"]}
    for b in g["bindings"]:
        if b["knative_service"] not in have_ksvc:
            out["ksvc"].append((b["k8s_namespace"], b["knative_service"]))
        if b["runtime_root_pvc"] and b["runtime_root_pvc"] not in have_pvc:
            out["pvc"].append((b["k8s_namespace"], b["runtime_root_pvc"]))
            out["pv"].append((None, f"{b['runtime_root_pvc']}-pv"))
    return out


async def cmd_backup(args: argparse.Namespace, db, target: dict, g: dict) -> None:
    out_dir = Path(args.out_dir or f"/root/backups/{datetime.now():%Y-%m-%d}-{target['code']}-cleanup")
    out_dir.mkdir(parents=True, exist_ok=True)
    m = _mysql_settings()
    dump = out_dir / f"hermes_cloud_DB-pre{target['code']}.sql.gz"
    print(f"[backup] mysqldump → {dump}")
    env = {"MYSQL_PWD": m["password"], "PATH": "/usr/bin:/bin:/usr/local/bin"}
    with gzip.open(dump, "wb") as fz:
        proc = subprocess.run(
            ["mysqldump", "--ssl-mode=DISABLED", "--single-transaction", "--no-tablespaces",
             "-h", m["host"], "-P", str(m["port"]), "-u", m["user"], m["db"]],
            stdout=subprocess.PIPE, env=env,
        )
        if proc.returncode != 0:
            sys.exit(f"[abort] mysqldump 失败: {proc.stderr.decode(errors='replace')[:500]}")
        fz.write(proc.stdout)
    print(f"[backup] dump 大小: {dump.stat().st_size / 1e6:.1f} MB")

    objs = k8s_object_names(g)
    manifest = {
        "target": {k: target[k] for k in ("code", "user_id", "org_id")},
        "bindings": g["bindings"],
        "k8s": {"ksvc": [n for _, n in objs["ksvc"]], "pvc": [n for _, n in objs["pvc"]],
                "pv": [n for _, n in objs["pv"]], "secret": [n for _, n in objs["secret"]]},
        "artifact_dirs": list(g["artifact_dirs"]),
    }
    for ns, name in objs["ksvc"]:
        r = run(["kubectl", "get", "ksvc", "-n", ns, name, "-o", "yaml"])
        (out_dir / f"ksvc-{name}.yaml").write_text(r.stdout)
    for ns, name in objs["pvc"]:
        r = run(["kubectl", "get", "pvc", "-n", ns, name, "-o", "yaml"])
        (out_dir / f"pvc-{name}.yaml").write_text(r.stdout)
    for _, name in objs["pv"]:
        r = run(["kubectl", "get", "pv", name, "-o", "yaml"])
        (out_dir / f"pv-{name}.yaml").write_text(r.stdout)
    for ns, name in objs["secret"]:
        r = run(["kubectl", "get", "secret", "-n", ns, name, "-o", "yaml"])
        (out_dir / f"secret-{name}.yaml").write_text(r.stdout)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))

    # PVC UID 一致性核对
    for b in g["bindings"]:
        if not (b["runtime_root_pvc"] and b["runtime_root_pvc_uid"]):
            continue
        r = run(["kubectl", "get", "pvc", "-n", b["k8s_namespace"], b["runtime_root_pvc"], "-o", "jsonpath={.metadata.uid}"])
        live = (r.stdout or "").strip()
        if live and live != b["runtime_root_pvc_uid"]:
            print(f"[WARN] PVC {b['runtime_root_pvc']} live uid {live} ≠ DB 记录 {b['runtime_root_pvc_uid']},停止前需人工核对")
    print(f"[backup] 完成: {out_dir}")


# ---------------------------------------------------------------------------
# unbind-shared:跨用户引用解除(走平台服务通道)
# ---------------------------------------------------------------------------

async def cmd_unbind_shared(args: argparse.Namespace, db, target: dict, g: dict) -> None:
    print("计划解除的跨用户引用:")
    print_inventory_refs(g)
    confirm_apply(args, target, "解除跨用户共享资产引用")

    from app.models.runtime_agent_profile import RuntimeAgentProfile
    from app.schemas.profile_desired import DefaultProfileAssetMutation
    from app.services.expert_installations import ExpertInstallationService
    from app.services.personal_knowledge_provider import build_personal_knowledge_provider
    from app.services.profile_desired_mutations import ProfileDesiredMutationService
    from sqlalchemy import select

    failures: list[str] = []
    u = target["user_id"]
    code = target["code"]

    # 1) 他人安装的目标用户专家 → 平台卸载通道
    for b in g["expert_installs_by_others"]:
        op = f"offboard-{code}-expert-uninstall-{b['expert_package_id']}-{b['runtime_agent_profile_id']}"
        try:
            await ExpertInstallationService(db).uninstall(
                org_id=int(b["installer_org_id"]),
                user_id=int(b["installer_user_id"]),
                runtime_workspace_id=int(b["runtime_workspace_id"]),
                runtime_agent_profile_id=int(b["runtime_agent_profile_id"]),
                operation_id=op,
            )
            await db.commit()
            print(f"[ok] expert {b['expert_package_id']} 已从 user {b['installer_user_id']} profile {b['runtime_agent_profile_id']} 卸载")
        except Exception as exc:  # noqa: BLE001 - 逐项容错,汇总报告
            await db.rollback()
            failures.append(f"expert uninstall profile {b['runtime_agent_profile_id']}: {type(exc).__name__}: {exc}")

    # 2) 他人 Profile 引用的目标用户 MCP → rollout 撤销通道
    for b in g["mcp_refs_by_others"]:
        try:
            profile = (
                await db.execute(
                    select(RuntimeAgentProfile).where(RuntimeAgentProfile.id == int(b["runtime_agent_profile_id"]))
                )
            ).scalar_one()
            await ProfileDesiredMutationService(db).refresh_asset_for_rollout(
                profile=profile,
                asset_type="mcp",
                asset_id=int(b["mcp_id"]),
                action="delete",
                rollout_id=f"offboard-{code}-mcp-{b['mcp_id']}-{b['runtime_agent_profile_id']}",
                created_by_user_id=int(u),
            )
            await db.commit()
            print(f"[ok] mcp {b['mcp_id']} 已从 profile {b['runtime_agent_profile_id']} 撤销")
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            failures.append(f"mcp revoke profile {b['runtime_agent_profile_id']}: {type(exc).__name__}: {exc}")

    # 3) 目标用户拥有的 KB:外部数据集删除 → 推进外部 default Profile heads → 删 DB 行
    for kb in g["kbs"]:
        try:
            provider_name = str(kb["provider"] or "")
            dataset_id = str(kb["dataset_id"] or "")
            if provider_name and dataset_id:
                await build_personal_knowledge_provider(provider_name).delete_dataset(dataset_id)
                print(f"[ok] kb {kb['id']} 外部数据集 {dataset_id} 已删除")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"kb {kb['id']} dataset delete: {type(exc).__name__}: {exc}")
        try:
            rows = await q(
                db,
                "SELECT pb.id AS binding_id, p.org_id, p.user_id AS profile_user_id, p.runtime_workspace_id, "
                "pb.runtime_agent_profile_id FROM profile_knowledge_binding pb "
                "JOIN runtime_agent_profile p ON p.id = pb.runtime_agent_profile_id "
                "WHERE pb.knowledge_base_id = :kb AND p.profile_kind = 'default' AND p.status = 'active' "
                "AND (p.user_id IS NULL OR p.user_id <> :u)",
                {"kb": kb["id"], "u": u},
            )
            for row in rows:
                await ProfileDesiredMutationService(db).mutate_default_asset(
                    DefaultProfileAssetMutation(
                        org_id=int(row["org_id"]),
                        user_id=int(row["profile_user_id"]),
                        runtime_workspace_id=int(row["runtime_workspace_id"]),
                        runtime_agent_profile_id=int(row["runtime_agent_profile_id"]),
                        asset_type="knowledge",
                        action="uninstall",
                        asset_id=int(kb["id"]),
                        operation_id=f"offboard-{code}-kb-{kb['id']}-{row['binding_id']}",
                    )
                )
                await db.commit()
                print(f"[ok] kb {kb['id']} 已从 profile {row['runtime_agent_profile_id']} desired heads 移除")
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            failures.append(f"kb {kb['id']} head advance: {type(exc).__name__}: {exc}")
        try:
            await db.execute(text("DELETE FROM knowledge_base_file WHERE kb_id = :kb"), {"kb": kb["id"]})
            await db.execute(
                text("DELETE FROM permission WHERE asset_type = 'knowledge_base' AND asset_id = :kb"),
                {"kb": kb["id"]},
            )
            await db.execute(text("DELETE FROM profile_knowledge_binding WHERE knowledge_base_id = :kb"), {"kb": kb["id"]})
            await db.execute(text("DELETE FROM knowledge_base WHERE id = :kb"), {"kb": kb["id"]})
            await db.commit()
            print(f"[ok] kb {kb['id']} DB 行已删除")
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            failures.append(f"kb {kb['id']} db rows: {type(exc).__name__}: {exc}")

    if failures:
        print("\n[FAIL] 以下步骤失败,必须在继续 db-delete 前按 runbook 人工处理:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("[done] 跨用户引用解除完成")


def print_inventory_refs(g: dict) -> None:
    for b in g["expert_installs_by_others"]:
        print(f"  expert {b['expert_package_id']} → installer user {b['installer_user_id']} profile {b['runtime_agent_profile_id']}")
    for b in g["mcp_refs_by_others"]:
        print(f"  mcp {b['mcp_id']} → profile {b['runtime_agent_profile_id']}")
    for b in g["kb_bound_external_profiles"]:
        print(f"  kb {b['knowledge_base_id']} → profile {b['runtime_agent_profile_id']}")
    for kb in g["kbs"]:
        print(f"  kb {kb['id']} ({kb['provider']}/{kb['dataset_id']}) 整体删除")


# ---------------------------------------------------------------------------
# delete-app-agents:平台生命周期 worker 通道
# ---------------------------------------------------------------------------

async def cmd_delete_app_agents(args: argparse.Namespace, db, target: dict, g: dict) -> None:
    if not g["app_agents"]:
        print("[skip] 无应用 Agent")
        return
    print("将置为 deleting 并交给 Backend worker 删除(K8s + 宿主机 + DB 由平台完成):")
    for a in g["app_agents"]:
        print(f"  agent {a['id']}  {a['name']!r}")
    confirm_apply(args, target, "删除应用 Agent")
    u = target["user_id"]
    await db.execute(
        text("UPDATE application_agent SET status='disabled', lifecycle_status='deleting', updated_at=NOW() WHERE created_by_user_id = :u AND lifecycle_status <> 'deleting'"),
        {"u": u},
    )
    await db.commit()
    print("[wait] 轮询 worker 完成(要求 Backend 正在运行)…")
    for _ in range(60):
        await asyncio.sleep(2)
        left = await q_scalar(db, "SELECT COUNT(*) FROM application_agent WHERE created_by_user_id = :u", {"u": u})
        if left == 0:
            print("[ok] 应用 Agent 已全部删除")
            return
        err = await q(
            db,
            "SELECT id, delete_last_error_code FROM application_agent WHERE created_by_user_id = :u AND delete_last_error_code IS NOT NULL",
            {"u": u},
        )
        for e in err:
            print(f"  [worker] agent {e['id']} 重试中,上次错误: {e['delete_last_error_code']}")
    sys.exit("[abort] 120s 内未完成;检查 Backend worker 日志后重跑本命令(幂等)")


# ---------------------------------------------------------------------------
# delete-runtime-infra:个人 Runtime 的 K8s + 宿主机
# ---------------------------------------------------------------------------

def _safe_rmtree(path: Path, root: Path, label: str) -> None:
    resolved = path.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        sys.exit(f"[abort] {label} 路径 {resolved} 不在 {root_resolved} 之下,拒绝删除")
    if path.is_symlink():
        sys.exit(f"[abort] {label} 路径 {path} 是符号链接,拒绝删除")
    if path.exists():
        shutil.rmtree(path)
        print(f"[ok] 已删除宿主机目录 {resolved}")


def _host_runtime_dirs(g: dict) -> list[Path]:
    """个人 Runtime 当前代 + 历史 g 代目录(锚定正则,仅限 HERMES_RUNTIME_ROOT 内)。"""
    s = Settings()
    root = Path(s.HERMES_RUNTIME_ROOT).resolve(strict=False)
    dirs: list[Path] = []
    for b in g["bindings"]:
        host = b["runtime_root_host_path"]
        if not host:
            continue
        p = Path(host)
        base = re.sub(r"-g\d+$", "", p.name)
        if not p.parent.resolve(strict=False).is_relative_to(root):
            print(f"[WARN] 跳过越界路径 {p}(不在 {root} 下,需人工核对)")
            continue
        dirs.append(p)
        for sibling in p.parent.glob(f"{base}-g*"):
            if re.fullmatch(re.escape(base) + r"-g\d+", sibling.name):
                dirs.append(sibling)
    return dirs


async def cmd_delete_runtime_infra(args: argparse.Namespace, db, target: dict, g: dict) -> None:
    s = Settings()
    objs = k8s_object_names(g)
    host_dirs = _host_runtime_dirs(g)
    agent_dirs = [
        Path(s.HERMES_APPLICATION_AGENT_RUNTIME_ROOT) / str(target["org_id"]) / str(a["id"])
        for a in g["app_agents"]
    ]
    artifact_root = Path(s.HERMES_PUBLIC_ASSET_ROOT) / "expert-release-artifacts"
    artifact_dirs = [artifact_root / name for name in g["artifact_dirs"]]

    print("将删除的 K8s 对象(精确名,含历史 g 代):")
    for ns, name in objs["ksvc"]:
        print(f"  ksvc      {ns}/{name}")
    for ns, name in objs["secret"]:
        print(f"  secret    {ns}/{name}")
    for ns, name in objs["pvc"]:
        print(f"  pvc       {ns}/{name}")
    for _, name in objs["pv"]:
        print(f"  pv        {name}  (local-path Retain,PVC 删除后必须手动)")
    print("将删除的宿主机目录:")
    for d in host_dirs + agent_dirs + artifact_dirs:
        print(f"  {d}")
    confirm_apply(args, target, "删除个人 Runtime K8s 与宿主机资源")

    for ns, name in objs["ksvc"]:
        run(["kubectl", "delete", "ksvc", "-n", ns, name, "--ignore-not-found"])
        print(f"[ok] ksvc {ns}/{name}")
    for ns, name in objs["secret"]:
        run(["kubectl", "delete", "secret", "-n", ns, name, "--ignore-not-found"])
        print(f"[ok] secret {ns}/{name}")
    for ns, name in objs["pvc"]:
        run(["kubectl", "delete", "pvc", "-n", ns, name, "--ignore-not-found"])
        print(f"[ok] pvc {ns}/{name}")
    for _, name in objs["pv"]:
        run(["kubectl", "delete", "pv", name, "--ignore-not-found"])
        print(f"[ok] pv {name}")
    for d in host_dirs:
        _safe_rmtree(d, Path(s.HERMES_RUNTIME_ROOT), "runtime")
    for d in agent_dirs:
        _safe_rmtree(d, Path(s.HERMES_APPLICATION_AGENT_RUNTIME_ROOT), "application-agent")
    for d in artifact_dirs:
        _safe_rmtree(d, artifact_root, "expert-artifact")
    print("[done] Runtime 基础设施删除完成")


# ---------------------------------------------------------------------------
# db-delete:单事务 mass delete(须在 unbind/agent/infra 完成后执行)
# ---------------------------------------------------------------------------

PRE_UPDATES = [
    f"UPDATE runtime_kanban_lease_event SET runtime_agent_profile_id = NULL WHERE runtime_agent_profile_id IN {EXT_SUB}",
    f"UPDATE runtime_agent_profile_revision SET expert_package_version_id = NULL WHERE runtime_agent_profile_id IN {EXT_SUB} AND expert_package_version_id IN {OV_SUB}",
    # 注意:UPDATE 目标表不能出现在同级 FROM 子查询(MySQL 1093),必须包一层派生表
    f"UPDATE runtime_agent_profile SET expert_package_id = NULL WHERE id IN (SELECT id FROM ({EXT_SUB}) AS _ext_ids) AND expert_package_id IN {OE_SUB}",
    f"UPDATE template_version SET source_expert_package_id = NULL WHERE source_expert_package_id IN {OE_SUB}",
    f"UPDATE template_version SET source_expert_package_version_id = NULL WHERE source_expert_package_version_id IN {OV_SUB}",
]

DELETES = [
    # plaza / 模板链(注意 1093:目标表出现在子查询时必须包派生表)
    f"DELETE FROM template_version WHERE template_id IN (SELECT id FROM ({TPL_SUB}) AS _tpl_ids)",
    f"DELETE FROM plaza_template WHERE id IN (SELECT id FROM ({TPL_SUB}) AS _tpl_ids)",
    f"DELETE FROM template_review WHERE submitter_user_id = :u OR reviewer_user_id = :u OR expert_package_id IN {OE_SUB} OR expert_package_version_id IN {OV_SUB}",
    # runtime 执行链
    f"DELETE FROM runtime_projection_receipt WHERE runtime_id IN {RT_IDS_SUB}",
    f"DELETE FROM runtime_admission_audit WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB} OR runtime_id IN {RT_IDS_SUB}",
    f"DELETE FROM model_usage_log WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}",
    f"DELETE FROM chat_file_artifact WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}",
    f"DELETE FROM chat_log WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}",
    f"DELETE FROM runtime_clarify_request WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}",
    f"DELETE FROM runtime_run WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB} OR stopped_by_user_id = :u",
    f"DELETE FROM runtime_session WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}",
    f"DELETE FROM runtime_kanban_lease_event WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}",
    f"DELETE FROM runtime_audit_log WHERE actor_user_id = :u OR runtime_workspace_id IN {WS_SUB}",
    f"DELETE FROM runtime_mcp_registry_state WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB} OR runtime_id IN {RT_IDS_SUB} OR runtime_binding_id IN {BINDINGS_SUB}",
    f"DELETE FROM runtime_mcp_registry_entry WHERE mcp_id IN {OM_SUB}",
    f"DELETE FROM runtime_platform_control_key WHERE runtime_id IN {RT_IDS_SUB}",
    f"DELETE FROM hermes_runtime_storage_generation WHERE runtime_binding_id IN {BINDINGS_SUB}",
    f"DELETE FROM hermes_runtime_schema_intent WHERE runtime_binding_id IN {BINDINGS_SUB} OR runtime_id IN {RT_IDS_SUB}",
    f"DELETE FROM hermes_runtime_schema_source_facts WHERE runtime_binding_id IN {BINDINGS_SUB} OR runtime_id IN {RT_IDS_SUB}",
    f"DELETE FROM hermes_runtime_incomplete_intent_supersession WHERE runtime_binding_id IN {BINDINGS_SUB} OR runtime_id IN {RT_IDS_SUB}",
    f"DELETE FROM hermes_upgrade_event WHERE runtime_binding_id IN {BINDINGS_SUB}",
    f"DELETE FROM hermes_upgrade_target WHERE runtime_binding_id IN {BINDINGS_SUB}",
    f"DELETE FROM hermes_runtime_binding WHERE id IN (SELECT id FROM ({BINDINGS_SUB}) AS _binding_ids)",
    # profile / 收敛链
    f"DELETE FROM profile_convergence_attempt WHERE profile_convergence_job_id IN (SELECT id FROM profile_convergence_job WHERE runtime_workspace_id IN {WS_SUB}) OR runtime_agent_profile_id IN {OP_SUB}",
    f"DELETE FROM profile_convergence_job WHERE runtime_workspace_id IN {WS_SUB}",
    f"DELETE FROM asset_rollout_target WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB} OR runtime_agent_profile_id IN {OP_SUB}",
    "DELETE FROM asset_rollout_batch WHERE created_by_user_id = :u",
    f"DELETE FROM runtime_agent_profile_revision WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB} OR runtime_agent_profile_id IN {OP_SUB}",
    f"DELETE FROM profile_skill_binding WHERE runtime_workspace_id IN {WS_SUB} OR skill_id IN {OS_SUB} OR runtime_agent_profile_id IN {OP_SUB}",
    f"DELETE FROM profile_mcp_binding WHERE runtime_workspace_id IN {WS_SUB} OR mcp_id IN {OM_SUB} OR runtime_agent_profile_id IN {OP_SUB}",
    f"DELETE FROM profile_knowledge_binding WHERE runtime_workspace_id IN {WS_SUB} OR runtime_agent_profile_id IN {OP_SUB}",
    f"DELETE FROM personal_config_bundle_artifact WHERE runtime_workspace_id IN {WS_SUB}",
    f"DELETE FROM runtime_agent_profile WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}",
    # 专家链
    f"DELETE FROM expert_kanban_collaboration_task WHERE runtime_workspace_id IN {WS_SUB} OR collaboration_id IN (SELECT id FROM expert_kanban_collaboration WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB})",
    f"DELETE FROM expert_kanban_collaboration WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}",
    f"DELETE FROM expert_profile_operation WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB} OR runtime_agent_profile_id IN {OP_SUB}",
    f"DELETE FROM installed_expert_binding WHERE expert_package_id IN {OE_SUB} OR user_id = :u OR runtime_workspace_id IN {WS_SUB} OR runtime_agent_profile_id IN {OP_SUB}",
    f"DELETE FROM expert_version_knowledge_snapshot WHERE expert_package_version_id IN {OV_SUB}",
    f"DELETE FROM expert_version_artifact WHERE expert_package_version_id IN {OV_SUB}",
    f"DELETE FROM expert_package_version WHERE expert_package_id IN {OE_SUB}",
    f"DELETE FROM expert_package WHERE owner_user_id = :u OR origin_runtime_workspace_id IN {WS_SUB}",
    # Skill 链
    f"DELETE FROM user_skillhub_installation WHERE skill_id IN {OS_SUB} OR user_id = :u OR runtime_workspace_id IN {WS_SUB} OR runtime_agent_profile_id IN {OP_SUB}",
    f"DELETE FROM skill_version WHERE skill_id IN {OS_SUB}",
    "DELETE FROM skill WHERE owner_user_id = :u",
    "DELETE FROM personal_skill_category WHERE owner_user_id = :u",
    # MCP 链
    f"DELETE FROM mcp_version WHERE mcp_id IN {OM_SUB}",
    f"DELETE FROM mcp_slug_lease WHERE mcp_id IN {OM_SUB}",
    "DELETE FROM mcp WHERE owner_user_id = :u",
    # 定时任务 + IM 链
    f"DELETE FROM scheduled_task_execution_lease WHERE task_id IN {ST_SUB}",
    f"DELETE FROM im_outbound_delivery WHERE channel_id IN {CH_SUB} OR scheduled_task_id IN {ST_SUB}",
    f"DELETE FROM scheduled_task WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB}",
    f"DELETE FROM im_delivery_checkpoint WHERE inbound_message_id IN {INB_SUB}",
    f"DELETE FROM im_message_attachment WHERE inbound_message_id IN {INB_SUB}",
    f"DELETE FROM im_inbound_message WHERE channel_id IN {CH_SUB} OR sender_platform_user_id = :u OR application_agent_id IN {AGENTS_SUB}",
    f"DELETE FROM im_execution_batch WHERE channel_id IN {CH_SUB}",
    f"DELETE FROM im_ingress_ticket_counter WHERE channel_id IN {CH_SUB}",
    f"DELETE FROM im_channel_lease WHERE channel_id IN {CH_SUB}",
    f"DELETE FROM im_onboarding_session WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB} OR application_agent_id IN {AGENTS_SUB}",
    f"DELETE FROM im_channel WHERE user_id = :u OR runtime_workspace_id IN {WS_SUB} OR application_agent_id IN {AGENTS_SUB}",
    # 应用 Agent 残留(worker 正常已清) + KB 残留(unbind 正常已清)
    "DELETE FROM application_agent_studio_session WHERE user_id = :u",
    "DELETE FROM application_agent_run WHERE subject_user_id = :u",
    "DELETE FROM application_agent_conversation WHERE subject_user_id = :u",
    "DELETE FROM application_agent_config_revision WHERE created_by_user_id = :u",
    "DELETE FROM application_agent_authorization_code WHERE user_id = :u",
    "DELETE FROM application_agent_memory WHERE subject_user_id = :u",
    f"DELETE FROM application_agent_personal_knowledge_binding WHERE knowledge_base_id IN {OKB_SUB} OR application_agent_id IN {AGENTS_SUB}",
    f"DELETE FROM application_agent_wecom_user_access WHERE application_agent_id IN {AGENTS_SUB}",
    f"DELETE FROM application_agent_runtime_binding WHERE application_agent_id IN {AGENTS_SUB}",
    "DELETE FROM application_client WHERE created_by_user_id = :u",
    "DELETE FROM application_agent WHERE created_by_user_id = :u OR updated_by_user_id = :u",
    f"DELETE FROM knowledge_base_file WHERE kb_id IN {OKB_SUB}",
    "DELETE FROM permission WHERE asset_type = 'knowledge_base' AND asset_id IN " + OKB_SUB,
    "DELETE FROM knowledge_base WHERE owner_user_id = :u",
    # 账号
    "DELETE FROM avatar_asset WHERE owner_user_id = :u",
    "DELETE FROM cloud_hermes_profile WHERE user_id = :u",
    "DELETE FROM user_role WHERE user_id = :u",
    "DELETE FROM auth_refresh_session WHERE user_id = :u",
    "DELETE FROM user WHERE id = :u",
    "DELETE FROM sys_user WHERE code = :c AND user_id = :su",
    "DELETE FROM sys_user_copy1 WHERE code = :c AND user_id = :su",
    "DELETE FROM admin_work_no_permissions WHERE work_no = :c",
]


async def cmd_db_delete(args: argparse.Namespace, db, target: dict, g: dict) -> None:
    p = {"u": target["user_id"], "c": target["code"], "su": target["sysu"]["user_id"]}
    if g["kbs"]:
        print(f"[WARN] 仍有 {len(g['kbs'])} 个 KB 未走 unbind-shared 的外部数据集删除,继续将造成外部数据集孤儿")
    if g["app_agents"]:
        print(f"[WARN] 仍有 {len(g['app_agents'])} 个应用 Agent 未走 delete-app-agents")
    print(f"计划:5 条 SET NULL 预更新 + {len(DELETES)} 条 DELETE(单事务,FOREIGN_KEY_CHECKS=0)")

    has_copy1 = await table_exists(db, "sys_user_copy1")
    statements = list(PRE_UPDATES) + [s for s in DELETES if "sys_user_copy1" not in s or has_copy1]
    if has_copy1 and not await column_exists(db, "sys_user_copy1", "user_id"):
        for i, s in enumerate(statements):
            if s.startswith("DELETE FROM sys_user_copy1"):
                statements[i] = "DELETE FROM sys_user_copy1 WHERE code = :c"

    if not args.apply:
        # dry-run:用 SELECT COUNT(*) 包裹原语句,只读预演每条会命中多少行,同时验证 SQL 合法性
        total = 0
        try:
            for s in statements:
                predicate = s.split(" WHERE ", 1)[1] if " WHERE " in s else "1=1"
                if s.startswith("UPDATE "):
                    tbl = s[len("UPDATE "):].split(" SET ")[0]
                else:
                    tbl = s[len("DELETE FROM "):].split(" WHERE")[0]
                wrapped = f"SELECT COUNT(*) FROM {tbl} WHERE {predicate}"
                n = (await db.execute(text(wrapped), p)).scalar() or 0
                total += n
                tag = "PRE" if s.startswith("UPDATE") else "DEL"
                print(f"{tag} {n:>7}  {tbl}")
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"[abort] dry-run 预演失败(SQL 需修正,未做任何写入): {type(exc).__name__}: {exc}")
        print(f"[dry-run] 以上为将命中行数,合计约 {total} 行;确认后加 --apply 执行")
        return

    typed = input(f"确认 DB mass delete:输入工号 {target['code']} 并回车 > ")
    if typed.strip() != target["code"]:
        sys.exit("[abort] 工号不匹配,已取消")
    total = 0
    try:
        for s in PRE_UPDATES:
            res = await db.execute(text(s), p)
            print(f"PRE {res.rowcount:>6}  {s.split(' SET ')[0].replace('UPDATE ', '')}")
        await db.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for s in statements[len(PRE_UPDATES):]:
            res = await db.execute(text(s), p)
            total += res.rowcount
            tbl = s.split(" FROM ")[1].split(" WHERE")[0]
            print(f"DEL {res.rowcount:>7}  {tbl}")
        await db.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        await db.commit()
        print(f"[ok] 已提交,共删除 {total} 行")
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        sys.exit(f"[abort] 已回滚: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

async def cmd_verify(args: argparse.Namespace, db) -> None:
    code = args.code
    target = await resolve_target(db, code, allow_missing=True)
    ok = True

    def check(name: str, bad: bool) -> None:
        nonlocal ok
        print(f"  {'FAIL' if bad else 'PASS'}  {name}")
        ok = ok and not bad

    print("== 账号 ==")
    check("sys_user/user 已删除", target is not None)
    if target is None:
        print("  账号已不存在,后续按工号/宿主机残留核对:")
        admin = await q_scalar(db, "SELECT COUNT(*) FROM admin_work_no_permissions WHERE work_no = :c", {"c": code})
        check(f"admin_work_no_permissions={admin}", admin > 0)
        for t in ("sys_user_copy1",):
            if await table_exists(db, t):
                n = await q_scalar(db, f"SELECT COUNT(*) FROM {t} WHERE code = :c", {"c": code})
                check(f"{t}={n}", n > 0)
        r = run(["kubectl", "get", "pvc,pv,ksvc,secret", "-A", "-o", "name"])
        hits = [line for line in (r.stdout or "").splitlines() if code in line]
        check(f"K8s 残留 {hits or '无'}", bool(hits))
        s = Settings()
        for root in (s.HERMES_RUNTIME_ROOT, s.HERMES_APPLICATION_AGENT_RUNTIME_ROOT):
            base = Path(root)
            hits: list[str] = []
            if base.exists():  # 只做两层浅扫:<root>/<org>/<工号*>,不递归整个树
                for org_dir in base.iterdir():
                    if org_dir.is_dir():
                        hits += [str(p) for p in org_dir.glob(f"*{code}*")]
            check(f"{root} 残留 {hits or '无'}", bool(hits))
        print("[done] 验证完成")
        sys.exit(0 if ok else 1)

    g = await gather(db, target)
    p = {"u": target["user_id"], "c": code}
    print("== 资源 ==")
    check(f"bindings={len(g['bindings'])}", bool(g["bindings"]))
    check(f"app_agents={len(g['app_agents'])}", bool(g["app_agents"]))
    check(f"experts={len(g['experts'])}", bool(g["experts"]))
    check(f"skills={len(g['skills'])}", bool(g["skills"]))
    check(f"mcps={len(g['mcps'])}", bool(g["mcps"]))
    check(f"kbs={len(g['kbs'])}", bool(g["kbs"]))
    print("== 跨用户悬挂引用 ==")
    check(f"expert installs by others={len(g['expert_installs_by_others'])}", bool(g["expert_installs_by_others"]))
    check(f"mcp refs by others={len(g['mcp_refs_by_others'])}", bool(g["mcp_refs_by_others"]))
    dangling = await q_scalar(
        db,
        f"SELECT COUNT(*) FROM runtime_agent_profile p WHERE p.id IN {EXT_SUB} AND ("
        f"p.expert_package_id IN {OE_SUB} OR p.id IN (SELECT runtime_agent_profile_id FROM runtime_agent_profile_revision WHERE expert_package_version_id IN {OV_SUB}))",
        p,
    )
    check(f"EXT Profile 悬挂 expert 引用={dangling}", dangling > 0)
    print("== 审计/用量 ==")
    check(f"projection_receipt={g['counts']['runtime_projection_receipt']}", g['counts']['runtime_projection_receipt'] > 0)
    check(f"admission_audit={g['counts']['runtime_admission_audit']}", g['counts']['runtime_admission_audit'] > 0)
    check(f"admin perms={g['counts']['admin_work_no_permissions']}", g['counts']['admin_work_no_permissions'] > 0)
    print("[done] 验证完成")
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

COMMANDS = ("inventory", "backup", "unbind-shared", "delete-app-agents", "delete-runtime-infra", "db-delete", "verify")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("code", help="离职员工工号,例如 10001")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--apply", action="store_true", help="执行破坏性操作(默认 dry-run)")
    parser.add_argument("--out-dir", default=None, help="backup 输出目录")
    args = parser.parse_args()

    async with AsyncSessionLocal() as db:
        if args.command == "verify":
            await cmd_verify(args, db)
            return
        target = await resolve_target(db, args.code)
        g = await gather(db, target)
        if args.command == "inventory":
            print_inventory(g)
        elif args.command == "backup":
            await cmd_backup(args, db, target, g)
        elif args.command == "unbind-shared":
            await cmd_unbind_shared(args, db, target, g)
        elif args.command == "delete-app-agents":
            await cmd_delete_app_agents(args, db, target, g)
        elif args.command == "delete-runtime-infra":
            await cmd_delete_runtime_infra(args, db, target, g)
        elif args.command == "db-delete":
            await cmd_db_delete(args, db, target, g)


if __name__ == "__main__":
    asyncio.run(main())
