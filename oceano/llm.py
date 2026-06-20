"""LLM client layer — any number of OpenAI-compatible endpoints, + streaming.

Clients are cached per (base_url, api_key). All callers may override the endpoint;
omitting it falls back to the local llama-swap defaults in config.
"""
import json
import re
from functools import lru_cache

import httpx
from openai import OpenAI

import config


@lru_cache(maxsize=32)
def client(base_url, api_key):
    # connect timeout fails fast on a down endpoint; read/write/pool default to LLM_TIMEOUT
    # (the per-chunk idle ceiling when streaming) so a hung socket can't stall a turn forever.
    return OpenAI(base_url=base_url, api_key=api_key or "sk-no-key-needed",
                  timeout=httpx.Timeout(config.LLM_TIMEOUT, connect=config.LLM_CONNECT_TIMEOUT))


# Some llama.cpp builds don't extract tool calls from certain models (e.g. Qwen3.5
# reasoning models) when STREAMING — the model's <tool_call> block leaks into the
# text as-is. Non-streaming parses fine; this recovers the streaming case.
_TC_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_TC_FN = re.compile(r"<function=([^>\s]+)\s*>", re.DOTALL)
_TC_PARAM = re.compile(r"<parameter=([^>\s]+)\s*>\s*(.*?)\s*</parameter>", re.DOTALL)


def _parse_leaked_tool_calls(text):
    """Pull tool calls out of leaked <tool_call> blocks (Qwen XML or JSON form)."""
    out = []
    for i, block in enumerate(_TC_BLOCK.findall(text or "")):
        block = block.strip()
        m = _TC_FN.search(block)
        if m:                                     # <function=name><parameter=k>v</parameter>
            name = m.group(1).strip()
            args = {k.strip(): v.strip() for k, v in _TC_PARAM.findall(block)}
            out.append({"id": f"call_{i}", "name": name, "args": json.dumps(args)})
        else:                                     # {"name": ..., "arguments": {...}}
            try:
                obj = json.loads(block)
            except (ValueError, TypeError):
                continue
            if obj.get("name"):
                out.append({"id": f"call_{i}", "name": obj["name"],
                            "args": json.dumps(obj.get("arguments", {}))})
    return out


def _c(base_url, api_key):
    return client(base_url or config.LLM_BASE_URL, api_key or config.LLM_API_KEY)


def _model(model):
    """A concrete model id for the call. There is no hardcoded default any more: an explicit
    `model` wins, else we resolve the configured/served one (delegate.resolve_primary) so
    model-less callers (the workflow decision gate, the skill publish gate) still work. An
    empty result means nothing is set up — the endpoint then 400s, the right 'configure a
    model in Rivers' signal."""
    if model:
        return model
    try:
        from oceano import delegate
        return delegate.resolve_primary()["model"] or config.MODEL
    except Exception:
        return config.MODEL


def chat(messages, tools=None, model=None, temperature=0.2, base_url=None, api_key=None,
         return_usage=False, max_tokens=None):
    """One completion. Returns the raw `message` (text content OR tool_calls).
    With return_usage=True, returns (message, completion_tokens)."""
    resp = _c(base_url, api_key).chat.completions.create(
        model=_model(model),
        messages=messages,
        tools=tools or None,
        tool_choice="auto" if tools else None,
        temperature=temperature,
        max_tokens=max_tokens,
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
        model=_model(model), messages=messages,
        tools=tools or None, tool_choice="auto" if tools else None,
        temperature=temperature, stream=True, stream_options={"include_usage": True},
    )
    calls, usage, prompt, buf = {}, 0, 0, ""
    for chunk in resp:
        u = getattr(chunk, "usage", None)
        if u:
            usage = u.completion_tokens
            prompt = getattr(u, "prompt_tokens", 0) or prompt   # actual context size processed
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta
        rc = getattr(d, "reasoning_content", None) or (getattr(d, "model_extra", None) or {}).get("reasoning_content")
        if rc:
            buf += rc
            yield {"reasoning": rc}
        if d.content:
            buf += d.content
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
    elif tools and "<tool_call>" in buf:          # streaming parser missed them → recover.
        # Only when tools were actually offered: with tools=None a <tool_call> block in
        # the text is just text, and reconstructing it would hand a tool-less turn
        # executable calls it was never given.
        leaked = _parse_leaked_tool_calls(buf)
        if leaked:
            yield {"tool_calls": leaked}
    yield {"usage": usage, "prompt_tokens": prompt}
