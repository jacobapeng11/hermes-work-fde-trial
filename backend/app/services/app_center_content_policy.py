from __future__ import annotations

from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser

from app.config import settings


PLATFORM_RETURN_MARKER = "data-app-center-platform-link"


@dataclass(frozen=True)
class _HtmlInjectionPoint:
    has_platform_link: bool
    body_end_offset: int | None


class _HtmlInjectionPointParser(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.has_platform_link = False
        self.body_end_offset: int | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag == "a" and any(name == PLATFORM_RETURN_MARKER for name, _ in attrs):
            self.has_platform_link = True
        if tag != "body" or self.body_end_offset is not None:
            return
        position_offset = _source_byte_offset(self.source, self.getpos())
        tag_text = self.get_starttag_text() or ""
        if position_offset is not None:
            self.body_end_offset = (
                position_offset
                + len(tag_text.encode("utf-8", "surrogateescape"))
            )


def _find_html_injection_point(content: bytes) -> _HtmlInjectionPoint:
    # surrogateescape preserves arbitrary package bytes while still allowing
    # HTMLParser to distinguish markup from text embedded in script/style.
    source = content.decode("utf-8", "surrogateescape")
    parser = _HtmlInjectionPointParser(source)
    parser.feed(source)
    parser.close()
    return _HtmlInjectionPoint(
        has_platform_link=parser.has_platform_link,
        body_end_offset=parser.body_end_offset,
    )


def _source_byte_offset(
    source: str,
    position: tuple[int, int],
) -> int | None:
    line_number, column = position
    lines = source.split("\n")
    if line_number < 1 or line_number > len(lines):
        return None
    byte_offset = 0
    for line in lines[:line_number - 1]:
        byte_offset += len(line.encode("utf-8", "surrogateescape")) + 1
    current_line = lines[line_number - 1]
    byte_offset += len(
        current_line[:column].encode("utf-8", "surrogateescape")
    )
    return byte_offset


def with_platform_return_link(content: bytes) -> bytes:
    injection_point = _find_html_injection_point(content)
    if injection_point.has_platform_link:
        return content
    platform_url = escape(settings.PUBLIC_WEB_BASE, quote=True)
    link = (
        f'<a href="{platform_url}" target="_top" data-app-center-platform-link '
        'style="position:fixed;top:16px;left:16px;z-index:2147483647;'
        'display:inline-flex;align-items:center;min-height:36px;padding:0 14px;'
        'border-radius:999px;background:#2563eb;color:#fff;'
        'font:600 14px/1 Arial,sans-serif;text-decoration:none;'
        'box-shadow:0 6px 18px rgba(15,23,42,.28);">返回平台</a>'
    ).encode("utf-8")
    body_end = injection_point.body_end_offset
    if body_end is None:
        return link + content
    return content[:body_end] + link + content[body_end:]
