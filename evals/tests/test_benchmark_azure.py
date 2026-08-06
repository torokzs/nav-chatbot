from __future__ import annotations

import httpx
import pytest

from evals.benchmark_azure import (
    AzureCommandError,
    build_revision_template,
    deployment_name,
    revision_suffix,
    validate_traffic_isolation,
    wait_for_deployment_data_plane,
    wait_for_revision,
)


def test_traffic_isolation_requires_explicit_revision_weights() -> None:
    app = {
        "properties": {
            "latestReadyRevisionName": "api--staging",
            "configuration": {
                "activeRevisionsMode": "Multiple",
                "ingress": {
                    "fqdn": "api.example.test",
                    "traffic": [
                        {"revisionName": "api--stable", "weight": 100},
                        {"revisionName": "api--staging", "weight": 0},
                    ],
                },
            },
        }
    }

    assert validate_traffic_isolation(app) == ("api--stable", "api.example.test")


def test_traffic_isolation_rejects_latest_revision_rule() -> None:
    app = {
        "properties": {
            "latestReadyRevisionName": "api--stable",
            "configuration": {
                "activeRevisionsMode": "Multiple",
                "ingress": {
                    "fqdn": "api.example.test",
                    "traffic": [{"latestRevision": True, "weight": 100}],
                },
            },
        }
    }

    with pytest.raises(AzureCommandError, match="latestRevision"):
        validate_traffic_isolation(app)


def test_deployment_name_is_run_scoped_and_bounded() -> None:
    name = deployment_name("12345678901234567890", 8, "A-Very-Long-Model-Name" * 5)

    assert name.startswith("bench-901234567890-8-")
    assert len(name) <= 64


def test_revision_suffix_changes_between_run_attempts() -> None:
    assert revision_suffix("123456-1", 1) != revision_suffix("123456-2", 1)


def test_wait_for_deployment_data_plane_retries_propagation() -> None:
    responses = iter(
        [
            httpx.Response(
                404,
                json={"error": {"code": "DeploymentNotFound"}},
            ),
            httpx.Response(200, json={"model": "gpt-5-mini"}),
        ]
    )
    sleeps: list[float] = []

    def request(url: str, **kwargs) -> httpx.Response:
        assert url.endswith("/openai/deployments/temporary/chat/completions")
        assert kwargs["headers"]["Authorization"] == "Bearer token"
        return next(responses)

    with httpx.Client() as client:
        result = wait_for_deployment_data_plane(
            client,
            endpoint="https://foundry.example",
            deployment="temporary",
            access_token="token",
            request=request,
            monotonic=iter([0.0, 1.0]).__next__,
            sleep=sleeps.append,
        )

    assert result["model"] == "gpt-5-mini"
    assert sleeps == [10.0]


def test_build_revision_template_retargets_deployment_without_mutating_base() -> None:
    revision = {
        "properties": {
            "template": {
                "containers": [
                    {
                        "name": "backend",
                        "env": [
                            {
                                "name": "AZURE_AI_FOUNDRY_CHAT_DEPLOYMENT",
                                "value": "production",
                            }
                        ],
                    }
                ],
                "revisionSuffix": None,
            }
        }
    }

    template = build_revision_template(
        revision,
        deployment="benchmark-model",
        suffix="b123-1",
        environment_overrides={"BENCHMARK_CONTEXT_TOKEN": "run-secret"},
    )

    assert template["revisionSuffix"] == "b123-1"
    assert template["containers"][0]["env"][0]["value"] == "benchmark-model"
    assert template["containers"][0]["env"][1] == {
        "name": "BENCHMARK_CONTEXT_TOKEN",
        "value": "run-secret",
    }
    assert revision["properties"]["template"]["containers"][0]["env"][0]["value"] == "production"


def test_wait_for_revision_retries_while_arm_child_is_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: list[object] = [
        AzureCommandError("RevisionNotFound"),
        {
            "properties": {
                "healthState": "Healthy",
                "runningState": "Running",
                "fqdn": "benchmark.example.test",
            }
        },
    ]

    def fake_run_az(_args: list[str]) -> object:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("evals.benchmark_azure.run_az", fake_run_az)

    assert (
        wait_for_revision(
            "rg",
            "app",
            "app--benchmark",
            subscription_id="sub",
            interval_seconds=0,
        )
        == "https://benchmark.example.test"
    )
