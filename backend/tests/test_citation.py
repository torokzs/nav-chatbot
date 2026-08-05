from app.models.schemas import ChunkResult
from app.services.citation import format_citations, render_citation


def test_render_citation_formats_chunk_metadata() -> None:
    chunk = ChunkResult(
        content="Teszt tartalom",
        metadata={
            "adoev": 2024,
            "fuzet_szam": "12",
            "fuzet_cim": "Áfa tudnivalók",
            "page_from": 4,
            "page_to": 5,
            "breadcrumb": "Adózás > ÁFA",
        },
        score=0.9,
    )

    assert render_citation(chunk) == "[12 – Áfa tudnivalók, 4-5. oldal]"



def test_format_citations_deduplicates_sources() -> None:
    chunks = [
        ChunkResult(
            content="Első rész",
            metadata={
                "adoev": 2024,
                "fuzet_szam": "12",
                "fuzet_cim": "Áfa tudnivalók",
                "page_from": 4,
                "page_to": 5,
                "breadcrumb": "Adózás > ÁFA",
            },
            score=0.9,
        ),
        ChunkResult(
            content="Második rész",
            metadata={
                "adoev": 2024,
                "fuzet_szam": "12",
                "fuzet_cim": "Áfa tudnivalók",
                "page_from": 4,
                "page_to": 5,
                "breadcrumb": "Adózás > ÁFA",
            },
            score=0.8,
        ),
    ]

    citations = format_citations(chunks)

    assert len(citations) == 1
    assert citations[0].fuzet_szam == "12"
    assert citations[0].page_from == 4
    assert citations[0].page_to == 5
    assert citations[0].adoev == 2024
    assert citations[0].url == "/api/documents/2024/12/pdf"


def test_format_citations_returns_top_three_ranked_sources() -> None:
    chunks = [
        ChunkResult(
            content=f"Rész {index}",
            metadata={
                "adoev": 2024,
                "fuzet_szam": str(index),
                "fuzet_cim": f"Füzet {index}",
                "page_from": index,
                "page_to": index,
                "breadcrumb": f"Fejezet {index}",
            },
            score=score,
        )
        for index, score in [(1, 0.3), (2, 0.9), (3, 0.7), (4, 0.8)]
    ]

    citations = format_citations(chunks)

    assert [citation.fuzet_szam for citation in citations] == ["2", "4", "3"]
