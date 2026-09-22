"""Chat-completion transport for the existing, unchanged unified choice policy.

The adapter records its own source hash alongside the common runtime hash.
It declares page-mode support and inherits the unchanged grouping policy.
"""
from __future__ import annotations

import json
import os
import time

import httpx

from wikirace import unified_protocol as protocol
from wikirace.unified_brain import UnifiedBrain

OUTPUT_INSTRUCTION = (
    '\n\nReturn only one JSON object with this exact structure: '
    '{"answers":{"next":{"choice":"A"}}}. '
    'Replace A with exactly one offered code. Do not return an explanation, '
    'Markdown, URLs, or additional fields.'
)


def choice_schema(codes):
    def obj(properties):
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}
    return obj({"answers": obj({"next": obj({"choice": {"type": "string", "enum": list(codes)}})})})


def chat_body(request):
    question = request["questions"]["next"]
    return {
        "model": request["model"], "temperature": 0, "max_tokens": 2048,
        "messages": [
            {"role": "system", "content": question["instructions"] + OUTPUT_INSTRUCTION},
            {"role": "user", "content": json.dumps({
                "evidence": request["state"],
                "options": [{"code": code, "description": description}
                            for code, description in question["criteria"].items()]}, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "WikiChoice", "strict": True, "schema": choice_schema(question["criteria"])}},
    }


def parse_choice(data, codes):
    choices = data.get("choices", [])
    if len(choices) != 1 or choices[0].get("finish_reason") != "stop":
        raise ValueError("chat_choice_missing_or_incomplete")
    value = json.loads(choices[0]["message"]["content"])
    if (not isinstance(value, dict) or set(value) != {"answers"}
            or not isinstance(value["answers"], dict) or set(value["answers"]) != {"next"}
            or not isinstance(value["answers"]["next"], dict) or set(value["answers"]["next"]) != {"choice"}):
        raise ValueError("invalid_chat_choice_schema")
    code = value["answers"]["next"]["choice"]
    if not isinstance(code, str) or code not in codes:
        raise ValueError("chat_choice_not_offered")
    return code


class ChatBackend:
    def __init__(self, *, client=None, client_factory=None, api_key=None, base_url=None, model=None):
        self.api_key = api_key if api_key is not None else os.environ["HOTDAY_API_KEY"]
        self.base_url = (base_url or os.environ.get("HOTDAY_BASE_URL", "https://api.hotday.uk/v1")).rstrip("/")
        self.model = model or os.environ.get("HOTDAY_MODEL", "models/gemini-3.8-flash")
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=60, follow_redirects=False))
        self.client = client or self.client_factory()
        self.reset_episode()

    def reset_episode(self):
        self._deadline = None

    def set_deadline(self, deadline):
        self._deadline = deadline

    def close(self):
        self.client.close()

    def redact(self, value):
        return json.loads(json.dumps(value).replace(self.api_key, "[REDACTED]")) if self.api_key else value

    def post(self, body):
        started = time.perf_counter()
        for attempt in range(3):
            remaining = 60 if self._deadline is None else self._deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError("episode_deadline_exceeded")
            # The shared evaluator closes every brain at the end of an episode.
            # A later task in the same suite needs a fresh HTTP connection pool.
            if self.client.is_closed:
                self.client = self.client_factory()
            try:
                response = self.client.post(self.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.api_key}, json=body, timeout=min(60, remaining))
                response.raise_for_status()
                data = self.redact(response.json())
                data["_transport"] = {"attempts": attempt + 1,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000)}
                return data
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (429, 502, 503, 504, 529) or attempt == 2:
                    raise
            except httpx.TransportError:
                if attempt == 2:
                    raise
            delay = 0.5 * 2 ** attempt
            if self._deadline is not None and self._deadline - time.perf_counter() <= delay:
                raise TimeoutError("episode_deadline_exceeded")
            time.sleep(delay)


class UnifiedChatBrain(UnifiedBrain):
    supports_page_mode = True

    def __init__(self, *, backend=None, planner=None):
        super().__init__("jev", backend=backend or ChatBackend(), planner=planner)
        self.name = "gemini"

    def load(self):
        pass

    def infer(self, item, audit):
        if not audit["fits"]:
            raise ValueError("unified_input_exceeds_budget")
        request = {"model": self.model, "state": item["state"], "questions": protocol.choice_questions(item)}
        body = chat_body(request)
        started = time.perf_counter()
        raw = None
        try:
            raw = self.backend.post(body)
            code = parse_choice(raw, request["questions"]["next"]["criteria"])
        except Exception as exc:
            response = getattr(exc, "response", None)
            exc.brain_debug = {"unified_failed_request": {
                "canonical": item, "request": request, "chat_request": body, "input_audit": audit,
                "status_code": getattr(response, "status_code", None),
                "response_body": self.backend.redact(getattr(response, "text", "")[:10000]),
                "native_response": raw}}
            raise
        winner = next(o["id"] for o in item["options"] if o["code"] == code)
        usage = raw.get("usage", {})
        response = {"model": raw.get("model", self.model), "answers": {"next": {"choice": code}},
                    "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                              "output_tokens": usage.get("completion_tokens", 0),
                              "total_tokens": usage.get("total_tokens", 0)},
                    "_transport": raw["_transport"], "native_response": raw}
        return winner, {"canonical": item, "input_audit": audit, "request": request,
                        "chat_request": body, "response": response, "winner": winner,
                        "milliseconds": round((time.perf_counter() - started) * 1000, 3)}
