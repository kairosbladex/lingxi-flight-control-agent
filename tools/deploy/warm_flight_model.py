"""Bounded startup readiness for the dedicated model, not a flight request."""
import json
import time
from urllib.request import Request, urlopen


def warm(base='http://192.168.130.238:11438', model='qwen3.5:9b'):
    deadline = time.monotonic() + 150
    while True:
        try:
            with urlopen(base + '/api/version', timeout=3) as response:
                json.load(response)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError('model startup unavailable') from None
            time.sleep(1)
    request = Request(base + '/api/generate', headers={'Content-Type': 'application/json'},
        data=json.dumps({'model': model, 'keep_alive': -1, 'stream': False,
                         'options': {'num_ctx': 32768}}).encode())
    with urlopen(request, timeout=max(1, deadline - time.monotonic())) as response:
        result = json.load(response)
    if result.get('done') is not True:
        raise RuntimeError('model warmup incomplete')
    with urlopen(base + '/api/ps', timeout=5) as response:
        models = json.load(response).get('models', [])
    if not any(m.get('name') == model and m.get('context_length') == 32768
               and m.get('size_vram', 0) > 0 for m in models):
        raise RuntimeError('resident model not verified')
    print('flight model ready: resident GPU model, context=32768')


if __name__ == '__main__':
    warm()
