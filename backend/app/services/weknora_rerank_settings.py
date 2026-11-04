from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WeKnoraRerankConfig


WeKnoraRerankMode = Literal["model", "disabled"]


@dataclass(frozen=True)
class WeKnoraRerankSetting:
    mode: WeKnoraRerankMode


class WeKnoraRerankSettingStoreError(RuntimeError):
    """The persisted Hermes-side rerank policy cannot be read or written."""


class WeKnoraRerankSettingService:
    """Persist whether Hermes requests override WeKnora rerank off."""

    PROVIDER = "weknora"

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_setting(self) -> WeKnoraRerankSetting:
        try:
            row = await self._row()
        except SQLAlchemyError as exc:
            raise WeKnoraRerankSettingStoreError() from exc
        return WeKnoraRerankSetting(
            mode="model" if row is None or row.rerank_enabled else "disabled",
        )

    async def get_mode(self) -> WeKnoraRerankMode:
        return (await self.get_setting()).mode

    async def is_rerank_enabled(self) -> bool:
        return (await self.get_setting()).mode == "model"

    async def set_mode(
        self,
        *,
        mode: WeKnoraRerankMode,
        updated_by_user_id: int,
    ) -> WeKnoraRerankSetting:
        try:
            row = await self._row(for_update=True)
            enabled = mode == "model"
            if row is None:
                row = WeKnoraRerankConfig(
                    provider=self.PROVIDER,
                    rerank_enabled=enabled,
                    updated_by_user_id=updated_by_user_id,
                )
                self.db.add(row)
            else:
                row.rerank_enabled = enabled
                row.updated_by_user_id = updated_by_user_id
            await self.db.commit()
        except SQLAlchemyError as exc:
            await self.db.rollback()
            raise WeKnoraRerankSettingStoreError() from exc
        return WeKnoraRerankSetting(mode=mode)

    async def _row(self, *, for_update: bool = False) -> WeKnoraRerankConfig | None:
        statement = select(WeKnoraRerankConfig).where(
            WeKnoraRerankConfig.provider == self.PROVIDER
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.db.execute(statement)
        return result.scalar_one_or_none()
