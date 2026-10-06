import time
from openai import OpenAI, RateLimitError, APITimeoutError, APIError
from analyst.config import OPENROUTER_API_KEY, NVIDIA_API_KEY, LLM_PROVIDER, MODELS

# ---------- provider setup ----------

_PROVIDERS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": OPENROUTER_API_KEY,
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key": NVIDIA_API_KEY,
    },
}

_provider_cfg = _PROVIDERS.get(LLM_PROVIDER)
if not _provider_cfg:
    raise ValueError(f"Unknown LLM_PROVIDER '{LLM_PROVIDER}'. Use 'nvidia' or 'openrouter'.")

# Use a placeholder key during tests / when env is incomplete so imports don't crash.
# Real calls will fail with a clear auth error instead.
_api_key = _provider_cfg["api_key"] or "not-set"

client = OpenAI(
    base_url=_provider_cfg["base_url"],
    api_key=_api_key,
)

class PipelineError(Exception):
    pass

def call_llm(task: str, messages: list[dict], max_retries: int = 1) -> str:
    """Call the model configured for `task`, falling back down the list on failure."""
    cfg = MODELS[task]
    candidates = [cfg["primary"], *cfg.get("fallback", [])]
    last_err = None

    for model in candidates:
        for attempt in range(max_retries + 1):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=cfg.get("temp", 0),
                )
                return resp.choices[0].message.content
            except (RateLimitError, APITimeoutError, APIError) as e:
                last_err = e
                time.sleep(2 * (attempt + 1))   # simple backoff
    raise PipelineError(f"All models failed for task '{task}': {last_err}")