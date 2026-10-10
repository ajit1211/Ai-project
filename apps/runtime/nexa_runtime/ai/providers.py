"""Which provider adapters this process runs. One client per provider, for the process's life.

A provider is registered only when its API key is configured. The mock provider is
registered only in development and test (`RuntimeSettings.mock_provider_enabled`, which
refuses to be switched on anywhere else), so mock targets in the catalog are skipped in
staging and production and its control phrases are never live for real users.
"""

from nexa_ai.providers.anthropic import AnthropicProvider
from nexa_ai.providers.base import AIProvider, Timeouts
from nexa_ai.providers.mock import MockProvider
from nexa_ai.providers.openai import OpenAIProvider
from nexa_common.logs import get_logger
from nexa_runtime.config import RuntimeSettings

log = get_logger("nexa.runtime.ai.providers")


def timeouts(settings: RuntimeSettings) -> Timeouts:
    return Timeouts(
        connect=settings.ai_timeout_connect,
        request=settings.ai_timeout_request,
        first_token=settings.ai_timeout_first_token,
        idle=settings.ai_timeout_idle,
        total=settings.ai_timeout_total,
    )


def build_providers(settings: RuntimeSettings) -> dict[str, AIProvider]:
    providers: dict[str, AIProvider] = {}
    limits = timeouts(settings)
    if settings.ai_anthropic_api_key is not None:
        providers["anthropic"] = AnthropicProvider(
            api_key=settings.ai_anthropic_api_key.get_secret_value(),
            base_url=settings.ai_anthropic_base_url,
            timeouts=limits,
        )
    if settings.ai_openai_api_key is not None:
        providers["openai"] = OpenAIProvider(
            api_key=settings.ai_openai_api_key.get_secret_value(),
            base_url=settings.ai_openai_base_url,
            timeouts=limits,
        )
    if settings.mock_provider_enabled:
        providers["mock"] = MockProvider(control_phrases=True)
    log.info("ai.providers.registered", providers=sorted(providers))
    if not providers:
        log.warning("ai.providers.none", hint="set AI_ANTHROPIC_API_KEY or AI_OPENAI_API_KEY")
    return providers
