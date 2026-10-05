from __future__ import annotations

import logging
import os

from find_my_job.embeddings_local import HashEmbeddings
from find_my_job.models import AppConfig, LlmConfig

logger = logging.getLogger("find_my_job.llm")


def google_api_key() -> str | None:
    return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or None


def has_openai_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY"))


def openai_api_key() -> str | None:
    return os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY") or None


def has_cloud_key(config: AppConfig | LlmConfig | None = None) -> bool:
    provider = "google"
    if config is not None:
        llm = config.llm if isinstance(config, AppConfig) else config
        provider = (llm.provider or "google").lower()
    if provider in {"google", "gemini"}:
        return bool(google_api_key())
    if provider in {"openai", "openai_compatible"}:
        return bool(openai_api_key())
    return bool(google_api_key() or openai_api_key())


def resolve_llm_mode(config: AppConfig) -> str:
    """Return 'cloud' or 'offline'."""
    mode = (config.llm.mode or "auto").lower()
    provider = (config.llm.provider or "google").lower()

    if mode == "offline":
        return "offline"

    if mode in {"openai", "google", "cloud"}:
        # explicit “use API”
        if not has_cloud_key(config):
            hint = (
                "GOOGLE_API_KEY"
                if provider in {"google", "gemini"}
                else "OPENAI_API_KEY"
            )
            raise RuntimeError(
                f"llm.mode={mode} but {hint} is missing. "
                "Set the key, or set llm.mode: offline / auto."
            )
        return "cloud"

    # auto
    if has_cloud_key(config):
        return "cloud"
    logger.info(
        "No cloud API key — offline mode (local RAG + template letters). "
        "Set GOOGLE_API_KEY or OPENROUTER_API_KEY / OPENAI_API_KEY for cloud LLM."
    )
    return "offline"


def get_chat_model(config: AppConfig | LlmConfig, *, temperature: float = 0.2):
    llm = config.llm if isinstance(config, AppConfig) else config
    provider = (llm.provider or "google").lower()

    if provider in {"google", "gemini"}:
        from langchain_google_genai import ChatGoogleGenerativeAI

        key = google_api_key()
        if not key:
            raise RuntimeError(
                "GOOGLE_API_KEY (or GEMINI_API_KEY) is not set. "
                "Create one at https://aistudio.google.com/apikey"
            )
        return ChatGoogleGenerativeAI(
            model=llm.model,
            google_api_key=key,
            temperature=temperature,
        )

    if provider in {"openai", "openai_compatible"}:
        from langchain_openai import ChatOpenAI

        key = openai_api_key()
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY or OPENROUTER_API_KEY is not set "
                "(OpenRouter: https://openrouter.ai/keys)"
            )
        kwargs: dict = {
            "model": llm.model,
            "temperature": temperature,
            "api_key": key,
        }
        if llm.base_url:
            kwargs["base_url"] = llm.base_url
        return ChatOpenAI(**kwargs)

    raise ValueError(
        f"Unsupported llm.provider={provider!r}. "
        "Use 'google', 'openai', or 'openai_compatible'."
    )


def get_embeddings(config: AppConfig, *, force_local: bool = False):
    """Resume/JD similarity embeddings.

    Default is local HashEmbeddings — cloud embed APIs (Gemini free tier ~100
    RPM) cannot score hundreds of jobs per run.
    """
    mode = (config.llm.mode or "auto").lower()
    emb_pref = (config.rag.embeddings or "local").lower()
    provider = (config.llm.provider or "google").lower()

    if force_local or mode == "offline" or emb_pref == "local":
        return HashEmbeddings()

    if provider in {"google", "gemini"}:
        key = google_api_key()
        if not key:
            logger.warning("No GOOGLE_API_KEY — using local embeddings")
            return HashEmbeddings()
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        emb_model = config.rag.embedding_model or "gemini-embedding-001"
        legacy = {
            "text-embedding-004",
            "text-embedding-3-small",
            "text-embedding-3-large",
            "embedding-001",
            "models/text-embedding-004",
            "models/embedding-001",
        }
        if emb_model in legacy or emb_model.startswith("text-embedding-3"):
            emb_model = "gemini-embedding-001"
        if emb_model.startswith("models/"):
            emb_model = emb_model.removeprefix("models/")
        return GoogleGenerativeAIEmbeddings(
            model=emb_model,
            google_api_key=key,
        )

    if not has_openai_key():
        return HashEmbeddings()
    from langchain_openai import OpenAIEmbeddings

    kwargs: dict = {"model": config.rag.embedding_model}
    if config.llm.base_url:
        kwargs["base_url"] = config.llm.base_url
    return OpenAIEmbeddings(**kwargs)
