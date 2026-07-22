"""Shared, secret-safe app inventory validation for Control Tower helpers."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def normalize_apps(
    apps: list[dict],
    *,
    exact_count: int | None = None,
    allow_local_http: bool = False,
) -> list[dict]:
    if exact_count is not None and len(apps) != exact_count:
        raise ValueError(f"inventory requires exactly {exact_count} apps")
    if not apps:
        raise ValueError("fleet inventory is empty")

    normalized: list[dict] = []
    seen_names: set[str] = set()
    seen_urls: set[str] = set()
    for raw in apps:
        if not isinstance(raw, dict):
            raise ValueError("each inventory entry must be an object")
        name = str(raw.get("name") or "").strip()
        raw_url = str(raw.get("url") or "").strip()
        if not name or not raw_url:
            raise ValueError("each inventory app requires name and url")
        name_key = name.casefold()
        if name_key in seen_names:
            raise ValueError("inventory contains duplicate app names")

        parsed = urlsplit(raw_url)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("app URLs must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("app URLs must not contain query or fragment")
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValueError("app URL requires a hostname")
        scheme = parsed.scheme.lower()
        local_http = (
            scheme == "http"
            and host in _LOCAL_HOSTS
            and allow_local_http
        )
        if scheme != "https" and not local_http:
            raise ValueError("app URLs require https")
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("app URL contains an invalid port") from error
        display_host = f"[{host}]" if ":" in host else host
        netloc = display_host if port is None else f"{display_host}:{port}"
        path = parsed.path.rstrip("/")
        normalized_url = urlunsplit((scheme, netloc, path, "", ""))
        url_key = normalized_url.casefold()
        if url_key in seen_urls:
            raise ValueError("inventory contains duplicate normalized URLs")

        seen_names.add(name_key)
        seen_urls.add(url_key)
        normalized.append({
            "name": name,
            "url": normalized_url,
            "token": str(raw.get("token") or ""),
        })
    return sorted(normalized, key=lambda app: (app["name"].casefold(), app["url"]))


__all__ = ["normalize_apps"]
