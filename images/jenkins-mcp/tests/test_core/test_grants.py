import json

import pytest

from mcp_jenkins.core import grants

_CLEANUP = 'pmm3-ha-eks-cleanup'
_GRANTS = {
    'pmm-automation': {
        'pmm': {
            'pmm3-nightly-orchestrator': {},
            _CLEANUP: {
                'require': ['ACTION', 'CLUSTER_NAME'],
                'allow': {'ACTION': ['LIST_ONLY', 'DELETE_CLUSTER']},
                'pattern': {'CLUSTER_NAME': 'pmm-ha-[a-z0-9-]{1,40}'},
            },
        }
    }
}


@pytest.fixture
def grant_env(monkeypatch):
    monkeypatch.setenv('MCP_JENKINS_SERVICE_GRANTS', json.dumps(_GRANTS))
    grants.get_grants.cache_clear()
    yield
    grants.get_grants.cache_clear()


def _token(mocker, username='pmm-automation', groups=None):
    claims = {
        'preferred_username': username,
        'groups': ['jenkins-mcp-service-accounts', 'jenkins-mcp-master-pmm'] if groups is None else groups,
    }
    return mocker.patch('mcp_jenkins.core.grants.get_access_token', return_value=mocker.Mock(claims=claims))


def _cleanup(data, build_type='buildWithParameters'):
    return grants.build_denial('pmm', {'fullname': _CLEANUP, 'build_type': build_type, 'data': data})


_NAMED = {'ACTION': 'DELETE_CLUSTER', 'CLUSTER_NAME': 'pmm-ha-test-1'}


def test_listed_job_without_rules_is_granted(grant_env, mocker):
    _token(mocker)
    assert grants.build_denial('pmm', {'fullname': 'pmm3-nightly-orchestrator', 'build_type': 'build'}) is None


def test_unlisted_job_is_refused(grant_env, mocker):
    _token(mocker)
    reason = grants.build_denial('pmm', {'fullname': 'pmm3-ha-eks', 'build_type': 'build'})
    assert 'not granted' in reason


def test_other_master_is_refused(grant_env, mocker):
    _token(mocker)
    assert grants.build_denial('ps80', {'fullname': 'pmm3-nightly-orchestrator'}) is not None


def test_unresolved_master_is_refused(grant_env, mocker):
    _token(mocker)
    assert grants.build_denial(None, {'fullname': 'pmm3-nightly-orchestrator'}) is not None


@pytest.mark.parametrize('fullname', [['pmm3-nightly-orchestrator'], {'a': 1}, None, 7])
def test_non_string_fullname_is_refused_not_raised(grant_env, mocker, fullname):
    _token(mocker)
    assert grants.build_denial('pmm', {'fullname': fullname}) is not None


def test_non_string_master_is_refused_not_raised(grant_env, mocker):
    _token(mocker)
    assert grants.build_denial({'pmm': 1}, {'fullname': 'pmm3-nightly-orchestrator'}) is not None


def test_named_cleanup_is_granted(grant_env, mocker):
    _token(mocker)
    assert _cleanup(_NAMED) is None


def test_required_parameter_missing_or_blank_is_refused(grant_env, mocker):
    _token(mocker)
    assert 'CLUSTER_NAME' in _cleanup({'ACTION': 'DELETE_CLUSTER'})
    assert _cleanup({'ACTION': 'DELETE_CLUSTER', 'CLUSTER_NAME': '  '}) is not None
    # Omitting ACTION would let Jenkins apply its default, so a guarded parameter is required too.
    assert 'ACTION' in _cleanup({'CLUSTER_NAME': 'pmm-ha-test-1'})


def test_value_outside_the_allow_list_is_refused(grant_env, mocker):
    _token(mocker)
    for action in ('DELETE_ALL', 'delete_all', ' DELETE_CLUSTER'):
        assert 'allows ACTION' in _cleanup({**_NAMED, 'ACTION': action})


@pytest.mark.parametrize(
    'name', ['pmm-ha-test-1,pmm-ha-test-2', 'other-cluster', 'pmm-ha-test-1\n', 'pmm-ha-' + 'x' * 41]
)
def test_name_outside_the_pattern_is_refused(grant_env, mocker, name):
    _token(mocker)
    assert 'CLUSTER_NAME' in _cleanup({**_NAMED, 'CLUSTER_NAME': name})


@pytest.mark.parametrize('value', [['DELETE_ALL'], ('DELETE_ALL',), None, True, 1, {'v': 'DELETE_ALL'}])
def test_non_string_parameter_value_is_refused(grant_env, mocker, value):
    # requests form-encodes a list as a repeated field, which Jenkins reads as the plain value.
    _token(mocker)
    assert 'string parameter values only' in _cleanup({**_NAMED, 'ACTION': value})


def test_non_string_value_is_refused_on_a_rule_free_job(grant_env, mocker):
    _token(mocker)
    args = {'fullname': 'pmm3-nightly-orchestrator', 'build_type': 'buildWithParameters', 'data': {'X': ['a']}}
    assert grants.build_denial('pmm', args) is not None


def test_rule_bearing_job_refuses_the_build_endpoint(grant_env, mocker):
    # /build reads parameters from a submitted `json` form field that the rules never see.
    _token(mocker)
    override = json.dumps({'parameter': [{'name': 'ACTION', 'value': 'DELETE_ALL'}]})
    assert 'buildWithParameters' in _cleanup({**_NAMED, 'json': override}, build_type='build')
    assert 'buildWithParameters' in _cleanup(_NAMED, build_type='build')


def test_data_that_is_not_a_dict_is_refused(grant_env, mocker):
    _token(mocker)
    assert _cleanup('ACTION=DELETE_ALL') is not None


def test_human_with_a_granted_username_is_refused(grant_env, mocker):
    # Only members of the service-accounts group are looked up, a human cannot borrow a grant by name.
    _token(mocker, groups=['jenkins-mcp-master-pmm'])
    assert grants.build_denial('pmm', {'fullname': 'pmm3-nightly-orchestrator'}) is not None


def test_non_list_groups_claim_is_refused(grant_env, mocker):
    _token(mocker, groups='jenkins-mcp-service-accounts')
    assert grants.build_denial('pmm', {'fullname': 'pmm3-nightly-orchestrator'}) is not None


def test_service_account_without_grants_is_refused(grant_env, mocker):
    _token(mocker, username='other-bot')
    assert grants.build_denial('pmm', {'fullname': 'pmm3-nightly-orchestrator'}) is not None


def test_no_token_is_refused(grant_env, mocker):
    mocker.patch('mcp_jenkins.core.grants.get_access_token', return_value=None)
    assert grants.build_denial('pmm', {'fullname': 'pmm3-nightly-orchestrator'}) is not None


def test_unset_env_grants_nothing(monkeypatch, mocker):
    monkeypatch.delenv('MCP_JENKINS_SERVICE_GRANTS', raising=False)
    grants.get_grants.cache_clear()
    _token(mocker)
    assert grants.build_denial('pmm', {'fullname': 'pmm3-nightly-orchestrator'}) is not None
    grants.get_grants.cache_clear()


@pytest.mark.parametrize(
    'rule',
    [
        {'deny': {'ACTION': ['DELETE_ALL']}},  # unknown key (typo or retired rule)
        {'allow': {'ACTION': 'DELETE_CLUSTER'}},  # a bare string, not a list
        {'pattern': {'CLUSTER_NAME': '('}},  # regex that does not compile
    ],
)
def test_malformed_grants_fail_at_load(monkeypatch, rule):
    monkeypatch.setenv('MCP_JENKINS_SERVICE_GRANTS', json.dumps({'bot': {'pmm': {'job': rule}}}))
    grants.get_grants.cache_clear()
    with pytest.raises(ValueError, match='malformed'):
        grants.get_grants()
    grants.get_grants.cache_clear()


@pytest.mark.asyncio
async def test_lifespan_refuses_to_start_on_malformed_grants(monkeypatch):
    from mcp_jenkins.core.lifespan import lifespan

    monkeypatch.setenv('MCP_JENKINS_SERVICE_GRANTS', '{not json')
    grants.get_grants.cache_clear()
    with pytest.raises(ValueError, match='malformed'):
        async with lifespan(None):
            pass
    grants.get_grants.cache_clear()
