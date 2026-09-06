from click.testing import CliRunner

from mcp_jenkins import main


def test_main_stdio(mocker):
    mocker.patch('mcp_jenkins.asyncio')
    mock_mcp = mocker.patch('mcp_jenkins.server.mcp')

    CliRunner().invoke(main, ['--transport', 'stdio'])

    mock_mcp.run_async.assert_called_once_with(transport='stdio')


def test_main_sse(mocker):
    mocker.patch('mcp_jenkins.asyncio')
    mock_mcp = mocker.patch('mcp_jenkins.server.mcp')

    CliRunner().invoke(main, ['--transport', 'sse', '--host', '127.0.0.1', '--port', '9887'])
    mock_mcp.run_async.assert_called_once_with(transport='sse', host='127.0.0.1', port=9887)


def test_main_streamable_http(mocker):
    mocker.patch('mcp_jenkins.asyncio')
    mock_mcp = mocker.patch('mcp_jenkins.server.mcp')

    CliRunner().invoke(
        main,
        ['--transport', 'streamable-http', '--host', '127.0.0.1', '--port', '9887'],
    )
    mock_mcp.run_async.assert_called_once_with(transport='streamable-http', host='127.0.0.1', port=9887)


def test_main(mocker):
    mocker.patch('mcp_jenkins.asyncio')
    mock_mcp = mocker.patch('mcp_jenkins.server.mcp')

    CliRunner().invoke(
        main,
        [
            '--transport',
            'stdio',
            '--jenkins-master',
            'ps80',
            '--read-only',
        ],
    )

    mock_mcp.run_async.assert_called_once_with(transport='stdio')
    mock_mcp.enable.assert_called_once_with(tags={'read'}, only=True)


def test_main_default_mode_fails_closed_to_read_only(mocker):
    # No mode flag must apply the read-tags-only filter. Without it every tool stays registered,
    # including the RCE-class run_groovy_script. A forgotten flag must not silently expose writes.
    mocker.patch('mcp_jenkins.asyncio')
    mock_mcp = mocker.patch('mcp_jenkins.server.mcp')
    mock_logger = mocker.patch('mcp_jenkins.logger')

    result = CliRunner().invoke(main, ['--transport', 'stdio'])

    assert result.exit_code == 0
    mock_mcp.enable.assert_called_once_with(tags={'read'}, only=True)
    mock_mcp.run_async.assert_called_once_with(transport='stdio')
    mock_logger.warning.assert_called_once()  # warned that no mode flag was given


def test_main_both_mode_flags_are_mutually_exclusive(mocker):
    # --read-only + --enable-operate is a contradiction; refuse to start rather than pick one.
    mocker.patch('mcp_jenkins.asyncio')
    mock_mcp = mocker.patch('mcp_jenkins.server.mcp')

    result = CliRunner().invoke(main, ['--transport', 'stdio', '--read-only', '--enable-operate'])

    assert result.exit_code != 0
    mock_mcp.enable.assert_not_called()  # no tools enabled when the flags conflict
    mock_mcp.run_async.assert_not_called()  # server never starts
