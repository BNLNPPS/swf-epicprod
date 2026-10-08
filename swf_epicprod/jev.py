"""The one Jev client (docs/JEV.md): TypeSafe AI's decision model, which
answers typed questions (choice, noul, score) about a state with
probabilities instead of text.

Every epicprod use of Jev calls ``decide``; the key is
``TYPESAFE_API_KEY`` in the environment of the production-operations
agent (never the web tier). Model unavailability (503), overload (529)
and rate limiting (429) are retried with backoff, since 7% of the calls
of the first replay (2026-10-07) failed with 503.
"""
import json
import os
import time
import urllib.error
import urllib.request

URL = 'https://api.typesafe.ai/v1/systemone'
MODEL = 'jev-latest'
USD_PER_INPUT_TOKEN = 42e-9   # output tokens are free
RETRY_CODES = (429, 503, 529)
RETRIES = 4
TIMEOUT_S = 120
MAX_CHOICE_OPTIONS = 255
SCORE_LEVELS = (2, 10)
CITATION = 'Jev, TypeSafe AI (https://docs.typesafe.ai)'


class JevError(RuntimeError):
    pass


def decide(state, questions, key=None):
    """POST one request; returns (answers, usage) where usage carries
    input_tokens and the estimated cost. Raises JevError with the reason."""
    key = key or os.environ.get('TYPESAFE_API_KEY')
    if not key:
        raise JevError('TYPESAFE_API_KEY is not set')
    body = json.dumps({'model': MODEL, 'state': state, 'questions': questions}).encode()
    delay = 2.0
    for attempt in range(RETRIES + 1):
        req = urllib.request.Request(URL, data=body, headers={
            'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                answer = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors='replace')[:300]
            if exc.code in RETRY_CODES and attempt < RETRIES:
                time.sleep(delay)
                delay *= 2
                continue
            raise JevError(f'HTTP {exc.code}: {detail}') from exc
        except (OSError, ValueError) as exc:
            if attempt < RETRIES:
                time.sleep(delay)
                delay *= 2
                continue
            raise JevError(f'{type(exc).__name__}: {exc}') from exc
        usage = dict(answer.get('usage') or {})
        usage['cost_usd'] = int(usage.get('input_tokens') or 0) * USD_PER_INPUT_TOKEN
        usage['attempts'] = attempt + 1
        return answer.get('answers') or {}, usage
    raise JevError('no answer')
