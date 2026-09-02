from __future__ import annotations

import base64
import json
from typing import Any

from azure.identity import (
    AzureAuthorityHosts,
    ClientSecretCredential,
    DefaultAzureCredential,
)
from azure.core.credentials import TokenCredential

from sentinel_loader.models import ConnectionConfig

CLOUD_SETTINGS = {
    "public": {
        "authority": AzureAuthorityHosts.AZURE_PUBLIC_CLOUD,
        "arm": "https://management.azure.com",
        "arm_scope": "https://management.azure.com/.default",
        "monitor_scope": "https://monitor.azure.com/.default",
        "graph": "https://graph.microsoft.com",
    },
    "usgovernment": {
        "authority": AzureAuthorityHosts.AZURE_GOVERNMENT,
        "arm": "https://management.usgovcloudapi.net",
        "arm_scope": "https://management.usgovcloudapi.net/.default",
        "monitor_scope": "https://monitor.azure.us/.default",
        "graph": "https://graph.microsoft.us",
    },
    "china": {
        "authority": AzureAuthorityHosts.AZURE_CHINA,
        "arm": "https://management.chinacloudapi.cn",
        "arm_scope": "https://management.chinacloudapi.cn/.default",
        "monitor_scope": "https://monitor.azure.cn/.default",
        "graph": "https://microsoftgraph.chinacloudapi.cn",
    },
}


def cloud_settings(cloud: str) -> dict[str, Any]:
    return CLOUD_SETTINGS.get(cloud, CLOUD_SETTINGS["public"])


def build_credential(config: ConnectionConfig) -> TokenCredential:
    settings = cloud_settings(config.cloud)
    if config.auth_mode == "service_principal":
        if not (config.tenant_id and config.client_id and config.client_secret):
            raise ValueError("Tenant ID, client ID, and client secret are required for a service principal.")
        return ClientSecretCredential(
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            client_secret=config.client_secret,
            authority=settings["authority"],
        )
    return DefaultAzureCredential(authority=settings["authority"])


def principal_oid_from_token(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
    return claims.get("oid") or claims.get("sub")
