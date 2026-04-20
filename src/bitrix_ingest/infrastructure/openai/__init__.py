"""OpenAI API adapters."""
from .responses_client import OpenAiResponsesClient
from .transcription_client import OpenAiTranscriptionClient

__all__ = ["OpenAiTranscriptionClient", "OpenAiResponsesClient"]
