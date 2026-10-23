import json

import pytest

from mcp_jenkins.core import grants

_GRANTS = {
    'pmm-automation': {
        'pmm': {
            'pmm3-nightly-orchestrator': {},
            'pmm3-ha-eks-cleanup': {'require': ['CLUSTER_NAME'], 'deny': {'ACTION': ['DELETE_ALL']}},
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


def test_required_parameter_missing_or_blank_is_refused(grant_env, mocker):
    _token(mocker)
    job = 'pmm3-ha-eks-cleanup'
    assert 'CLUSTER_NAME' in grants.build_denial('pmm', {'fullname': job, 'build_type': 'build'})
    assert grants.build_denial('pmm', {'fullname': job, 'data': {'CLUSTER_NAME': '  '}}) is not None


def test_denied_parameter_value_is_refused(grant_env, mocker):
    _token(mocker)
    data = {'ACTION': 'DELETE_ALL', 'CLUSTER_NAME': 'pmm-ha-test-1'}
    assert 'DELETE_ALL' in grants.build_denial('pmm', {'fullname': 'pmm3-ha-eks-cleanup', 'data': data})


def test_named_cleanup_is_granted(grant_env, mocker):
    _token(mocker)
    data = {'ACTION': 'DELETE_CLUSTER', 'CLUSTER_NAME': 'pmm-ha-test-1'}
    assert grants.build_denial('pmm', {'fullname': 'pmm3-ha-eks-cleanup', 'data': data}) is None


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


def test_malformed_grants_fail_at_load(monkeypatch):
    monkeypatch.setenv('MCP_JENKINS_SERVICE_GRANTS', json.dumps({'bot': {'pmm': {'job': {'deny': 'ACTION'}}}}))
    grants.get_grants.cache_clear()
    with pytest.raises(ValueError):
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
