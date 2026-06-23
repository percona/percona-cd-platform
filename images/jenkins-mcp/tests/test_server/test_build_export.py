import pytest

from mcp_jenkins.server import build_export


@pytest.fixture
def mock_jenkins(mocker):
    mj = mocker.Mock()
    mocker.patch('mcp_jenkins.server.build_export.jenkins', return_value=mj)
    mocker.patch('mcp_jenkins.server.build_export._selected_master', return_value='pxc')
    mocker.patch('mcp_jenkins.server.build_export._emit_export_audit')
    return mj


@pytest.fixture
def mock_s3(mocker):
    upload = mocker.patch('mcp_jenkins.server.build_export.s3_export.upload_stream', return_value=123)
    presign = mocker.patch(
        'mcp_jenkins.server.build_export.s3_export.presign_response',
        return_value={'url': 'https://signed', 'key': 'k', 'size_bytes': 123},
    )
    return {'upload': upload, 'presign': presign}


@pytest.mark.asyncio
async def test_export_build_log_uses_last_build(mock_jenkins, mock_s3, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 7
    mock_jenkins.stream_build_console_output.return_value = mocker.MagicMock()

    result = await build_export.export_build_log(mocker.Mock(), fullname='PXC/build')

    assert result == {'url': 'https://signed', 'key': 'k', 'size_bytes': 123}
    mock_jenkins.stream_build_console_output.assert_called_once_with(fullname='PXC/build', number=7)
    mock_s3['upload'].assert_called_once()
    mock_s3['presign'].assert_called_once()


@pytest.mark.asyncio
async def test_export_build_log_explicit_number(mock_jenkins, mock_s3, mocker):
    mock_jenkins.stream_build_console_output.return_value = mocker.MagicMock()

    await build_export.export_build_log(mocker.Mock(), fullname='PXC/build', number=3)

    mock_jenkins.stream_build_console_output.assert_called_once_with(fullname='PXC/build', number=3)
    mock_jenkins.get_item.assert_not_called()


@pytest.mark.asyncio
async def test_export_build_log_no_builds_raises(mock_jenkins, mock_s3, mocker):
    mock_jenkins.get_item.return_value.lastBuild = None

    with pytest.raises(ValueError, match='No build found'):
        await build_export.export_build_log(mocker.Mock(), fullname='PXC/build')


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['../etc/passwd', '%2e%2e/x', '/abs', 'a/../../b', 'a\\b', '', 'x\x00y'])
async def test_export_build_artifact_rejects_unsafe_path(mock_jenkins, mock_s3, mocker, bad):
    with pytest.raises(ValueError):  # noqa: PT011
        await build_export.export_build_artifact(mocker.Mock(), fullname='J', relative_path=bad)
    mock_jenkins.stream_build_artifact.assert_not_called()


@pytest.mark.asyncio
async def test_export_build_artifact_happy(mock_jenkins, mock_s3, mocker):
    mock_jenkins.get_item.return_value.lastBuild.number = 9
    mock_jenkins.stream_build_artifact.return_value = mocker.MagicMock()

    result = await build_export.export_build_artifact(mocker.Mock(), fullname='J', relative_path='reports/out.txt')

    assert result['url'] == 'https://signed'
    mock_jenkins.stream_build_artifact.assert_called_once_with(fullname='J', number=9, relative_path='reports/out.txt')
    _, kwargs = mock_s3['upload'].call_args
    assert kwargs['filename'] == 'out.txt'
    assert kwargs['content_type'] == 'text/plain'
