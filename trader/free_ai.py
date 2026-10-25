"""A free AI for the agents' everyday questions, through any provider with an OpenAI-compatible chat API.

Free tiers are rationed by requests and tokens instead of dollars, change without notice, and answer with smaller
models than Claude. So this client counts every limit itself, never retries in a loop (a refused call still counts
against Google's daily quota), checks every answer against the question's JSON schema before an agent sees it, and
returns None on any failure: the caller then asks Claude, if Claude has budget left, or uses its free rules.

The key travels only in the Authorization header: never in a URL, a log line or a Telegram message.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Preset:
    label: str
    base_url: str
    models: tuple[str, ...]          # tried in this order; each model has its own daily allowance unless shared_daily
    json_mode: str = "json_schema"   # json_schema: the provider enforces the form; json_object: the form is in the prompt
    rpm: int = 10                    # requests a minute the desk allows itself per model (below the provider's limit)
    tpm: int = 0                     # tokens a minute per model (0 = no limit to respect)
    tpd: int = 0                     # tokens a day per model (0 = no limit)
    daily: int = 400                 # requests a day per model the desk allows itself
    shared_daily: bool = False       # one daily allowance for all models of the account
    reset_tz: str = "UTC"            # when the provider's daily allowance starts again
    burst: int = 6                   # the most buy/sell questions in one market check
    max_tokens: int = 2048
    key_url: str = ""                # where the owner gets a key
    trains_on_free_data: bool = False
    extra: dict = field(default_factory=dict)   # model name prefix -> extra request fields


# Figures checked against each provider's own pages on 2026-10-25, set a little below the published limits.
# Google publishes its free limits only inside AI Studio (aistudio.google.com/rate-limit): about 500 requests a day
# and 10 a minute per Flash-Lite model. Every number can be changed in the settings (FREE_AI_MODELS,
# FREE_AI_DAILY_REQUESTS), because free tiers change.
PRESETS: dict[str, Preset] = {
    "gemini": Preset(
        "Google Gemini (free tier)", "https://generativelanguage.googleapis.com/v1beta/openai",
        ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"), rpm=8, tpm=200_000, daily=400,
        reset_tz="America/Los_Angeles", burst=6, max_tokens=4096, key_url="https://aistudio.google.com/apikey",
        trains_on_free_data=True),
    "groq": Preset(
        "Groq (free plan)", "https://api.groq.com/openai/v1", ("openai/gpt-oss-120b", "openai/gpt-oss-20b"),
        rpm=25, tpm=6_500, tpd=180_000, daily=900, burst=3, max_tokens=1500, key_url="https://console.groq.com/keys",
        extra={"openai/gpt-oss": {"reasoning_effort": "low"}}),
    "mistral": Preset(
        "Mistral (free mode)", "https://api.mistral.ai/v1", ("mistral-small-latest", "ministral-8b-latest"),
        rpm=40, tpm=400_000, daily=1000, burst=6, key_url="https://console.mistral.ai/api-keys",
        trains_on_free_data=True),
    "cloudflare": Preset(
        "Cloudflare Workers AI (free plan)", "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        ("@cf/google/gemma-4-26b-a4b-it",), json_mode="json_object", rpm=100, daily=180, shared_daily=True,
        burst=6, key_url="https://dash.cloudflare.com/?to=/:account/ai/workers-ai"),
    "openrouter": Preset(
        "OpenRouter (free models)", "https://openrouter.ai/api/v1", ("openrouter/free",), rpm=15, daily=45,
        shared_daily=True, burst=3, max_tokens=4096, key_url="https://openrouter.ai/keys", trains_on_free_data=True),
    "custom": Preset("your own OpenAI-compatible service", "", (), json_mode="json_object", rpm=20, daily=500, burst=4),
}

MAX_WAIT_SECONDS = 20     # the longest the desk waits for a per-minute slot before moving on
COOLDOWN_SECONDS = 60     # after a "too many requests a minute" or a quick server error
OUTAGE_COOLDOWN_SECONDS = 600   # after a timeout, a lost connection or a gateway timeout: longer than one market
                                # check, so each model costs at most one timeout per check instead of one per question
BAD_REQUESTS_TO_SWITCH_OFF = 3  # the same model refusing this many questions in a row (HTTP 400) is switched off
# a 429 that means "come back tomorrow", not "slow down": Google's quota id ...PerDay..., Groq's (RPD)/(TPD), Cloudflare's
# error 3036, OpenRouter's free-models-per-day. Word boundaries keep identifiers like an org id from matching.
DAILY_LIMIT = re.compile(r'perday|per day|quota_exceeded|\bdaily\b|\b[rt]pd\b|free-models-per-day|"code": 3036\b')
GONE_MODEL = re.compile(r"no such model|model_not_found|does not exist|decommissioned|invalid model|unknown model|"
                        r"is not a valid model|\b5007\b")
TIMEOUT = (10, 60)        # connect, read
SAFETY_MARGIN = 0.9       # plan with 90% of each daily allowance


def reset_day(tz_name: str, now: float) -> str:
    """The provider's current quota day (Google restarts its daily quotas at midnight US Pacific time)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - no time zone database: Pacific standard time is close enough
        tz = dt.timezone(dt.timedelta(hours=-8)) if tz_name != "UTC" else dt.timezone.utc
    return dt.datetime.fromtimestamp(now, tz).date().isoformat()


def minutes_to_reset(tz_name: str, now: float) -> float:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = dt.timezone(dt.timedelta(hours=-8)) if tz_name != "UTC" else dt.timezone.utc
    local = dt.datetime.fromtimestamp(now, tz)
    midnight = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(), tz)
    return max(1.0, (midnight - local).total_seconds() / 60)


# ---- answers: parse and check against the question's JSON schema ----
def parse_json(text: str):
    """The JSON object in a model's answer, also when it is wrapped in ``` fences or preceded by <think> notes."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except ValueError:
        i, k = t.find("{"), t.rfind("}")
        if i < 0 or k <= i:
            raise
        return json.loads(t[i:k + 1])


def conform(schema: dict, value, path: str = "answer"):
    """The value in the schema's shape, or ValueError. Lenient where it is safe (a number written as text, an enum in
    the wrong case, extra fields are dropped), strict where an agent depends on it (required fields, types, enums)."""
    kind = schema.get("type")
    if isinstance(kind, list):
        for k in kind:
            try:
                return conform({**schema, "type": k}, value, path)
            except ValueError:
                continue
        raise ValueError(f"{path}: none of {kind}")
    if "enum" in schema:
        if value in schema["enum"]:
            return value
        if isinstance(value, str):
            for option in schema["enum"]:
                if isinstance(option, str) and option.lower() == value.strip().lower():
                    return option
        raise ValueError(f"{path}: {value!r} is not one of {schema['enum']}")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path}: not an object")
        props = schema.get("properties", {})
        out = {}
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key}: missing")
        for key, sub in props.items():
            if key in value:
                try:
                    out[key] = conform(sub, value[key], f"{path}.{key}")
                except ValueError:
                    if key in schema.get("required", []):
                        raise
        return out
    if kind == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path}: not a list")
        items, out = schema.get("items", {}), []
        for i, v in enumerate(value):
            try:
                out.append(conform(items, v, f"{path}[{i}]"))
            except ValueError as e:
                log.info("Free AI answer: dropped %s", e)
        return out
    if kind in ("number", "integer"):
        if isinstance(value, bool):
            raise ValueError(f"{path}: not a number")
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError:
                raise ValueError(f"{path}: not a number") from None
        if not isinstance(value, (int, float)) or math.isnan(value) or math.isinf(value):
            raise ValueError(f"{path}: not a number")
        return int(round(value)) if kind == "integer" else float(value)
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise ValueError(f"{path}: not true/false")
    if kind == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        raise ValueError(f"{path}: not text")
    return value


class FreeAI:
    """Same questions as ClaudeJSON.ask(), answered by a free tier. No web search (research() returns None)."""

    def __init__(self, provider: str, api_key: str, models=(), base_url: str = "", daily_requests: int = 0,
                 store=None, session=None, clock=time.time, sleep=time.sleep):
        self.provider = provider
        self.preset = PRESETS[provider]
        self.label = self.preset.label
        self._key = api_key
        self.models = [m for m in models if m] or list(self.preset.models)
        self.base_url = (base_url or self.preset.base_url).rstrip("/")
        self.daily = int(daily_requests) if daily_requests else self.preset.daily
        self.store, self.clock, self.sleep = store, clock, sleep
        self.session = session or requests.Session()
        self._lock = threading.Lock()   # the Telegram thread reads the counters while a check asks
        self._recent: dict[str, list[list]] = {}          # model -> [time, tokens, quota day] of the last minute's calls
        self._cool: dict[str, float] = {}                 # model -> time it may be asked again
        self._json_in_prompt: set[str] = set()            # models that refused the json_schema format
        self.disabled: dict[str, str] = {}                # model (or "*" for all) -> why, until the desk restarts
        self._bad400: dict[str, int] = {}                 # model -> HTTP 400 refusals in a row
        self.problems: list[dict] = []                    # for one Telegram alert each
        self.usage = self._load()

    def __repr__(self) -> str:  # never the key
        return f"FreeAI({self.provider}, models={self.models})"

    # ---- counters (kept in the journal, so a restart never resets the day's count) ----
    def _today(self) -> str:
        return reset_day(self.preset.reset_tz, self.clock())

    def _fresh(self) -> dict:
        return {"provider": self.provider, "day": self._today(), "models": {}, "exhausted": [], "by_caller": {},
                "alerted": False}

    def _load(self) -> dict:
        saved = self.store.get("free_ai_usage") if self.store else None
        if saved and saved.get("provider") == self.provider and saved.get("day") == self._today():
            return saved
        return self._fresh()

    def _save(self) -> None:
        if self.store:
            self.store.set("free_ai_usage", self.usage)

    def _roll(self) -> None:
        if self.usage.get("day") != self._today():
            self.usage = self._fresh()
            self._save()

    def _m(self, model: str) -> dict:
        return self.usage["models"].setdefault(model, {"requests": 0, "tokens": 0, "answers": 0, "bad": 0})

    def _used(self, model: str) -> int:
        if self.preset.shared_daily:
            return sum(m.get("requests", 0) for m in self.usage["models"].values())
        return self._m(model)["requests"]

    def usable(self, model: str) -> bool:
        return ("*" not in self.disabled and model not in self.disabled and model not in self.usage["exhausted"]
                and self._used(model) < self.daily
                and (not self.preset.tpd or self._m(model)["tokens"] < self.preset.tpd * SAFETY_MARGIN))

    def requests_today(self) -> int:
        with self._lock:
            self._roll()
            return sum(m.get("requests", 0) for m in self.usage["models"].values())

    def _token_requests_left(self, model: str) -> float:
        """Requests the model's remaining daily tokens still pay for (unlimited when the provider has no token cap),
        at the model's own average size today (2,000 tokens a request before it has answered five)."""
        if not self.preset.tpd:
            return math.inf
        u = self._m(model)
        done = u.get("answers", 0) + u.get("bad", 0)
        avg = u["tokens"] / done if done >= 5 and u["tokens"] else 2000
        return max(0.0, self.preset.tpd * SAFETY_MARGIN - u["tokens"]) / max(1.0, avg)

    def requests_left(self) -> float:
        """Requests the desk may still make today, keeping 10% of each allowance (requests, and tokens where the
        provider also caps tokens a day) in reserve."""
        with self._lock:
            self._roll()
            models = [m for m in self.models if self.usable(m)]
            if not models:
                return 0.0
            cap = self.daily * SAFETY_MARGIN
            if self.preset.shared_daily:
                return max(0.0, cap - self._used(models[0]))
            return sum(max(0.0, min(cap - self._used(m), self._token_requests_left(m))) for m in models)

    def minutes_to_reset(self) -> float:
        return minutes_to_reset(self.preset.reset_tz, self.clock())

    def status(self) -> dict:
        with self._lock:
            self._roll()
            return {"provider": self.provider, "label": self.label, "models": list(self.models),
                    "daily": self.daily, "shared": self.preset.shared_daily, "usage": json.loads(json.dumps(self.usage)),
                    "disabled": dict(self.disabled), "reset_hours": round(self.minutes_to_reset() / 60, 1),
                    "reset_tz": self.preset.reset_tz}

    def take_problems(self) -> list[dict]:
        with self._lock:
            out, self.problems = self.problems, []
            return out

    def _problem(self, kind: str, text: str) -> None:
        self.problems.append({"kind": kind, "text": text})
        log.warning("Free AI (%s): %s", self.provider, text)

    # ---- the one question ----
    def research(self, *args, **kwargs) -> None:
        return None   # free tiers have no web search the desk can use: the caller judges from market data

    def ask(self, system: str, prompt: str, schema: dict, effort: str = "medium", caller: str = "other") -> dict | None:
        for model in self.models:
            answer, stop = self._ask_model(model, system, prompt, schema, caller)
            if answer is not None:
                return answer
            if stop:
                break
        self._note_used_up()
        return None

    def _note_used_up(self) -> None:
        """One 'daily' alert per quota day once no model can answer any more, whether the provider said so (429) or
        the desk's own counters ran out first (the usual case: the presets stay below the providers' limits).
        Models switched off for another reason (a wrong key, a retired model) have their own alert."""
        with self._lock:
            self._roll()
            capped = [m for m in self.models if m not in self.disabled]
            if "*" in self.disabled or not capped or any(self.usable(m) for m in capped) or self.usage.get("alerted"):
                return
            self.usage["alerted"] = True
            self._save()
            self._problem("daily", f"the free daily allowance of {self.label} is used up; Claude (if it has budget) or "
                                   f"the free rules answer until it restarts in about {self.minutes_to_reset() / 60:.0f} h")

    def _wait(self, model: str, tokens: int) -> float:
        """Seconds until this call fits the model's per-minute request and token limits."""
        now = self.clock()
        recent = [r for r in self._recent.get(model, []) if r[0] > now - 60]
        self._recent[model] = recent
        wait = 0.0
        if len(recent) >= self.preset.rpm:
            wait = recent[len(recent) - self.preset.rpm][0] + 60 - now
        if self.preset.tpm:
            total = sum(r[1] for r in recent) + tokens
            for r in recent:   # oldest first: wait until enough of the last minute's tokens have expired
                if total <= self.preset.tpm:
                    break
                total -= r[1]
                wait = max(wait, r[0] + 60 - now)
        return max(0.0, wait)

    def _body(self, model: str, system: str, prompt: str, schema: dict) -> dict:
        in_prompt = self.preset.json_mode == "json_object" or model in self._json_in_prompt
        if in_prompt:
            system = (system + "\n\nReply with one JSON object only, no other text, matching this JSON Schema:\n"
                      + json.dumps(schema))
            fmt = {"type": "json_object"}
        else:
            system = system + "\n\nReply in JSON."
            fmt = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
        body = {"model": model, "max_tokens": self.preset.max_tokens, "response_format": fmt,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        for prefix, fields in self.preset.extra.items():
            if model.startswith(prefix):
                body.update(fields)
        return body

    def _ask_model(self, model, system, prompt, schema, caller, retry_format=True) -> tuple[dict | None, bool]:
        """(answer or None, stop: True when no other model of this provider can answer either)."""
        with self._lock:
            self._roll()
            if "*" in self.disabled:
                return None, True
            if not self.usable(model) or self.clock() < self._cool.get(model, 0):
                return None, False
            body = self._body(model, system, prompt, schema)
            tokens = len(json.dumps(body)) // 4 + 400
            if self.preset.tpm and tokens > self.preset.tpm:
                return None, False   # bigger than this model's per-minute allowance: a provider would refuse it
            wait = self._wait(model, tokens)
            if wait > MAX_WAIT_SECONDS:
                return None, False
        if wait:
            self.sleep(wait)
        with self._lock:
            self._roll()
            m = self._m(model)
            m["requests"] += 1        # counted before sending: a refused request still counts against the quota
            c = self.usage["by_caller"].setdefault(caller, {"calls": 0, "tokens": 0})
            c["calls"] += 1
            entry = [self.clock(), tokens, self.usage["day"]]   # the quota day this request was counted in
            self._recent.setdefault(model, []).append(entry)
            self._save()
        try:
            r = self.session.post(f"{self.base_url}/chat/completions", json=body, timeout=TIMEOUT,
                                  headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        except Exception as e:  # noqa: BLE001 - network errors, and a key with a character no HTTP header can carry
            with self._lock:
                if isinstance(e, UnicodeError):
                    self.disabled["*"] = "the key has a character that is not plain text"
                    self._problem("key", f"FREE_AI_API_KEY for {self.label} contains a character that is not a plain "
                                         "letter or digit (often an invisible character or a curly quote from a "
                                         "copy-paste); paste it again with desk setup. Until then Claude or the free "
                                         "rules answer.")
                    return None, True
                # the whole provider is unreachable, not just this model: rest them all
                rest = self.models if isinstance(e, (requests.ConnectionError, requests.ConnectTimeout)) else [model]
                for m in rest:
                    self._cool[m] = self.clock() + OUTAGE_COOLDOWN_SECONDS
            log.warning("Free AI (%s) unreachable: %s", self.provider, self._scrub(type(e).__name__ + " " + str(e))[:200])
            return None, False
        if r.status_code == 200:
            self._bad400.pop(model, None)
            return self._answer(model, r, schema, caller, entry), False
        return self._refused(model, r, system, prompt, schema, caller, retry_format, entry)

    def _answer(self, model, r, schema, caller, entry) -> dict | None:
        try:
            body = r.json()
            choice = body["choices"][0]
            content = choice["message"].get("content") or ""
            if isinstance(content, list):   # some services return content parts
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            used = int((body.get("usage") or {}).get("total_tokens") or entry[1])
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            content, used = "", entry[1]
        with self._lock:
            entry[1] = used
            same_day = self.usage.get("day") == entry[2]
            # a request counted before the provider's midnight is not charged to the new day
            m = self._m(model) if same_day else {"tokens": 0, "answers": 0, "bad": 0}
            m["tokens"] += used
            if same_day:
                self.usage["by_caller"].setdefault(caller, {"calls": 0, "tokens": 0})["tokens"] += used
            try:
                answer = conform(schema, parse_json(content))
            except (ValueError, TypeError) as e:
                m["bad"] += 1
                self._save()
                log.warning("Free AI (%s/%s) answer did not match the form: %s", self.provider, model, str(e)[:200])
                return None
            m["answers"] += 1
            self._save()
            return answer

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def _error_text(self, r) -> str:
        try:
            data = r.json()
        except ValueError:
            return self._scrub(r.text or "")[:2000]
        if isinstance(data, list) and data:   # Google's OpenAI-compatible errors come wrapped in a list
            data = data[0]
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            parts = [str(err.get(k)) for k in ("message", "status", "code", "type") if err.get(k)]
            parts += [json.dumps(err[k]) for k in ("details", "metadata") if err.get(k)]
            text = " ".join(parts)
        else:
            text = str(err)
        if isinstance(data, dict) and data.get("errors"):   # Cloudflare: {"errors": [{"code": 3036, ...}]}
            text += " " + json.dumps(data["errors"])
        return self._scrub(text)   # whole: Google puts the deciding quota id after a long help text

    def _refused(self, model, r, system, prompt, schema, caller, retry_format, entry) -> tuple[dict | None, bool]:
        status, text = r.status_code, self._error_text(r)
        low = text.lower()
        now = self.clock()
        retry = False
        with self._lock:
            if status == 429:
                if DAILY_LIMIT.search(low):
                    # yesterday's refusal (the provider's midnight passed meanwhile) says nothing about today
                    if self.usage.get("day") == entry[2] and model not in self.usage["exhausted"]:
                        self.usage["exhausted"].append(model)
                        self._save()
                    return None, False
                try:
                    pause = float(r.headers.get("retry-after") or COOLDOWN_SECONDS)
                except ValueError:
                    pause = COOLDOWN_SECONDS
                self._cool[model] = now + max(5.0, min(600.0, pause))
                log.info("Free AI (%s/%s) busy for %.0f s", self.provider, model, pause)
                return None, False
            sent_schema = self.preset.json_mode == "json_schema" and model not in self._json_in_prompt
            if status == 400 and retry_format and sent_schema and any(
                    k in low for k in ("response_format", "json_schema", "schema", "structured")):
                self._json_in_prompt.add(model)   # the form goes into the instructions from now on
                retry = True
            elif status in (401, 403) or (status == 400 and ("api key" in low or "api_key" in low)):
                if "5035" in low or "requires a workers paid plan" in low:
                    self.disabled[model] = "needs a paid plan"
                    self._problem("model", f"{model} needs a paid plan on {self.label}; set another model in "
                                           "FREE_AI_MODELS (desk setup)")
                    return None, False
                self.disabled["*"] = f"refused the key ({status})"
                self._problem("key", f"{self.label} refused the key (HTTP {status}: {text[:160]}). Check "
                                     f"FREE_AI_API_KEY with desk setup; until then Claude or the free rules answer.")
                return None, True
            elif status in (404, 410) or (status == 400 and GONE_MODEL.search(low)):
                self.disabled[model] = "unknown model"
                self._problem(f"model:{model}", f"{self.label} does not accept the model {model} (HTTP {status}: "
                                                f"{text[:120]}); the desk uses its other models. Choose current model "
                                                "names with desk setup (FREE_AI_MODELS).")
                return None, False
            elif status == 413:
                return None, False   # too large for this model's limits: the next model, or Claude
            elif status == 400:
                n = self._bad400[model] = self._bad400.get(model, 0) + 1
                log.warning("Free AI (%s/%s) refused the question (HTTP 400): %s", self.provider, model, text[:200])
                if n >= BAD_REQUESTS_TO_SWITCH_OFF:
                    self.disabled[model] = "keeps refusing (HTTP 400)"
                    self._problem(f"model:{model}", f"{self.label} refused {n} questions in a row for {model} (HTTP 400: "
                                                    f"{text[:120]}); the desk uses its other models until it restarts.")
                return None, False
            else:
                self._cool[model] = now + (OUTAGE_COOLDOWN_SECONDS if status in (502, 503, 504) else COOLDOWN_SECONDS)
                log.warning("Free AI (%s/%s) error %s: %s", self.provider, model, status, text[:200])
                return None, False
        if retry:
            log.info("Free AI (%s/%s) refused the JSON schema format; asking with the form in the prompt",
                     self.provider, model)
            return self._ask_model(model, system, prompt, schema, caller, retry_format=False)
        return None, False


def from_settings(s, store=None) -> FreeAI | None:
    if not s.use_free_ai:
        return None
    return FreeAI(s.free_ai_provider, s.free_ai_key, s.free_ai_models, s.free_ai_base_url, s.free_ai_daily_requests,
                  store=store)
