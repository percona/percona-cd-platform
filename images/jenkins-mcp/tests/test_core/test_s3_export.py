import pytest

from mcp_jenkins.core import s3_export


@pytest.fixture
def _bucket_env(monkeypatch):
    monkeypatch.setenv('MCP_S3_BUCKET', 'test-bucket')
    for fn in (s3_export._bucket, s3_export._region, s3_export._presign_ttl):
        fn.cache_clear()
    yield
    for fn in (s3_export._bucket, s3_export._region, s3_export._presign_ttl):
        fn.cache_clear()


def test_bucket_unset_raises(monkeypatch):
    monkeypatch.delenv('MCP_S3_BUCKET', raising=False)
    s3_export._bucket.cache_clear()
    with pytest.raises(RuntimeError, match='MCP_S3_BUCKET'):
        s3_export._bucket()
    s3_export._bucket.cache_clear()


@pytest.mark.parametrize(('raw', 'expected'), [('99999', 21600), ('3600', 3600), ('-5', 1), ('1', 1)])
def test_presign_ttl_bounds(monkeypatch, raw, expected):
    monkeypatch.setenv('MCP_S3_PRESIGN_TTL', raw)
    s3_export._presign_ttl.cache_clear()
    assert s3_export._presign_ttl() == expected
    s3_export._presign_ttl.cache_clear()


def test_presign_ttl_default(monkeypatch):
    monkeypatch.delenv('MCP_S3_PRESIGN_TTL', raising=False)
    s3_export._presign_ttl.cache_clear()
    assert s3_export._presign_ttl() == 21600
    s3_export._presign_ttl.cache_clear()


def test_presign_ttl_non_int_raises(monkeypatch):
    monkeypatch.setenv('MCP_S3_PRESIGN_TTL', 'abc')
    s3_export._presign_ttl.cache_clear()
    with pytest.raises(ValueError, match='positive integer'):
        s3_export._presign_ttl()
    s3_export._presign_ttl.cache_clear()


def test_safe_filename():
    assert s3_export.safe_filename('e"vil;\r\n.txt') == 'e_vil___.txt'
    assert s3_export.safe_filename('a/b/c.tar.gz') == 'c.tar.gz'
    assert s3_export.safe_filename('') == 'artifact.bin'


def test_build_key_scheme():
    key = s3_export.build_key('log', 'pxc', 'PXC/pxc-8.0/build', 42, 'x.txt')
    assert key.startswith('exports/log/pxc/PXC-pxc-8.0-build/42/')
    assert key.endswith('/x.txt')


def test_upload_stream_ok(_bucket_env, mocker):
    client = mocker.Mock()
    mocker.patch('mcp_jenkins.core.s3_export._s3_client', return_value=client)

    def fake_upload(reader, bucket, key, **kwargs: object):
        reader.read(8)

    client.upload_fileobj.side_effect = fake_upload
    client.head_object.return_value = {'ContentLength': 8}
    response = mocker.Mock()
    response.raw.read.side_effect = [b'12345678', b'']
    response.headers = {'Content-Length': '8'}

    size = s3_export.upload_stream(response, key='k', content_type='text/plain', filename='f.txt')

    assert size == 8
    client.delete_object.assert_not_called()
    _, kwargs = client.upload_fileobj.call_args
    assert kwargs['ExtraArgs']['ContentType'] == 'text/plain'
    assert kwargs['ExtraArgs']['ContentDisposition'] == 'attachment; filename="f.txt"'
    assert kwargs['Config'].use_threads is False


def test_upload_stream_truncated_deletes_and_raises(_bucket_env, mocker):
    client = mocker.Mock()
    mocker.patch('mcp_jenkins.core.s3_export._s3_client', return_value=client)

    def fake_upload(reader, bucket, key, **kwargs: object):
        reader.read(8)

    client.upload_fileobj.side_effect = fake_upload
    client.head_object.return_value = {'ContentLength': 8}
    response = mocker.Mock()
    response.raw.read.side_effect = [b'12345678', b'']
    response.headers = {'Content-Length': '100'}

    with pytest.raises(RuntimeError, match='integrity'):
        s3_export.upload_stream(response, key='k', content_type='text/plain', filename='f.txt')

    client.delete_object.assert_called_once_with(Bucket='test-bucket', Key='k')


def test_presign_response_shape(_bucket_env, mocker):
    client = mocker.Mock()
    client.generate_presigned_url.return_value = 'https://signed-url'
    mocker.patch('mcp_jenkins.core.s3_export._s3_client', return_value=client)

    result = s3_export.presign_response(key='k', size=10, content_type='text/plain', filename='f.txt')

    assert result['url'] == 'https://signed-url'
    assert result['bucket'] == 'test-bucket'
    assert result['key'] == 'k'
    assert result['size_bytes'] == 10
    assert result['requested_ttl_seconds'] == 21600
    assert 'bearer' in result['expiry_caveat']
    client.generate_presigned_url.assert_called_once_with(
        'get_object', Params={'Bucket': 'test-bucket', 'Key': 'k'}, ExpiresIn=21600
    )
