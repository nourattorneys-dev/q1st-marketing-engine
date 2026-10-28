"""Model calls for client work. Talks to the shared model server directly (not Nour's chat wrapper, which adds the
signed-in Nour reader's answer preferences and writes Nour's audit). Returns the text AND the token counts for metering."""
from __future__ import annotations
import json, re

from . import db


async def chat(messages: list, *, max_tokens: int, temperature: float = 0.3) -> tuple[str, int, int]:
    from shared import vllm_client                                      # the server's model address, key and served name
    engine = (await db.settings(["llm_engine"])).get("llm_engine") or "nemotron"
    client, served = vllm_client._client(engine), vllm_client.model_name(engine)
    r = await client.chat.completions.create(model=served, messages=messages, max_tokens=int(max_tokens), temperature=temperature)
    u = getattr(r, "usage", None)
    return (r.choices[0].message.content or "").strip(), int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0)


def json_from(text: str, want: type = dict):
    """The first JSON value of the wanted shape (an object by default) in a model answer - it may wrap it in prose or ``` fences,
    or answer a list where an object was asked: values of another shape are skipped. The real decoder at each opening bracket,
    so brackets inside quoted text never break it."""
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    dec = json.JSONDecoder()
    i = 0
    while i < len(t):
        if t[i] in "{[":
            try:
                v, end = dec.raw_decode(t, i)
            except ValueError:
                i += 1
                continue
            if isinstance(v, want):
                return v
            i = end                                                     # skip the whole value of the other shape, not just its bracket
            continue
        i += 1
    raise ValueError("no JSON in the model answer")
