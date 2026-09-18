"""Fixed, credential-free ALLOW smoke request and strict equality contract."""
import json

_REQUEST = '{"method":"POST","path":"/v1/chat/completions","body":{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hi"}],"max_completion_tokens":1}}'


def allow_request():
    return json.loads(_REQUEST)


def validate_allow_request(request):
    try:
        canonical = lambda obj: json.dumps(obj, sort_keys=True, allow_nan=False)
        if canonical(request) != canonical(allow_request()):
            raise ValueError()
    except Exception:
        raise ValueError("Smoke request rejected") from None
