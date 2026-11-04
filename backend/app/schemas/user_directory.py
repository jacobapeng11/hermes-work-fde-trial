from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UserDirectorySearchItem(BaseModel):
    id: int
    name: str
    user_code: str | None = None


class DepartmentTreeNode(BaseModel):
    department_id: str
    name: str
    parent_id: str | None = None
    parent_name: str | None = None
    virtual: bool = False
    children: list["DepartmentTreeNode"] = Field(default_factory=list)


class PersonalKnowledgePermissionDirectoryUserNode(BaseModel):
    node_type: Literal["user"] = "user"
    # 平台 User 表 id；目录按 sys_user 全量通讯录展示人员，未在平台
    # 开户的人员为 None，前端必须置灰不可选。
    user_id: int | None
    # 是否完成过首次登录（预开户但未登录的账号仍显示“未激活”）。
    logged_in: bool = False
    name: str
    user_code: str | None = None
    department_id: str | None = None
    status: Literal["active"] = "active"


class PersonalKnowledgePermissionDirectoryDepartmentNode(BaseModel):
    node_type: Literal["department"] = "department"
    department_id: str
    name: str
    parent_id: str | None = None
    parent_name: str | None = None
    virtual: bool = False
    children: list[
        "PersonalKnowledgePermissionDirectoryDepartmentNode"
        " | PersonalKnowledgePermissionDirectoryUserNode"
    ] = Field(default_factory=list)


PersonalKnowledgePermissionDirectoryNode = (
    PersonalKnowledgePermissionDirectoryDepartmentNode
    | PersonalKnowledgePermissionDirectoryUserNode
)


class OrganizationDirectoryOrganizationNode(BaseModel):
    node_type: Literal["organization"] = "organization"
    organization_id: int
    name: str
    children: list[PersonalKnowledgePermissionDirectoryNode] = Field(default_factory=list)


OrganizationDirectoryNode = (
    OrganizationDirectoryOrganizationNode
    | PersonalKnowledgePermissionDirectoryDepartmentNode
    | PersonalKnowledgePermissionDirectoryUserNode
)
