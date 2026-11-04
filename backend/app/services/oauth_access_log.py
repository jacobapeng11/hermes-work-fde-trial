"""Keep OAuth2 authorization codes and desktop flow nonces out of access logs."""
import logging
import re


class OAuthAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        def scrub(value):
            if not isinstance(value, str):
                return value
            return re.sub(r"(/api/v1/auth/oauth2/(?:callback|login|complete))\?[^\s\"]*",
                          r"\1?[redacted]", value)

        if isinstance(record.args, tuple):
            record.args = tuple(scrub(value) for value in record.args)
        record.msg = scrub(record.msg)
        return True


def install_oauth_access_log_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, OAuthAccessLogFilter) for item in logger.filters):
        logger.addFilter(OAuthAccessLogFilter())
