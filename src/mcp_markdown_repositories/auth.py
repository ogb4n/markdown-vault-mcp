"""Interactive bootstrap for a renewable delegated OneDrive token."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import time

import requests

from .repository import LOGIN_ROOT, RepositoryError


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authorize this server to access the configured OneDrive account."
    )
    parser.add_argument(
        "--refresh-token-file",
        default=os.getenv("ONEDRIVE_REFRESH_TOKEN_FILE"),
        help="File that will receive the refresh token (or ONEDRIVE_REFRESH_TOKEN_FILE).",
    )
    arguments = parser.parse_args()
    client_id = os.getenv("ONEDRIVE_CLIENT_ID")
    tenant = os.getenv("ONEDRIVE_TENANT_ID", "consumers")
    if not client_id or not arguments.refresh_token_file:
        raise SystemExit(
            "ONEDRIVE_CLIENT_ID and --refresh-token-file (or "
            "ONEDRIVE_REFRESH_TOKEN_FILE) are required."
        )

    device = requests.post(
        f"{LOGIN_ROOT}/{tenant}/oauth2/v2.0/devicecode",
        data={"client_id": client_id, "scope": "offline_access Files.ReadWrite"},
        timeout=(3, 20),
    )
    if not device.ok:
        raise SystemExit(f"OneDrive device authorization could not start ({device.status_code}).")
    payload = device.json()
    print(payload["message"], flush=True)

    deadline = time.monotonic() + int(payload["expires_in"])
    interval = int(payload.get("interval", 5))
    while time.monotonic() < deadline:
        response = requests.post(
            f"{LOGIN_ROOT}/{tenant}/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": payload["device_code"],
            },
            timeout=(3, 20),
        )
        if response.ok:
            token = response.json().get("refresh_token")
            if not isinstance(token, str) or not token:
                raise SystemExit("OneDrive authorization did not return a refresh token.")
            _write_secret(Path(arguments.refresh_token_file), token)
            print(f"Refresh token saved to {arguments.refresh_token_file}.", flush=True)
            return
        try:
            error = response.json().get("error")
        except ValueError:
            error = None
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        if error == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        raise SystemExit(f"OneDrive authorization failed: {error or response.status_code}")
    raise SystemExit("OneDrive device authorization expired before it was completed.")


def _write_secret(destination: Path, token: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        temporary.write_text(f"{token}\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except OSError as error:
        raise RepositoryError(f"Cannot write OneDrive refresh token: {error}") from error
