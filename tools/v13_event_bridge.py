#!/usr/bin/env python3
"""Feed already-authorized platform events into the isolated V1.3 Java extension.

This tool deliberately does not invent a KTD event endpoint. The caller must provide
event JSON from an existing read-only platform source (file or stdin); the bridge then
forwards it to the Java extension with the server-owned binding.
"""
import argparse
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from lingxi_flight_control.v13_extension import Binding


def call(opener, url, token, project, body):
    request = Request(url, data=json.dumps(body).encode(), method='POST', headers={
        'Authorization': 'Bearer ' + token, 'X-Project-Id': project,
        'Content-Type': 'application/json'})
    try:
        response = opener.open(request, timeout=8)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise RuntimeError('event bridge response too large')
        return response.status, json.loads(raw or b'{}')


def events(stream):
    raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise RuntimeError('event input exceeds budget')
    text = raw if isinstance(raw, str) else raw.decode('utf-8')
    if not text.strip():
        return
    try:
        value = json.loads(text)
    except ValueError:
        for line in text.splitlines():
            if line.strip():
                yield json.loads(line)
        return
    if isinstance(value, list):
        yield from value
    elif isinstance(value, dict):
        yield value
    else:
        raise ValueError('event input must be a JSON object or array')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--java', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--site', required=True)
    parser.add_argument('--hangar', required=True)
    parser.add_argument('--drone', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--token-env', required=True)
    parser.add_argument('--events', default='-')
    args = parser.parse_args()
    if not args.java.startswith(('http://127.0.0.1:', 'http://localhost:', 'https://')):
        raise SystemExit('Java endpoint must be loopback HTTP or HTTPS')
    token = os.environ.get(args.token_env, '')
    if len(token) < 24:
        raise SystemExit('event token missing')
    binding = Binding(args.site, args.hangar, args.drone, args.task).value()
    opener = build_opener(ProxyHandler({}))
    stream = sys.stdin if args.events == '-' else open(args.events, encoding='utf-8')
    try:
        count = 0
        for event in events(stream):
            if not isinstance(event, dict):
                raise ValueError('each event must be an object')
            body = {'binding': binding, **event}
            status, _ = call(opener, args.java.rstrip('/') + '/flight-v13/events',
                             token, args.project, body)
            if status not in {200, 409}:
                raise RuntimeError('authoritative event rejected')
            count += 1
        print(json.dumps({'status': 'forwarded', 'events': count}, ensure_ascii=False))
    finally:
        if stream is not sys.stdin:
            stream.close()


if __name__ == '__main__':
    main()
