import sys

import pytest

from sf.models import list_native_models


@pytest.fixture
def native_cli(tmp_path, monkeypatch):
    fake = tmp_path / 'models'
    fake.write_text(f'''#!{sys.executable}
import json, sys, os, time
for line in sys.stdin:
    req = json.loads(line)
    mode = os.environ.get('MODEL_TEST_MODE', 'success')
    if mode == 'timeout':
        time.sleep(10)
    if mode == 'malformed':
        print('not json', flush=True)
        continue
    if mode == 'exit':
        print('catalog unavailable', file=sys.stderr, flush=True)
        sys.exit(1)
    if 'request_id' in req:
        assert req['request']['subtype'] == 'initialize'
        response = {{'subtype': 'success', 'request_id': req['request_id'],
                    'response': {{'models': [{{'value': 'sonnet'}}, {{'value': 'opus'}}]}}}}
        if mode == 'empty':
            response['response']['models'] = []
        if mode == 'error':
            response = {{'subtype': 'error', 'request_id': req['request_id'], 'error': 'not authenticated'}}
        print(json.dumps({{'type': 'control_response', 'response': response}}), flush=True)
    elif req.get('method') == 'initialize':
        print(json.dumps({{'id': req['id'], 'result': {{}}}}), flush=True)
    elif req.get('method') == 'model/list':
        assert req['params'].get('includeHidden') is not True
        cursor = req['params'].get('cursor')
        result = {{'data': [{{'id': 'opaque', 'model': 'gpt-test-2' if cursor else 'gpt-test'}}],
                  'nextCursor': None if cursor else 'page2'}}
        if mode == 'empty':
            result = {{'data': [], 'nextCursor': None}}
        print(json.dumps({{'method': 'notice'}}), flush=True)
        msg = {{'id': req['id'], 'result': result}}
        if mode == 'error':
            msg = {{'id': req['id'], 'error': {{'message': 'not authenticated'}}}}
        print(json.dumps(msg), flush=True)
''')
    fake.chmod(0o755)
    monkeypatch.setenv('SF_CODEX', str(fake))
    monkeypatch.setenv('SF_CLAUDE', str(fake))
    return fake


@pytest.mark.parametrize('harness,expected', [('codex', ['gpt-test', 'gpt-test-2']),
                                             ('claude-code', ['sonnet', 'opus'])])
def test_native_catalog(native_cli, harness, expected):
    assert list_native_models(harness) == (expected, None)


@pytest.mark.parametrize('harness', ['codex', 'claude-code'])
@pytest.mark.parametrize('mode', ['error', 'malformed', 'exit', 'timeout', 'empty'])
def test_native_discovery_failure(native_cli, monkeypatch, harness, mode):
    monkeypatch.setenv('MODEL_TEST_MODE', mode)
    # Only the timeout case tests a short deadline; other cases must reach their intended error.
    timeout = 0.2 if mode == 'timeout' else 2
    models, error = list_native_models(harness, timeout=timeout)
    assert models == []
    assert harness in error
    if mode == 'timeout':
        assert 'timed out' in error
    else:
        assert 'timed out' not in error
    if mode == 'error':
        assert 'not authenticated' in error
    if mode == 'exit':
        assert 'catalog unavailable' in error


def test_missing_binary(monkeypatch):
    monkeypatch.setenv('SF_CODEX', '/does-not-exist/sf-codex')
    models, error = list_native_models('codex')
    assert models == [] and 'No such file' in error
