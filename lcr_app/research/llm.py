"""Google Gemini access (free API key from Google AI Studio).

* API keys are stored in the operating system's credential store via `keyring`
  (Windows Credential Manager on Windows) – never in a file or in the repo.
* Free-tier rate limits (HTTP 429) are handled by waiting and retrying.
* `tool_loop` lets the model call our own Python functions (search papers,
  read abstracts, ask the user …). The model never browses on its own, so
  every source it can cite comes from our verified tools.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable

KEY_SERVICE = "LCRSweep"
GET_KEY_URL = "https://aistudio.google.com/apikey"


class LLMError(RuntimeError):
    pass


class LLMCancelled(LLMError):
    pass


# --------------------------------------------------------------- key storage
def get_secret(name: str) -> str:
    try:
        import keyring

        v = keyring.get_password(KEY_SERVICE, name)
        if v:
            return v
    except Exception:
        pass
    env = {"gemini_api_key": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "s2_api_key": ("S2_API_KEY",),
           "openalex_api_key": ("OPENALEX_API_KEY",)}.get(name, ())
    for e in env:
        if os.environ.get(e):
            return os.environ[e]
    return ""


def set_secret(name: str, value: str) -> bool:
    """Store in the OS credential store. Returns False if no secure store is available."""
    try:
        import keyring
        from keyring.backends import fail

        if isinstance(keyring.get_keyring(), fail.Keyring):
            return False
        if value:
            keyring.set_password(KEY_SERVICE, name, value)
        else:
            try:
                keyring.delete_password(KEY_SERVICE, name)
            except Exception:
                pass
        return True
    except Exception:
        return False


# ------------------------------------------------------------------- helpers
def pick_default_model(names: list[str]) -> str:
    """Prefer the newest plain 'flash' model (good quality, generous free tier)."""
    bad = ("lite", "image", "tts", "live", "audio", "embedding", "vision", "exp", "learnlm", "gemma", "robotics",
           "computer", "native")

    def ver(n: str) -> float:
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return float(m.group(1)) if m else 0.0

    clean = [n for n in names if "gemini" in n and not any(b in n for b in bad)]
    flash = [n for n in clean if "flash" in n]
    for group in (flash, clean, names):
        if group:
            stable = [n for n in group if "preview" not in n] or group
            return sorted(stable, key=lambda n: (ver(n), -len(n)), reverse=True)[0]
    return ""


def extract_json(text: str):
    """Parse JSON from a model answer (plain, fenced ```json``` or embedded)."""
    if not text:
        raise ValueError("empty answer")
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t.find(open_c), t.rfind(close_c)
        if i >= 0 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found in the answer")


# --------------------------------------------------------------------- Gemini
class GeminiLLM:
    def __init__(self, api_key: str, model: str = "", status: Callable[[str], None] | None = None,
                 before_send: Callable[[str, str], bool] | None = None) -> None:
        if not api_key:
            raise LLMError(f"No Gemini API key. Create a free one at {GET_KEY_URL} and paste it in AI settings.")
        try:
            from google import genai
        except ImportError as e:
            raise LLMError("The 'google-genai' package is missing: pip install google-genai") from e
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.status = status or (lambda s: None)
        self.before_send = before_send
        self.tokens = 0
        self.calls = 0

    # models -------------------------------------------------------------
    def list_models(self) -> list[str]:
        names = []
        try:
            for m in self.client.models.list():
                acts = getattr(m, "supported_actions", None) or []
                if not acts or "generateContent" in acts:
                    names.append(m.name.replace("models/", ""))
        except Exception as e:
            raise LLMError(self._explain(e)) from e
        return sorted(names)

    def ensure_model(self) -> str:
        if not self.model:
            self.model = pick_default_model(self.list_models())
            if not self.model:
                raise LLMError("No Gemini model is available for this key.")
        return self.model

    # low level ----------------------------------------------------------
    @staticmethod
    def _explain(e: Exception) -> str:
        s = str(e)
        if "API_KEY_INVALID" in s or "API key not valid" in s:
            return "The Gemini API key is not valid. Create a new one at " + GET_KEY_URL
        if "PERMISSION_DENIED" in s or " 403" in s:
            return "Permission denied by Google (key restricted, or the API is not available in your region)."
        if "RESOURCE_EXHAUSTED" in s or "429" in s:
            return "Free-tier quota exhausted for now. Wait a minute (or until tomorrow for the daily quota)."
        return s[:400]

    def _call(self, contents, config, task: str, preview: str):
        if self.before_send and not self.before_send(task, preview):
            raise LLMCancelled("Cancelled by the user")
        self.ensure_model()
        delay = 0.0
        for attempt in range(5):
            if delay:
                self.status(f"Gemini rate limit — waiting {delay:.0f} s (free tier) …")
                time.sleep(delay)
            try:
                resp = self.client.models.generate_content(model=self.model, contents=contents, config=config)
                self.calls += 1
                um = getattr(resp, "usage_metadata", None)
                self.tokens += int(getattr(um, "total_token_count", 0) or 0)
                return resp
            except Exception as e:  # google.genai.errors.APIError and network errors
                s = str(e)
                code = getattr(e, "code", None)
                if code in (429, 500, 503) or "RESOURCE_EXHAUSTED" in s or "UNAVAILABLE" in s:
                    if "PerDay" in s or "per day" in s.lower():
                        raise LLMError("The daily free quota of this model is used up. Try again tomorrow or "
                                       "choose another model in AI settings.") from e
                    m = re.search(r"retry(?:Delay| in)[^0-9]*([0-9.]+)\s*s", s)
                    delay = min(float(m.group(1)) + 1 if m else 15.0 * (attempt + 1), 90.0)
                    continue
                raise LLMError(self._explain(e)) from e
        raise LLMError("Gemini kept refusing the request (rate limit). Try again later.")

    def generate(self, task: str, system: str, prompt: str, temperature: float = 0.3) -> str:
        from google.genai import types

        cfg = types.GenerateContentConfig(system_instruction=system, temperature=temperature)
        resp = self._call(prompt, cfg, task, prompt)
        return resp.text or ""

    def generate_json(self, task: str, system: str, prompt: str, schema: dict | None = None,
                      temperature: float = 0.2):
        from google.genai import types

        kw = {"response_mime_type": "application/json"}
        if schema:
            kw["response_json_schema"] = schema
        cfg = types.GenerateContentConfig(system_instruction=system, temperature=temperature, **kw)
        resp = self._call(prompt, cfg, task, prompt)
        try:
            return extract_json(resp.text or "")
        except ValueError as e:
            raise LLMError(f"Gemini did not return valid JSON for '{task}'") from e

    def tool_loop(self, task: str, system: str, prompt: str, tools: dict[str, tuple[dict, Callable]],
                  max_steps: int = 8, on_call: Callable[[str, dict], None] | None = None,
                  temperature: float = 0.3) -> str:
        """Let the model call `tools` (name -> (json schema of parameters + description, function))."""
        from google.genai import types

        decls = [types.FunctionDeclaration(name=n, description=spec.get("description", ""),
                                           parameters_json_schema=spec.get("parameters", {"type": "object",
                                                                                           "properties": {}}))
                 for n, (spec, _) in tools.items()]
        cfg = types.GenerateContentConfig(
            system_instruction=system, temperature=temperature, tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
        for step in range(max_steps):
            resp = self._call(contents, cfg, f"{task} (step {step + 1})", prompt if step == 0 else
                              "(continuation: results of the tool calls listed in the log)")
            calls = resp.function_calls or []
            if not calls:
                return resp.text or ""
            contents.append(resp.candidates[0].content)
            parts = []
            for c in calls:
                args = dict(c.args or {})
                if on_call:
                    on_call(c.name, args)
                fn = tools.get(c.name, (None, None))[1]
                try:
                    result = fn(**args) if fn else {"error": f"unknown tool {c.name}"}
                except LLMCancelled:
                    raise
                except Exception as e:  # report tool errors back to the model
                    result = {"error": str(e)[:300]}
                parts.append(types.Part.from_function_response(name=c.name, response={"result": result}))
            contents.append(types.Content(role="user", parts=parts))
        # step budget used up: ask for the final answer without tools
        contents.append(types.Content(role="user", parts=[types.Part.from_text(
            text="The tool-call budget is used up. Give your final answer now in the requested format.")]))
        cfg2 = types.GenerateContentConfig(system_instruction=system, temperature=temperature)
        return self._call(contents, cfg2, f"{task} (final)", "(final answer)").text or ""
