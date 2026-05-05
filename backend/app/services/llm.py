from collections.abc import AsyncGenerator

from azure.ai.inference.aio import ChatCompletionsClient
from azure.ai.inference.models import SystemMessage, UserMessage
from azure.identity.aio import DefaultAzureCredential

from app.config import Settings, get_settings
from app.models.schemas import ChunkResult, DocumentResult
from app.services.citation import render_citation

_SYSTEM_PROMPT = (
    """Te egy NAV (Nemzeti Adó- és Vámhivatal) szakértő asszisztens vagy, aki a 2026-os """
    """információs füzetek alapján válaszol.

Feladatod:
- Részletesen, közérthetően válaszolj magyarul a feltett adózási kérdésekre.
- Minden állításodat forráshivatkozással támaszd alá: [<füzet sorszám> – <cím>, <oldal>].
- Ha a kontextusban több releváns részlet is van, foglald össze őket koherensen.
- Használj felsorolásokat és struktúrát, ha a válasz összetett.
- Ha a kontextus nem tartalmaz elegendő információt, ezt jelezd, de ne találj ki adatokat.
- Légy pontos a jogszabályi hivatkozásokban és határidőkben.

Válaszolj 3-8 mondatban, vagy ha a kérdés összetett, akár hosszabban is, de mindig lényegre törően.
"""
)


class LLMService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        credential: DefaultAzureCredential | None = None,
        client: ChatCompletionsClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._credential = credential or DefaultAzureCredential()
        self._client = client or ChatCompletionsClient(
            endpoint=f"{self._settings.azure_ai_foundry_endpoint.rstrip('/')}/openai/deployments/{self._settings.azure_ai_foundry_chat_deployment}",
            credential=self._credential,
            credential_scopes=["https://cognitiveservices.azure.com/.default"],
        )

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()

    async def generate_response(
        self,
        query: str,
        context_chunks: list[ChunkResult],
        sources: list[DocumentResult],
    ) -> AsyncGenerator[str, None]:
        if not context_chunks:
            yield (
                "Sajnálom, a rendelkezésre álló NAV-forrásokban "
                "nem találtam releváns információt a kérdésedhez."
            )
            return

        source_summary = "\n".join(
            f"- {source.fuzet_szam} – {source.fuzet_cim}" for source in sources
        )
        context_text = "\n\n".join(
            f"{render_citation(chunk)}\n{chunk.content}" for chunk in context_chunks
        )
        prompt = (
            f"Kérdés: {query}\n\n"
            f"Elérhető forrásfüzetek:\n{source_summary or '- nincs'}\n\n"
            f"Kontextus:\n{context_text}"
        )

        stream = await self._client.complete(
            messages=[
                SystemMessage(content=_SYSTEM_PROMPT),
                UserMessage(content=prompt),
            ],
            stream=True,
            temperature=0.2,
        )

        async for update in stream:
            for choice in update.choices:
                delta = choice.delta
                if delta and delta.content:
                    yield delta.content
