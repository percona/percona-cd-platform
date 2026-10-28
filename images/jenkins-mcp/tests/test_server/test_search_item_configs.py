import inspect
import threading
import time

import pytest
from requests.exceptions import HTTPError

from mcp_jenkins.jenkins.model.item import serialize_item
from mcp_jenkins.server import item

_CONFIG = """<?xml version='1.1' encoding='UTF-8'?>
<flow-definition>
  <authToken>abc123</authToken>
  <script>export UPLOAD_HOST=upload.example</script>
  <hudson.util.Secret>{AQAAABAAAAAQxyz+/=}</hudson.util.Secret>
  <value>UPLOAD_HOST</value>
</flow-definition>"""


def _job(fullname: str) -> object:
    return serialize_item(
        {'_class': 'WorkflowJob', 'name': fullname, 'fullName': fullname, 'url': 'u', 'color': 'blue'}
    )


@pytest.fixture
def mock_jenkins(mocker):
    mock_jenkins = mocker.Mock()
    mocker.patch('mcp_jenkins.server.item.jenkins', return_value=mock_jenkins)
    yield mock_jenkins


def test_is_sync_so_fastmcp_offloads_it():
    assert not inspect.iscoroutinefunction(item.search_item_configs)


def test_returns_matching_lines_only(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job('pmm3-a'), _job('pmm3-b')]
    configs = {'pmm3-a': _CONFIG, 'pmm3-b': '<project/>'}
    mock_jenkins.get_item_config.side_effect = lambda fullname: configs[fullname]

    out = item.search_item_configs(mocker.Mock(), pattern='UPLOAD_HOST', fullname_pattern='^pmm3-')

    assert out['jobs'] == [
        {
            'fullname': 'pmm3-a',
            'matches': [
                {'line_no': 4, 'line': '<script>export UPLOAD_HOST=upload.example</script>'},
                {'line_no': 6, 'line': '<value>UPLOAD_HOST</value>'},
            ],
        }
    ]
    assert (out['jobs_selected'], out['jobs_read'], out['match_count']) == (2, 2, 2)


def test_blanks_secrets_in_matching_lines(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job('j')]
    mock_jenkins.get_item_config.return_value = _CONFIG

    out = item.search_item_configs(mocker.Mock(), pattern='>', fullname_pattern='j')

    text = str(out)
    assert 'abc123' not in text
    assert 'AQAAAB' not in text
    assert '<authToken>[redacted]</authToken>' in text


@pytest.mark.parametrize('pattern', ['.*', 'pmm|.*', '.{0,}'])
def test_rejects_catch_all_job_pattern(mock_jenkins, mocker, pattern):
    with pytest.raises(ValueError, match='must not match every name'):
        item.search_item_configs(mocker.Mock(), pattern='x', fullname_pattern=pattern)
    mock_jenkins.query_items.assert_not_called()


def test_caps_jobs_and_reports_errors_inline(mock_jenkins, mocker):
    mock_jenkins.query_items.return_value = [_job(f'j{i}') for i in range(3)]
    configs = {'j0': HTTPError('403 Forbidden'), 'j1': '<a>x</a>', 'j2': '<a>x</a>'}

    def config(fullname):
        if isinstance(configs[fullname], Exception):
            raise configs[fullname]
        return configs[fullname]

    mock_jenkins.get_item_config.side_effect = config

    out = item.search_item_configs(mocker.Mock(), pattern='x', fullname_pattern='j', limit=2)

    assert out['errors'] == [{'fullname': 'j0', 'error': 'HTTPError: 403 Forbidden'}]
    assert out['jobs'] == [{'fullname': 'j1', 'matches': [{'line_no': 1, 'line': '<a>x</a>'}]}]
    assert out['notes'] == ['3 jobs matched fullname_pattern, read 0 to 1, call again with offset=2 for the rest']

    rest = item.search_item_configs(mocker.Mock(), pattern='x', fullname_pattern='j', limit=2, offset=2)

    assert rest['jobs'] == [{'fullname': 'j2', 'matches': [{'line_no': 1, 'line': '<a>x</a>'}]}]
    assert rest['notes'] == []


def test_caps_matches_per_job(mock_jenkins, mocker):
    mocker.patch.object(item, '_MAX_CONFIG_MATCHES_PER_JOB', 2)
    mock_jenkins.query_items.return_value = [_job('j')]
    mock_jenkins.get_item_config.return_value = 'x\nx\nx'

    out = item.search_item_configs(mocker.Mock(), pattern='x', fullname_pattern='j')

    assert len(out['jobs'][0]['matches']) == 2
    assert out['jobs'][0]['truncated'] is True


def test_time_budget_names_the_resume_offset(mock_jenkins, mocker):
    mocker.patch.object(item, '_CONFIG_SEARCH_SECONDS', 0.2)
    mock_jenkins.query_items.return_value = [_job('fast'), _job('slow')]

    def config(fullname):
        if fullname == 'slow':
            time.sleep(1)
        return '<a>x</a>'

    mock_jenkins.get_item_config.side_effect = config

    out = item.search_item_configs(mocker.Mock(), pattern='x', fullname_pattern='j')

    assert [j['fullname'] for j in out['jobs']] == ['fast']
    assert out['notes'] == ['stopped before slow: time budget spent, call again with offset=1']


def test_keeps_at_most_the_worker_count_in_flight_per_call(mock_jenkins, mocker):
    in_flight = peak = 0
    lock = threading.Lock()

    def config(fullname):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.01)
        with lock:
            in_flight -= 1
        return '<a>x</a>'

    submitted = mocker.spy(item._config_pool, 'submit')
    mock_jenkins.query_items.return_value = [_job(f'j{i}') for i in range(40)]
    mock_jenkins.get_item_config.side_effect = config

    out = item.search_item_configs(mocker.Mock(), pattern='x', fullname_pattern='j')

    assert out['jobs_read'] == 40
    assert peak <= item._CONFIG_WORKERS
    assert submitted.call_count == 40


def test_match_budget_stop_cancels_queued_reads(mock_jenkins, mocker):
    mocker.patch.object(item, '_MAX_CONFIG_MATCHES', 1)
    mock_jenkins.query_items.return_value = [_job(f'j{i}') for i in range(30)]
    mock_jenkins.get_item_config.return_value = '<a>x</a>'
    submitted = mocker.spy(item._config_pool, 'submit')

    out = item.search_item_configs(mocker.Mock(), pattern='x', fullname_pattern='j')

    assert out['match_count'] == 1
    assert out['notes'] == ['stopped before j1: match budget spent, call again with offset=1']
    assert submitted.call_count <= 1 + item._CONFIG_WORKERS


@pytest.mark.parametrize(
    'line',
    ['<privateKey>-----BEGIN RSA-----</privateKey>', '<passwd>hunter2</passwd>', '<awsAccessKey>AKIA</awsAccessKey>'],
)
def test_redacts_private_key_passwd_and_access_key(line):
    assert '>[redacted]<' in item._redact(line)
