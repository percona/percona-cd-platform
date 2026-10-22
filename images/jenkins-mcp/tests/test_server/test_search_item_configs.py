import inspect

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
    mock_jenkins.get_item_config.side_effect = [_CONFIG, '<project/>']

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
    mock_jenkins.get_item_config.side_effect = [HTTPError('403 Forbidden'), '<a>x</a>']

    out = item.search_item_configs(mocker.Mock(), pattern='x', fullname_pattern='j', limit=2)

    assert out['errors'] == [{'fullname': 'j0', 'error': 'HTTPError: 403 Forbidden'}]
    assert out['jobs'] == [{'fullname': 'j1', 'matches': [{'line_no': 1, 'line': '<a>x</a>'}]}]
    assert out['notes'] == ['3 jobs matched fullname_pattern, only the first 2 were read']


def test_caps_matches_per_job(mock_jenkins, mocker):
    mocker.patch.object(item, '_MAX_CONFIG_MATCHES_PER_JOB', 2)
    mock_jenkins.query_items.return_value = [_job('j')]
    mock_jenkins.get_item_config.return_value = 'x\nx\nx'

    out = item.search_item_configs(mocker.Mock(), pattern='x', fullname_pattern='j')

    assert len(out['jobs'][0]['matches']) == 2
    assert out['jobs'][0]['truncated'] is True
