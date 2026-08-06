from collections.abc import AsyncGenerator

from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

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
        credential: AsyncTokenCredential | None = None,
        client: AsyncAzureOpenAI | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._credential = credential
        if client is not None:
            self._client = client
        else:
            self._credential = credential or DefaultAzureCredential()
            token_provider = get_bearer_token_provider(
                self._credential,
                "https://cognitiveservices.azure.com/.default",
            )
            self._client = AsyncAzureOpenAI(
                azure_endpoint=self._settings.azure_ai_foundry_endpoint,
                azure_ad_token_provider=token_provider,
                api_version="2024-10-21",
            )

    async def close(self) -> None:
        await self._client.close()
        if self._credential is not None:
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

        stream = await self._client.chat.completions.create(
            model=self._settings.azure_ai_foundry_chat_deployment,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            stream=True,
            temperature=0.2,
        )

        async for update in stream:
            for choice in update.choices:
                delta = choice.delta
                if delta.content:
                    yield delta.content
