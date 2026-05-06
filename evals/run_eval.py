from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from azure.ai.evaluation import (
    FluencyEvaluator,
    GroundednessEvaluator,
    RelevanceEvaluator,
    evaluate,
)
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

LOGGER = logging.getLogger("nav_eval")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_THRESHOLD_FILE = SCRIPT_DIR / "metrics.py"
DATASET_FILE = "qa.jsonl"
SMOKE_DATASET_FILE = "qa.smoke.jsonl"
EVAL_INPUT_FILE = SCRIPT_DIR / ".foundry_eval_input.jsonl"
EVAL_OUTPUT_FILE = SCRIPT_DIR / "foundry_eval_results.json"


@dataclass(slots=True)
class ChatRunResult:
    question: str
    response: str
    sources: list[dict[str, Any]]


class EvaluationConfigurationError(RuntimeError):
    """Raised when required evaluation configuration is missing."""


class CitationCorrectnessEvaluator:
    def __call__(
        self,
        *,
        response: str,
        expected_fuzet: str,
        expected_page: int | str,
        sources: str | list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        page = int(expected_page)
        parsed_sources = self._parse_sources(sources)
        matched_source = any(
            str(item.get("fuzet_szam", "")) == str(expected_fuzet)
            and int(item.get("page_from", 0)) <= page <= int(item.get("page_to", item.get("page_from", 0)))
            for item in parsed_sources
        )
        matched_text = self._response_mentions_citation(response=response, expected_fuzet=str(expected_fuzet), expected_page=page)
        score = 1.0 if matched_source or matched_text else 0.0
        return {
            "citation_correctness": score,
            "citation_correctness_reason": (
                "A várt füzet- és oldalhivatkozás megtalálható a források között vagy a válasz szövegében."
                if score
                else "A várt füzet- és oldalhivatkozás nem azonosítható."
            ),
        }

    @staticmethod
    def _parse_sources(sources: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if sources is None:
            return []
        if isinstance(sources, list):
            return [item for item in sources if isinstance(item, dict)]
        if not sources:
            return []
        try:
            parsed = json.loads(sources)
        except json.JSONDecodeError:
            return []
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []

    @staticmethod
    def _response_mentions_citation(*, response: str, expected_fuzet: str, expected_page: int) -> bool:
        if not response:
            return False
        citation_patterns = [
            rf"\[{re.escape(expected_fuzet)}[^\]]*{expected_page}(?:[^\d]|$)",
            rf"{re.escape(expected_fuzet)}\s*[–,-]\s*[^\n]*{expected_page}\.\s*oldal",
            rf"{re.escape(expected_fuzet)}[^\n]*{expected_page}-{expected_page}\.\s*oldal",
        ]
        return any(re.search(pattern, response, flags=re.IGNORECASE) for pattern in citation_patterns)



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run NAV chatbot evaluation against the backend and Foundry evaluators.")
    parser.add_argument("--endpoint", required=True, help="Base URL of the backend, for example http://localhost:8000")
    parser.add_argument("--smoke", action="store_true", help="Use evals/qa.smoke.jsonl instead of the full dataset")
    parser.add_argument("--threshold-file", default=str(DEFAULT_THRESHOLD_FILE), help="Path to the Python file that defines THRESHOLDS")
    return parser.parse_args()



def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )



def load_threshold_module(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"Threshold file not found: {path}")
    spec = importlib.util.spec_from_file_location("nav_eval_thresholds", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load threshold file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "THRESHOLDS"):
        raise AttributeError(f"Threshold file does not define THRESHOLDS: {path}")
    return module



def load_dataset(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {index} in {path}: {exc}") from exc
            rows.append(row)
    if not rows:
        raise ValueError(f"Dataset is empty: {path}")
    return rows



def build_chat_url(endpoint: str) -> str:
    cleaned = endpoint.rstrip("/")
    return cleaned if cleaned.endswith("/api/chat") else f"{cleaned}/api/chat"



def parse_sse_payload(line: str) -> dict[str, Any] | None:
    if not line.startswith("data: "):
        return None
    payload = line.removeprefix("data: ").strip()
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid SSE payload received from backend: {payload}") from exc



def call_chat_endpoint(client: httpx.Client, endpoint: str, question: str) -> ChatRunResult:
    url = build_chat_url(endpoint)
    response_text_parts: list[str] = []
    sources: list[dict[str, Any]] = []

    LOGGER.debug("Calling %s", url)
    with client.stream("POST", url, json={"message": question}) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            event = parse_sse_payload(line)
            if event is None:
                continue
            event_type = event.get("type")
            content = event.get("content")
            if event_type == "token" and isinstance(content, str):
                response_text_parts.append(content)
            elif event_type == "sources" and isinstance(content, list):
                sources = [item for item in content if isinstance(item, dict)]
            elif event_type == "error":
                raise RuntimeError(str(content or "Unknown backend error"))
            elif event_type == "done":
                break

    answer = "".join(response_text_parts).strip()
    if not answer:
        raise RuntimeError("The backend returned an empty answer.")
    return ChatRunResult(question=question, response=answer, sources=sources)



def sources_to_context(sources: list[dict[str, Any]]) -> str:
    if not sources:
        return "A backend válaszában nem érkezett külön forráslista."
    lines: list[str] = []
    for source in sources:
        page_from = int(source.get("page_from", 0))
        page_to = int(source.get("page_to", page_from))
        page_label = f"{page_from}. oldal" if page_from == page_to else f"{page_from}-{page_to}. oldal"
        lines.append(
            f"[{source.get('fuzet_szam', '')}, {page_label}] {source.get('fuzet_cim', '')} | {source.get('breadcrumb', '')}".strip()
        )
    return "\n".join(lines)



def build_eval_rows(dataset_rows: list[dict[str, Any]], endpoint: str) -> list[dict[str, Any]]:
    timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=30.0)
    eval_rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=timeout, headers={"Accept": "text/event-stream"}) as client:
        for index, row in enumerate(dataset_rows, start=1):
            question = str(row["question"])
            LOGGER.info("[%s/%s] Evaluating question: %s", index, len(dataset_rows), question)
            run_result = call_chat_endpoint(client, endpoint, question)
            eval_rows.append(
                {
                    "query": question,
                    "response": run_result.response,
                    "context": sources_to_context(run_result.sources),
                    "ground_truth": row["expected_answer"],
                    "expected_fuzet": str(row["expected_fuzet"]),
                    "expected_page": int(row["expected_page"]),
                    "sources": json.dumps(run_result.sources, ensure_ascii=False),
                }
            )
    return eval_rows



def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")



def resolve_azure_ai_project() -> str | dict[str, str] | None:
    """Resolve Foundry project config for cloud logging. Returns None if not configured."""
    for key in ("AZURE_AI_PROJECT_URL", "AI_FOUNDRY_PROJECT_URL", "FOUNDRY_PROJECT_URL"):
        value = os.getenv(key)
        if value:
            return value

    subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
    resource_group_name = os.getenv("AZURE_RESOURCE_GROUP")
    project_name = os.getenv("AZURE_AI_PROJECT_NAME") or os.getenv("AI_FOUNDRY_PROJECT_NAME")
    if subscription_id and resource_group_name and project_name:
        return {
            "subscription_id": subscription_id,
            "resource_group_name": resource_group_name,
            "project_name": project_name,
        }

    LOGGER.warning(
        "No Foundry project configured (AZURE_AI_PROJECT_URL or AZURE_SUBSCRIPTION_ID + "
        "AZURE_RESOURCE_GROUP + AZURE_AI_PROJECT_NAME). Running evaluation locally without cloud logging."
    )
    return None



def resolve_model_config() -> dict[str, str]:
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")
    if not endpoint or not api_key or not deployment:
        raise EvaluationConfigurationError(
            "Missing evaluator model configuration. Set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT."
        )
    return {
        "azure_endpoint": endpoint,
        "api_key": api_key,
        "azure_deployment": deployment,
        "api_version": api_version,
    }



def run_foundry_evaluation(eval_input_path: Path) -> dict[str, Any]:
    model_config = resolve_model_config()
    azure_ai_project = resolve_azure_ai_project()
    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)

    evaluators = {
        "groundedness": GroundednessEvaluator(model_config),
        "relevance": RelevanceEvaluator(model_config),
        "fluency": FluencyEvaluator(model_config),
        "citation_correctness": CitationCorrectnessEvaluator(),
    }
    evaluator_config = {
        "groundedness": {
            "column_mapping": {
                "query": "${data.query}",
                "context": "${data.context}",
                "response": "${data.response}",
            }
        },
        "relevance": {
            "column_mapping": {
                "query": "${data.query}",
                "response": "${data.response}",
            }
        },
        "fluency": {
            "column_mapping": {
                "query": "${data.query}",
                "response": "${data.response}",
            }
        },
        "citation_correctness": {
            "column_mapping": {
                "response": "${data.response}",
                "expected_fuzet": "${data.expected_fuzet}",
                "expected_page": "${data.expected_page}",
                "sources": "${data.sources}",
            }
        },
    }

    kwargs: dict[str, Any] = {
        "data": str(eval_input_path),
        "evaluators": evaluators,
        "evaluator_config": evaluator_config,
        "output_path": str(EVAL_OUTPUT_FILE),
    }
    # Cloud logging requires a Foundry project; skip if not configured
    if azure_ai_project is not None:
        kwargs["azure_ai_project"] = azure_ai_project
        kwargs["credential"] = credential
    else:
        LOGGER.info("Running evaluation locally (no Foundry project for cloud logging).")

    try:
        result = evaluate(**kwargs)
    except TypeError as exc:
        if "credential" not in str(exc):
            raise
        LOGGER.warning("Installed azure-ai-evaluation version does not accept credential in evaluate(); retrying without it.")
        kwargs.pop("credential", None)
        result = evaluate(**kwargs)

    if isinstance(result, dict):
        return result
    return {
        "metrics": getattr(result, "metrics", {}),
        "rows": getattr(result, "rows", []),
        "studio_url": getattr(result, "studio_url", None),
    }



def extract_metric(metrics: dict[str, Any], metric_name: str) -> float | None:
    direct_candidates = [
        metric_name,
        f"{metric_name}.{metric_name}",
        f"{metric_name}.gpt_{metric_name}",
        f"gpt_{metric_name}",
        f"{metric_name}.value",
    ]
    for candidate in direct_candidates:
        value = metrics.get(candidate)
        if isinstance(value, (int, float)):
            return float(value)
    for key, value in metrics.items():
        if not isinstance(value, (int, float)):
            continue
        if key.endswith(f".{metric_name}") or key.endswith(f"gpt_{metric_name}"):
            return float(value)
    return None



def print_summary(summary: dict[str, float | None], threshold_module: Any) -> bool:
    threshold_report = threshold_module.summarize_thresholds(summary, threshold_module.THRESHOLDS)
    all_passed = threshold_module.all_metrics_pass(summary, threshold_module.THRESHOLDS)
    print("Evaluation summary:")
    for metric_name, details in threshold_report.items():
        value = details["value"]
        threshold = details["threshold"]
        status = "PASS" if details["passed"] else "FAIL"
        value_label = "n/a" if value is None else f"{value:.3f}"
        print(f" - {metric_name}: {value_label} (threshold: {threshold:.3f}) => {status}")
    return all_passed



def main() -> int:
    load_dotenv()
    load_dotenv(SCRIPT_DIR.parent / ".env")
    args = parse_args()
    threshold_module = load_threshold_module(Path(args.threshold_file).resolve())
    dataset_path = SCRIPT_DIR / (SMOKE_DATASET_FILE if args.smoke else DATASET_FILE)
    dataset_rows = load_dataset(dataset_path)
    LOGGER.info("Loaded %s questions from %s", len(dataset_rows), dataset_path)

    eval_rows = build_eval_rows(dataset_rows, args.endpoint)
    write_jsonl(EVAL_INPUT_FILE, eval_rows)
    LOGGER.info("Prepared evaluation payload: %s", EVAL_INPUT_FILE)

    try:
        result = run_foundry_evaluation(EVAL_INPUT_FILE)
    finally:
        if EVAL_INPUT_FILE.exists():
            EVAL_INPUT_FILE.unlink()

    metrics = result.get("metrics", {}) if isinstance(result, dict) else {}
    summary = {
        "groundedness": extract_metric(metrics, "groundedness"),
        "relevance": extract_metric(metrics, "relevance"),
        "fluency": extract_metric(metrics, "fluency"),
        "citation_correctness": extract_metric(metrics, "citation_correctness"),
    }
    passed = print_summary(summary, threshold_module)
    studio_url = result.get("studio_url") if isinstance(result, dict) else None
    if studio_url:
        print(f"Foundry results: {studio_url}")
    print(f"Detailed results written to: {EVAL_OUTPUT_FILE}")
    return 0 if passed else 1


if __name__ == "__main__":
    configure_logging()
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - CLI failure path
        LOGGER.exception("Evaluation run failed: %s", exc)
        raise SystemExit(1)
