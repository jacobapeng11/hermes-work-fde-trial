from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.database import get_db
from app.schemas.user_directory import (
    DepartmentTreeNode,
    OrganizationDirectoryNode,
    UserDirectorySearchItem,
)
from app.services.runtime_workspace import Principal
from app.services.user_directory import UserDirectoryService


router = APIRouter(prefix="/directory", tags=["user-directory"])


class DirectoryUserProvisionRequest(BaseModel):
    user_codes: list[str] = Field(default_factory=list, max_length=100)


@router.get("/users/search", response_model=list[UserDirectorySearchItem])
async def search_directory_users(
    keyword: str = Query(default="", max_length=100),
    limit: int = Query(default=50, ge=1, le=50),
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> list[UserDirectorySearchItem]:
    return await UserDirectoryService(db).search_users(
        principal=principal,
        keyword=keyword,
        limit=limit,
    )


@router.post("/users/provision", response_model=list[UserDirectorySearchItem])
async def provision_directory_users(
    payload: DirectoryUserProvisionRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> list[UserDirectorySearchItem]:
    service = UserDirectoryService(db)
    try:
        items = await service.provision_users_by_codes(
            principal=principal,
            user_codes=payload.user_codes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return items


@router.get("/departments/tree", response_model=list[DepartmentTreeNode])
async def get_directory_department_tree(
    _principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> list[DepartmentTreeNode]:
    return await UserDirectoryService(db).department_tree()


@router.get("/tree", response_model=list[OrganizationDirectoryNode])
async def get_organization_directory_tree(
    keyword: str = Query(default="", max_length=100),
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> list[OrganizationDirectoryNode]:
    return await UserDirectoryService(db).organization_directory(
        principal=principal,
        keyword=keyword,
    )
