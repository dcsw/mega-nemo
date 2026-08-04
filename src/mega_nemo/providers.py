"""Provider and model vocabulary.

NemoClaw speaks two dialects for the same thing: *installer keys* that
``nemoclaw onboard`` / ``NEMOCLAW_PROVIDER`` accept (``build``, ``openai``,
``gemini``, ...) and *OpenShell provider names* that end up in
``~/.nemoclaw/sandboxes.json`` and that ``nemoclaw inference set`` accepts
(``nvidia-prod``, ``openai-api``, ...).

mega-nemo accepts either dialect on the command line, normalizes to the
installer key when shelling out to ``onboard``, and reports the OpenShell name
when describing an existing sandbox.

Mirrored from NemoClaw v0.0.90:
  src/lib/onboard/providers.ts            (REMOTE_PROVIDER_CONFIG)
  src/lib/actions/inference-set.ts        (SUPPORTED_PROVIDER_NAMES, aliases)
  src/lib/onboard/inference-providers/types.ts (REMOTE_PROVIDER_NAMES)

If you bump NemoClaw and the vocabulary drifts, `mega doctor` flags it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    """One entry of NemoClaw's provider table."""

    key: str  # installer key: what NEMOCLAW_PROVIDER wants
    name: str  # OpenShell provider name: what sandboxes.json records
    label: str
    credential_env: str
    default_model: str | None = None
    #: ``catalog`` = pick from a remote list, ``curated`` = fixed list,
    #: ``input`` = you must supply an endpoint URL and model yourself.
    model_mode: str = "catalog"
    endpoint_required: bool = False
    local: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        key="build",
        name="nvidia-prod",
        label="NVIDIA Endpoints (build.nvidia.com)",
        credential_env="NVIDIA_INFERENCE_API_KEY",
        default_model="nvidia/nemotron-3-super-120b-a12b",
        model_mode="catalog",
        aliases=("cloud", "nvidia", "nvidia-prod", "nvidia-endpoints"),
    ),
    Provider(
        key="openrouter",
        name="openrouter-api",
        label="OpenRouter",
        credential_env="OPENROUTER_API_KEY",
        model_mode="catalog",
        aliases=("open-router", "openrouterai", "openrouter-api"),
    ),
    Provider(
        key="openai",
        name="openai-api",
        label="OpenAI",
        credential_env="OPENAI_API_KEY",
        default_model="gpt-5.4",
        model_mode="curated",
        aliases=("openai-api",),
    ),
    Provider(
        key="anthropic",
        name="anthropic-prod",
        label="Anthropic",
        credential_env="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-6",
        model_mode="curated",
        aliases=("anthropic-prod",),
    ),
    Provider(
        key="anthropicCompatible",
        name="compatible-anthropic-endpoint",
        label="Anthropic-compatible endpoint",
        credential_env="COMPATIBLE_ANTHROPIC_API_KEY",
        model_mode="input",
        endpoint_required=True,
        aliases=("anthropiccompatible", "compatible-anthropic-endpoint"),
    ),
    Provider(
        key="gemini",
        name="gemini-api",
        label="Google Gemini",
        credential_env="GEMINI_API_KEY",
        default_model="gemini-2.5-flash",
        model_mode="curated",
        aliases=("gemini-api", "google"),
    ),
    Provider(
        key="hermesProvider",
        name="hermes-provider",
        label="Hermes Provider (Nous portal)",
        credential_env="OPENAI_API_KEY",
        model_mode="curated",
        aliases=("hermes", "hermes-provider", "hermesprovider", "nous", "nous-portal"),
    ),
    Provider(
        key="custom",
        name="compatible-endpoint",
        label="OpenAI-compatible endpoint",
        credential_env="COMPATIBLE_API_KEY",
        model_mode="input",
        endpoint_required=True,
        aliases=("compatible-endpoint", "compatible"),
    ),
    Provider(
        key="ollama",
        name="ollama-local",
        label="Ollama (host-local)",
        credential_env="OLLAMA_API_KEY",
        model_mode="input",
        local=True,
        aliases=("ollama-local",),
    ),
    Provider(
        key="vllm",
        name="vllm-local",
        label="vLLM (host-local)",
        credential_env="COMPATIBLE_API_KEY",
        model_mode="input",
        local=True,
        aliases=("vllm-local",),
    ),
    Provider(
        key="nim-local",
        name="nvidia-nim",
        label="NVIDIA NIM (host-local container)",
        credential_env="NVIDIA_INFERENCE_API_KEY",
        model_mode="input",
        local=True,
        aliases=("nim", "nvidia-nim"),
    ),
)

_BY_TOKEN: dict[str, Provider] = {}
for _p in PROVIDERS:
    for _token in (_p.key, _p.name, *_p.aliases):
        _BY_TOKEN[_token.lower()] = _p


class UnknownProvider(ValueError):
    def __init__(self, token: str) -> None:
        options = ", ".join(sorted({p.key for p in PROVIDERS}))
        super().__init__(f"unknown provider {token!r}. Known providers: {options}")
        self.token = token


def resolve(token: str) -> Provider:
    """Accept an installer key, an OpenShell name, or a common alias."""
    try:
        return _BY_TOKEN[token.strip().lower()]
    except KeyError:
        raise UnknownProvider(token) from None


# ---------------------------------------------------------------------------
# Agent runtimes
# ---------------------------------------------------------------------------

#: `nemoclaw agents list` on v0.0.90. The value is what --agent wants.
AGENTS: dict[str, str] = {
    "openclaw": "openclaw",
    "hermes": "hermes",
    "nemohermes": "hermes",
    "langchain-deepagents-code": "langchain-deepagents-code",
    "nemo-deepagents": "langchain-deepagents-code",
    "dcode": "langchain-deepagents-code",
    "deepagents": "langchain-deepagents-code",
    "deepagents-code": "langchain-deepagents-code",
    "langchain": "langchain-deepagents-code",
}

DEFAULT_AGENT = "langchain-deepagents-code"

#: Agents that support --dcode-auto-approval / dcode session semantics.
DCODE_AGENTS = {"langchain-deepagents-code"}


def resolve_agent(token: str) -> str:
    key = token.strip().lower()
    if key not in AGENTS:
        options = ", ".join(sorted(set(AGENTS)))
        raise ValueError(f"unknown agent {token!r}. Known agents/aliases: {options}")
    return AGENTS[key]


def is_dcode(agent: str) -> bool:
    return resolve_agent(agent) in DCODE_AGENTS


# ---------------------------------------------------------------------------
# Policy presets `mega` applies to a sandbox by default.
# ---------------------------------------------------------------------------

#: keystone pulls a GitHub release; metaswarm/graphify pull npm + PyPI; the
#: triangle workflow needs github. These are NemoClaw policy preset names.
DEFAULT_POLICIES: tuple[str, ...] = ("github", "npm", "pypi")

#: Added when the provider runs on the host rather than in the cloud.
LOCAL_INFERENCE_POLICY = "local-inference"
