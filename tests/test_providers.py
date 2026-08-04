from __future__ import annotations

import pytest

from mega_nemo import providers
from mega_nemo.config import Settings
from mega_nemo.nemoclaw import NemoclawError, build_spec


@pytest.mark.parametrize(
    "token,expected_key,expected_name",
    [
        ("build", "build", "nvidia-prod"),
        ("cloud", "build", "nvidia-prod"),
        ("nvidia-prod", "build", "nvidia-prod"),
        ("BUILD", "build", "nvidia-prod"),
        ("openai", "openai", "openai-api"),
        ("openai-api", "openai", "openai-api"),
        ("open-router", "openrouter", "openrouter-api"),
        ("anthropicCompatible", "anthropicCompatible", "compatible-anthropic-endpoint"),
        ("nous", "hermesProvider", "hermes-provider"),
    ],
)
def test_provider_dialects_resolve(token: str, expected_key: str, expected_name: str) -> None:
    p = providers.resolve(token)
    assert (p.key, p.name) == (expected_key, expected_name)


def test_unknown_provider_lists_options() -> None:
    with pytest.raises(providers.UnknownProvider, match="build"):
        providers.resolve("definitely-not-a-provider")


@pytest.mark.parametrize("alias", ["dcode", "deepagents", "langchain", "nemo-deepagents"])
def test_dcode_aliases(alias: str) -> None:
    assert providers.resolve_agent(alias) == "langchain-deepagents-code"
    assert providers.is_dcode(alias)


def test_openclaw_is_not_dcode() -> None:
    assert not providers.is_dcode("openclaw")


def test_unknown_agent_rejected() -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        providers.resolve_agent("gpt-agent")


# --- build_spec -------------------------------------------------------------


def test_build_spec_defaults_model_from_provider() -> None:
    spec = build_spec(Settings(provider="build", model=None, agent="dcode"))
    assert spec.model == "nvidia/nemotron-3-super-120b-a12b"
    assert spec.agent == "langchain-deepagents-code"


def test_build_spec_requires_provider() -> None:
    with pytest.raises(NemoclawError, match="no provider"):
        build_spec(Settings(provider=None))


def test_build_spec_requires_endpoint_for_custom() -> None:
    with pytest.raises(NemoclawError, match="needs an explicit endpoint"):
        build_spec(Settings(provider="custom", model="my-model"))


def test_build_spec_accepts_endpoint_for_custom() -> None:
    spec = build_spec(Settings(provider="custom", model="m", endpoint_url="http://h:8000/v1"))
    assert spec.endpoint_url == "http://h:8000/v1"


def test_build_spec_rejects_bad_auto_approval() -> None:
    s = Settings(provider="build", dcode_auto_approval="maybe")
    with pytest.raises(NemoclawError, match="dcode_auto_approval"):
        build_spec(s)


def test_env_uses_installer_key_not_openshell_name() -> None:
    # NEMOCLAW_PROVIDER speaks installer keys; passing "nvidia-prod" there fails.
    env = build_spec(Settings(provider="nvidia-prod", model="m")).env()
    assert env["NEMOCLAW_PROVIDER"] == "build"
    assert env["NEMOCLAW_MODEL"] == "m"
    assert env["NEMOCLAW_NON_INTERACTIVE"] == "1"


def test_local_provider_adds_local_inference_policy() -> None:
    spec = build_spec(Settings(provider="ollama", model="llama3", endpoint_url="http://x/v1"))
    assert providers.LOCAL_INFERENCE_POLICY in spec.policies


def test_cloud_provider_omits_local_inference_policy() -> None:
    spec = build_spec(Settings(provider="build"))
    assert providers.LOCAL_INFERENCE_POLICY not in spec.policies
