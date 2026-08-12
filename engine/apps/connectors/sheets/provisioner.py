"""Create and share per-client Google Sheets destinations through Drive API."""

from __future__ import annotations

from urllib.parse import quote

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from apps.connectors.google_auth import get_google_session


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
SPREADSHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"


class DriveProvisioningError(RuntimeError):
    """A sanitized failure while creating or sharing a client spreadsheet."""


class DriveSpreadsheetProvisioner:
    """Provision spreadsheets in a Shared Drive using service-account auth."""

    def __init__(self, session=None, parent_folder_id=None, admin_email=None):
        self.session = session
        self.parent_folder_id = str(
            parent_folder_id
            if parent_folder_id is not None
            else getattr(settings, "GOOGLE_DRIVE_PARENT_FOLDER_ID", "")
        ).strip()
        self.admin_email = str(
            admin_email
            if admin_email is not None
            else getattr(settings, "ADMIN_GOOGLE_EMAIL", "")
        ).strip()

    def _session(self):
        if self.session is None:
            self.session = get_google_session(
                [DRIVE_SCOPE],
                credential_setting_name="GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE",
            )
        return self.session

    def _request_json(self, method, url, *, payload=None, source):
        timeout = getattr(settings, "GOOGLE_API_TIMEOUT_SECONDS", 60)
        try:
            response = self._session().request(
                method,
                url,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            if response.status_code == 204:
                return {}
            return response.json()
        except requests.Timeout as exc:
            raise DriveProvisioningError(
                f"{source} timed out after {timeout} seconds."
            ) from exc
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", "unknown")
            raise DriveProvisioningError(
                f"{source} returned HTTP {status}. Check Drive API enablement, "
                "Shared Drive membership, folder access, and sharing policy."
            ) from exc
        except ValueError as exc:
            raise DriveProvisioningError(
                f"{source} returned a non-JSON response."
            ) from exc
        except requests.RequestException as exc:
            raise DriveProvisioningError(
                f"{source} failed: {exc.__class__.__name__}."
            ) from exc

    def _validate_configuration(self):
        if not self.parent_folder_id:
            raise DriveProvisioningError(
                "GOOGLE_DRIVE_PARENT_FOLDER_ID is required for automatic "
                "spreadsheet creation. It must identify a folder in a Shared Drive."
            )
        if not self.admin_email:
            raise DriveProvisioningError(
                "ADMIN_GOOGLE_EMAIL is required for automatic spreadsheet sharing."
            )
        try:
            validate_email(self.admin_email)
        except ValidationError as exc:
            raise DriveProvisioningError(
                "ADMIN_GOOGLE_EMAIL must be a valid email address."
            ) from exc

    def create_spreadsheet(self, title):
        self._validate_configuration()
        payload = {
            "name": str(title),
            "mimeType": SPREADSHEET_MIME_TYPE,
            "parents": [self.parent_folder_id],
        }
        result = self._request_json(
            "POST",
            f"{DRIVE_FILES_URL}?supportsAllDrives=true&fields=id",
            payload=payload,
            source="Google Drive spreadsheet creation",
        )
        spreadsheet_id = str(result.get("id", "")).strip()
        if not spreadsheet_id:
            raise DriveProvisioningError(
                "Google Drive spreadsheet creation returned no file ID."
            )
        return spreadsheet_id

    def share_editor(self, spreadsheet_id):
        self._validate_configuration()
        encoded_id = quote(str(spreadsheet_id), safe="")
        return self._request_json(
            "POST",
            f"{DRIVE_FILES_URL}/{encoded_id}/permissions"
            "?supportsAllDrives=true&sendNotificationEmail=true&fields=id",
            payload={
                "type": "user",
                "role": "writer",
                "emailAddress": self.admin_email,
            },
            source="Google Drive spreadsheet sharing",
        )

    def delete_spreadsheet(self, spreadsheet_id):
        """Best-effort cleanup for a newly created file that could not be shared."""
        encoded_id = quote(str(spreadsheet_id), safe="")
        self._request_json(
            "DELETE",
            f"{DRIVE_FILES_URL}/{encoded_id}?supportsAllDrives=true",
            source="Google Drive spreadsheet cleanup",
        )

    def provision(self, client_name):
        spreadsheet_id = self.create_spreadsheet(
            f"{client_name} - SEO Opportunities"
        )
        try:
            self.share_editor(spreadsheet_id)
        except Exception:
            try:
                self.delete_spreadsheet(spreadsheet_id)
            except DriveProvisioningError:
                # Preserve the original sharing error; the file ID was never saved
                # to the Client and administrators can inspect Drive audit logs.
                pass
            raise
        return spreadsheet_id
