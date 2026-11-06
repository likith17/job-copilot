"""Model access for every AI step, behind one function: parse().

Each task (grade, tailor, sponsorship, structure) names a provider and a model
in config.yaml, so a task can move between Claude, a local Ollama model, or a
free hosted API without touching pipeline code:

    llm:
      providers:
        anthropic: {type: anthropic}
        local:     {type: openai, base_url: http://localhost:11434/v1}
        groq:      {type: openai, base_url: https://api.groq.com/openai/v1, api_key_env: GROQ_API_KEY}
      tasks:
        grade:  {provider: local, model: "qwen3:8b"}
        tailor: {provider: anthropic, model: claude-opus-5-5, effort: medium}

"openai" means any OpenAI-compatible chat endpoint (Ollama, Groq, Gemini's
compatibility endpoint, OpenRouter, LM Studio, vLLM).
"""
import copy
import json
import os
import re
import threading
import warnings
from types import SimpleNamespace

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv()

DEFAULT_PROVIDERS = {
    "anthropic": {"type": "anthropic"},
    "local": {"type": "openai", "base_url": "http://localhost:11434/v1", "api_key": "ollama"},
}


class LLMError(Exception):
    """One request failed; other jobs can still go ahead."""


class LLMUnavailable(LLMError):
    """The provider can't serve any request (no credit, bad key, not running,
    quota spent). The run stops calling it but still saves and exports."""


# ---------------- configuration ----------------

def task_settings(cfg, task):
    """Resolve {provider settings + model + effort} for a task name. A task may
    name a `fallback` ({provider, model, ...}) used when its own model can't serve."""
    llm_cfg = cfg.llm or {}
    providers = {**DEFAULT_PROVIDERS, **(llm_cfg.get("providers") or {})}

    def resolve(spec, where):
        spec = dict(spec or {})
        name = spec.get("provider", "anthropic")
        if name not in providers:
            raise LLMUnavailable(f"settings: {where} uses unknown provider '{name}'")
        return {**providers[name], "provider": name, **spec}

    task_cfg = dict((llm_cfg.get("tasks") or {}).get(task) or {})
    fallback = task_cfg.pop("fallback", None)
    settings = resolve(task_cfg, f"task '{task}'")
    if fallback:
        settings["fallback"] = resolve(fallback, f"task '{task}' fallback")
    return settings


class _Ping(BaseModel):
    ok: bool


_preflight_ok = {}   # (provider, model) -> time of the last successful check
PREFLIGHT_TTL = 30 * 60


def preflight(settings, say=print):
    """Check that a task's model can serve; switch to its fallback if not.
    Returns the settings to use. One tiny call instead of failing job by job,
    and only every 30 minutes: a passing check is remembered."""
    import time
    key = (settings.get("provider"), settings.get("model"))
    if time.time() - _preflight_ok.get(key, 0) < PREFLIGHT_TTL:
        return settings
    try:
        parse({**settings, "effort": "low"}, [{"type": "text", "text": "Reply with ok=true."}], "ping",
              _Ping, max_tokens=1000)
        _preflight_ok[key] = time.time()
        return settings
    except LLMUnavailable as exc:
        fb = settings.get("fallback")
        if not fb:
            raise
        say(f"Writer {settings['model']} unavailable ({exc}); using fallback {fb['model']}.")
        return {**fb, "fell_back_from": settings["model"]}
    except LLMError:
        return settings   # a one-off bad ping isn't an outage


# ---------------- clients ----------------

_clients = {}
_lock = threading.Lock()


def _client(settings):
    key = (settings["type"], settings.get("base_url"), settings.get("api_key_env"))
    with _lock:
        if key not in _clients:
            if settings["type"] == "anthropic":
                import anthropic
                _clients[key] = anthropic.Anthropic()
            else:
                import openai
                api_key = (os.getenv(settings["api_key_env"]) if settings.get("api_key_env")
                           else settings.get("api_key", "none"))
                if not api_key:
                    raise LLMUnavailable(f"{settings['provider']}: set {settings['api_key_env']} in .env")
                _clients[key] = openai.OpenAI(base_url=settings["base_url"], api_key=api_key,
                                              timeout=settings.get("timeout", 600), max_retries=2)
        return _clients[key]


RATE_LIMIT_WAITS = (20, 60, 120, 300)     # seconds; free tiers recover within minutes


class RateLimited(LLMUnavailable):
    """The provider said slow down. Waited out in parse(); only a limit that
    outlasts every wait stops the run."""


def parse(settings, system, user, schema, max_tokens=16000):
    """One structured-output call. `system` is a list of text blocks (Anthropic
    form, so callers control cache breakpoints). Returns (parsed, usage)."""
    import time
    call = _parse_anthropic if settings["type"] == "anthropic" else _parse_openai
    for wait in (*RATE_LIMIT_WAITS, None):
        try:
            return call(settings, system, user, schema, max_tokens)
        except RateLimited:
            if wait is None:
                raise
            time.sleep(wait)


# ---------------- Anthropic ----------------

def _parse_anthropic(settings, system, user, schema, max_tokens):
    import anthropic

    try:
        with warnings.catch_warnings():
            # parse() needs output_format to accept a Pydantic type; the SDK
            # merges it into output_config.format, so the warning is cosmetic.
            warnings.simplefilter("ignore", DeprecationWarning)
            response = _client(settings).beta.messages.parse(
                model=settings["model"],
                max_tokens=max_tokens,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": settings.get("effort", "medium")},
                output_format=schema,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise LLMUnavailable(f"Anthropic rejected the API key: {exc.message}") from exc
    except anthropic.BadRequestError as exc:
        if "credit balance" in str(exc).lower():
            raise LLMUnavailable("Anthropic credit balance is too low. Add credits, or switch "
                                 "the tasks in config.yaml to a free provider.") from exc
        raise LLMError(str(exc)) from exc
    except anthropic.APIConnectionError as exc:
        raise LLMUnavailable(f"Can't reach the Anthropic API: {exc}") from exc

    if response.stop_reason == "refusal":
        raise LLMError("model declined the request")
    if response.stop_reason == "max_tokens":
        raise LLMError("output was cut off at max_tokens")
    if response.parsed_output is None:
        raise LLMError("model returned no structured output")
    return response.parsed_output, response.usage


# ---------------- OpenAI-compatible (Ollama, Groq, Gemini, ...) ----------------

def _inline_refs(schema):
    """Replace $ref/$defs with inline copies; several endpoints reject refs."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _extract_json(text):
    text = FENCE.sub("", THINK.sub("", text or "").strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


def _parse_openai(settings, system, user, schema, max_tokens):
    import openai

    json_schema = _inline_refs(schema.model_json_schema())
    system_text = "\n\n".join(b["text"] for b in system)
    # Smaller models follow a schema better when they can also read it.
    system_text += ("\n\nRespond with one JSON object matching this JSON Schema, and nothing else:\n"
                    + json.dumps(json_schema))
    messages = [{"role": "system", "content": system_text}, {"role": "user", "content": user}]
    formats = [{"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": json_schema}},
               {"type": "json_object"}]

    client = _client(settings)
    last_error = None
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=settings["model"], messages=messages, response_format=formats[0],
                max_tokens=max_tokens, temperature=settings.get("temperature", 0.2),
                # Provider-specific options from config, e.g. {reasoning_effort: none}
                extra_body=settings.get("extra_body"),
            )
        except openai.APIConnectionError as exc:
            raise LLMUnavailable(f"Can't reach {settings['provider']} at {settings['base_url']}. "
                                 f"If it's Ollama, is it running (`ollama serve`)?") from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMUnavailable(f"{settings['provider']} rejected the API key") from exc
        except openai.RateLimitError as exc:
            raise RateLimited(f"{settings['provider']} rate limit or daily quota reached") from exc
        except openai.NotFoundError as exc:
            raise LLMUnavailable(f"{settings['provider']} has no model '{settings['model']}'"
                                 + (f" (run `ollama pull {settings['model']}`)"
                                    if "11434" in settings.get("base_url", "") else "")) from exc
        except openai.BadRequestError as exc:
            if len(formats) > 1:   # endpoint doesn't take json_schema: fall back to JSON mode
                formats.pop(0)
                continue
            raise LLMError(str(exc)) from exc
        except openai.APIStatusError as exc:
            if exc.status_code == 402:   # e.g. Ollama cloud model outside the free plan
                raise LLMUnavailable(f"{settings['model']} needs a paid plan on {settings['provider']}") from exc
            raise LLMError(str(exc)) from exc

        choice = response.choices[0]
        content = choice.message.content or ""
        try:
            parsed = schema.model_validate_json(_extract_json(content))
        except ValidationError as exc:
            last_error = exc
            if choice.finish_reason == "length":
                raise LLMError("output was cut off at max_tokens") from exc
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"That JSON was invalid: {exc.errors()[:3]}. "
                                                     "Reply with the corrected JSON object only."}]
            continue
        u = response.usage
        # Ollama silently drops the start of prompts longer than its context
        # window (default 4K). ~4 chars per token; well under that means cut.
        sent = sum(len(m["content"]) for m in messages) / 4
        if u and u.prompt_tokens and u.prompt_tokens < 0.6 * sent:
            raise LLMUnavailable(
                f"{settings['model']} read only {u.prompt_tokens} of ~{int(sent)} prompt tokens: its context "
                f"window is too small. Use a model built with a larger num_ctx (see ollama/*.Modelfile).")
        usage = SimpleNamespace(input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                                output_tokens=getattr(u, "completion_tokens", 0) or 0,
                                cache_read_input_tokens=0)
        return parsed, usage
    raise LLMError(f"no valid JSON after retries: {last_error}")


def min_fit(cfg):
    """Shortlist cutoff for fit grades. Models grade on different scales (one
    may run ~18 points more generous than another on the same jobs), so the
    grade task can carry its own calibrated cutoff; scoring.min_fit is the default."""
    task = ((cfg.llm or {}).get("tasks") or {}).get("grade") or {}
    return task.get("min_fit", cfg.scoring.get("min_fit", 70))
