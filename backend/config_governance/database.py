from __future__ import annotations


class DatabaseSettings:
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = "change-me"
    MYSQL_DB: str = "hermes_cloud"
    MYSQL_SSL_MODE: str = "DISABLED"
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
