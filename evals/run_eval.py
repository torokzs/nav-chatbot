from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import tiktoken
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
    evaluation_context: list[dict[str, Any]]
    ttft_seconds: float | None
    total_latency_seconds: float
    output_tokens: int
    output_tokens_source: str

    @property
    def tokens_per_second(self) -> float | None:
        if self.output_tokens <= 0 or self.ttft_seconds is None:
            return None
        generation_seconds = self.total_latency_seconds - self.ttft_seconds
        if generation_seconds <= 0:
            return None
        return self.output_tokens / generation_seconds


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
    ) -> dict[str, Any]:
        page = int(expected_page)
        parsed_sources = self._parse_sources(sources)
        normalized_booklet = str(expected_fuzet).lstrip("0") or "0"
        matched_source = any(
            (str(item.get("fuzet_szam", "")).lstrip("0") or "0") == normalized_booklet
            and int(item.get("page_from", 0)) <= page <= int(item.get("page_to", item.get("page_from", 0)))
            for item in parsed_sources
        )
        matched_text = self._response_mentions_citation(response=response, expected_fuzet=str(expected_fuzet), expected_page=page)
        score = 1.0 if matched_source and matched_text else 0.0
        return {
            "citation_correctness": score,
            "citation_correctness_reason": (
                "A válasz és a backend forráslistája is tartalmazza a várt füzet- és oldalhivatkozást."
                if score
                else "A várt füzet- és oldalhivatkozás nem azonosítható egyszerre a válaszban és a forráslistában."
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
        booklet = expected_fuzet.lstrip("0") or "0"
        booklet_pattern = rf"0*{re.escape(booklet)}" if booklet.isdigit() else re.escape(booklet)
        bracket_pattern = re.compile(
            rf"\[\s*{booklet_pattern}\s*[–—-](?P<body>[^\]]+)\]",
            flags=re.IGNORECASE,
        )
        for citation in bracket_pattern.finditer(response):
            body = citation.group("body")
            page_list = re.search(
                r",\s*(?P<pages>\d[\d\s.,–—-]*)\s*oldal",
                body,
                flags=re.IGNORECASE,
            )
            if page_list is None:
                continue
            for start_text, end_text in re.findall(
                r"(\d+)(?:\s*[–—-]\s*(\d+))?",
                page_list.group("pages"),
            ):
                start = int(start_text)
                end = int(end_text) if end_text else start
                if start <= expected_page <= end:
                    return True
        citation_patterns = [
            rf"\[{booklet_pattern}[^\]]*{expected_page}(?:[^\d]|$)",
            rf"{booklet_pattern}\s*[–,-]\s*[^\n]*{expected_page}\.\s*oldal",
            rf"{booklet_pattern}[^\n]*{expected_page}-{expected_page}\.\s*oldal",
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



def call_chat_endpoint(
    client: httpx.Client,
    endpoint: str,
    question: str,
    adoev: int,
    *,
    evaluation_context: list[dict[str, Any]] | None = None,
    retrieval_only: bool = False,
    benchmark_token: str | None = None,
) -> ChatRunResult:
    url = build_chat_url(endpoint)
    response_text_parts: list[str] = []
    sources: list[dict[str, Any]] = []
    evaluation_context: list[dict[str, Any]] = []
    started = time.perf_counter()
    first_token_at: float | None = None

    LOGGER.debug("Calling %s", url)
    payload: dict[str, Any] = {
        "message": question,
        "adoev": adoev,
        "include_evaluation_context": True,
        "evaluation_retrieval_only": retrieval_only,
    }
    if evaluation_context is not None:
        payload["evaluation_context"] = evaluation_context
    headers = (
        {"x-benchmark-context-token": benchmark_token}
        if benchmark_token
        else None
    )
    with client.stream(
        "POST",
        url,
        json=payload,
        headers=headers,
    ) as response:
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
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                response_text_parts.append(content)
            elif event_type == "context" and isinstance(content, list):
                evaluation_context = [item for item in content if isinstance(item, dict)]
            elif event_type == "sources" and isinstance(content, list):
                sources = [item for item in content if isinstance(item, dict)]
            elif event_type == "error":
                raise RuntimeError(str(content or "Unknown backend error"))
            elif event_type == "done":
                break

    answer = "".join(response_text_parts).strip()
    if not answer and not retrieval_only:
        raise RuntimeError("The backend returned an empty answer.")
    completed = time.perf_counter()
    output_tokens = len(tiktoken.get_encoding("cl100k_base").encode(answer))
    return ChatRunResult(
        question=question,
        response=answer,
        sources=sources,
        evaluation_context=evaluation_context,
        ttft_seconds=None if first_token_at is None else first_token_at - started,
        total_latency_seconds=completed - started,
        output_tokens=output_tokens,
        output_tokens_source="cl100k_base estimate; backend SSE does not expose usage",
    )



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


def evaluation_context_to_text(
    context_chunks: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> str:
    content = [
        str(chunk.get("content", "")).strip()
        for chunk in context_chunks
        if str(chunk.get("content", "")).strip()
    ]
    return "\n\n".join(content) if content else sources_to_context(sources)



def build_eval_rows(dataset_rows: list[dict[str, Any]], endpoint: str) -> list[dict[str, Any]]:
    timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=30.0)
    eval_rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=timeout, headers={"Accept": "text/event-stream"}) as client:
        for index, row in enumerate(dataset_rows, start=1):
            question = str(row["question"])
            LOGGER.info("[%s/%s] Evaluating question: %s", index, len(dataset_rows), question)
            adoev = int(row["adoev"])
            run_result = call_chat_endpoint(client, endpoint, question, adoev)
            eval_rows.append(
                {
                    "query": question,
                    "response": run_result.response,
                    "context": evaluation_context_to_text(
                        run_result.evaluation_context,
                        run_result.sources,
                    ),
                    "ground_truth": row["expected_answer"],
                    "expected_fuzet": str(row["expected_fuzet"]),
                    "expected_page": int(row["expected_page"]),
                    "adoev": adoev,
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



def resolve_model_config() -> tuple[dict[str, str], Any]:
    """Return (model_config_dict, credential_or_None)."""
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    api_key = os.getenv("AZURE_OPENAI_API_KEY")

    if not endpoint or not deployment:
        raise EvaluationConfigurationError(
            "Missing evaluator model configuration. Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT."
        )

    config: dict[str, str] = {
        "azure_endpoint": endpoint,
        "azure_deployment": deployment,
    }
    if api_key:
        config["api_key"] = api_key
        return config, None
    LOGGER.info("No AZURE_OPENAI_API_KEY set; using DefaultAzureCredential for evaluator model.")
    return config, DefaultAzureCredential(exclude_interactive_browser_credential=True)



def run_foundry_evaluation(
    eval_input_path: Path,
    *,
    output_path: Path = EVAL_OUTPUT_FILE,
    citation_only: bool = False,
) -> dict[str, Any]:
    model_config, evaluator_credential = resolve_model_config()
    azure_ai_project = resolve_azure_ai_project()

    eval_kwargs: dict[str, Any] = {}
    if evaluator_credential is not None:
        eval_kwargs["credential"] = evaluator_credential

    evaluators: dict[str, Any] = {
        "citation_correctness": CitationCorrectnessEvaluator(),
    }
    evaluator_config: dict[str, Any] = {
        "citation_correctness": {
            "column_mapping": {
                "response": "${data.response}",
                "expected_fuzet": "${data.expected_fuzet}",
                "expected_page": "${data.expected_page}",
                "sources": "${data.sources}",
            }
        },
    }

    # PR smoke validates the deployed LLM through the backend call above and keeps
    # evaluation deterministic with citation checks; full runs add GPT judges.
    if not citation_only:
        evaluators["groundedness"] = GroundednessEvaluator(model_config, **eval_kwargs)
        evaluators["relevance"] = RelevanceEvaluator(model_config, **eval_kwargs)
        evaluators["fluency"] = FluencyEvaluator(model_config, **eval_kwargs)
        evaluator_config["groundedness"] = {
            "column_mapping": {
                "query": "${data.query}",
                "context": "${data.context}",
                "response": "${data.response}",
            }
        }
        evaluator_config["relevance"] = {
            "column_mapping": {
                "query": "${data.query}",
                "response": "${data.response}",
            }
        }
        evaluator_config["fluency"] = {
            "column_mapping": {
                "query": "${data.query}",
                "response": "${data.response}",
            }
        }

    kwargs: dict[str, Any] = {
        "data": str(eval_input_path),
        "evaluators": evaluators,
        "evaluator_config": evaluator_config,
        "max_concurrency": 1,
        "output_path": str(output_path),
    }
    # Cloud logging requires a Foundry project; skip if not configured
    if azure_ai_project is not None:
        kwargs["azure_ai_project"] = azure_ai_project
        if evaluator_credential is not None:
            kwargs["credential"] = evaluator_credential
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



def print_summary(
    summary: dict[str, float | None],
    threshold_module: Any,
    thresholds: dict[str, float] | None = None,
) -> bool:
    active_thresholds = thresholds or threshold_module.THRESHOLDS
    threshold_report = threshold_module.summarize_thresholds(summary, active_thresholds)
    all_passed = threshold_module.all_metrics_pass(summary, active_thresholds)
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
        result = run_foundry_evaluation(EVAL_INPUT_FILE, citation_only=args.smoke)
    finally:
        if EVAL_INPUT_FILE.exists():
            EVAL_INPUT_FILE.unlink()

    metrics = result.get("metrics", {}) if isinstance(result, dict) else {}
    summary = {
        "citation_correctness": extract_metric(metrics, "citation_correctness"),
    }
    if not args.smoke:
        summary.update(
            {
                "groundedness": extract_metric(metrics, "groundedness"),
                "relevance": extract_metric(metrics, "relevance"),
                "fluency": extract_metric(metrics, "fluency"),
            }
        )
    active_thresholds = (
        {"citation_correctness": threshold_module.THRESHOLDS["citation_correctness"]}
        if args.smoke
        else threshold_module.THRESHOLDS
    )
    passed = print_summary(summary, threshold_module, active_thresholds)

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
