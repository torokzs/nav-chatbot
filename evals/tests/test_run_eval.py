from __future__ import annotations

from pathlib import Path

from evals.run_eval import CitationCorrectnessEvaluator, load_dataset


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
