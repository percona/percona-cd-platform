import pytest
from click.testing import CliRunner

from mcp_jenkins import main
from mcp_jenkins.server import ALLOW_UNAUTHENTICATED_HTTP, OIDC_SETTINGS, http_auth_error

_HTTP = ('sse', 'streamable-http')
_VALUES = {
    'MCP_OIDC_ISSUER': 'https://idp.example/app/',
    'MCP_OIDC_JWKS_URI': 'https://idp.example/jwks/',
    'MCP_OIDC_AUDIENCE': 'jenkins-mcp',
    'MCP_PUBLIC_BASE_URL': 'https://jenkins-mcp.example',
}


@pytest.fixture(autouse=True)
def no_settings(monkeypatch):
    for name in (*OIDC_SETTINGS, ALLOW_UNAUTHENTICATED_HTTP):
        monkeypatch.delenv(name, raising=False)


def _set_all_but(monkeypatch, *missing: str) -> None:
    for name, value in _VALUES.items():
        if name not in missing:
            monkeypatch.setenv(name, value)


@pytest.mark.parametrize('transport', _HTTP)
def test_http_without_settings_is_refused_and_names_every_setting(transport):
    error = http_auth_error(transport, None)

    assert error is not None
    assert all(name in error for name in OIDC_SETTINGS)
    assert ALLOW_UNAUTHENTICATED_HTTP in error


@pytest.mark.parametrize('missing', OIDC_SETTINGS)
@pytest.mark.parametrize('transport', _HTTP)
def test_each_missing_setting_is_named(monkeypatch, transport, missing):
    _set_all_but(monkeypatch, missing)

    error = http_auth_error(transport, None)

    assert error is not None
    assert f'missing {missing}' in error
    assert 'partly set' in error


@pytest.mark.parametrize('missing', OIDC_SETTINGS)
def test_partly_set_is_refused_even_with_the_switch(monkeypatch, missing):
    _set_all_but(monkeypatch, missing)
    monkeypatch.setenv(ALLOW_UNAUTHENTICATED_HTTP, '1')

    assert http_auth_error('streamable-http', None) is not None


def test_empty_value_counts_as_missing(monkeypatch):
    _set_all_but(monkeypatch)
    monkeypatch.setenv('MCP_OIDC_AUDIENCE', '')

    assert 'missing MCP_OIDC_AUDIENCE' in http_auth_error('streamable-http', object())


@pytest.mark.parametrize('transport', _HTTP)
def test_switch_allows_an_open_start_with_a_warning(monkeypatch, mocker, transport):
    monkeypatch.setenv(ALLOW_UNAUTHENTICATED_HTTP, '1')
    warning = mocker.patch('mcp_jenkins.server.logger.warning')

    assert http_auth_error(transport, None) is None
    warning.assert_called_once()
    assert 'WITHOUT authentication' in warning.call_args.args[0]


@pytest.mark.parametrize('value', ['0', 'true', 'yes', ''])
def test_switch_takes_only_the_value_1(monkeypatch, value):
    monkeypatch.setenv(ALLOW_UNAUTHENTICATED_HTTP, value)

    assert http_auth_error('streamable-http', None) is not None


def test_stdio_needs_no_settings():
    assert http_auth_error('stdio', None) is None


@pytest.mark.parametrize('transport', _HTTP)
def test_full_settings_start_with_the_provider(monkeypatch, transport):
    _set_all_but(monkeypatch)

    assert http_auth_error(transport, object()) is None
    assert 'no auth provider' in http_auth_error(transport, None)


def test_main_refuses_http_without_settings(mocker):
    mocker.patch('mcp_jenkins.asyncio')
    mock_mcp = mocker.patch('mcp_jenkins.server.mcp', auth=None)
    error = mocker.patch('mcp_jenkins.logger.error')

    result = CliRunner().invoke(main, ['--transport', 'streamable-http', '--enable-operate'])

    assert result.exit_code == 1
    assert 'MCP_OIDC_ISSUER' in error.call_args.args[0]
    mock_mcp.run_async.assert_not_called()
    mock_mcp.enable.assert_not_called()


def test_main_refuses_http_with_partial_settings_and_the_switch(monkeypatch, mocker):
    _set_all_but(monkeypatch, 'MCP_OIDC_JWKS_URI')
    monkeypatch.setenv(ALLOW_UNAUTHENTICATED_HTTP, '1')
    mocker.patch('mcp_jenkins.asyncio')
    mock_mcp = mocker.patch('mcp_jenkins.server.mcp', auth=None)

    result = CliRunner().invoke(main, ['--transport', 'sse'])

    assert result.exit_code == 1
    mock_mcp.run_async.assert_not_called()


def test_main_stdio_starts_without_settings(mocker):
    mocker.patch('mcp_jenkins.asyncio')
    mock_mcp = mocker.patch('mcp_jenkins.server.mcp', auth=None)

    result = CliRunner().invoke(main, ['--transport', 'stdio', '--read-only'])

    assert result.exit_code == 0
    mock_mcp.run_async.assert_called_once_with(transport='stdio')
