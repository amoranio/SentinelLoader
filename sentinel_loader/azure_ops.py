from __future__ import annotations

import time
import uuid
from typing import Any, Callable

import httpx
from azure.core.credentials import TokenCredential
from azure.core.exceptions import HttpResponseError
from azure.monitor.ingestion import LogsIngestionClient

from sentinel_loader.azure_auth import cloud_settings, principal_oid_from_token
from sentinel_loader.models import ConnectionConfig, ExistingDestination, ProvisionResult
from sentinel_loader.schema import ensure_cl_suffix, stream_name_for

TABLE_API = "2023-09-01"
DCR_API = "2023-03-11"
WORKSPACE_API = "2023-09-01"
ROLE_API = "2022-04-01"
# Monitoring Metrics Publisher
METRICS_PUBLISHER_ROLE = "3913510d-42f4-4eac-9e97-ea22a523bf54"


class AzureError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class SentinelClient:
    def __init__(self, config: ConnectionConfig, credential: TokenCredential):
        self.config = config
        self.credential = credential
        self.settings = cloud_settings(config.cloud)
        self._http = httpx.Client(timeout=60.0)

    def close(self) -> None:
        self._http.close()

    def _arm_token(self) -> str:
        return self.credential.get_token(self.settings["arm_scope"]).token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._arm_token()}",
            "Content-Type": "application/json",
        }

    def _arm(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = self._http.request(method, url, headers=self._headers(), **kwargs)
        if response.status_code >= 400:
            raise AzureError(
                _format_arm_error(response),
                status_code=response.status_code,
                detail=_safe_json(response),
            )
        return response

    def workspace_resource_id(self) -> str:
        c = self.config
        return (
            f"/subscriptions/{c.subscription_id}/resourceGroups/{c.resource_group}"
            f"/providers/Microsoft.OperationalInsights/workspaces/{c.workspace_name}"
        )

    def workspace_url(self) -> str:
        return f"{self.settings['arm']}{self.workspace_resource_id()}"

    def get_workspace(self) -> dict[str, Any]:
        response = self._arm("GET", f"{self.workspace_url()}?api-version={WORKSPACE_API}")
        return response.json()

    def list_tables(self) -> list[dict[str, Any]]:
        url = f"{self.workspace_url()}/tables?api-version={TABLE_API}"
        response = self._arm("GET", url)
        items = response.json().get("value", [])
        out = []
        for item in items:
            name = item.get("name") or ""
            props = item.get("properties") or {}
            out.append(
                {
                    "name": name,
                    "plan": props.get("plan") or props.get("tablePlan"),
                    "is_custom": name.endswith("_CL"),
                }
            )
        out.sort(key=lambda t: (not t["is_custom"], t["name"].lower()))
        return out

    def list_dcrs(self) -> list[dict[str, Any]]:
        c = self.config
        url = (
            f"{self.settings['arm']}/subscriptions/{c.subscription_id}/resourceGroups/"
            f"{c.resource_group}/providers/Microsoft.Insights/dataCollectionRules"
            f"?api-version={DCR_API}"
        )
        response = self._arm("GET", url)
        items = []
        for item in response.json().get("value", []):
            props = item.get("properties") or {}
            endpoint = _dcr_ingestion_endpoint(item)
            items.append(
                {
                    "name": item.get("name"),
                    "id": item.get("id"),
                    "immutable_id": props.get("immutableId"),
                    "ingestion_endpoint": endpoint,
                    "streams": list((props.get("streamDeclarations") or {}).keys()),
                    "kind": item.get("kind"),
                    "output_streams": [
                        flow.get("outputStream")
                        for flow in (props.get("dataFlows") or [])
                        if flow.get("outputStream")
                    ],
                }
            )
        return items

    def create_or_update_table(self, table_name: str, schema_payload: dict[str, Any], plan: str = "Analytics") -> dict[str, Any]:
        table = ensure_cl_suffix(table_name)
        payload = schema_payload
        payload.setdefault("properties", {})["plan"] = plan
        payload["properties"]["schema"]["name"] = table
        url = f"{self.workspace_url()}/tables/{table}?api-version={TABLE_API}"
        response = self._arm("PUT", url, json=payload)
        body = response.json()
        self._wait_for_table(table)
        return body

    def _wait_for_table(self, table: str, timeout_s: int = 180) -> None:
        url = f"{self.workspace_url()}/tables/{table}?api-version={TABLE_API}"
        deadline = time.time() + timeout_s
        last_state = "unknown"
        while time.time() < deadline:
            try:
                response = self._arm("GET", url)
                props = (response.json().get("properties") or {})
                last_state = str(props.get("provisioningState") or props.get("resultDescription") or "Succeeded")
                if last_state.lower() in {"succeeded", "success", ""}:
                    return
            except AzureError as exc:
                if exc.status_code not in {404, 409}:
                    raise
            time.sleep(3)
        raise AzureError(f"Timed out waiting for table {table} (last state: {last_state}).")

    def create_or_update_dcr(
        self,
        dcr_name: str,
        location: str,
        table_name: str,
        stream_declaration: dict[str, Any],
    ) -> dict[str, Any]:
        table = ensure_cl_suffix(table_name)
        stream = stream_name_for(table)
        c = self.config
        url = (
            f"{self.settings['arm']}/subscriptions/{c.subscription_id}/resourceGroups/"
            f"{c.resource_group}/providers/Microsoft.Insights/dataCollectionRules/{dcr_name}"
            f"?api-version={DCR_API}"
        )
        body = {
            "location": location,
            "kind": "Direct",
            "properties": {
                "description": f"SentinelLoader ingest stream for {table}",
                "streamDeclarations": stream_declaration,
                "destinations": {
                    "logAnalytics": [
                        {
                            "workspaceResourceId": self.workspace_resource_id(),
                            "name": "law",
                        }
                    ]
                },
                "dataFlows": [
                    {
                        "streams": [stream],
                        "destinations": ["law"],
                        "transformKql": "source",
                        "outputStream": f"Custom-{table}",
                    }
                ],
            },
        }
        response = self._arm("PUT", url, json=body)
        created = response.json()
        # Re-GET to pick up logsIngestion endpoint / immutableId.
        for _ in range(20):
            latest = self._arm("GET", url).json()
            if (latest.get("properties") or {}).get("immutableId") and _dcr_ingestion_endpoint(latest):
                return latest
            time.sleep(2)
        return created

    def grant_metrics_publisher(self, dcr_resource_id: str) -> bool:
        token = self._arm_token()
        oid = principal_oid_from_token(token)
        if not oid:
            raise AzureError("Could not determine the signed-in principal object ID from the access token.")
        assignment_name = str(uuid.uuid4())
        url = (
            f"{self.settings['arm']}{dcr_resource_id}/providers/Microsoft.Authorization/"
            f"roleAssignments/{assignment_name}?api-version={ROLE_API}"
        )
        role_def = (
            f"/subscriptions/{self.config.subscription_id}/providers/"
            f"Microsoft.Authorization/roleDefinitions/{METRICS_PUBLISHER_ROLE}"
        )
        payload = {
            "properties": {
                "roleDefinitionId": role_def,
                "principalId": oid,
                "principalType": "ServicePrincipal"
                if self.config.auth_mode == "service_principal"
                else None,
            }
        }
        if payload["properties"]["principalType"] is None:
            del payload["properties"]["principalType"]
        try:
            self._arm("PUT", url, json=payload)
            return True
        except AzureError as exc:
            detail = str(exc.detail or "") + str(exc)
            if exc.status_code in {409} or "RoleAssignmentExists" in detail:
                return True
            raise AzureError(
                "Created the table and DCR, but could not grant Monitoring Metrics Publisher "
                "on the DCR. Grant that role to this identity in the Azure portal "
                "(DCR → Access control → Add role assignment), then ingest using the existing "
                f"DCR. Details: {exc}",
                status_code=exc.status_code,
                detail=exc.detail,
            ) from exc

    def provision(
        self,
        table_name: str,
        schema_payload: dict[str, Any],
        stream_declaration: dict[str, Any],
        plan: str = "Analytics",
        dcr_name: str | None = None,
    ) -> ProvisionResult:
        table = ensure_cl_suffix(table_name)
        workspace = self.get_workspace()
        location = workspace.get("location")
        if not location:
            raise AzureError("Workspace response did not include a location.")
        safe_dcr = dcr_name or f"dcr-{table.lower().replace('_', '-')}"[:64]
        messages: list[str] = []
        warnings: list[str] = []

        self.create_or_update_table(table, schema_payload, plan=plan)
        messages.append(f"Ensured custom table {table} exists in workspace {self.config.workspace_name}.")

        dcr = self.create_or_update_dcr(safe_dcr, location, table, stream_declaration)
        props = dcr.get("properties") or {}
        immutable = props.get("immutableId") or ""
        endpoint = _dcr_ingestion_endpoint(dcr)
        messages.append(f"Ensured Direct DCR {safe_dcr} (immutable ID {immutable}).")

        assigned = False
        try:
            assigned = self.grant_metrics_publisher(dcr.get("id") or "")
            if assigned:
                messages.append("Granted Monitoring Metrics Publisher on the DCR to this identity.")
        except AzureError as exc:
            warnings.append(str(exc))

        if not endpoint or not immutable:
            raise AzureError(
                "DCR was created but the logs ingestion endpoint or immutable ID is not ready yet. "
                "Wait a minute and retry ingest using existing-table mode."
            )

        return ProvisionResult(
            created_table=True,
            created_dcr=True,
            assigned_role=assigned,
            table_name=table,
            dcr_name=safe_dcr,
            dcr_immutable_id=immutable,
            ingestion_endpoint=endpoint,
            stream_name=stream_name_for(table),
            messages=messages,
            warnings=warnings,
        )

    def ingest(
        self,
        destination: ExistingDestination,
        records: list[dict[str, Any]],
        on_chunk: Callable[[int], None] | None = None,
    ) -> int:
        if not records:
            return 0
        credential_scopes = [cloud_settings(self.config.cloud)["monitor_scope"]]
        client = LogsIngestionClient(
            endpoint=destination.ingestion_endpoint,
            credential=self.credential,
            credential_scopes=credential_scopes,
        )
        sent = 0
        errors: list[str] = []

        def on_error(error: Any) -> None:
            failed = getattr(error, "failed_logs", []) or []
            errors.append(f"{error.error} ({len(failed)} rows)")

        # The SDK chunks to 1MB gzip automatically.
        try:
            client.upload(
                rule_id=destination.dcr_immutable_id,
                stream_name=destination.stream_name,
                logs=records,
                on_error=on_error,
            )
            sent = len(records) if not errors else len(records)
        except HttpResponseError as exc:
            raise AzureError(f"Ingestion API rejected the upload: {exc.message or exc}") from exc
        finally:
            client.close()
        if on_chunk:
            on_chunk(len(records))
        if errors:
            raise AzureError("Some batches failed to ingest: " + "; ".join(errors))
        return sent


def _dcr_ingestion_endpoint(dcr: dict[str, Any]) -> str | None:
    props = dcr.get("properties") or {}
    logs = props.get("logsIngestion") or {}
    if isinstance(logs, dict) and logs.get("endpoint"):
        return logs["endpoint"]
    endpoints = props.get("endpoints") or {}
    if isinstance(endpoints, dict) and endpoints.get("logsIngestion"):
        return endpoints["logsIngestion"]
    return None


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _format_arm_error(response: httpx.Response) -> str:
    payload = _safe_json(response)
    if isinstance(payload, dict):
        err = payload.get("error") or payload
        if isinstance(err, dict):
            code = err.get("code") or response.status_code
            message = err.get("message") or response.text
            return f"Azure ARM {response.status_code} ({code}): {message}"
    return f"Azure ARM {response.status_code}: {response.text[:800]}"
