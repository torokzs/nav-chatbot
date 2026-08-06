from __future__ import annotations

from evals.run_eval import CitationCorrectnessEvaluator


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
