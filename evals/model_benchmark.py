from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential

from benchmark_azure import (
    AzureCommandError,
    cleanup_registry,
    create_revision,
    deactivate_revision,
    deployment_name,
    fetch_retail_prices,
    revision_suffix,
    run_az,
    validate_traffic_isolation,
    wait_for_deployment_data_plane,
    wait_for_revision,
    write_registry,
)
from benchmark_core import (
    QUALITY_THRESHOLDS,
    CandidateResult,
    ModelCandidate,
    TokenPrice,
    apply_account_quota,
    build_focused_shortlist,
    build_shortlist,
    estimate_preflight_cost,
    normalize_retail_price,
    parse_available_models,
    quality_score,
    rank_results,
    select_questions,
    summarize_timings,
)
from run_eval import (
    call_chat_endpoint,
    evaluation_context_to_sources,
    evaluation_context_to_text,
    extract_metric,
    load_dataset,
    run_foundry_evaluation,
    write_jsonl,
)

LOGGER = logging.getLogger("model_benchmark")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = SCRIPT_DIR / "qa.jsonl"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "benchmark-results"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the cost-aware Azure AI model benchmark.")
    subparsers = parser.add_subparsers(dest="command")
    run_parser = subparsers.add_parser("run", help="Discover, deploy, evaluate, report, and clean up.")
    cleanup_parser = subparsers.add_parser("cleanup", help="Clean resources recorded in a run registry.")

    for command_parser in (run_parser, cleanup_parser):
        command_parser.add_argument(
            "--resource-group",
            default=os.getenv("AZURE_RESOURCE_GROUP"),
            required=os.getenv("AZURE_RESOURCE_GROUP") is None,
        )
        command_parser.add_argument(
            "--ai-account",
            default=os.getenv("AZURE_AI_ACCOUNT_NAME"),
            required=os.getenv("AZURE_AI_ACCOUNT_NAME") is None,
        )
        command_parser.add_argument(
            "--container-app",
            default=os.getenv("AZURE_CONTAINER_APP_NAME"),
            required=os.getenv("AZURE_CONTAINER_APP_NAME") is None,
        )
        command_parser.add_argument(
            "--registry",
            type=Path,
            default=DEFAULT_OUTPUT_DIR / "benchmark_resources.json",
        )

    run_parser.add_argument("--location", default=os.getenv("AZURE_LOCATION"), required=os.getenv("AZURE_LOCATION") is None)
    run_parser.add_argument(
        "--judge-deployment",
        default=os.getenv("AZURE_AI_JUDGE_DEPLOYMENT"),
        required=os.getenv("AZURE_AI_JUDGE_DEPLOYMENT") is None,
    )
    run_parser.add_argument("--judge-endpoint", default=os.getenv("AZURE_OPENAI_ENDPOINT"))
    run_parser.add_argument("--project-url", default=os.getenv("AZURE_AI_PROJECT_URL"))
    run_parser.add_argument("--run-id", default=os.getenv("GITHUB_RUN_ID", "local"))
    run_parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    run_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    run_parser.add_argument("--max-estimated-cost-usd", type=float, default=100.0)
    run_parser.add_argument("--deployment-capacity", type=int, default=100)
    run_parser.add_argument("--requests-per-minute", type=int, default=25)
    run_parser.add_argument(
        "--question-count",
        type=int,
        default=100,
        help="Number of deterministic leading dataset questions to benchmark (1-100).",
    )
    run_parser.add_argument(
        "--candidate-models",
        default=os.getenv("BENCHMARK_CANDIDATE_MODELS", ""),
        help=(
            "Comma-separated exact model names for focused comparison. "
            "Empty uses the family-based catalog shortlist."
        ),
    )
    run_parser.add_argument(
        "--dynamic-model-max-cost-per-question-usd",
        type=float,
        default=1.0,
        help=(
            "Conservative generation cost ceiling for dynamic routers whose "
            "underlying model determines billing."
        ),
    )
    run_parser.add_argument(
        "--base-revision",
        default=os.getenv("AZURE_CONTAINER_APP_BASE_REVISION"),
        help="Optional healthy zero-traffic branch revision to copy instead of production.",
    )
    run_parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Discover candidates and evaluate the cost gate without creating paid resources.",
    )
    run_parser.add_argument("--input-tokens-per-question", type=int, default=8_000)
    run_parser.add_argument("--output-tokens-per-question", type=int, default=1_000)
    run_parser.add_argument("--judge-input-tokens-per-evaluation", type=int, default=10_000)
    run_parser.add_argument("--judge-output-tokens-per-evaluation", type=int, default=500)

    args = parser.parse_args()
    if args.command is None:
        parser.error("A command is required: run or cleanup.")
    return args


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _model_details(deployment: dict[str, Any]) -> tuple[str, str, str]:
    properties = deployment.get("properties", {})
    model = properties.get("model", {}) if isinstance(properties, dict) else {}
    sku = deployment.get("sku", {})
    scale_settings = (
        properties.get("scaleSettings", {})
        if isinstance(properties, dict)
        else {}
    )
    return (
        str(model.get("name", "")),
        str(model.get("version", "")),
        str(sku.get("name", "") or scale_settings.get("scaleType", "")),
    )


def _revision_deployment(revision: dict[str, Any]) -> str:
    properties = revision.get("properties", {})
    template = properties.get("template", {}) if isinstance(properties, dict) else {}
    containers = template.get("containers", []) if isinstance(template, dict) else []
    if not isinstance(containers, list):
        return ""
    for container in containers:
        if not isinstance(container, dict):
            continue
        env = container.get("env", [])
        if not isinstance(env, list):
            continue
        for variable in env:
            if (
                isinstance(variable, dict)
                and variable.get("name") == "AZURE_AI_FOUNDRY_CHAT_DEPLOYMENT"
            ):
                return str(variable.get("value", "")).strip()
    return ""


def _price_dict(price: TokenPrice | None) -> dict[str, Any] | None:
    if price is None:
        return None
    return {
        "input_per_million_usd": price.input_per_million_usd,
        "output_per_million_usd": price.output_per_million_usd,
        "source_meters": list(price.source_meters),
    }


def _write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "benchmark_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# Azure AI model benchmark",
        "",
        f"- Run: `{report['run_id']}`",
        f"- Status: **{report['status']}**",
        f"- Started: {report['started_at']}",
        f"- Finished: {report.get('finished_at') or 'in progress'}",
        "",
        "## Preflight cost gate",
        "",
    ]
    preflight = report.get("preflight", {})
    estimate = preflight.get("estimated_cost_usd")
    estimate_label = "unknown" if estimate is None else f"${estimate:.4f}"
    lines.extend(
        [
            f"- Estimate: **{estimate_label}**",
            f"- Budget: **${preflight.get('max_estimated_cost_usd', 0):.2f}**",
            f"- Decision: **{preflight.get('decision', 'pending')}**",
        ]
    )
    if preflight.get("unknown_prices"):
        lines.append(
            "- Unknown prices: "
            + ", ".join(f"`{item}`" for item in preflight["unknown_prices"])
        )
    lines.extend(
        [
            "",
            "## Ranking",
            "",
            "| Rank | Slot | Model | Status | Cost | Groundedness | Relevance | Fluency | Citation | p95 latency |",
            "|---:|---|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    ranked_models = {
        item["model"]: index
        for index, item in enumerate(report.get("ranking", []), start=1)
    }
    for candidate in report.get("candidates", []):
        model = candidate.get("model") or "-"
        quality = candidate.get("quality", {})
        timing = candidate.get("timing", {})
        cost = candidate.get("estimated_cost_usd")
        lines.append(
            "| {rank} | {slot} | {model} | {status} | {cost} | {groundedness} | "
            "{relevance} | {fluency} | {citation} | {latency} |".format(
                rank=ranked_models.get(model, "-"),
                slot=candidate.get("slot", "-"),
                model=model,
                status=candidate.get("status", "-"),
                cost="unknown" if cost is None else f"${cost:.4f}",
                groundedness=_metric_label(quality.get("groundedness")),
                relevance=_metric_label(quality.get("relevance")),
                fluency=_metric_label(quality.get("fluency")),
                citation=_metric_label(quality.get("citation_correctness")),
                latency=_seconds_label(timing.get("latency_p95_seconds")),
            )
        )
    lines.extend(["", "## Recommendation", ""])
    recommendation = report.get("recommendation")
    if recommendation:
        lines.append(
            f"**{recommendation['model']}** is the lowest-cost candidate that passed every "
            "quality threshold and had known Retail Prices API pricing."
        )
    else:
        lines.append("No candidate was eligible for recommendation.")
    lines.extend(
        [
            "",
            "## Speed metrics",
            "",
            "| Model | TTFT mean | TTFT p50 | TTFT p95 | Latency mean | Latency p50 | "
            "Latency p95 | Tokens/s mean | Tokens/s p50 | Tokens/s p95 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for candidate in report.get("candidates", []):
        if not candidate.get("model"):
            continue
        timing = candidate.get("timing", {})
        lines.append(
            "| {model} | {ttft_mean} | {ttft_p50} | {ttft_p95} | {latency_mean} | "
            "{latency_p50} | {latency_p95} | {throughput_mean} | {throughput_p50} | "
            "{throughput_p95} |".format(
                model=candidate["model"],
                ttft_mean=_seconds_label(timing.get("ttft_mean_seconds")),
                ttft_p50=_seconds_label(timing.get("ttft_p50_seconds")),
                ttft_p95=_seconds_label(timing.get("ttft_p95_seconds")),
                latency_mean=_seconds_label(timing.get("latency_mean_seconds")),
                latency_p50=_seconds_label(timing.get("latency_p50_seconds")),
                latency_p95=_seconds_label(timing.get("latency_p95_seconds")),
                throughput_mean=_rate_label(
                    timing.get("throughput_mean_tokens_per_second")
                ),
                throughput_p50=_rate_label(
                    timing.get("throughput_p50_tokens_per_second")
                ),
                throughput_p95=_rate_label(
                    timing.get("throughput_p95_tokens_per_second")
                ),
            )
        )
    if report.get("fatal_error"):
        lines.extend(["", "## Failure", "", str(report["fatal_error"])])
    cleanup_errors = report.get("cleanup_errors", [])
    if cleanup_errors:
        lines.extend(["", "## Cleanup errors", ""])
        lines.extend(f"- {error}" for error in cleanup_errors)
    (output_dir / "benchmark_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _metric_label(value: Any) -> str:
    return "-" if value is None else f"{float(value):.3f}"


def _seconds_label(value: Any) -> str:
    return "-" if value is None else f"{float(value):.3f}s"


def _rate_label(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2f}"


def _assign_prices(
    candidates: list[ModelCandidate],
    price_items: list[dict[str, Any]],
    location: str,
) -> None:
    for candidate in candidates:
        candidate.price = normalize_retail_price(
            price_items,
            candidate.name,
            candidate.sku,
            location,
        )


def _create_deployment(
    candidate: ModelCandidate,
    *,
    name: str,
    resource_group: str,
    ai_account: str,
) -> None:
    command = [
        "cognitiveservices",
        "account",
        "deployment",
        "create",
        "--resource-group",
        resource_group,
        "--name",
        ai_account,
        "--deployment-name",
        name,
        "--model-name",
        candidate.name,
        "--model-version",
        candidate.version,
        "--model-format",
        candidate.model_format,
        "--sku-name",
        candidate.sku,
        "--sku-capacity",
        str(candidate.deployment_capacity),
    ]
    if candidate.source:
        command.extend(["--model-source", candidate.source])
    run_az(command)


def _cleanup_candidate(
    *,
    registry: dict[str, Any],
    registry_path: Path,
    revision: str | None,
    deployment: str | None,
    resource_group: str,
    container_app: str,
    ai_account: str,
    subscription_id: str,
) -> list[str]:
    errors: list[str] = []
    if revision:
        try:
            deactivate_revision(
                subscription_id=subscription_id,
                resource_group=resource_group,
                container_app=container_app,
                revision=revision,
            )
            registry["revisions"].remove(revision)
        except (AzureCommandError, ValueError) as exc:
            errors.append(str(exc))
    if deployment:
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
            registry["deployments"].remove(deployment)
        except (AzureCommandError, ValueError) as exc:
            errors.append(str(exc))
    write_registry(registry_path, registry)
    return errors


def _run_questions(
    endpoint: str,
    dataset: list[dict[str, Any]],
    frozen_contexts: list[dict[str, Any]],
    benchmark_token: str,
    output_path: Path,
    requests_per_minute: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    question_results: list[dict[str, Any]] = []
    eval_rows: list[dict[str, Any]] = []
    timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=30.0)
    minimum_interval = 60 / requests_per_minute
    last_started: float | None = None
    with httpx.Client(timeout=timeout, headers={"Accept": "text/event-stream"}) as client:
        for index, row in enumerate(dataset, start=1):
            if last_started is not None:
                elapsed = time.monotonic() - last_started
                if elapsed < minimum_interval:
                    time.sleep(minimum_interval - elapsed)
            last_started = time.monotonic()
            question = str(row["question"])
            adoev = int(row["adoev"])
            LOGGER.info("[%d/%d] %s", index, len(dataset), question)
            try:
                frozen = frozen_contexts[index - 1]
                result = call_chat_endpoint(
                    client,
                    endpoint,
                    question,
                    adoev,
                    evaluation_context=list(frozen["evaluation_context"]),
                    benchmark_token=benchmark_token,
                )
                question_results.append(
                    {
                        "index": index,
                        "question": question,
                        "adoev": adoev,
                        "response": result.response,
                        "sources": result.sources,
                        "evaluation_context": result.evaluation_context,
                        "ttft_seconds": result.ttft_seconds,
                        "total_latency_seconds": result.total_latency_seconds,
                        "output_tokens": result.output_tokens,
                        "output_tokens_source": result.output_tokens_source,
                        "tokens_per_second": result.tokens_per_second,
                        "error": None,
                    }
                )
                eval_rows.append(
                    {
                        "query": question,
                        "adoev": adoev,
                        "response": result.response,
                        "context": evaluation_context_to_text(
                            result.evaluation_context,
                            result.sources,
                        ),
                        "ground_truth": row["expected_answer"],
                        "expected_fuzet": str(row["expected_fuzet"]),
                        "expected_page": int(row["expected_page"]),
                        "sources": json.dumps(
                            evaluation_context_to_sources(
                                result.evaluation_context,
                                result.sources,
                            ),
                            ensure_ascii=False,
                        ),
                    }
                )
            except Exception as exc:
                LOGGER.exception("Question %d failed", index)
                question_results.append(
                    {
                        "index": index,
                        "question": question,
                        "error": str(exc),
                        "ttft_seconds": None,
                        "total_latency_seconds": None,
                        "output_tokens": None,
                        "tokens_per_second": None,
                    }
                )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(question_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return question_results, eval_rows


def _prefetch_contexts(
    endpoint: str,
    dataset: list[dict[str, Any]],
    output_path: Path,
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=30.0)
    with httpx.Client(
        timeout=timeout,
        headers={"Accept": "text/event-stream"},
    ) as client:
        for index, row in enumerate(dataset, start=1):
            question = str(row["question"])
            LOGGER.info(
                "[retrieval %d/%d] %s",
                index,
                len(dataset),
                question,
            )
            result = call_chat_endpoint(
                client,
                endpoint,
                question,
                int(row["adoev"]),
                retrieval_only=True,
            )
            if not result.evaluation_context:
                raise RuntimeError(
                    f"Retrieval returned no context for question {index}."
                )
            contexts.append(
                {
                    "index": index,
                    "question": question,
                    "adoev": int(row["adoev"]),
                    "evaluation_context": result.evaluation_context,
                    "sources": result.sources,
                }
            )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(contexts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return contexts


def _quality_metrics(eval_rows: list[dict[str, Any]], candidate_dir: Path) -> dict[str, float | None]:
    eval_input = candidate_dir / "evaluation_input.jsonl"
    eval_output = candidate_dir / "evaluation_results.json"
    write_jsonl(eval_input, eval_rows)
    result = run_foundry_evaluation(eval_input, output_path=eval_output)
    metrics = result.get("metrics", {}) if isinstance(result, dict) else {}
    return {
        metric: extract_metric(metrics, metric)
        for metric in QUALITY_THRESHOLDS
    }


def _candidate_cost(
    candidate: ModelCandidate,
    judge_price: TokenPrice,
    question_rows: list[dict[str, Any]],
    args: argparse.Namespace,
) -> float:
    assert candidate.price is not None
    output_tokens = sum(
        int(row["output_tokens"])
        for row in question_rows
        if isinstance(row.get("output_tokens"), int)
    )
    generation = (
        len(question_rows)
        * args.dynamic_model_max_cost_per_question_usd
        if candidate.family == "router"
        else candidate.price.estimate(
            len(question_rows) * args.input_tokens_per_question,
            output_tokens,
        )
    )
    judge = len(question_rows) * 3 * judge_price.estimate(
        args.judge_input_tokens_per_evaluation,
        args.judge_output_tokens_per_evaluation,
    )
    return generation + judge


def run_benchmark(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    registry = {"run_id": args.run_id, "deployments": [], "revisions": []}
    subscription_id: str | None = None
    write_registry(args.registry, registry)
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": args.run_id,
        "status": "initializing",
        "started_at": _iso_now(),
        "finished_at": None,
        "config": {
            "resource_group": args.resource_group,
            "ai_account": args.ai_account,
            "container_app": args.container_app,
            "base_revision": args.base_revision,
            "location": args.location,
            "judge_deployment": args.judge_deployment,
            "deployment_capacity": args.deployment_capacity,
            "question_count": args.question_count,
            "candidate_models": [
                name.strip()
                for name in args.candidate_models.split(",")
                if name.strip()
            ],
            "dynamic_model_max_cost_per_question_usd": (
                args.dynamic_model_max_cost_per_question_usd
            ),
            "dataset": str(args.dataset),
            "thresholds": QUALITY_THRESHOLDS,
            "token_accounting": (
                "Output tokens use cl100k_base estimates because the backend SSE contract "
                "does not expose provider usage. Input and judge tokens use conservative "
                "workflow assumptions."
            ),
        },
        "inventory": {},
        "preflight": {"max_estimated_cost_usd": args.max_estimated_cost_usd},
        "candidates": [],
        "ranking": [],
        "recommendation": None,
        "cleanup_errors": [],
    }
    exit_code = 1
    try:
        if args.max_estimated_cost_usd <= 0:
            raise ValueError("max_estimated_cost_usd must be greater than zero.")
        if args.deployment_capacity <= 0:
            raise ValueError("deployment_capacity must be greater than zero.")
        if args.requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be greater than zero.")
        if args.dynamic_model_max_cost_per_question_usd <= 0:
            raise ValueError(
                "dynamic_model_max_cost_per_question_usd must be greater than zero."
            )
        full_dataset = load_dataset(args.dataset)
        if len(full_dataset) != 100:
            raise ValueError(
                f"Canonical benchmark dataset requires exactly 100 questions; found {len(full_dataset)}."
            )
        dataset = select_questions(full_dataset, args.question_count)

        app = run_az(
            [
                "resource",
                "show",
                "--resource-group",
                args.resource_group,
                "--name",
                args.container_app,
                "--resource-type",
                "Microsoft.App/containerApps",
                "--api-version",
                "2024-03-01",
            ]
        )
        production_revision, _ = validate_traffic_isolation(app)
        subscription_id = str(
            run_az(["account", "show"]).get("id", "")
        )
        if not subscription_id:
            raise AzureCommandError("Azure subscription ID could not be resolved.")
        base_revision = production_revision
        if args.base_revision:
            base_revision = args.base_revision
        revision_url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/resourceGroups/{args.resource_group}"
            "/providers/Microsoft.App/containerApps"
            f"/{args.container_app}/revisions/{base_revision}"
            "?api-version=2024-03-01"
        )
        revision_response = run_az(
            ["rest", "--method", "get", "--url", revision_url]
        )
        revision_properties = (
            revision_response.get("properties", {})
            if isinstance(revision_response, dict)
            else {}
        )
        health = str(revision_properties.get("healthState", "")).lower()
        running = str(revision_properties.get("runningState", "")).lower()
        if health != "healthy" or running != "running":
            raise AzureCommandError(
                f"Base revision {base_revision} is not healthy and running."
            )
        current_deployment = (
            _revision_deployment(revision_response)
            if isinstance(revision_response, dict)
            else ""
        )
        if not current_deployment:
            raise AzureCommandError(
                f"Base revision {base_revision} has no current chat deployment."
            )
        report["config"]["base_revision"] = base_revision
        report["config"]["current_deployment"] = current_deployment
        catalog_url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/providers/Microsoft.CognitiveServices/locations/{args.location}"
            "/models?api-version=2024-10-01"
        )
        catalog_response = run_az(
            [
                "rest",
                "--method",
                "get",
                "--url",
                catalog_url,
            ]
        )
        usage_url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/providers/Microsoft.CognitiveServices/locations/{args.location}"
            "/usages?api-version=2024-10-01"
        )
        usage_response = run_az(
            [
                "rest",
                "--method",
                "get",
                "--url",
                usage_url,
            ]
        )
        raw_models = (
            catalog_response.get("value", [])
            if isinstance(catalog_response, dict)
            else []
        )
        usage = (
            usage_response.get("value", [])
            if isinstance(usage_response, dict)
            else []
        )
        if not isinstance(raw_models, list):
            raise AzureCommandError("Model discovery did not return a list.")
        candidates = parse_available_models(raw_models, args.deployment_capacity)
        usage_items = usage if isinstance(usage, list) else []
        apply_account_quota(candidates, usage_items)
        with httpx.Client() as price_client:
            price_items = fetch_retail_prices(price_client, location=args.location)
        _assign_prices(candidates, price_items, args.location)
        requested_models = [
            name.strip()
            for name in args.candidate_models.split(",")
            if name.strip()
        ]
        shortlist = (
            build_focused_shortlist(candidates, requested_models)
            if requested_models
            else build_shortlist(candidates)
        )
        for slot in shortlist:
            candidate = slot.candidate
            if (
                candidate is not None
                and candidate.family == "router"
            ):
                assumed_tokens = (
                    args.input_tokens_per_question
                    + args.output_tokens_per_question
                )
                rate = (
                    args.dynamic_model_max_cost_per_question_usd
                    * 1_000_000
                    / assumed_tokens
                )
                candidate.price = TokenPrice(
                    input_per_million_usd=rate,
                    output_per_million_usd=rate,
                    source_meters=(
                        "operator ceiling: dynamic router underlying model",
                    ),
                )

        judge_deployment = run_az(
            [
                "cognitiveservices",
                "account",
                "deployment",
                "show",
                "--resource-group",
                args.resource_group,
                "--name",
                args.ai_account,
                "--deployment-name",
                args.judge_deployment,
            ]
        )
        judge_model, judge_version, judge_sku = _model_details(judge_deployment)
        if not judge_model or not judge_version or not judge_sku:
            raise AzureCommandError("Judge deployment model/version/SKU could not be resolved.")
        judge_price = normalize_retail_price(
            price_items,
            judge_model,
            judge_sku,
            args.location,
        )
        current_deployment_details = (
            judge_deployment
            if current_deployment == args.judge_deployment
            else run_az(
                [
                    "cognitiveservices",
                    "account",
                    "deployment",
                    "show",
                    "--resource-group",
                    args.resource_group,
                    "--name",
                    args.ai_account,
                    "--deployment-name",
                    current_deployment,
                ]
            )
        )
        current_model, current_version, current_sku = _model_details(
            current_deployment_details
        )
        if not current_model or not current_version or not current_sku:
            raise AzureCommandError(
                "Current deployment model/version/SKU could not be resolved."
            )
        current_price = normalize_retail_price(
            price_items,
            current_model,
            current_sku,
            args.location,
        )
        current_candidate = ModelCandidate(
            name=current_model,
            version=current_version,
            model_format="OpenAI",
            sku=current_sku,
            quota=None,
            family="current",
            deployment_capacity=0,
            price=current_price,
            slot="current-model",
        )
        selected_all = [
            slot.candidate
            for slot in shortlist
            if slot.candidate is not None
        ]
        selected = [current_candidate] + [
            candidate for candidate in selected_all if candidate.price is not None
        ]
        excluded_unknown_price = [
            candidate.identifier
            for candidate in selected_all
            if candidate.price is None
        ]
        report["inventory"] = {
            "available_model_count": len(candidates),
            "models": [candidate.to_dict() for candidate in candidates],
            "account_usage": usage,
            "retail_price_item_count": len(price_items),
            "judge": {
                "deployment": args.judge_deployment,
                "model": judge_model,
                "version": judge_version,
                "sku": judge_sku,
                "price": _price_dict(judge_price),
            },
            "current_model": {
                "deployment": current_deployment,
                "model": current_model,
                "version": current_version,
                "sku": current_sku,
                "price": _price_dict(current_price),
            },
            "shortlist": [slot.to_dict() for slot in shortlist],
        }
        for slot in shortlist:
            if slot.status == "skipped":
                report["candidates"].append(
                    CandidateResult(
                        slot=slot.slot,
                        model=None,
                        status="skipped",
                        reason=slot.reason,
                    ).to_dict()
                )
            elif slot.candidate and slot.candidate.price is None:
                report["candidates"].append(
                    CandidateResult(
                        slot=slot.slot,
                        model=slot.candidate.identifier,
                        status="skipped_unknown_pricing",
                        reason=(
                            "No unambiguous Azure Retail Prices API input/output meters "
                            "were found; paid execution was skipped."
                        ),
                        pricing={"generation": None},
                    ).to_dict()
                )

        estimate, unknown = estimate_preflight_cost(
            selected,
            judge_price,
            questions=len(dataset),
            input_tokens_per_question=args.input_tokens_per_question,
            output_tokens_per_question=args.output_tokens_per_question,
            judge_input_tokens_per_evaluation=args.judge_input_tokens_per_evaluation,
            judge_output_tokens_per_evaluation=args.judge_output_tokens_per_evaluation,
        )
        decision = "approved"
        if not selected:
            decision = "blocked_no_priced_candidates"
        elif estimate is None:
            decision = "blocked_unknown_pricing"
        elif estimate > args.max_estimated_cost_usd:
            decision = "blocked_over_budget"
        report["preflight"] = {
            "estimated_cost_usd": estimate,
            "max_estimated_cost_usd": args.max_estimated_cost_usd,
            "unknown_prices": excluded_unknown_price + unknown,
            "excluded_candidate_count": len(excluded_unknown_price),
            "decision": decision,
            "assumptions": {
                "questions": len(dataset),
                "candidate_input_tokens_per_question": args.input_tokens_per_question,
                "candidate_output_tokens_per_question": args.output_tokens_per_question,
                "judge_evaluators_per_question": 3,
                "judge_input_tokens_per_evaluation": args.judge_input_tokens_per_evaluation,
                "judge_output_tokens_per_evaluation": args.judge_output_tokens_per_evaluation,
            },
            "candidate_estimates": {
                candidate.identifier: len(dataset)
                * candidate.price.estimate(
                    args.input_tokens_per_question,
                    args.output_tokens_per_question,
                )
                for candidate in selected
                if candidate.price is not None
            },
            "judge_estimate_usd": (
                None
                if judge_price is None
                else len(dataset)
                * len(selected)
                * 3
                * judge_price.estimate(
                    args.judge_input_tokens_per_evaluation,
                    args.judge_output_tokens_per_evaluation,
                )
            ),
        }
        if decision != "approved":
            report["status"] = decision
            exit_code = 2
            return exit_code
        if args.preflight_only:
            report["status"] = "preflight_approved"
            exit_code = 0
            return exit_code

        account = run_az(
            [
                "cognitiveservices",
                "account",
                "show",
                "--resource-group",
                args.resource_group,
                "--name",
                args.ai_account,
            ]
        )
        properties = account.get("properties", {}) if isinstance(account, dict) else {}
        judge_endpoint = args.judge_endpoint or str(properties.get("endpoint", ""))
        if not judge_endpoint:
            raise AzureCommandError("Judge endpoint could not be resolved.")
        os.environ["AZURE_OPENAI_ENDPOINT"] = judge_endpoint
        os.environ["AZURE_OPENAI_DEPLOYMENT"] = args.judge_deployment
        if args.project_url:
            os.environ["AZURE_AI_PROJECT_URL"] = args.project_url

        report["status"] = "running"
        base_endpoint = wait_for_revision(
            args.resource_group,
            args.container_app,
            base_revision,
            subscription_id=subscription_id,
        )
        frozen_contexts = _prefetch_contexts(
            base_endpoint,
            dataset,
            args.output_dir / "frozen_retrieval_contexts.json",
        )
        benchmark_token = secrets.token_urlsafe(32)
        report["config"]["retrieval_mode"] = "prefetched_once"
        for index, candidate in enumerate(selected, start=1):
            deployment: str | None = None
            revision: str | None = None
            candidate_result = CandidateResult(
                slot=str(candidate.slot),
                model=(
                    f"{current_deployment} ({candidate.identifier})"
                    if candidate.slot == "current-model"
                    else candidate.identifier
                ),
                status="running",
                pricing={"generation": _price_dict(candidate.price), "judge": _price_dict(judge_price)},
            )
            report["candidates"].append(candidate_result.to_dict())
            candidate_dir = args.output_dir / f"{index:02d}-{candidate.slot}"
            try:
                if candidate.slot == "current-model":
                    revision_deployment = current_deployment
                else:
                    deployment_to_create = deployment_name(
                        args.run_id,
                        index,
                        candidate.name,
                    )
                    _create_deployment(
                        candidate,
                        name=deployment_to_create,
                        resource_group=args.resource_group,
                        ai_account=args.ai_account,
                    )
                    deployment = deployment_to_create
                    registry["deployments"].append(deployment)
                    write_registry(args.registry, registry)
                    revision_deployment = deployment
                    credential = DefaultAzureCredential(
                        exclude_interactive_browser_credential=True
                    )
                    try:
                        access_token = credential.get_token(
                            "https://cognitiveservices.azure.com/.default"
                        ).token
                    finally:
                        credential.close()
                    with httpx.Client() as readiness_client:
                        wait_for_deployment_data_plane(
                            readiness_client,
                            endpoint=judge_endpoint,
                            deployment=deployment,
                            access_token=access_token,
                        )
                suffix = revision_suffix(args.run_id, index)
                revision = create_revision(
                    subscription_id=subscription_id,
                    resource_group=args.resource_group,
                    container_app=args.container_app,
                    base_revision=base_revision,
                    deployment=revision_deployment,
                    suffix=suffix,
                    environment_overrides={
                        "BENCHMARK_CONTEXT_TOKEN": benchmark_token,
                    },
                )
                registry["revisions"].append(revision)
                write_registry(args.registry, registry)
                endpoint = wait_for_revision(
                    args.resource_group,
                    args.container_app,
                    revision,
                    subscription_id=subscription_id,
                )
                question_rows, eval_rows = _run_questions(
                    endpoint,
                    dataset,
                    frozen_contexts,
                    benchmark_token,
                    candidate_dir / "questions.json",
                    args.requests_per_minute,
                )
                timing = summarize_timings(question_rows)
                if not eval_rows:
                    raise RuntimeError("Every backend question failed; quality evaluation was skipped.")
                quality = _quality_metrics(eval_rows, candidate_dir)
                candidate_result.timing = timing
                candidate_result.quality = quality
                candidate_result.estimated_cost_usd = _candidate_cost(
                    candidate,
                    judge_price,
                    question_rows,
                    args,
                )
                candidate_result.pricing["estimated_cost_per_question_usd"] = (
                    candidate_result.estimated_cost_usd / len(dataset)
                )
                candidate_result.status = (
                    "completed"
                    if timing["successful"] == len(dataset)
                    else "partial_failure"
                )
                if candidate_result.status == "partial_failure":
                    candidate_result.reason = (
                        f"{timing['failed']} of {len(dataset)} backend questions failed."
                    )
            except Exception as exc:
                LOGGER.exception("Candidate %s failed", candidate.identifier)
                candidate_result.status = "failed"
                candidate_result.reason = str(exc)
            finally:
                report["cleanup_errors"].extend(
                    _cleanup_candidate(
                        registry=registry,
                        registry_path=args.registry,
                        revision=revision,
                        deployment=deployment,
                        resource_group=args.resource_group,
                        container_app=args.container_app,
                        ai_account=args.ai_account,
                        subscription_id=subscription_id,
                    )
                )
                report["candidates"][-1] = candidate_result.to_dict()
                _write_report(report, args.output_dir)

        results = [CandidateResult(**item) for item in report["candidates"]]
        ranked = rank_results(results)
        report["ranking"] = [
            {
                "model": result.model,
                "slot": result.slot,
                "estimated_cost_usd": result.estimated_cost_usd,
                "quality_score": quality_score(result.quality),
                "latency_p95_seconds": result.timing.get("latency_p95_seconds"),
            }
            for result in ranked
        ]
        if ranked:
            winner = ranked[0]
            report["recommendation"] = {
                "model": winner.model,
                "slot": winner.slot,
                "estimated_cost_usd": winner.estimated_cost_usd,
                "quality": winner.quality,
                "quality_thresholds": QUALITY_THRESHOLDS,
                "latency_p95_seconds": winner.timing.get("latency_p95_seconds"),
            }
            report["status"] = "completed"
            exit_code = 0
        else:
            report["status"] = "completed_without_recommendation"
            exit_code = 1
    except Exception as exc:
        LOGGER.exception("Benchmark failed")
        report["status"] = "failed"
        report["fatal_error"] = str(exc)
        exit_code = 1
    finally:
        report["cleanup_errors"].extend(
            cleanup_registry(
                args.registry,
                resource_group=args.resource_group,
                ai_account=args.ai_account,
                container_app=args.container_app,
                subscription_id=subscription_id,
            )
        )
        report["finished_at"] = _iso_now()
        if report["cleanup_errors"] and exit_code == 0:
            report["status"] = "cleanup_failed"
            exit_code = 1
        _write_report(report, args.output_dir)
    return exit_code


def main() -> int:
    args = parse_args()
    if args.command == "cleanup":
        errors = cleanup_registry(
            args.registry,
            resource_group=args.resource_group,
            ai_account=args.ai_account,
            container_app=args.container_app,
        )
        for error in errors:
            LOGGER.error(error)
        return 1 if errors else 0
    return run_benchmark(args)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    sys.exit(main())
