import pytest

from app.config import Settings
from app.models.schemas import ChunkResult, DocumentResult
from app.services.retrieval import RetrievalService


@pytest.mark.asyncio
async def test_retrieve_merges_global_chunks_and_updates_documents(
    mock_settings: Settings,
) -> None:
    service = object.__new__(RetrievalService)

    async def fake_retrieve_documents(queries: list[str], adoev: int) -> list[DocumentResult]:
        assert queries == ["TAO túlfizetés", "tao befizetés visszaigénylés"]
        assert adoev == 2024
        return [
            DocumentResult(
                adoev=2024,
                fuzet_szam="12",
                fuzet_cim="Általános tájékoztató",
                score=0.61,
            )
        ]

    chunk_calls: list[tuple[list[str], list[str], int]] = []
    filtered_duplicate = ChunkResult(
        content="Duplikált rész",
        metadata={
            "adoev": 2024,
            "fuzet_szam": "12",
            "fuzet_cim": "Általános tájékoztató",
            "page_from": 3,
            "page_to": 3,
            "breadcrumb": "Eljárás",
        },
        score=0.4,
    )
    global_duplicate = ChunkResult(
        content="Duplikált rész",
        metadata={
            "adoev": 2024,
            "fuzet_szam": "12",
            "fuzet_cim": "Általános tájékoztató",
            "page_from": 3,
            "page_to": 3,
            "breadcrumb": "Eljárás",
        },
        score=0.65,
    )
    global_hit = ChunkResult(
        content="A tao túlfizetés szabálya a 55-ös füzetben szerepel.",
        metadata={
            "adoev": 2024,
            "fuzet_szam": "55",
            "fuzet_cim": "Tao-felajánlás",
            "page_from": 8,
            "page_to": 8,
            "breadcrumb": "Túlfizetés",
        },
        score=0.92,
    )

    async def fake_retrieve_chunks(
        queries: list[str],
        booklet_ids: list[str],
        adoev: int,
        *,
        top_k: int = 8,
    ) -> list[ChunkResult]:
        assert adoev == 2024
        chunk_calls.append((queries, booklet_ids, top_k))
        if booklet_ids:
            return [filtered_duplicate]
        return [global_hit, global_duplicate]

    service._retrieve_documents = fake_retrieve_documents  # type: ignore[method-assign]
    service._retrieve_chunks = fake_retrieve_chunks  # type: ignore[method-assign]

    chunks, documents = await service.retrieve(
        "TAO túlfizetés",
        ["tao befizetés visszaigénylés", "TAO túlfizetés"],
        2024,
    )

    assert chunk_calls == [
        (["TAO túlfizetés", "tao befizetés visszaigénylés"], ["12"], 8),
        (["TAO túlfizetés", "tao befizetés visszaigénylés"], [], 5),
    ]
    assert [chunk.metadata.get("fuzet_szam") for chunk in chunks] == ["55", "12"]
    assert chunks[1].score == 0.65
    assert [document.fuzet_szam for document in documents] == ["55", "12"]
    assert documents[0].fuzet_cim == "Tao-felajánlás"
