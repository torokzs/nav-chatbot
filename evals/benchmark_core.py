from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

QUALITY_THRESHOLDS = {
    "groundedness": 4.0,
    "relevance": 4.0,
    "fluency": 4.0,
    "citation_correctness": 0.8,
}

SLOT_SPECS = (
    ("frontier-openai", "openai", "frontier"),
    ("frontier-claude", "claude", "frontier"),
    ("efficient-openai-1", "openai", "efficient"),
    ("efficient-openai-2", "openai", "efficient"),
    ("efficient-claude-1", "claude", "efficient"),
    ("efficient-claude-2", "claude", "efficient"),
    ("mistral", "mistral", "efficient"),
    ("deepseek", "deepseek", "efficient"),
)

FRONTIER_PRIORITY = {
    "openai": (
        "gpt-5.2",
        "gpt-5.1",
        "gpt-5",
        "o3",
        "gpt-4.1",
        "gpt-4o",
    ),
    "claude": (
        "claude-opus-4.1",
        "claude-opus-4",
        "claude-sonnet-4.5",
        "claude-sonnet-4",
        "claude-3-7-sonnet",
    ),
}

EFFICIENCY_PRIORITY = {
    "openai": (
        "gpt-5-nano",
        "gpt-5-mini",
        "gpt-4.1-nano",
        "gpt-4.1-mini",
        "gpt-4o-mini",
    ),
    "claude": (
        "claude-haiku",
        "claude-3-5-haiku",
        "claude-sonnet",
    ),
    "mistral": (
        "ministral",
        "mistral-small",
        "mistral-medium",
        "mistral-large",
    ),
    "deepseek": (
        "deepseek-v3",
        "deepseek-r1",
        "deepseek",
    ),
}


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def detect_family(model_name: str, model_format: str = "") -> str | None:
    value = f"{model_name} {model_format}".lower()
    if "claude" in value or "anthropic" in value:
        return "claude"
    if "mistral" in value or "ministral" in value:
        return "mistral"
    if "deepseek" in value:
        return "deepseek"
    if (
        "openai" in value
        or re.search(r"(?:^|[\s_-])(gpt|o1|o3|o4)(?:[\s_.-]|$)", value)
    ):
        return "openai"
    return None


def _is_chat_candidate(model_name: str) -> bool:
    normalized = _normalized(model_name)
    excluded = (
        "audio",
        "codex",
        "computer-use",
        "dall-e",
        "deep-research",
        "embedding",
        "image",
        "model-router",
        "realtime",
        "search-preview",
        "transcribe",
        "tts",
        "whisper",
    )
    return not any(value in normalized for value in excluded)


@dataclass(slots=True, frozen=True)
class TokenPrice:
    input_per_million_usd: float
    output_per_million_usd: float
    source_meters: tuple[str, ...]

    def estimate(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_per_million_usd
            + output_tokens * self.output_per_million_usd
        ) / 1_000_000


@dataclass(slots=True)
class ModelCandidate:
    name: str
    version: str
    model_format: str
    sku: str
    quota: float | None
    family: str
    deployment_capacity: int = 1
    remaining_quota: float | None = None
    source: str | None = None
    price: TokenPrice | None = None
    slot: str | None = None

    @property
    def identifier(self) -> str:
        return f"{self.name}:{self.version}:{self.sku}"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["identifier"] = self.identifier
        return result


@dataclass(slots=True)
class SlotResult:
    slot: str
    status: str
    candidate: ModelCandidate | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "status": self.status,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "reason": self.reason,
        }


@dataclass(slots=True)
class CandidateResult:
    slot: str
    model: str | None
    status: str
    reason: str | None = None
    quality: dict[str, float | None] = field(default_factory=dict)
    timing: dict[str, float | None] = field(default_factory=dict)
    pricing: dict[str, Any] = field(default_factory=dict)
    estimated_cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _capacity_maximum(sku: Mapping[str, Any]) -> float | None:
    capacity = sku.get("capacity")
    if not isinstance(capacity, Mapping):
        return None
    value = capacity.get("maximum")
    return float(value) if isinstance(value, (int, float)) else None


def _available_skus(
    skus: Iterable[Mapping[str, Any]],
    requested_capacity: int,
) -> list[tuple[str, float | None, int]]:
    realtime: list[tuple[str, float | None, int]] = []
    for sku in skus:
        name = str(sku.get("name", "")).strip()
        if not name or any(word in name.lower() for word in ("batch", "provisioned")):
            continue
        maximum = _capacity_maximum(sku)
        if maximum is not None and maximum <= 0:
            continue
        capacity = sku.get("capacity")
        capacity_map = capacity if isinstance(capacity, Mapping) else {}
        minimum = float(capacity_map.get("minimum", 1))
        default = float(capacity_map.get("default", minimum))
        step = max(float(capacity_map.get("step", 1)), 1)
        desired = max(float(requested_capacity), minimum, default)
        if maximum is not None:
            desired = min(desired, maximum)
        units = minimum + math.ceil(max(desired - minimum, 0) / step) * step
        if maximum is not None:
            units = min(units, maximum)
        realtime.append((name, maximum, max(1, math.floor(units))))
    order = {"globalstandard": 0, "datazonestandard": 1, "standard": 2}
    return sorted(realtime, key=lambda item: (order.get(_compact(item[0]), 10), item[0]))


def parse_available_models(
    raw_models: Sequence[Mapping[str, Any]],
    requested_capacity: int = 10,
) -> list[ModelCandidate]:
    latest: dict[tuple[str, str, str], ModelCandidate] = {}
    for item in raw_models:
        model = item.get("model") if isinstance(item.get("model"), Mapping) else item
        assert isinstance(model, Mapping)
        name = str(model.get("name", "")).strip()
        version = str(model.get("version", "")).strip()
        model_format = str(model.get("format", model.get("modelFormat", ""))).strip()
        family = detect_family(name, model_format)
        skus_value = item.get("skus", model.get("skus", []))
        skus = skus_value if isinstance(skus_value, list) else []
        available_skus = _available_skus(
            (sku for sku in skus if isinstance(sku, Mapping)),
            requested_capacity,
        )
        if (
            not name
            or not version
            or not model_format
            or family is None
            or not _is_chat_candidate(name)
        ):
            continue
        for sku, quota, capacity in available_skus:
            candidate = ModelCandidate(
                name=name,
                version=version,
                model_format=model_format,
                sku=sku,
                quota=quota,
                family=family,
                deployment_capacity=capacity,
                source=str(model.get("source", "")).strip() or None,
            )
            key = (name.lower(), model_format.lower(), sku.lower())
            existing = latest.get(key)
            if existing is None or _version_key(version) > _version_key(existing.version):
                latest[key] = candidate
    return sorted(latest.values(), key=lambda candidate: candidate.identifier.lower())


def _version_key(version: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", version)
    return tuple(int(number) for number in numbers) if numbers else (0,)


def _priority_index(name: str, priorities: Sequence[str]) -> int:
    normalized_name = _normalized(name)
    for index, priority in enumerate(priorities):
        if _normalized(priority) in normalized_name:
            return index
    return len(priorities) + 1


def _candidate_order(candidate: ModelCandidate, tier: str) -> tuple[Any, ...]:
    sku_order = {"globalstandard": 0, "datazonestandard": 1, "standard": 2}
    sku_priority = sku_order.get(_compact(candidate.sku), 10)
    if tier == "frontier":
        priorities = FRONTIER_PRIORITY.get(candidate.family, ())
        return (
            _priority_index(candidate.name, priorities),
            tuple(-part for part in _version_key(candidate.version)),
            sku_priority,
            candidate.name.lower(),
        )
    priorities = EFFICIENCY_PRIORITY.get(candidate.family, ())
    price_total = (
        candidate.price.input_per_million_usd + candidate.price.output_per_million_usd
        if candidate.price
        else math.inf
    )
    return (
        price_total,
        _priority_index(candidate.name, priorities),
        sku_priority,
        candidate.name.lower(),
        tuple(-part for part in _version_key(candidate.version)),
    )


def build_shortlist(candidates: Sequence[ModelCandidate]) -> list[SlotResult]:
    selected: set[str] = set()
    results: list[SlotResult] = []
    for slot, family, tier in SLOT_SPECS:
        available = [
            candidate
            for candidate in candidates
            if candidate.family == family
            and candidate.name.lower() not in selected
            and candidate.deployment_capacity > 0
        ]
        if not available:
            results.append(
                SlotResult(
                    slot=slot,
                    status="skipped",
                    reason=f"No available {family} model remained for the {tier} slot.",
                )
            )
            continue
        candidate = min(available, key=lambda item: _candidate_order(item, tier))
        candidate.slot = slot
        selected.add(candidate.name.lower())
        results.append(SlotResult(slot=slot, status="selected", candidate=candidate))
    return results


def _unit_tokens(unit: str) -> int | None:
    compact = _compact(unit)
    if (
        "1million" in compact
        or "1000000" in compact
        or compact.startswith("1m")
    ):
        return 1_000_000
    if "1000" in compact or compact.startswith("1k"):
        return 1_000
    if "token" in compact:
        return 1
    return None


def _model_matches_text(model_name: str, text: str) -> bool:
    parts = re.findall(r"[a-z0-9]+", model_name.lower())
    if not parts:
        return False
    part_sets = [(parts, False)]
    if len(parts) > 2 and parts[-1].isdigit() and len(parts[-1]) >= 4:
        part_sets.append((parts[:-1], False))
    if parts[0] in {"gpt", "mistral"} and len(parts) > 1:
        part_sets.append((parts[1:], True))
    disallowed_suffixes = {
        "mini",
        "nano",
        "small",
        "medium",
        "large",
        "haiku",
        "sonnet",
        "opus",
        "pro",
        "codex",
        "chat",
        "deep",
    }
    for candidate_parts, anchored in part_sets:
        pattern = re.compile(
            (r"^\s*" if anchored else r"(?<![a-z0-9])")
            + r"[\s._-]*".join(re.escape(part) for part in candidate_parts)
            + r"(?![a-z0-9])",
            flags=re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            remainder = text[match.end() :]
            suffix = re.match(r"[\s._-]+([a-z0-9]+)", remainder.lower())
            if suffix and (
                suffix.group(1) in disallowed_suffixes
                or suffix.group(1).isdigit()
            ):
                continue
            return True
    return False


def apply_account_quota(
    candidates: Sequence[ModelCandidate],
    usage_items: Sequence[Mapping[str, Any]],
) -> None:
    for candidate in candidates:
        remaining_values: list[float] = []
        for usage in usage_items:
            name_value = usage.get("name")
            name = name_value if isinstance(name_value, Mapping) else {}
            text = f"{name.get('value', '')} {name.get('localizedValue', '')}"
            if not _model_matches_text(candidate.name, text):
                continue
            compact_text = _compact(text)
            sku_key = _compact(candidate.sku)
            usage_tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
            if sku_key == "globalstandard" and not (
                "global" in text.lower() or usage_tokens.intersection({"gl", "glbl"})
            ):
                continue
            if sku_key == "datazonestandard" and not (
                "datazone" in compact_text or usage_tokens.intersection({"dz", "dzone"})
            ):
                continue
            if sku_key == "standard" and (
                "global" in text.lower()
                or usage_tokens.intersection({"gl", "glbl"})
                or "datazone" in compact_text
                or usage_tokens.intersection({"dz", "dzone"})
            ):
                continue
            if (
                sku_key not in {"standard", "globalstandard", "datazonestandard"}
                and sku_key not in compact_text
            ):
                continue
            current = usage.get("currentValue")
            limit = usage.get("limit")
            if isinstance(current, (int, float)) and isinstance(limit, (int, float)):
                remaining_values.append(max(float(limit) - float(current), 0))
        if remaining_values:
            candidate.remaining_quota = min(remaining_values)
            candidate.deployment_capacity = min(
                candidate.deployment_capacity,
                math.floor(candidate.remaining_quota),
            )


def normalize_retail_price(
    items: Sequence[Mapping[str, Any]],
    model_name: str,
    sku: str,
    region: str | None = None,
) -> TokenPrice | None:
    sku_key = _compact(sku)
    input_values: list[tuple[float, str]] = []
    output_values: list[tuple[float, str]] = []
    for item in items:
        meter_region = str(item.get("armRegionName", "")).lower()
        if region and meter_region not in ("", "global", region.lower()):
            continue
        text = " ".join(
            str(item.get(key, ""))
            for key in ("productName", "skuName", "meterName", "armSkuName")
        )
        model_meter_text = " ".join(
            str(item.get(key, ""))
            for key in ("skuName", "meterName", "armSkuName")
        )
        lower_text = text.lower()
        compact_text = _compact(text)
        if not _model_matches_text(model_name, model_meter_text):
            continue
        text_tokens = set(re.findall(r"[a-z0-9]+", lower_text))
        if sku_key == "globalstandard" and not (
            "global" in lower_text or text_tokens.intersection({"gl", "glbl"})
        ):
            continue
        if sku_key == "datazonestandard" and not (
            "datazone" in compact_text or text_tokens.intersection({"dz", "dzone"})
        ):
            continue
        if sku_key == "standard" and (
            "global" in lower_text
            or text_tokens.intersection({"gl", "glbl"})
            or "datazone" in compact_text
            or text_tokens.intersection({"dz", "dzone"})
        ):
            continue
        if sku_key not in {"standard", "globalstandard", "datazonestandard"}:
            sku_words = {
                word for word in re.split(r"[^a-z0-9]+", sku.lower()) if len(word) >= 4
            }
            if sku_words and not any(word in lower_text for word in sku_words):
                continue
        if (
            any(
                word in lower_text
                for word in ("cached", "batch", "fine tuning", "fine-tun", "training")
            )
            or text_tokens.intersection({"cache", "cchd", "cd", "ft", "rft"})
        ):
            continue
        if "priority" not in sku_key and text_tokens.intersection({"pp", "priority"}):
            continue
        token_count = _unit_tokens(str(item.get("unitOfMeasure", "")))
        retail_price = item.get("retailPrice")
        meter_name = str(item.get("meterName", ""))
        if token_count is None or not isinstance(retail_price, (int, float)):
            continue
        per_million = float(retail_price) * 1_000_000 / token_count
        effective_date = str(item.get("effectiveStartDate", ""))
        if "input" in lower_text or text_tokens.intersection({"inp", "inpt"}):
            input_values.append((per_million, f"{effective_date}|{meter_name}"))
        elif "output" in lower_text or text_tokens.intersection(
            {"opt", "out", "outp", "outpt"}
        ):
            output_values.append((per_million, f"{effective_date}|{meter_name}"))
    input_values = _latest_price_values(input_values)
    output_values = _latest_price_values(output_values)
    input_prices = {round(value, 12) for value, _ in input_values}
    output_prices = {round(value, 12) for value, _ in output_values}
    if len(input_prices) != 1 or len(output_prices) != 1:
        return None
    return TokenPrice(
        input_per_million_usd=input_prices.pop(),
        output_per_million_usd=output_prices.pop(),
        source_meters=tuple(sorted({name for _, name in input_values + output_values})),
    )


def _latest_price_values(values: list[tuple[float, str]]) -> list[tuple[float, str]]:
    if not values:
        return values
    latest = max(name.partition("|")[0] for _, name in values)
    return [
        (value, name.partition("|")[2])
        for value, name in values
        if name.partition("|")[0] == latest
    ]


def estimate_preflight_cost(
    candidates: Sequence[ModelCandidate],
    judge_price: TokenPrice | None,
    *,
    questions: int,
    input_tokens_per_question: int,
    output_tokens_per_question: int,
    judge_input_tokens_per_evaluation: int,
    judge_output_tokens_per_evaluation: int,
    judge_evaluators: int = 3,
) -> tuple[float | None, list[str]]:
    unknown: list[str] = []
    candidate_cost = 0.0
    for candidate in candidates:
        if candidate.price is None:
            unknown.append(candidate.identifier)
            continue
        candidate_cost += questions * candidate.price.estimate(
            input_tokens_per_question,
            output_tokens_per_question,
        )
    if judge_price is None:
        unknown.append("judge")
    if unknown:
        return None, unknown
    assert judge_price is not None
    judge_cost = questions * len(candidates) * judge_evaluators * judge_price.estimate(
        judge_input_tokens_per_evaluation,
        judge_output_tokens_per_evaluation,
    )
    return candidate_cost + judge_cost, []


def percentile(values: Sequence[float], percentile_value: float) -> float | None:
    if not values:
        return None
    if not 0 <= percentile_value <= 100:
        raise ValueError("percentile must be between 0 and 100")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile_value / 100
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def summarize_timings(rows: Sequence[Mapping[str, Any]]) -> dict[str, float | int | None]:
    successful = [row for row in rows if not row.get("error")]
    ttft = [float(row["ttft_seconds"]) for row in successful if row.get("ttft_seconds") is not None]
    latency = [
        float(row["total_latency_seconds"])
        for row in successful
        if row.get("total_latency_seconds") is not None
    ]
    throughput = [
        float(row["tokens_per_second"])
        for row in successful
        if row.get("tokens_per_second") is not None
    ]
    return {
        "questions": len(rows),
        "successful": len(successful),
        "failed": len(rows) - len(successful),
        "ttft_mean_seconds": mean(ttft) if ttft else None,
        "ttft_p50_seconds": percentile(ttft, 50),
        "ttft_p95_seconds": percentile(ttft, 95),
        "latency_mean_seconds": mean(latency) if latency else None,
        "latency_p50_seconds": percentile(latency, 50),
        "latency_p95_seconds": percentile(latency, 95),
        "throughput_mean_tokens_per_second": mean(throughput) if throughput else None,
        "throughput_p50_tokens_per_second": percentile(throughput, 50),
        "throughput_p95_tokens_per_second": percentile(throughput, 95),
    }


def quality_passes(
    quality: Mapping[str, float | int | None],
    thresholds: Mapping[str, float] = QUALITY_THRESHOLDS,
) -> bool:
    return all(
        quality.get(metric) is not None
        and float(quality[metric]) >= threshold  # type: ignore[arg-type]
        for metric, threshold in thresholds.items()
    )


def quality_score(quality: Mapping[str, float | int | None]) -> float:
    if any(quality.get(metric) is None for metric in QUALITY_THRESHOLDS):
        return 0.0
    return (
        float(quality["groundedness"]) / 5
        + float(quality["relevance"]) / 5
        + float(quality["fluency"]) / 5
        + float(quality["citation_correctness"])
    )


def rank_results(results: Sequence[CandidateResult]) -> list[CandidateResult]:
    eligible = [
        result
        for result in results
        if result.status == "completed"
        and result.estimated_cost_usd is not None
        and quality_passes(result.quality)
    ]

    def sort_key(result: CandidateResult) -> tuple[float, float, float]:
        p95 = result.timing.get("latency_p95_seconds")
        return (
            float(result.estimated_cost_usd),
            -quality_score(result.quality),
            float(p95) if p95 is not None else math.inf,
        )

    return sorted(eligible, key=sort_key)
