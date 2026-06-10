"""LLM client layer — any number of OpenAI-compatible endpoints, + streaming.

Clients are cached per (base_url, api_key). All callers may override the endpoint;
omitting it falls back to the local llama-swap defaults in config.
"""
from functools import lru_cache

from openai import OpenAI

import config


@lru_cache(maxsize=32)
def client(base_url, api_key):
    return OpenAI(base_url=base_url, api_key=api_key or "sk-no-key-needed")


def _c(base_url, api_key):
    return client(base_url or config.LLM_BASE_URL, api_key or config.LLM_API_KEY)


def chat(messages, tools=None, model=None, temperature=0.2, base_url=None, api_key=None,
         return_usage=False):
    """One completion. Returns the raw `message` (text content OR tool_calls).
    With return_usage=True, returns (message, completion_tokens)."""
    resp = _c(base_url, api_key).chat.completions.create(
        model=model or config.MODEL,
        messages=messages,
        tools=tools or None,
        tool_choice="auto" if tools else None,
        temperature=temperature,
    )
    msg = resp.choices[0].message
    if return_usage:
        return msg, (resp.usage.completion_tokens if getattr(resp, "usage", None) else 0)
    return msg


def stream(messages, tools=None, model=None, temperature=0.2, base_url=None, api_key=None):
    """Stream one completion. Yields, in arrival order:
        {'reasoning': txt}   model thinking (reasoning_content), if any
        {'content':   txt}   answer text deltas
    then at the end:
        {'tool_calls': [{'id','name','args'}, ...]}   (only if the model called tools)
        {'usage': completion_tokens}
    """
    resp = _c(base_url, api_key).chat.completions.create(
        model=model or config.MODEL, messages=messages,
        tools=tools or None, tool_choice="auto" if tools else None,
        temperature=temperature, stream=True, stream_options={"include_usage": True},
    )
    calls, usage = {}, 0
    for chunk in resp:
        if getattr(chunk, "usage", None):
            usage = chunk.usage.completion_tokens
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta
        rc = getattr(d, "reasoning_content", None) or (getattr(d, "model_extra", None) or {}).get("reasoning_content")
        if rc:
            yield {"reasoning": rc}
        if d.content:
            yield {"content": d.content}
        for tc in (d.tool_calls or []):
            slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
            if tc.id:
                slot["id"] = tc.id
            if tc.function and tc.function.name:
                slot["name"] = tc.function.name
            if tc.function and tc.function.arguments:
                slot["args"] += tc.function.arguments
    if calls:
        yield {"tool_calls": [calls[i] for i in sorted(calls)]}
    yield {"usage": usage}
