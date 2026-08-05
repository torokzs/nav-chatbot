from __future__ import annotations

import json
import hashlib
import logging
import subprocess
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

LOGGER = logging.getLogger("model_benchmark.azure")


class AzureCommandError(RuntimeError):
    """Raised when an Azure CLI command fails."""


def run_az(
    args: Sequence[str],
    *,
    output_json: bool = True,
    check: bool = True,
) -> Any:
    command = ["az", *args, "--only-show-errors"]
    if output_json:
        command.extend(["--output", "json"])
    LOGGER.info("Running Azure CLI: az %s", " ".join(args))
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if check and completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise AzureCommandError(f"Azure CLI failed ({completed.returncode}): {message}")
    if completed.returncode != 0:
        return None
    if not output_json or not completed.stdout.strip():
        return completed.stdout.strip()
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AzureCommandError("Azure CLI returned invalid JSON.") from exc


def fetch_retail_prices(
    client: httpx.Client,
    *,
    location: str,
    request: Callable[..., httpx.Response] | None = None,
) -> list[dict[str, Any]]:
    requester = request or client.get
    services = ("Foundry Models", "Azure Machine Learning")
    collected: list[dict[str, Any]] = []
    for service in services:
        for meter_region in {location.lower(), "global"}:
            price_filter = (
                f"serviceName eq '{service}' and armRegionName eq '{meter_region}'"
            )
            query = urllib.parse.urlencode({"$filter": price_filter})
            url: str | None = f"https://prices.azure.com/api/retail/prices?{query}"
            while url:
                response = requester(url, timeout=60.0)
                response.raise_for_status()
                payload = response.json()
                items = payload.get("Items", [])
                if isinstance(items, list):
                    collected.extend(item for item in items if isinstance(item, dict))
                next_page = payload.get("NextPageLink")
                url = str(next_page) if next_page else None
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in collected:
        key = (str(item.get("meterId", "")), str(item.get("effectiveStartDate", "")))
        unique[key] = item
    return list(unique.values())


def validate_traffic_isolation(app: Mapping[str, Any]) -> tuple[str, str]:
    properties = app.get("properties")
    if not isinstance(properties, Mapping):
        raise AzureCommandError("Container App response is missing properties.")
    configuration = properties.get("configuration")
    if not isinstance(configuration, Mapping):
        raise AzureCommandError("Container App response is missing configuration.")
    if str(configuration.get("activeRevisionsMode", "")).lower() != "multiple":
        raise AzureCommandError(
            "Container App must use multiple revision mode for a traffic-free benchmark."
        )
    ingress = configuration.get("ingress")
    if not isinstance(ingress, Mapping):
        raise AzureCommandError("Container App ingress is not configured.")
    traffic = ingress.get("traffic")
    if not isinstance(traffic, list) or not traffic:
        raise AzureCommandError("Container App ingress has no explicit production traffic rule.")
    unsafe = [
        item
        for item in traffic
        if not isinstance(item, Mapping)
        or bool(item.get("latestRevision"))
        or not item.get("revisionName")
    ]
    if unsafe:
        raise AzureCommandError(
            "Production traffic must target explicit revisions; latestRevision/label-only "
            "rules could route traffic to a benchmark revision."
        )
    weighted_revisions = [
        item
        for item in traffic
        if isinstance(item, Mapping)
        and isinstance(item.get("weight"), (int, float))
        and float(item["weight"]) > 0
    ]
    if not weighted_revisions:
        raise AzureCommandError("Container App has no positive production traffic weight.")
    base_revision = str(
        max(weighted_revisions, key=lambda item: float(item["weight"])).get(
            "revisionName",
            "",
        )
    ).strip()
    fqdn = str(ingress.get("fqdn", "")).strip()
    if not base_revision or not fqdn:
        raise AzureCommandError("Container App has no production base revision or ingress FQDN.")
    return base_revision, fqdn


def deployment_name(run_id: str, index: int, model_name: str) -> str:
    safe_run = "".join(character for character in run_id.lower() if character.isalnum())[-12:]
    safe_model = "".join(
        character if character.isalnum() else "-"
        for character in model_name.lower()
    ).strip("-")
    value = f"bench-{safe_run}-{index}-{safe_model}"
    return value[:64].rstrip("-")


def revision_suffix(run_id: str, index: int) -> str:
    run_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:10]
    return f"b{run_hash}-{index}"


def wait_for_revision(
    resource_group: str,
    container_app: str,
    revision: str,
    *,
    timeout_seconds: int = 600,
    interval_seconds: int = 10,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_state = "unknown"
    while time.monotonic() < deadline:
        payload = run_az(
            [
                "containerapp",
                "revision",
                "show",
                "--resource-group",
                resource_group,
                "--name",
                container_app,
                "--revision",
                revision,
            ]
        )
        properties = payload.get("properties", {}) if isinstance(payload, dict) else {}
        health = str(properties.get("healthState", "")).lower()
        running = str(properties.get("runningState", "")).lower()
        last_state = f"health={health or 'unknown'}, running={running or 'unknown'}"
        if health == "healthy" and running == "running":
            fqdn = str(properties.get("fqdn", "")).strip()
            if not fqdn:
                raise AzureCommandError(f"Healthy revision {revision} has no FQDN.")
            return f"https://{fqdn}"
        time.sleep(interval_seconds)
    raise AzureCommandError(f"Revision {revision} was not healthy within timeout ({last_state}).")


def write_registry(path: Path, registry: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    temporary.replace(path)


def cleanup_registry(
    path: Path,
    *,
    resource_group: str,
    ai_account: str,
    container_app: str,
) -> list[str]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    revisions_value = payload.get("revisions", [])
    deployments_value = payload.get("deployments", [])
    revisions = revisions_value if isinstance(revisions_value, list) else []
    deployments = deployments_value if isinstance(deployments_value, list) else []
    for revision_value in reversed(revisions.copy()):
        revision = str(revision_value)
        if not revision.startswith(f"{container_app}--b"):
            errors.append(f"Refusing to deactivate unscoped revision: {revision}")
            continue
        try:
            run_az(
                [
                    "containerapp",
                    "revision",
                    "deactivate",
                    "--resource-group",
                    resource_group,
                    "--name",
                    container_app,
                    "--revision",
                    revision,
                ],
                output_json=False,
            )
            revisions.remove(revision_value)
        except AzureCommandError as exc:
            if _is_not_found(exc):
                revisions.remove(revision_value)
            else:
                errors.append(str(exc))
    for deployment_value in reversed(deployments.copy()):
        deployment = str(deployment_value)
        if not deployment.startswith("bench-"):
            errors.append(f"Refusing to delete unscoped deployment: {deployment}")
            continue
        try:
            run_az(
                [
                    "cognitiveservices",
                    "account",
                    "deployment",
                    "delete",
                    "--resource-group",
                    resource_group,
                    "--name",
                    ai_account,
                    "--deployment-name",
                    deployment,
                ],
                output_json=False,
            )
            deployments.remove(deployment_value)
        except AzureCommandError as exc:
            if _is_not_found(exc):
                deployments.remove(deployment_value)
            else:
                errors.append(str(exc))
    payload["revisions"] = revisions
    payload["deployments"] = deployments
    write_registry(path, payload)
    return errors


def _is_not_found(exc: AzureCommandError) -> bool:
    message = str(exc).lower()
    return any(value in message for value in ("not found", "resourcenotfound", "deploymentnotfound"))
