from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from benchmark_azure import (
    AzureCommandError,
    cleanup_registry,
    deployment_name,
    fetch_retail_prices,
    revision_suffix,
    run_az,
    validate_traffic_isolation,
    wait_for_revision,
    write_registry,
)
from benchmark_core import (
    QUALITY_THRESHOLDS,
    CandidateResult,
    ModelCandidate,
    TokenPrice,
    apply_account_quota,
    build_shortlist,
    estimate_preflight_cost,
    normalize_retail_price,
    parse_available_models,
    quality_score,
    rank_results,
    summarize_timings,
)
from run_eval import (
    call_chat_endpoint,
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
    run_parser.add_argument("--deployment-capacity", type=int, default=10)
    run_parser.add_argument("--requests-per-minute", type=int, default=25)
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
    return (
        str(model.get("name", "")),
        str(model.get("version", "")),
        str(sku.get("name", "")),
    )


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


def _create_revision(
    *,
    resource_group: str,
    container_app: str,
    base_revision: str,
    deployment: str,
    suffix: str,
) -> str:
    revision = f"{container_app}--{suffix}"
    run_az(
        [
            "containerapp",
            "revision",
            "copy",
            "--resource-group",
            resource_group,
            "--name",
            container_app,
            "--from-revision",
            base_revision,
            "--revision-suffix",
            suffix,
            "--set-env-vars",
            f"AZURE_AI_FOUNDRY_CHAT_DEPLOYMENT={deployment}",
        ]
    )
    return revision


def _cleanup_candidate(
    *,
    registry: dict[str, Any],
    registry_path: Path,
    revision: str | None,
    deployment: str | None,
    resource_group: str,
    container_app: str,
    ai_account: str,
) -> list[str]:
    errors: list[str] = []
    if revision:
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
            LOGGER.info("[%d/%d] %s", index, len(dataset), question)
            try:
                result = call_chat_endpoint(client, endpoint, question)
                question_results.append(
                    {
                        "index": index,
                        "question": question,
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
                        "response": result.response,
                        "context": evaluation_context_to_text(
                            result.evaluation_context,
                            result.sources,
                        ),
                        "ground_truth": row["expected_answer"],
                        "expected_fuzet": str(row["expected_fuzet"]),
                        "expected_page": int(row["expected_page"]),
                        "sources": json.dumps(result.sources, ensure_ascii=False),
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
    generation = candidate.price.estimate(
        len(question_rows) * args.input_tokens_per_question,
        output_tokens,
    )
    judge = len(question_rows) * 3 * judge_price.estimate(
        args.judge_input_tokens_per_evaluation,
        args.judge_output_tokens_per_evaluation,
    )
    return generation + judge


def run_benchmark(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    registry = {"run_id": args.run_id, "deployments": [], "revisions": []}
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
            "location": args.location,
            "judge_deployment": args.judge_deployment,
            "deployment_capacity": args.deployment_capacity,
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
        dataset = load_dataset(args.dataset)
        if len(dataset) != 100:
            raise ValueError(f"Full benchmark requires exactly 100 questions; found {len(dataset)}.")

        app = run_az(
            [
                "containerapp",
                "show",
                "--resource-group",
                args.resource_group,
                "--name",
                args.container_app,
            ]
        )
        base_revision, _ = validate_traffic_isolation(app)
        raw_models = run_az(
            [
                "cognitiveservices",
                "account",
                "list-models",
                "--resource-group",
                args.resource_group,
                "--name",
                args.ai_account,
            ]
        )
        usage = run_az(
            [
                "cognitiveservices",
                "account",
                "list-usage",
                "--resource-group",
                args.resource_group,
                "--name",
                args.ai_account,
            ]
        )
        if not isinstance(raw_models, list):
            raise AzureCommandError("Model discovery did not return a list.")
        candidates = parse_available_models(raw_models, args.deployment_capacity)
        usage_items = usage if isinstance(usage, list) else []
        apply_account_quota(candidates, usage_items)
        with httpx.Client() as price_client:
            price_items = fetch_retail_prices(price_client, location=args.location)
        _assign_prices(candidates, price_items, args.location)
        shortlist = build_shortlist(candidates)

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
        selected_all = [
            slot.candidate
            for slot in shortlist
            if slot.candidate is not None
        ]
        selected = [candidate for candidate in selected_all if candidate.price is not None]
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
        for index, candidate in enumerate(selected, start=1):
            deployment: str | None = None
            revision: str | None = None
            candidate_result = CandidateResult(
                slot=str(candidate.slot),
                model=candidate.identifier,
                status="running",
                pricing={"generation": _price_dict(candidate.price), "judge": _price_dict(judge_price)},
            )
            report["candidates"].append(candidate_result.to_dict())
            candidate_dir = args.output_dir / f"{index:02d}-{candidate.slot}"
            try:
                deployment_to_create = deployment_name(args.run_id, index, candidate.name)
                _create_deployment(
                    candidate,
                    name=deployment_to_create,
                    resource_group=args.resource_group,
                    ai_account=args.ai_account,
                )
                deployment = deployment_to_create
                registry["deployments"].append(deployment)
                write_registry(args.registry, registry)

                suffix = revision_suffix(args.run_id, index)
                revision = _create_revision(
                    resource_group=args.resource_group,
                    container_app=args.container_app,
                    base_revision=base_revision,
                    deployment=deployment,
                    suffix=suffix,
                )
                registry["revisions"].append(revision)
                write_registry(args.registry, registry)
                endpoint = wait_for_revision(
                    args.resource_group,
                    args.container_app,
                    revision,
                )
                question_rows, eval_rows = _run_questions(
                    endpoint,
                    dataset,
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
