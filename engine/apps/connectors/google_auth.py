"""Authenticated REST transports for Google Search Console and GA4."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import requests
from django.conf import settings
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials


GSC_READONLY_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GA4_READONLY_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"


class GoogleTransportError(RuntimeError):
    """A sanitized Google credential or HTTP transport failure."""


def get_google_session(
    scopes,
    credential_setting_name="GOOGLE_API_SERVICE_ACCOUNT_FILE",
):
    """Build an authorized service-account session with least-privilege scopes."""
    credential_setting = getattr(settings, credential_setting_name, "")
    if not credential_setting:
        raise GoogleTransportError(
            f"{credential_setting_name} is required for this Google API operation."
        )
    credential_path = Path(credential_setting).expanduser()
    if not credential_path.is_file():
        raise GoogleTransportError(
            f"Google service-account file does not exist: {credential_path}"
        )
    try:
        credentials = Credentials.from_service_account_file(
            str(credential_path), scopes=list(scopes)
        )
        return AuthorizedSession(credentials)
    except Exception as exc:
        raise GoogleTransportError(
            f"Could not load Google service-account credentials: {exc.__class__.__name__}"
        ) from exc


def _post_json(session, url, payload, source):
    timeout = getattr(settings, "GOOGLE_API_TIMEOUT_SECONDS", 60)
    try:
        response = session.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.Timeout as exc:
        raise GoogleTransportError(
            f"{source} request timed out after {timeout} seconds."
        ) from exc
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", "unknown")
        raise GoogleTransportError(
            f"{source} API returned HTTP {status}. Check API enablement and property access."
        ) from exc
    except ValueError as exc:
        raise GoogleTransportError(
            f"{source} API returned a non-JSON response."
        ) from exc
    except requests.RequestException as exc:
        raise GoogleTransportError(
            f"{source} request failed: {exc.__class__.__name__}"
        ) from exc


def gsc_live_executor(params, session=None):
    """Execute one Search Analytics query without mutating audit parameters."""
    payload = dict(params)
    site_url = str(payload.pop("siteUrl", "")).strip()
    if not site_url:
        raise GoogleTransportError("GSC request is missing siteUrl.")
    encoded_site_url = quote(site_url, safe="")
    url = (
        "https://www.googleapis.com/webmasters/v3/sites/"
        f"{encoded_site_url}/searchAnalytics/query"
    )
    session = session or get_google_session([GSC_READONLY_SCOPE])
    return _post_json(session, url, payload, "GSC")


def ga4_live_executor(params, session=None):
    """Execute one GA4 runReport request without mutating audit parameters."""
    payload = dict(params)
    property_name = str(payload.pop("property", "")).strip()
    prefix = "properties/"
    if not property_name.startswith(prefix) or not property_name[len(prefix):].isdigit():
        raise GoogleTransportError(
            "GA4 request property must use properties/<numeric_id>."
        )
    url = f"https://analyticsdata.googleapis.com/v1beta/{property_name}:runReport"
    session = session or get_google_session([GA4_READONLY_SCOPE])
    return _post_json(session, url, payload, "GA4")
