"""Heuristic classifier to distinguish real users from email-security scanners.

Used on trial-link hits (`/trial/inspect`, `/trial/redeem`) to log scanner
pre-fetches separately from genuine prospect visits. Defense in depth — the
primary anti-prefetch barrier is the click-to-activate button on the landing
page; this module exists only for analytics and observability.
"""

from __future__ import annotations

import re

from fastapi import Request

# Ordered: most specific first. Each entry is (regex, scanner_name).
# Patterns match against a lowercased User-Agent.
_SCANNER_UA_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"mimecast"), "mimecast"),
    (re.compile(r"proofpoint|urldefense"), "proofpoint"),
    (re.compile(r"bingpreview|microsoftpreview"), "ms_safelinks"),
    (re.compile(r"barracuda"), "barracuda"),
    (re.compile(r"symantec|cloudswg|clouddlp"), "symantec"),
    (re.compile(r"forcepoint"), "forcepoint"),
    (re.compile(r"trendmicro"), "trendmicro"),
    (re.compile(r"sophos"), "sophos"),
    (re.compile(r"googleimageproxy"), "google_image_proxy"),
    (re.compile(r"slackbot-linkexpanding"), "slack"),
    (re.compile(r"facebookexternalhit"), "facebook"),
    (re.compile(r"twitterbot"), "twitter"),
    (re.compile(r"linkedinbot"), "linkedin"),
    (re.compile(r"whatsapp"), "whatsapp"),
    (re.compile(r"telegrambot"), "telegram"),
    (re.compile(r"discordbot"), "discord"),
    (re.compile(r"safelinks|atp-link-safety"), "ms_safelinks"),
]

# Generic bot signals — set classification to bot but no specific scanner_name.
_GENERIC_BOT_PATTERNS = re.compile(
    r"headlesschrome|phantomjs|puppeteer|playwright|selenium|bot/|crawler|spider|scanner",
    re.IGNORECASE,
)

# Microsoft SafeLinks signature headers (set by O365/Defender).
_MS_HEADER_NAMES = (
    "x-msedge-clientip",
    "x-forefront-antispam-report",
    "x-ms-exchange-organization-network-message-id",
)


def classify_request(request: Request) -> tuple[str, str | None]:
    """Classify an incoming request as human / bot / unknown.

    Returns:
        (classification, scanner_name)
        classification: "human", "bot", or "unknown"
        scanner_name: short identifier of the detected scanner, or None
    """
    ua_raw = request.headers.get("User-Agent") or ""
    ua = ua_raw.lower().strip()

    # Microsoft SafeLinks signature headers are the strongest signal.
    headers_lower = {k.lower() for k in request.headers.keys()}
    if any(h in headers_lower for h in _MS_HEADER_NAMES):
        return ("bot", "ms_safelinks")

    # No User-Agent at all — almost always automation.
    if not ua:
        return ("bot", None)

    # Match known scanner UA patterns.
    for pattern, name in _SCANNER_UA_PATTERNS:
        if pattern.search(ua):
            return ("bot", name)

    # Generic automation signals (headless browsers, crawler tags).
    if _GENERIC_BOT_PATTERNS.search(ua_raw):
        return ("bot", None)

    # Browsers always send Accept-Language. Its absence on a UA that *claims*
    # to be a browser is a strong tell that it's a sandbox or naive scanner.
    accept_language = request.headers.get("Accept-Language")
    looks_like_browser = "mozilla" in ua or "webkit" in ua or "gecko" in ua
    if looks_like_browser and not accept_language:
        return ("unknown", None)

    return ("human", None)
