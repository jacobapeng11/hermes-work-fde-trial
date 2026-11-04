from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.models import SysUser, User
from app.services.enterprise_idp import EnterpriseIdpAssertionService, EnterpriseIdpAuthenticationError
from app.services.sso_identity import (
    SqlAlchemySsoIdentityRepository,
    SsoIdentityError,
    SsoIdentityService,
)


class MemoryIdentityRepository:
    def __init__(
        self,
        users: list[User] | None = None,
        hrm_users: list[SysUser] | None = None,
    ) -> None:
        self.users = users or []
        self.hrm_users = hrm_users or []
        self.hrm_available = True
        self.roles: set[tuple[int, int, str]] = set()
        self.commits = 0
        self.lookups: list[str] = []

    async def find_hrm_users(self, *, work_no: str) -> list[SysUser]:
        self.lookups.append("hrm")
        if not self.hrm_available:
            return []
        return [row for row in self.hrm_users if row.code == work_no]

    async def find_candidates(self, *, org_id: int, sub_account: str) -> list[User]:
        self.lookups.append("platform")
        return [
            user
            for user in self.users
            if user.org_id == org_id
            and (user.user_code == sub_account or user.sso_subject == sub_account)
        ]

    async def add_user(self, user: User) -> None:
        user.id = len(self.users) + 1
        self.users.append(user)

    async def ensure_user_role(self, *, org_id: int, user_id: int, role_code: str) -> None:
        self.roles.add((org_id, user_id, role_code))

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture(autouse=True)
def enterprise_idp_settings() -> None:
    original = (
        settings.ENTERPRISE_IDP_AES_KEY,
        settings.ENTERPRISE_IDP_AES_IV,
        settings.ENTERPRISE_IDP_ASSERTION_MAX_AGE_SECONDS,
        settings.ENTERPRISE_IDP_CLOCK_SKEW_SECONDS,
        settings.ENTERPRISE_IDP_TIMEZONE,
    )
    settings.ENTERPRISE_IDP_AES_KEY = "0123456789abcdef"
    settings.ENTERPRISE_IDP_AES_IV = "fedcba9876543210"
    settings.ENTERPRISE_IDP_ASSERTION_MAX_AGE_SECONDS = 6 * 60 * 60
    settings.ENTERPRISE_IDP_CLOCK_SKEW_SECONDS = 120
    settings.ENTERPRISE_IDP_TIMEZONE = "Asia/Shanghai"
    yield
    (
        settings.ENTERPRISE_IDP_AES_KEY,
        settings.ENTERPRISE_IDP_AES_IV,
        settings.ENTERPRISE_IDP_ASSERTION_MAX_AGE_SECONDS,
        settings.ENTERPRISE_IDP_CLOCK_SKEW_SECONDS,
        settings.ENTERPRISE_IDP_TIMEZONE,
    ) = original


def encrypt_assertion(payload: dict[str, str]) -> str:
    padder = PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(json.dumps(payload, separators=(",", ":")).encode()) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(settings.ENTERPRISE_IDP_AES_KEY.encode()),
        modes.CBC(settings.ENTERPRISE_IDP_AES_IV.encode()),
    ).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


def test_enterprise_idp_assertion_decrypts_and_uses_shared_six_hour_deadline() -> None:
    issued_at = datetime(2026, 7, 13, 17, 48, 36, tzinfo=ZoneInfo("Asia/Shanghai"))
    token = encrypt_assertion({"workNo": "employee-7", "time": "2026-07-13 17:48:36"})

    assertion = EnterpriseIdpAssertionService().decrypt_and_validate(
        token,
        now=issued_at + timedelta(minutes=55),
    )

    assert assertion.work_no == "employee-7"
    assert assertion.issued_at == issued_at
    assert assertion.expires_at == issued_at + timedelta(hours=6, minutes=-2)
    assert assertion.token_fingerprint == hashlib.sha256(base64.b64decode(token)).hexdigest()


def test_enterprise_idp_assertion_accepts_query_parser_space_for_base64_plus() -> None:
    issued_at = datetime(2026, 7, 13, 17, 48, 36, tzinfo=ZoneInfo("Asia/Shanghai"))
    token = encrypt_assertion({"workNo": "employee-7", "time": "2026-07-13 17:48:36"})
    if "+" not in token:
        pytest.skip("generated ciphertext did not contain a plus")

    assertion = EnterpriseIdpAssertionService().decrypt_and_validate(
        token.replace("+", " "),
        now=issued_at,
    )
    canonical = EnterpriseIdpAssertionService().decrypt_and_validate(token, now=issued_at)

    assert assertion.work_no == "employee-7"
    assert assertion.token_fingerprint == canonical.token_fingerprint


@pytest.mark.parametrize(
    ("payload", "now"),
    [
        ({"workNo": "employee-7", "time": "bad"}, datetime(2026, 7, 13, tzinfo=ZoneInfo("Asia/Shanghai"))),
        (
            {"workNo": "employee-7", "time": "2026-07-13 10:00:00"},
            datetime(2026, 7, 13, 16, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    ],
)
def test_enterprise_idp_assertion_rejects_invalid_or_expired_payload(
    payload: dict[str, str], now: datetime
) -> None:
    with pytest.raises(EnterpriseIdpAuthenticationError):
        EnterpriseIdpAssertionService().decrypt_and_validate(encrypt_assertion(payload), now=now)


@pytest.mark.asyncio
async def test_work_number_creates_one_platform_user_and_default_role() -> None:
    repository = MemoryIdentityRepository()
    service = SsoIdentityService(repository)

    identity = await service.resolve_or_create(org_id=7, work_no=" employee-7 ")

    assert identity.user_id == 1
    assert identity.user_code == "employee-7"
    assert identity.name == "用户"
    assert repository.users[0].password_hash is None
    assert repository.users[0].login_count == 1
    assert repository.roles == {(7, 1, "user")}


@pytest.mark.asyncio
async def test_work_number_syncs_exact_hrm_name_and_id_before_platform_lookup() -> None:
    repository = MemoryIdentityRepository(
        hrm_users=[
            SysUser(user_id="hrm-7", code="employee-7", name="示例用户乙", username="zhangyi")
        ]
    )

    identity = await SsoIdentityService(repository).resolve_or_create(
        org_id=7,
        work_no=" employee-7 ",
    )

    assert identity.name == "示例用户乙"
    assert repository.users[0].name == "示例用户乙"
    assert repository.users[0].user_code == "employee-7"
    assert repository.users[0].hrm_user_id == "hrm-7"
    assert repository.lookups == ["hrm", "platform"]


@pytest.mark.asyncio
async def test_hrm_username_is_used_when_name_is_blank() -> None:
    repository = MemoryIdentityRepository(
        hrm_users=[
            SysUser(user_id="hrm-7", code="employee-7", name=" ", username="zhangyi")
        ]
    )

    identity = await SsoIdentityService(repository).resolve_or_create(
        org_id=7,
        work_no="employee-7",
    )

    assert identity.name == "zhangyi"
    assert repository.users[0].hrm_user_id == "hrm-7"


@pytest.mark.asyncio
async def test_missing_hrm_uses_generic_name_for_new_user() -> None:
    repository = MemoryIdentityRepository()

    identity = await SsoIdentityService(repository).resolve_or_create(
        org_id=7,
        work_no="employee-7",
    )

    assert identity.name == "用户"
    assert repository.users[0].hrm_user_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "username"),
    [
        ("伪造姓名\n工号：other", "bad\rname"),
        ("<!-- PLATFORM_IDENTITY_BEGIN -->", "<!-- PLATFORM_IDENTITY_END -->"),
        ("bad\x00name", "bad\x1fname"),
    ],
)
async def test_invalid_hrm_names_do_not_enter_platform_identity(
    name: str,
    username: str,
) -> None:
    repository = MemoryIdentityRepository(
        hrm_users=[
            SysUser(
                user_id="hrm-7",
                code="employee-7",
                name=name,
                username=username,
            )
        ]
    )

    identity = await SsoIdentityService(repository).resolve_or_create(
        org_id=7,
        work_no="employee-7",
    )

    assert identity.name == "用户"
    assert repository.users[0].hrm_user_id is None


@pytest.mark.asyncio
async def test_work_number_reuses_existing_platform_user_and_rejects_disabled_user() -> None:
    existing = User(
        id=42,
        org_id=7,
        user_code="employee-7",
        sso_subject="employee-7",
        name="employee-7",
        status="active",
    )
    repository = MemoryIdentityRepository([existing])
    service = SsoIdentityService(repository)

    identity = await service.resolve_or_create(org_id=7, work_no="employee-7")

    assert identity.user_id == 42
    existing.status = "disabled"
    with pytest.raises(SsoIdentityError, match="disabled"):
        await service.resolve_or_create(org_id=7, work_no="employee-7")


@pytest.mark.asyncio
async def test_hrm_lookup_failure_keeps_login_available_and_existing_name() -> None:
    existing = User(
        id=42,
        org_id=7,
        user_code="employee-7",
        sso_subject="employee-7",
        name="已有姓名",
        status="active",
    )
    repository = MemoryIdentityRepository(users=[existing])
    repository.hrm_available = False

    identity = await SsoIdentityService(repository).resolve_or_create(
        org_id=7,
        work_no="employee-7",
    )

    assert identity.name == "已有姓名"
    assert identity.user_code == "employee-7"
    assert repository.lookups == ["hrm", "platform"]


@pytest.mark.asyncio
async def test_missing_or_ambiguous_hrm_preserves_existing_name() -> None:
    existing = User(
        id=42,
        org_id=7,
        user_code="employee-7",
        sso_subject="employee-7",
        name="已有姓名",
        status="active",
    )
    repository = MemoryIdentityRepository(
        users=[existing],
        hrm_users=[
            SysUser(user_id="hrm-a", code="employee-7", name="姓名 A", username="a"),
            SysUser(user_id="hrm-b", code="employee-7", name="姓名 B", username="b"),
        ],
    )

    identity = await SsoIdentityService(repository).resolve_or_create(
        org_id=7,
        work_no="employee-7",
    )

    assert identity.name == "已有姓名"
    assert existing.hrm_user_id is None


class FailingHrmDatabase:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def execute(self, statement: object) -> object:
        raise SQLAlchemyError(
            "query failed for employee-7 示例用户乙 token=secret@example.com"
        )

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_sqlalchemy_hrm_lookup_failure_rolls_back_without_logging_pii(
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = FailingHrmDatabase()
    repository = SqlAlchemySsoIdentityRepository(database)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="app.services.sso_identity"):
        result = await repository.find_hrm_users(work_no="employee-7")

    assert result == []
    assert database.rollbacks == 1
    assert "HRM identity lookup unavailable" in caplog.text
    assert "employee-7" not in caplog.text
    assert "示例用户乙" not in caplog.text
    assert "secret@example.com" not in caplog.text
