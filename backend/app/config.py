from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="../.env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        env_nested_delimiter="__",
    )

    azure_search_endpoint: str = Field(validation_alias="AZURE_SEARCH_ENDPOINT")
    azure_search_index_chunks: str = Field(
        default="nav-chunks", validation_alias="AZURE_SEARCH_INDEX_CHUNKS"
    )
    azure_search_index_documents: str = Field(
        default="nav-documents", validation_alias="AZURE_SEARCH_INDEX_DOCUMENTS"
    )
    azure_ai_foundry_endpoint: str = Field(validation_alias="AZURE_AI_FOUNDRY_ENDPOINT")
    azure_ai_foundry_chat_deployment: str = Field(
        default="model-router",
        validation_alias="AZURE_AI_FOUNDRY_CHAT_DEPLOYMENT",
    )
    azure_ai_foundry_embedding_deployment: str = Field(
        default="text-embedding-3-large",
        validation_alias="AZURE_AI_FOUNDRY_EMBEDDING_DEPLOYMENT",
    )
    azure_key_vault_url: str | None = Field(default=None, validation_alias="AZURE_KEY_VAULT_URL")
    applicationinsights_connection_string: str | None = Field(
        default=None,
        validation_alias="APPLICATIONINSIGHTS_CONNECTION_STRING",
    )
    rate_limit_rpm: int = Field(default=30, validation_alias="RATE_LIMIT_RPM")
    max_input_length: int = Field(default=2000, validation_alias="MAX_INPUT_LENGTH")
    frontend_origin: str = Field(
        default="http://localhost:3000",
        validation_alias="FRONTEND_ORIGIN",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
