from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import subprocess
import time
import urllib.parse
from copy import deepcopy
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
    executable = shutil.which("az.cmd" if os.name == "nt" else "az")
    if executable is None:
        executable = shutil.which("az")
    if executable is None:
        raise AzureCommandError("Azure CLI executable was not found on PATH.")
    command = [executable, *args, "--only-show-errors"]
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


def _subscription_id() -> str:
    account = run_az(["account", "show"])
    subscription_id = str(account.get("id", "")) if isinstance(account, dict) else ""
    if not subscription_id:
        raise AzureCommandError("Azure subscription ID could not be resolved.")
    return subscription_id


def _container_app_url(
    subscription_id: str,
    resource_group: str,
    container_app: str,
) -> str:
    return (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}/providers/Microsoft.App"
        f"/containerApps/{container_app}"
    )


def build_revision_template(
    revision: Mapping[str, Any],
    *,
    deployment: str,
    suffix: str,
    environment_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    properties = revision.get("properties")
    template_value = properties.get("template") if isinstance(properties, Mapping) else None
    if not isinstance(template_value, Mapping):
        raise AzureCommandError("Base revision response is missing its template.")
    template = deepcopy(dict(template_value))
    containers_value = template.get("containers")
    if not isinstance(containers_value, list) or not containers_value:
        raise AzureCommandError("Base revision template has no containers.")

    overrides = {
        "AZURE_AI_FOUNDRY_CHAT_DEPLOYMENT": deployment,
        **(environment_overrides or {}),
    }
    updated: set[str] = set()
    for container in containers_value:
        if not isinstance(container, dict):
            continue
        env_value = container.get("env")
        env = env_value if isinstance(env_value, list) else []
        container["env"] = env
        for variable in env:
            name = str(variable.get("name", "")) if isinstance(variable, dict) else ""
            if isinstance(variable, dict) and name in overrides:
                variable.pop("secretRef", None)
                variable["value"] = overrides[name]
                updated.add(name)
    first_container = containers_value[0]
    if not isinstance(first_container, dict):
        raise AzureCommandError("Base revision template has an invalid container.")
    env_value = first_container.get("env")
    env = env_value if isinstance(env_value, list) else []
    first_container["env"] = env
    for name, value in overrides.items():
        if name not in updated:
            env.append({"name": name, "value": value})
    template["revisionSuffix"] = suffix
    return template


def create_revision(
    *,
    subscription_id: str,
    resource_group: str,
    container_app: str,
    base_revision: str,
    deployment: str,
    suffix: str,
    environment_overrides: Mapping[str, str] | None = None,
) -> str:
    app_url = _container_app_url(subscription_id, resource_group, container_app)
    revision_url = f"{app_url}/revisions/{base_revision}?api-version=2024-03-01"
    revision = run_az(["rest", "--method", "get", "--url", revision_url])
    if not isinstance(revision, Mapping):
        raise AzureCommandError(f"Base revision {base_revision} could not be read.")
    template = build_revision_template(
        revision,
        deployment=deployment,
        suffix=suffix,
        environment_overrides=environment_overrides,
    )
    run_az(
        [
            "rest",
            "--method",
            "patch",
            "--url",
            f"{app_url}?api-version=2024-03-01",
            "--headers",
            "Content-Type=application/json",
            "--body",
            json.dumps({"properties": {"template": template}}, separators=(",", ":")),
        ]
    )
    return f"{container_app}--{suffix}"


def deactivate_revision(
    *,
    subscription_id: str,
    resource_group: str,
    container_app: str,
    revision: str,
) -> None:
    app_url = _container_app_url(subscription_id, resource_group, container_app)
    run_az(
        [
            "rest",
            "--method",
            "post",
            "--url",
            f"{app_url}/revisions/{revision}/deactivate?api-version=2024-03-01",
        ],
        output_json=False,
    )


def wait_for_revision(
    resource_group: str,
    container_app: str,
    revision: str,
    *,
    subscription_id: str | None = None,
    timeout_seconds: int = 600,
    interval_seconds: int = 10,
) -> str:
    resolved_subscription_id = subscription_id or _subscription_id()
    app_url = _container_app_url(
        resolved_subscription_id,
        resource_group,
        container_app,
    )
    deadline = time.monotonic() + timeout_seconds
    last_state = "unknown"
    while time.monotonic() < deadline:
        try:
            payload = run_az(
                [
                    "rest",
                    "--method",
                    "get",
                    "--url",
                    f"{app_url}/revisions/{revision}?api-version=2024-03-01",
                ]
            )
        except AzureCommandError as exc:
            if not _is_not_found(exc):
                raise
            last_state = "provisioning"
            time.sleep(interval_seconds)
            continue
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
    subscription_id: str | None = None,
) -> list[str]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    revisions_value = payload.get("revisions", [])
    deployments_value = payload.get("deployments", [])
    revisions = revisions_value if isinstance(revisions_value, list) else []
    deployments = deployments_value if isinstance(deployments_value, list) else []
    resolved_subscription_id = (
        subscription_id or _subscription_id()
        if revisions
        else subscription_id
    )
    for revision_value in reversed(revisions.copy()):
        revision = str(revision_value)
        if not revision.startswith(f"{container_app}--b"):
            errors.append(f"Refusing to deactivate unscoped revision: {revision}")
            continue
        try:
            deactivate_revision(
                subscription_id=str(resolved_subscription_id),
                resource_group=resource_group,
                container_app=container_app,
                revision=revision,
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
    return any(
        value in message
        for value in (
            "not found",
            "resourcenotfound",
            "deploymentnotfound",
            "revisionnotfound",
        )
    )
