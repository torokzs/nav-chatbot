import re

from fastapi import Depends, HTTPException, status

from app.config import Settings, get_settings
from app.models.schemas import ChatRequest

_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"override\s+(the\s+)?(system|developer)?\s*prompt", re.IGNORECASE),
    re.compile(
        r"(reveal|show|print)\s+(the\s+)?"
        r"(system prompt|developer message|hidden instructions)",
        re.IGNORECASE,
    ),
    re.compile(r"you\s+are\s+now\s+(an?|my)", re.IGNORECASE),
]


def is_prompt_injection(message: str) -> bool:
    return any(pattern.search(message) for pattern in _INJECTION_PATTERNS)


def validate_message_input(message: str, settings: Settings) -> str:
    normalized = message.strip()
    if len(normalized) > settings.max_input_length:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Input exceeds maximum length of {settings.max_input_length} characters.",
        )
    if is_prompt_injection(normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Potential prompt injection detected.",
        )
    return normalized


async def guard_chat_request(
    payload: ChatRequest,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> ChatRequest:
    payload.message = validate_message_input(payload.message, settings)
    return payload
