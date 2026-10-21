#!/usr/bin/env python3
"""Cross-language V1.3 stack: real Java fixture + Python facade + built Vue page."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
JAVA_ROOT = Path('/Users/wangxiaolin/PycharmProjects/ktd_backend-real-flight-assistant')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request(opener, url, body, *, token=None, project=None, actor=None,
            csrf=None, method='POST', timeout=12):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if project:
        headers['X-Project-Id'] = project
    if actor:
        headers['X-Actor-Id'] = actor
    if csrf:
        headers['X-V13-CSRF'] = csrf
    req = Request(url, data=json.dumps(body).encode(), headers=headers, method=method)
    try:
        response = opener.open(req, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError('response exceeds test budget')
        return response.status, json.loads(raw or b'{}')


def wait_for_java(opener, java, token, project, actor, binding, timeout=35):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            status, value = request(opener, java + '/flight-v13/state', binding,
                                    token=token, project=project, actor=actor)
            if status == 200 and value.get('status') == 'observed':
                return value
            last = (status, value)
        except Exception as error:
            last = repr(error)
        time.sleep(0.3)
    raise RuntimeError(f'Java fixture did not become ready: {last!r}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ui', type=Path, default=Path(
        '/Users/wangxiaolin/PycharmProjects/lingxi-flight-agent-ui/dist-v13'))
    parser.add_argument('--classpath', type=Path, default=(
        JAVA_ROOT / 'biz-inspection/target/v13-cp.txt'))
    parser.add_argument('--serve-seconds', type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.serve_seconds <= 300:
        parser.error('serve-seconds must be 0..300')
    if not (args.ui / 'v13.html').is_file():
        raise SystemExit('build the V1.3 UI first')
    classpath = (
        str(JAVA_ROOT / 'biz-inspection/target/test-classes') + ':' +
        str(JAVA_ROOT / 'biz-inspection/target/classes') + ':' +
        args.classpath.read_text().strip())
    token = 'v13fixturetoken_' + uuid4().hex
    project = 'project-fixture'
    actor = 'actor-fixture'
    binding = {'siteId': 'site-fixture', 'hangarId': 'hangar-fixture',
               'droneId': 'drone-fixture', 'taskId': 'task-fixture'}
    journal = tempfile.mkdtemp(prefix='flight-v13-stack-')
    java_log = tempfile.TemporaryFile()
    java_port = free_port()
    ui_port = free_port()
    java = f'http://127.0.0.1:{java_port}/api'
    opener = build_opener(ProxyHandler({}))
    java_process = None
    ui_process = None
    signal.alarm(300)
    try:
        java_process = subprocess.Popen([
            'java', '-Xmx384m', '-XX:ActiveProcessorCount=2', '-cp', classpath,
            'com.fan.framework.inspection.flightv13.fixture.FlightV13FixtureApplication',
            '--server.address=127.0.0.1', f'--server.port={java_port}',
            '--lingxi.flight-v13.effects-enabled=true',
            f'--lingxi.flight-v13.effects-token={token}',
            f'--lingxi.flight-v13.journal-dir={journal}',
            '--spring.main.banner-mode=off', '--logging.level.root=ERROR',
        ], stdout=java_log, stderr=java_log)
        try:
            initial = wait_for_java(opener, java, token, project, actor, binding)
        except Exception:
            java_log.seek(0)
            details = java_log.read().decode('utf-8', 'replace')[-12000:]
            raise RuntimeError(f'Java fixture did not become ready; log tail:\n{details}')
        initial_version = initial['state']['version']
        status, no_task_annotation = request(
            opener, java + '/flight-v13/annotation-ai', {
                'binding': {**binding, 'taskId': ''},
                'annotationId': 'target-fixture', 'update': True,
                'algorithms': ['smoke'],
            }, token=token, project=project, actor=actor)
        assert status == 200
        assert {item['id'] for item in no_task_annotation['availableAlgorithms']} == {
            'car', 'smoke'}
        env = os.environ.copy()
        env['V13_JAVA_TOKEN'] = token
        ui_process = subprocess.Popen([
            'python3', str(ROOT / 'tools/v13_ui_server.py'),
            '--dist', str(args.ui), '--java', java,
            '--java-token-env', 'V13_JAVA_TOKEN',
            '--project', project, '--actor', actor,
            '--site', binding['siteId'], '--hangar', binding['hangarId'],
            '--drone', binding['droneId'], '--task', binding['taskId'],
            '--port', str(ui_port),
        ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                health = Request(f'http://127.0.0.1:{ui_port}/healthz', method='GET')
                with opener.open(health, timeout=2) as response:
                    value = json.loads(response.read())
                if value.get('status') == 'isolated-ui':
                    break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError('UI server did not become ready')
        origin = f'http://127.0.0.1:{ui_port}'
        try:
            opener.open(Request(origin + '/api/flight-v13/state', data=b'{}', method='POST',
                                headers={'Content-Type': 'application/json'}), timeout=5)
            raise AssertionError('CSRF-less request unexpectedly succeeded')
        except HTTPError as error:
            assert error.code == 403
        _, boot = request(opener, origin + '/api/flight-v13/bootstrap', {})
        csrf = boot['csrf']

        def ui(route, body):
            status, value = request(opener, origin + '/api/flight-v13/' + route, body,
                                      csrf=csrf)
            if status != 200:
                raise AssertionError((route, status, value))
            return value

        def java_event(event):
            body = {'binding': binding, **event}
            status, value = request(opener, java + '/flight-v13/events', body,
                                    token=token, project=project)
            if status != 200:
                raise AssertionError(('event', status, value))
            return value

        duplicates = ui('targets', {'name': '双塔'})
        assert duplicates['status'] == 'ambiguous'
        assert len(duplicates['candidates']) == 2
        body_target_status, body_target = request(
            opener, origin + '/api/flight-v13/targets', {'name': '立体大楼'}, csrf=csrf)
        assert body_target_status == 409
        assert body_target['code'] == 'TARGET_TYPE_UNSUPPORTED'
        body_ai_status, body_ai = request(
            opener, origin + '/api/flight-v13/annotation-ai',
            {'annotationId': 'target-body', 'update': True, 'algorithms': []}, csrf=csrf)
        assert body_ai_status == 409
        assert body_ai['code'] == 'ANNOTATION_AI_UNSUPPORTED'

        state = ui('state', {})
        assert state['status'] == 'observed' and state['state']['version'] == initial_version
        target = ui('targets', {'name': '1号水塔'})
        assert target['status'] == 'resolved'
        annotation = ui('annotation-ai', {
            'annotationId': 'target-fixture', 'update': True, 'algorithms': ['smoke']})
        assert annotation['algorithms'][0]['id'] == 'smoke'

        java_event({'eventId': 'enter-1', 'type': 'position', 'observedAt': 100,
                    'position': {'longitude': 118.0, 'latitude': 34.0, 'altitude': 50}})
        entered = java_event({'eventId': 'leave-1', 'type': 'position', 'observedAt': 101,
                              'position': {'longitude': 118.01, 'latitude': 34.01,
                                           'altitude': 50}})
        assert not entered['state']['activeAnnotationIds']
        assert not entered['state']['desiredAlgorithms']
        current = ui('state', {})
        ai_preview = ui('messages', {
            'text': '同时开启AI:smoke',
            'expectedVersion': current['state']['version']})
        ai_confirm = ui('confirm', {
            'confirmationId': ai_preview['card']['id'],
            'bindingHash': ai_preview['card']['hash'],
            'requestId': 'request-ai-global-1'})
        assert [item['id'] for item in ai_confirm['state']['globalAlgorithms']] == ['smoke']
        java_event({'eventId': 'enter-2', 'type': 'position', 'observedAt': 102,
                    'position': {'longitude': 118.0, 'latitude': 34.0, 'altitude': 50}})
        retained = java_event({'eventId': 'leave-2', 'type': 'position', 'observedAt': 103,
                               'position': {'longitude': 118.01, 'latitude': 34.01,
                                            'altitude': 50}})
        assert [item['id'] for item in retained['state']['desiredAlgorithms']] == ['smoke']

        preview = ui('messages', {
            'text': '围绕1号水塔绕一圈，半径30米',
            'expectedVersion': retained['state']['version'],
            'targetId': 'target-fixture'})
        assert preview['status'] == 'preview_required'
        assert preview['fence']['status'] == 'clear'
        assert preview['card']['request']['altitudeM'] == 50
        card = preview['card']
        accepted = ui('confirm', {'confirmationId': card['id'], 'bindingHash': card['hash'],
                                  'requestId': 'request-orbit-1'})
        assert accepted['status'] == 'accepted'
        assert accepted['state']['status'] == 'temporary'
        temporary_id = accepted['state']['temporaryId']
        completed = java_event({'eventId': 'temp-done-1', 'type': 'temporary_complete',
                                'temporaryId': temporary_id})
        assert completed['state']['status'] == 'paused'

        java_event({'eventId': 'low-1', 'type': 'low_battery_return'})
        charge = java_event({'eventId': 'charge-1', 'type': 'charging_complete'})
        assert charge['state']['status'] == 'ready_to_resume'
        assert charge['resumeRequired'] is True
        resume = ui('messages', {'text': '恢复航线',
                                 'expectedVersion': charge['state']['version']})
        resumed = ui('confirm', {'confirmationId': resume['card']['id'],
                                 'bindingHash': resume['card']['hash'],
                                 'requestId': 'request-resume-1'})
        assert resumed['state']['status'] == 'running'

        current = ui('state', {})
        stale_preview = ui('messages', {
            'text': '去1号水塔',
            'expectedVersion': current['state']['version'],
            'targetId': 'target-fixture'})
        assert stale_preview['card']['request']['altitudeM'] == 50
        status, _ = request(opener, java + '/fixture/flight-v13/mode',
                            {'mapping': True}, token=token, project=project)
        assert status == 200
        stale_status, stale_confirm = request(opener, origin + '/api/flight-v13/confirm', {
            'confirmationId': stale_preview['card']['id'],
            'bindingHash': stale_preview['card']['hash'],
            'requestId': 'request-stale-version',
        }, csrf=csrf)
        assert stale_status == 409
        assert stale_confirm['code'] == 'STATE_VERSION_CHANGED'
        current = ui('state', {})
        mapping_preview = ui('messages', {
            'text': '去1号水塔',
            'expectedVersion': current['state']['version'],
            'targetId': 'target-fixture'})
        assert any('成果失效' in warning for warning in mapping_preview['card']['warnings'])
        assert mapping_preview['card']['request']['altitudeM'] == 50
        mapping_confirm = ui('confirm', {
            'confirmationId': mapping_preview['card']['id'],
            'bindingHash': mapping_preview['card']['hash'],
            'requestId': 'request-mapping-1'})
        assert mapping_confirm['state']['mappingInvalidated'] is True
        assert mapping_confirm['state']['autoStitching'] is False
        assert mapping_confirm['state']['breakpointValid'] is False
        java_event({'eventId': 'mapping-temp-done', 'type': 'temporary_complete',
                    'temporaryId': mapping_confirm['state']['temporaryId']})
        no_resume = java_event({'eventId': 'mapping-charge', 'type': 'charging_complete'})
        assert no_resume['resumeRequired'] is False
        status, blocked_fence = request(opener, java + '/flight-v13/orbit-check', {
            'binding': binding, 'longitude': 118.003, 'latitude': 34.003,
            'radiusM': 30, 'altitudeM': 50,
        }, token=token, project=project, actor=actor)
        assert status == 200 and blocked_fence['fence']['status'] == 'blocked'

        html = Request(origin + '/v13.html')
        with opener.open(html, timeout=5) as response:
            page = response.read().decode('utf-8')
        assert '<div id="v13-app"' in page
        chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        if not chrome.is_file():
            raise RuntimeError('headless Chrome is required for the V1.3 render check')
        with tempfile.TemporaryDirectory(prefix='flight-v13-chrome-') as profile:
            rendered = subprocess.run([
                str(chrome), '--headless=new', '--disable-gpu', '--no-first-run',
                '--no-default-browser-check', f'--user-data-dir={profile}',
                '--virtual-time-budget=5000', '--dump-dom', origin + '/v13.html',
            ], capture_output=True, text=True, timeout=25)
        if rendered.returncode != 0:
            raise RuntimeError('headless Chrome render failed: '
                               + rendered.stderr[-4000:])
        assert '任务干预与识别控制' in rendered.stdout
        assert 'AI 生命周期' in rendered.stdout
        result = {
            'status': 'passed',
            'checks': [
                'java-controller-http', 'target-resolution', 'annotation-ai-persistence',
                'annotation-ai-without-task', 'csrf-required',
                'ambiguous-target-rejected', 'body-target-rejected',
                'position-enter-leave-lifecycle', 'orbit-fence-clear',
                'global-ai-retains-after-leave', 'orbit-fence-blocked',
                'target-altitude-default', 'stale-version-rejected',
                'preview-confirm-temporary', 'charging-resume',
                'mapping-interruption-invalidation', 'vue-static-page',
                'browser-rendered-vue',
            ],
            'java_port': java_port, 'ui_port': ui_port,
            'java': 'actual-new-controller-with-offline-fixture-dependencies',
            'ui': 'built-v13-html', 'device_effect': 'not_verified',
            'model': 'not-used', 'shared_environment': False,
        }
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.serve_seconds:
            time.sleep(args.serve_seconds)
    finally:
        signal.alarm(0)
        for process in (ui_process, java_process):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for port in (ui_port, java_port):
            with socket.socket() as probe:
                probe.settimeout(1)
                assert probe.connect_ex(('127.0.0.1', port)) != 0
        java_log.close()
        import shutil
        shutil.rmtree(journal, ignore_errors=True)


if __name__ == '__main__':
    main()
