from app.models.schemas import ChatSource, ChunkResult

_MAX_CITATIONS = 3


def render_citation(chunk: ChunkResult) -> str:
    metadata = chunk.metadata
    page_from = int(metadata.get("page_from", 0))
    page_to = int(metadata.get("page_to", page_from))
    page_label = f"{page_from}. oldal" if page_from == page_to else f"{page_from}-{page_to}. oldal"
    return (
        f"[{metadata.get('fuzet_szam', '')} – {metadata.get('fuzet_cim', '')}, {page_label}]"
    )


def format_citations(chunks: list[ChunkResult]) -> list[ChatSource]:
    ranked_sources: dict[tuple[str, str, int, int, str], tuple[ChatSource, float]] = {}
    for chunk in sorted(chunks, key=lambda item: item.score, reverse=True):
        metadata = chunk.metadata
        fuzet_szam = str(metadata.get("fuzet_szam", ""))
        source = ChatSource(
            fuzet_szam=fuzet_szam,
            fuzet_cim=str(metadata.get("fuzet_cim", "")),
            page_from=int(metadata.get("page_from", 0)),
            page_to=int(metadata.get("page_to", metadata.get("page_from", 0))),
            breadcrumb=str(metadata.get("breadcrumb", "")),
            url=f"/api/documents/{fuzet_szam}/pdf" if fuzet_szam else None,
        )
        key = (
            source.fuzet_szam,
            source.fuzet_cim,
            source.page_from,
            source.page_to,
            source.breadcrumb,
        )
        current = ranked_sources.get(key)
        if current is None or chunk.score > current[1]:
            ranked_sources[key] = (source, chunk.score)
    top_sources = sorted(ranked_sources.values(), key=lambda item: item[1], reverse=True)
    return [source for source, _ in top_sources[:_MAX_CITATIONS]]
