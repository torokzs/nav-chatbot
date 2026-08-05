from __future__ import annotations

import pytest

from evals.benchmark_core import (
    QUALITY_THRESHOLDS,
    CandidateResult,
    ModelCandidate,
    TokenPrice,
    apply_account_quota,
    build_shortlist,
    estimate_preflight_cost,
    normalize_retail_price,
    parse_available_models,
    percentile,
    quality_passes,
    rank_results,
)


def candidate(
    name: str,
    family: str,
    *,
    input_price: float = 1.0,
    output_price: float = 2.0,
) -> ModelCandidate:
    return ModelCandidate(
        name=name,
        version="2026-01-01",
        model_format="OpenAI" if family == "openai" else family.title(),
        sku="GlobalStandard",
        quota=100,
        family=family,
        price=TokenPrice(input_price, output_price, ("input", "output")),
    )


def test_shortlist_fills_required_family_slots_with_unique_models() -> None:
    models = [
        candidate("gpt-5", "openai", input_price=10),
        candidate("gpt-5-mini", "openai", input_price=1),
        candidate("gpt-5-nano", "openai", input_price=0.2),
        candidate("claude-opus-4", "claude", input_price=15),
        candidate("claude-sonnet-4", "claude", input_price=3),
        candidate("claude-haiku-4", "claude", input_price=0.5),
        candidate("mistral-small", "mistral"),
        candidate("deepseek-v3", "deepseek"),
    ]

    shortlist = build_shortlist(models)

    assert [slot.status for slot in shortlist] == ["selected"] * 8
    selected_names = [slot.candidate.name for slot in shortlist if slot.candidate]
    assert len(selected_names) == len(set(selected_names)) == 8
    assert shortlist[0].candidate and shortlist[0].candidate.name == "gpt-5"
    assert shortlist[1].candidate and shortlist[1].candidate.name == "claude-opus-4"
    assert shortlist[2].candidate and shortlist[2].candidate.name == "gpt-5-nano"


def test_frontier_slot_falls_back_to_highest_priority_known_price() -> None:
    unknown_frontier = candidate("gpt-5.6-sol", "openai")
    unknown_frontier.price = None
    known_frontier = candidate("gpt-5.2", "openai", input_price=1.75, output_price=14)
    newer_mini = candidate("gpt-5.4-mini", "openai", input_price=0.75, output_price=4.5)

    shortlist = build_shortlist([unknown_frontier, known_frontier, newer_mini])

    assert shortlist[0].candidate is not None
    assert shortlist[0].candidate.name == "gpt-5.2"


def test_shortlist_documents_unavailable_family_as_skipped() -> None:
    shortlist = build_shortlist([candidate("gpt-5", "openai")])

    skipped = {slot.slot: slot for slot in shortlist if slot.status == "skipped"}
    assert "frontier-claude" in skipped
    assert "mistral" in skipped
    assert "deepseek" in skipped
    assert skipped["mistral"].reason


def test_account_quota_caps_capacity_and_skips_exhausted_model() -> None:
    available = candidate("gpt-5-mini", "openai")
    available.deployment_capacity = 10
    exhausted = candidate("claude-haiku", "claude")
    exhausted.deployment_capacity = 10
    usage = [
        {
            "name": {
                "value": "OpenAI.GlobalStandard.gpt-5-mini",
                "localizedValue": "Global gpt-5-mini quota",
            },
            "currentValue": 7,
            "limit": 12,
        },
        {
            "name": {
                "value": "Anthropic.GlobalStandard.claude-haiku",
                "localizedValue": "Global claude-haiku quota",
            },
            "currentValue": 5,
            "limit": 5,
        },
    ]

    apply_account_quota([available, exhausted], usage)
    shortlist = build_shortlist([available, exhausted])

    assert available.deployment_capacity == 5
    assert exhausted.deployment_capacity == 0
    assert shortlist[1].status == "skipped"


def test_shortlist_falls_back_to_sku_with_remaining_quota() -> None:
    raw_models = [
        {
            "model": {
                "name": "gpt-5-mini",
                "version": "2026-01-01",
                "format": "OpenAI",
            },
            "skus": [
                {
                    "name": "GlobalStandard",
                    "capacity": {"minimum": 1, "default": 10, "maximum": 100, "step": 1},
                },
                {
                    "name": "DataZoneStandard",
                    "capacity": {"minimum": 1, "default": 10, "maximum": 100, "step": 1},
                },
            ],
        }
    ]
    models = parse_available_models(raw_models, requested_capacity=10)
    apply_account_quota(
        models,
        [
            {
                "name": {"value": "OpenAI.GlobalStandard.gpt-5-mini"},
                "currentValue": 100,
                "limit": 100,
            },
            {
                "name": {"value": "OpenAI.DataZoneStandard.gpt-5-mini"},
                "currentValue": 0,
                "limit": 100,
            },
        ],
    )

    shortlist = build_shortlist(models)

    assert len(models) == 2
    assert shortlist[0].candidate is not None
    assert shortlist[0].candidate.sku == "DataZoneStandard"
    assert shortlist[0].candidate.deployment_capacity == 10


def test_standard_quota_ignores_exhausted_global_quota() -> None:
    standard = candidate("gpt-5-mini", "openai")
    standard.sku = "Standard"
    standard.deployment_capacity = 10

    apply_account_quota(
        [standard],
        [
            {
                "name": {"value": "OpenAI.Standard.gpt-5-mini"},
                "currentValue": 0,
                "limit": 100,
            },
            {
                "name": {"value": "OpenAI.GlobalStandard.gpt-5-mini"},
                "currentValue": 100,
                "limit": 100,
            },
        ],
    )

    assert standard.deployment_capacity == 10
    assert standard.remaining_quota == 100


def test_retail_price_normalizes_per_million_tokens() -> None:
    items = [
        {
            "productName": "Azure OpenAI GPT 5 mini Global",
            "skuName": "GPT 5 mini Global",
            "meterName": "GPT 5 mini Global Input Tokens",
            "armRegionName": "global",
            "unitOfMeasure": "1K Tokens",
            "retailPrice": 0.00025,
        },
        {
            "productName": "Azure OpenAI GPT 5 mini Global",
            "skuName": "GPT 5 mini Global",
            "meterName": "GPT 5 mini Global Output Tokens",
            "armRegionName": "global",
            "unitOfMeasure": "1M Tokens",
            "retailPrice": 2.0,
        },
    ]

    price = normalize_retail_price(items, "gpt-5-mini", "GlobalStandard", "swedencentral")

    assert price is not None
    assert price.input_per_million_usd == pytest.approx(0.25)
    assert price.output_per_million_usd == pytest.approx(2.0)


def test_retail_price_is_unknown_when_meters_are_ambiguous() -> None:
    items = [
        {
            "productName": "Claude Haiku Global",
            "meterName": "Claude Haiku Global Input Tokens",
            "armRegionName": "global",
            "unitOfMeasure": "1M Tokens",
            "retailPrice": value,
        }
        for value in (0.8, 1.0)
    ]
    items.append(
        {
            "productName": "Claude Haiku Global",
            "meterName": "Claude Haiku Global Output Tokens",
            "armRegionName": "global",
            "unitOfMeasure": "1M Tokens",
            "retailPrice": 4.0,
        }
    )

    assert normalize_retail_price(items, "claude-haiku", "GlobalStandard", "swedencentral") is None


def test_retail_price_does_not_mix_base_and_mini_model_meters() -> None:
    items = []
    for model, input_price, output_price in (
        ("GPT 5", 1.25, 10.0),
        ("GPT 5 mini", 0.25, 2.0),
    ):
        items.extend(
            [
                {
                    "productName": f"Azure OpenAI {model} Global",
                    "meterName": f"{model} Global Input Tokens",
                    "armRegionName": "global",
                    "unitOfMeasure": "1M Tokens",
                    "retailPrice": input_price,
                },
                {
                    "productName": f"Azure OpenAI {model} Global",
                    "meterName": f"{model} Global Output Tokens",
                    "armRegionName": "global",
                    "unitOfMeasure": "1M Tokens",
                    "retailPrice": output_price,
                },
            ]
        )
    items.extend(
        [
            {
                "productName": "Unrelated model",
                "meterName": "MM3.5 Inp glbl Tokens",
                "armRegionName": "global",
                "unitOfMeasure": "1M Tokens",
                "retailPrice": 99.0,
            },
            {
                "productName": "Unrelated model",
                "meterName": "MM3.5 Outp glbl Tokens",
                "armRegionName": "global",
                "unitOfMeasure": "1M Tokens",
                "retailPrice": 99.0,
            },
            {
                "productName": "Azure OpenAI GPT5",
                "meterName": "GPT 5 pro inp glbl Tokens",
                "armRegionName": "global",
                "unitOfMeasure": "1M Tokens",
                "retailPrice": 15.0,
            },
            {
                "productName": "Azure OpenAI GPT5",
                "meterName": "GPT 5 pro out glbl Tokens",
                "armRegionName": "global",
                "unitOfMeasure": "1M Tokens",
                "retailPrice": 120.0,
            },
        ]
    )

    base = normalize_retail_price(items, "gpt-5", "GlobalStandard", "swedencentral")

    assert base is not None
    assert base.input_per_million_usd == pytest.approx(1.25)
    assert base.output_per_million_usd == pytest.approx(10.0)


def test_retail_price_excludes_deep_research_variant() -> None:
    items = [
        {
            "productName": "Azure OpenAI",
            "meterName": meter,
            "armRegionName": "swedencentral",
            "unitOfMeasure": "1M Tokens",
            "retailPrice": price,
        }
        for meter, price in (
            ("o3 Inpt DZone 1M Tokens", 2.0),
            ("o3 outpt DZone 1M Tokens", 8.0),
            ("o3 deep research Inpt DZone 1M Tokens", 11.0),
            ("o3 deep research outpt DZone 1M Tokens", 44.0),
        )
    ]

    price = normalize_retail_price(items, "o3", "DataZoneStandard", "swedencentral")

    assert price is not None
    assert price.input_per_million_usd == pytest.approx(2.0)
    assert price.output_per_million_usd == pytest.approx(8.0)


def test_retail_price_matches_versioned_deepseek_alias() -> None:
    items = [
        {
            "productName": "Azure Fireworks Models",
            "meterName": f"FW DeepSeek V3 {direction} DZ Tokens",
            "armRegionName": "swedencentral",
            "unitOfMeasure": "1K",
            "retailPrice": value,
        }
        for direction, value in (("Inp", 0.0006), ("Outp", 0.0018))
    ]

    price = normalize_retail_price(
        items,
        "DeepSeek-V3-0324",
        "DataZoneStandard",
        "swedencentral",
    )

    assert price is not None
    assert price.input_per_million_usd == pytest.approx(0.6)
    assert price.output_per_million_usd == pytest.approx(1.8)


def test_percentile_uses_linear_interpolation() -> None:
    values = [1.0, 2.0, 3.0, 4.0]

    assert percentile(values, 50) == pytest.approx(2.5)
    assert percentile(values, 95) == pytest.approx(3.85)
    assert percentile([], 95) is None


def test_all_quality_thresholds_are_mandatory() -> None:
    passing = dict(QUALITY_THRESHOLDS)
    assert quality_passes(passing)

    for metric in QUALITY_THRESHOLDS:
        failing = dict(passing)
        failing[metric] = QUALITY_THRESHOLDS[metric] - 0.01
        assert not quality_passes(failing)


def test_ranking_uses_cost_then_quality_then_p95() -> None:
    base_quality = {
        "groundedness": 4.2,
        "relevance": 4.2,
        "fluency": 4.2,
        "citation_correctness": 0.9,
    }
    expensive = CandidateResult(
        slot="a",
        model="expensive",
        status="completed",
        quality=base_quality,
        timing={"latency_p95_seconds": 1.0},
        estimated_cost_usd=3.0,
    )
    slower = CandidateResult(
        slot="b",
        model="slower",
        status="completed",
        quality=base_quality,
        timing={"latency_p95_seconds": 3.0},
        estimated_cost_usd=1.0,
    )
    higher_quality = CandidateResult(
        slot="c",
        model="higher-quality",
        status="completed",
        quality={**base_quality, "groundedness": 4.8},
        timing={"latency_p95_seconds": 4.0},
        estimated_cost_usd=1.0,
    )

    ranked = rank_results([expensive, slower, higher_quality])

    assert [result.model for result in ranked] == ["higher-quality", "slower", "expensive"]


def test_partial_or_failed_candidates_are_not_recommended() -> None:
    quality = {
        "groundedness": 5.0,
        "relevance": 5.0,
        "fluency": 5.0,
        "citation_correctness": 1.0,
    }
    partial = CandidateResult(
        slot="a",
        model="partial",
        status="partial_failure",
        quality=quality,
        estimated_cost_usd=0.1,
    )
    failed = CandidateResult(
        slot="b",
        model="failed",
        status="failed",
        quality=quality,
        estimated_cost_usd=0.1,
    )

    assert rank_results([partial, failed]) == []


def test_preflight_fails_closed_for_unknown_price() -> None:
    known = candidate("gpt-5-mini", "openai")
    unknown = candidate("claude-haiku", "claude")
    unknown.price = None
    judge = TokenPrice(2.0, 8.0, ("judge input", "judge output"))

    estimate, unknown_prices = estimate_preflight_cost(
        [known, unknown],
        judge,
        questions=100,
        input_tokens_per_question=8_000,
        output_tokens_per_question=1_000,
        judge_input_tokens_per_evaluation=10_000,
        judge_output_tokens_per_evaluation=500,
    )

    assert estimate is None
    assert unknown.identifier in unknown_prices


def test_preflight_includes_judge_calls_for_every_candidate() -> None:
    models = [candidate("gpt-5-mini", "openai"), candidate("claude-haiku", "claude")]
    judge = TokenPrice(2.0, 8.0, ("judge input", "judge output"))

    estimate, unknown = estimate_preflight_cost(
        models,
        judge,
        questions=100,
        input_tokens_per_question=1_000,
        output_tokens_per_question=100,
        judge_input_tokens_per_evaluation=2_000,
        judge_output_tokens_per_evaluation=200,
    )

    expected_generation = 2 * 100 * models[0].price.estimate(1_000, 100)
    expected_judge = 2 * 100 * 3 * judge.estimate(2_000, 200)
    assert unknown == []
    assert estimate == pytest.approx(expected_generation + expected_judge)
