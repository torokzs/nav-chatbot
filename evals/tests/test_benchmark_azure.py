from __future__ import annotations

import pytest

from evals.benchmark_azure import (
    AzureCommandError,
    build_revision_template,
    deployment_name,
    revision_suffix,
    validate_traffic_isolation,
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
    )

    assert template["revisionSuffix"] == "b123-1"
    assert template["containers"][0]["env"][0]["value"] == "benchmark-model"
    assert revision["properties"]["template"]["containers"][0]["env"][0]["value"] == "production"
