"""AI Optimization Module hook — advisory only (design invariant I-2).

The module does not exist yet: with SAIEP_AI_URL unset this is a no-op.
When set, POST <url>/recommend with the request; any error or a response
slower than the hard 2 s timeout yields None and the original request is
used unchanged. The recommendation is recorded, never applied.
"""
import json
import os
import urllib.request

TIMEOUT_SECONDS = 2.0


def recommend(request_dict, url=None, timeout=TIMEOUT_SECONDS):
    url = url if url is not None else os.environ.get('SAIEP_AI_URL', '')
    if not url:
        return None
    body = json.dumps(request_dict).encode()
    req = urllib.request.Request(
        url.rstrip('/') + '/recommend',
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(65536))
    except Exception:
        return None
    return data if isinstance(data, dict) else None
