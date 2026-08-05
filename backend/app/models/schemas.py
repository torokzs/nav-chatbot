from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

SUPPORTED_TAX_YEARS = tuple(range(2021, 2027))
TaxYear: TypeAlias = Literal[2021, 2022, 2023, 2024, 2025, 2026]


class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    message: str = Field(min_length=1)
    adoev: TaxYear
    conversation_id: str | None = None
    include_evaluation_context: bool = False

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be empty")
        return value


class ChatSource(BaseModel):
    adoev: TaxYear
    fuzet_szam: str
    fuzet_cim: str
    page_from: int
    page_to: int
    breadcrumb: str
    url: str | None = None


class ChatEvent(BaseModel):
    type: Literal["token", "context", "sources", "done", "error"]
    content: Any = None


class ChunkResult(BaseModel):
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float


class DocumentResult(BaseModel):
    adoev: TaxYear
    fuzet_szam: str
    fuzet_cim: str
    score: float
