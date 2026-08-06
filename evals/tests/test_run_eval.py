from __future__ import annotations

import json
from pathlib import Path

import httpx

from evals.run_eval import (
    CitationCorrectnessEvaluator,
    call_chat_endpoint,
    evaluation_context_to_sources,
    load_dataset,
)


def test_citation_correctness_requires_citation_in_response_and_sources() -> None:
    evaluator = CitationCorrectnessEvaluator()
    sources = [
        {
            "fuzet_szam": "1",
            "page_from": 10,
            "page_to": 10,
        }
    ]

    missing = evaluator(
        response="A bevallás határideje május 20.",
        expected_fuzet="01",
        expected_page=10,
        sources=sources,
    )
    cited = evaluator(
        response="A bevallás határideje május 20. [1 - SZJA, 10. oldal]",
        expected_fuzet="01",
        expected_page=10,
        sources=sources,
    )
    range_cited = evaluator(
        response="A szabály alkalmazható. [001 – SZJA, 8-12., 15. oldal]",
        expected_fuzet="01",
        expected_page=10,
        sources=sources,
    )

    assert missing["citation_correctness"] == 0.0
    assert cited["citation_correctness"] == 1.0
    assert range_cited["citation_correctness"] == 1.0


def test_evaluation_datasets_are_explicitly_2026_only() -> None:
    eval_dir = Path(__file__).parents[1]

    for dataset_name, expected_rows in (
        ("qa.jsonl", 100),
        ("qa.smoke.jsonl", 1),
    ):
        rows = load_dataset(eval_dir / dataset_name)
        assert len(rows) == expected_rows
        assert {
            (row["adoev"], row["ground_truth_tax_year"])
            for row in rows
        } == {(2026, 2026)}
        assert {
            row["ground_truth_source"]
            for row in rows
        } == {"azure_search_2026_index"}


def test_call_chat_endpoint_sends_frozen_context_and_token() -> None:
    frozen_context = [{"content": "2026-os szabály", "score": 0.91}]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["evaluation_context"] == frozen_context
        assert request.headers["x-benchmark-context-token"] == "secret"
        return httpx.Response(
            200,
            text=(
                'data: {"type":"token","content":"Válasz"}\n\n'
                'data: {"type":"context","content":[{"content":"2026-os szabály","score":0.91}]}\n\n'
                'data: {"type":"sources","content":[]}\n\n'
                'data: {"type":"done"}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = call_chat_endpoint(
            client,
            "https://benchmark.example",
            "Kérdés?",
            2026,
            evaluation_context=frozen_context,
            benchmark_token="secret",
        )

    assert result.response == "Válasz"
    assert result.evaluation_context == frozen_context


def test_call_chat_endpoint_retries_transient_403(monkeypatch) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(403)
        return httpx.Response(
            200,
            text='data: {"type":"done"}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr("evals.run_eval.time.sleep", lambda _seconds: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        call_chat_endpoint(
            client,
            "https://benchmark.example",
            "Kérdés?",
            2026,
            retrieval_only=True,
        )

    assert attempts == 2


def test_evaluation_context_sources_include_chunks_beyond_ui_limit() -> None:
    sources = evaluation_context_to_sources(
        [
            {
                "content": "Szabály",
                "metadata": {
                    "fuzet_szam": "012",
                    "page_from": 8,
                    "page_to": 8,
                },
            }
        ],
        [{"fuzet_szam": "005", "page_from": 32, "page_to": 32}],
    )

    assert sources == [
        {
            "fuzet_szam": "012",
            "page_from": 8,
            "page_to": 8,
        }
    ]
