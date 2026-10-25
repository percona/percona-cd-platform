import io
import tarfile
from contextlib import contextmanager

import pytest

from mcp_jenkins.server import build_artifact_search as bas


def _targz(members: dict[str, bytes], *, specials: list[tarfile.TarInfo] | None = None) -> bytes:
    """Build an in-memory .tar.gz from {name: bytes}, plus any special TarInfos (symlink/etc)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for info in specials or []:
            tf.addfile(info)
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, *, raw_bytes: bytes = b'', lines: list[str] | None = None) -> None:
        self.raw = io.BytesIO(raw_bytes)
        self._lines = lines or []
        self.headers: dict = {}

    def iter_lines(self, decode_unicode: bool = False):  # noqa: FBT001, FBT002
        yield from self._lines

    def close(self) -> None:
        pass


@contextmanager
def _stream(response: _FakeResponse):
    yield response


@pytest.fixture
def mock_jenkins(mocker):
    mc = mocker.Mock()
    mocker.patch('mcp_jenkins.server.build_artifact_search.jenkins', return_value=mc)
    return mc


def _set_stream(mock_jenkins, response: _FakeResponse) -> None:
    mock_jenkins.stream_build_artifact.return_value = _stream(response)


def _plain(*lines: str) -> _FakeResponse:
    """A plain (non-tar) artifact whose raw bytes are the given newline-terminated lines."""
    return _FakeResponse(raw_bytes=('\n'.join(lines) + '\n').encode())


# ---- grep (plain artifact) ----
@pytest.mark.asyncio
async def test_grep_plain_matches_with_context(mock_jenkins, mocker):
    _set_stream(mock_jenkins, _plain('noise', 'ERROR boom', 'after1', 'after2', 'ERROR two'))
    out = bas.grep_build_artifact(
        mocker.Mock(), fullname='j', relative_path='x.log', pattern='ERROR', number=1, context_lines=1
    )
    assert out['match_count'] == 2
    assert out['matches'][0]['line'] == 'ERROR boom'
    assert out['matches'][0]['before'] == ['noise']
    assert out['matches'][0]['after'] == ['after1']
    assert out['truncated'] is False
    assert out['limit_hit'] is False


@pytest.mark.asyncio
async def test_grep_max_matches_limit_hit(mock_jenkins, mocker):
    _set_stream(mock_jenkins, _plain(*[f'ERROR {i}' for i in range(10)]))
    out = bas.grep_build_artifact(
        mocker.Mock(), fullname='j', relative_path='x.log', pattern='ERROR', number=1, max_matches=3
    )
    assert out['match_count'] == 3
    assert out['limit_hit'] is True


@pytest.mark.asyncio
async def test_grep_truncates_long_line(mock_jenkins, mocker, monkeypatch):
    monkeypatch.setattr(bas, '_MAX_LINE_LEN', 5)
    _set_stream(mock_jenkins, _plain('ERRORxxxxxxxxxx'))
    out = bas.grep_build_artifact(mocker.Mock(), fullname='j', relative_path='x.log', pattern='ERROR', number=1)
    assert out['matches'][0]['line'] == 'ERROR'  # truncated to MAX_LINE_LEN at read time


@pytest.mark.asyncio
async def test_grep_plain_no_newline_blob_is_bounded(mock_jenkins, mocker, monkeypatch):
    # A multi-"MB" single line with no newline must NOT buffer past the line cap (HIGH-1 from review).
    monkeypatch.setattr(bas, '_MAX_LINE_LEN', 10)
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=b'ERROR' + b'x' * 100000))  # no newline
    out = bas.grep_build_artifact(mocker.Mock(), fullname='j', relative_path='x.log', pattern='ERROR', number=1)
    assert out['match_count'] == 1
    assert len(out['matches'][0]['line']) == 10  # capped, not 100005


@pytest.mark.asyncio
async def test_grep_regex_opt_in_and_invalid(mock_jenkins, mocker):
    _set_stream(mock_jenkins, _plain('fail: 42'))
    out = bas.grep_build_artifact(
        mocker.Mock(), fullname='j', relative_path='x.log', pattern=r'fail: \d+', number=1, literal=False
    )
    assert out['match_count'] == 1
    with pytest.raises(ValueError, match='invalid regex'):
        bas.grep_build_artifact(
            mocker.Mock(), fullname='j', relative_path='x.log', pattern='(unclosed', number=1, literal=False
        )


@pytest.mark.asyncio
async def test_grep_inside_tar_member(mock_jenkins, mocker):
    tgz = _targz({'work/main.dd_upgrade.log': b'CURRENT_TEST: main.dd_upgrade\nok\nERROR: AddressSanitizer\n'})
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=tgz))
    out = bas.grep_build_artifact(
        mocker.Mock(),
        fullname='j',
        relative_path='work/results/x-mtr_logs-5.tar.gz',
        pattern='AddressSanitizer',
        archive_member='work/main.dd_upgrade.log',
        number=1,
    )
    assert out['match_count'] == 1
    assert 'AddressSanitizer' in out['matches'][0]['line']


# ---- list ----
@pytest.mark.asyncio
async def test_list_archive_glob(mock_jenkins, mocker):
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=_targz({'a/x.log': b'1', 'a/y.txt': b'22', 'b/z.log': b'333'})))
    out = await bas.list_archive_artifact(mocker.Mock(), fullname='j', relative_path='a.tar.gz', glob='*.log', number=1)
    assert sorted(e['name'] for e in out['entries']) == ['a/x.log', 'b/z.log']
    assert {e['name']: e['size'] for e in out['entries']}['b/z.log'] == 3


@pytest.mark.asyncio
async def test_list_archive_max_entries_truncates(mock_jenkins, mocker):
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=_targz({f'f{i}.log': b'.' for i in range(5)})))
    out = await bas.list_archive_artifact(
        mocker.Mock(), fullname='j', relative_path='a.tar.gz', glob='*.log', number=1, max_entries=2
    )
    assert len(out['entries']) == 2
    assert out['truncated'] is True


@pytest.mark.asyncio
async def test_list_rejects_non_archive(mock_jenkins, mocker):
    with pytest.raises(ValueError, match='must be a .tar.gz'):
        await bas.list_archive_artifact(mocker.Mock(), fullname='j', relative_path='plain.log', number=1)


# ---- extract ----
@pytest.mark.asyncio
async def test_extract_inline_utf8(mock_jenkins, mocker):
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=_targz({'a/x.log': b'hello world'})))
    out = await bas.extract_archive_artifact(
        mocker.Mock(), fullname='j', relative_path='a.tar.gz', member='a/x.log', number=1
    )
    assert out == {'member': 'a/x.log', 'size_bytes': 11, 'encoding': 'utf-8', 'content': 'hello world'}


@pytest.mark.asyncio
async def test_extract_inline_base64_for_binary(mock_jenkins, mocker):
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=_targz({'a/blob.bin': b'\xff\xfe\x00'})))
    out = await bas.extract_archive_artifact(
        mocker.Mock(), fullname='j', relative_path='a.tar.gz', member='a/blob.bin', number=1
    )
    assert out['encoding'] == 'base64'


@pytest.mark.asyncio
async def test_extract_large_goes_to_s3(mock_jenkins, mocker):
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=_targz({'big.bin': b'X' * 100})))
    mocker.patch('mcp_jenkins.server.build_artifact_search._selected_master', return_value='ps80')
    mocker.patch('mcp_jenkins.server.build_artifact_search._emit_export_audit')
    mocker.patch('mcp_jenkins.server.build_artifact_search.s3_export.safe_filename', return_value='big.bin')
    mocker.patch('mcp_jenkins.server.build_artifact_search.s3_export.build_key', return_value='exports/archive/k')
    mocker.patch('mcp_jenkins.server.build_artifact_search.s3_export.upload_bytes', return_value=100)
    mocker.patch(
        'mcp_jenkins.server.build_artifact_search.s3_export.presign_response',
        return_value={'url': 'https://signed', 'bucket': 'b', 'key': 'exports/archive/k', 'size_bytes': 100},
    )
    out = await bas.extract_archive_artifact(
        mocker.Mock(), fullname='j', relative_path='a.tar.gz', member='big.bin', number=1, max_inline_bytes=4
    )
    assert out['url'] == 'https://signed'
    assert out['member'] == 'big.bin'


@pytest.mark.asyncio
async def test_extract_unknown_member_raises(mock_jenkins, mocker):
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=_targz({'a/x.log': b'hi'})))
    with pytest.raises(ValueError, match='member not found'):
        await bas.extract_archive_artifact(
            mocker.Mock(), fullname='j', relative_path='a.tar.gz', member='a/missing.log', number=1
        )


# ---- security gates ----
@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['../escape', '/etc/passwd', 'a\\b', 'ok/../../x', 'evil"; x="y'])
async def test_extract_rejects_unsafe_member(mock_jenkins, mocker, bad):
    with pytest.raises(ValueError, match='unsafe member|control char'):
        await bas.extract_archive_artifact(mocker.Mock(), fullname='j', relative_path='a.tar.gz', member=bad, number=1)


@pytest.mark.asyncio
async def test_extract_rejects_symlink_member(mock_jenkins, mocker):
    link = tarfile.TarInfo(name='evil-link')
    link.type = tarfile.SYMTYPE
    link.linkname = '/etc/passwd'
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=_targz({}, specials=[link])))
    with pytest.raises(ValueError, match='not a regular file'):
        await bas.extract_archive_artifact(
            mocker.Mock(), fullname='j', relative_path='a.tar.gz', member='evil-link', number=1
        )


@pytest.mark.asyncio
async def test_extract_rejects_oversized_member(mock_jenkins, mocker, monkeypatch):
    monkeypatch.setattr(bas, '_MAX_MEMBER_BYTES', 5)
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=_targz({'big.log': b'0123456789'})))
    with pytest.raises(ValueError, match='too large'):
        await bas.extract_archive_artifact(
            mocker.Mock(), fullname='j', relative_path='a.tar.gz', member='big.log', number=1
        )


@pytest.mark.asyncio
async def test_list_bomb_guard_decompressed_total(mock_jenkins, mocker, monkeypatch):
    monkeypatch.setattr(bas, '_MAX_DECOMPRESSED_TOTAL', 4)
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=_targz({f'f{i}.log': b'XXXX' for i in range(5)})))
    out = await bas.list_archive_artifact(mocker.Mock(), fullname='j', relative_path='a.tar.gz', number=1)
    assert out['scan_limited'] is True  # _LimitedReader tripped the cap (distinct from normal truncation)
    assert out['truncated'] is False


def test_limited_reader_clamps_before_read():
    # HIGH from the 3-model review: a single huge read (PAX/GNU header size) must not over-allocate.
    import io

    src = io.BytesIO(b'z' * 10_000)
    reader = bas._LimitedReader(src, limit=100)
    chunk = reader.read(10_000)  # asks for 10k, must get at most the 100-byte budget
    assert len(chunk) == 100
    with pytest.raises(bas._ArchiveLimitError):
        reader.read(10_000)  # budget exhausted -> raise (no further allocation)


def test_limited_reader_clamps_read_all():
    import io

    reader = bas._LimitedReader(io.BytesIO(b'z' * 10_000), limit=50)
    assert len(reader.read(-1)) == 50  # read(-1) is clamped to the budget, not the whole stream


@pytest.mark.asyncio
async def test_extract_bomb_guard_raises(mock_jenkins, mocker, monkeypatch):
    # A highly compressible member (1 MiB of zeros -> tiny gzip) must trip the decompressed cap.
    monkeypatch.setattr(bas, '_MAX_DECOMPRESSED_TOTAL', 4096)
    monkeypatch.setattr(bas, '_MAX_MEMBER_BYTES', 8 * 1024 * 1024)
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=_targz({'bomb.bin': b'\x00' * (1024 * 1024)})))
    with pytest.raises(ValueError, match='decompression limit'):
        await bas.extract_archive_artifact(
            mocker.Mock(), fullname='j', relative_path='a.tar.gz', member='bomb.bin', number=1
        )


@pytest.mark.asyncio
async def test_malformed_archive_raises_sanitized(mock_jenkins, mocker):
    _set_stream(mock_jenkins, _FakeResponse(raw_bytes=b'this is not a gzip tarball'))
    with pytest.raises(ValueError, match='invalid or unsupported tar.gz archive'):
        await bas.list_archive_artifact(mocker.Mock(), fullname='j', relative_path='a.tar.gz', number=1)


# ---- grep (glob over many artifacts) ----
def _artifacts(mocker, *paths: str) -> list:
    from mcp_jenkins.jenkins.model.build import Artifact

    return [Artifact(fileName=p.rsplit('/', 1)[-1], relativePath=p) for p in paths]


def _streams(mock_jenkins, contents: dict[str, list[str]]) -> None:
    def stream(*, fullname: str, number: int, relative_path: str):  # noqa: ARG001
        return _stream(_plain(*contents[relative_path]))

    mock_jenkins.stream_build_artifact.side_effect = stream


def test_grep_is_sync_so_fastmcp_offloads_it():
    import inspect

    assert not inspect.iscoroutinefunction(bas.grep_build_artifact)


def test_grep_glob_greps_each_matching_artifact(mock_jenkins, mocker):
    paths = [f'r/mtr-test-w_{i}.log' for i in (10, 2, 1)] + ['r/other.log']
    mock_jenkins.get_build_artifacts.return_value = _artifacts(mocker, *paths)
    _streams(mock_jenkins, {p: ['noise', f'Completed: {p}'] for p in paths})

    out = bas.grep_build_artifact(
        mocker.Mock(), fullname='j', relative_path='r/mtr-test-w_*.log', pattern='Completed:', number=79
    )

    assert [f['relative_path'] for f in out['files']] == [
        'r/mtr-test-w_1.log',
        'r/mtr-test-w_2.log',
        'r/mtr-test-w_10.log',
    ]
    assert out['files_matched'] == 3
    assert out['files_scanned'] == 3
    assert out['match_count'] == 3
    assert out['files_truncated'] is False
    streamed = [c.kwargs['relative_path'] for c in mock_jenkins.stream_build_artifact.call_args_list]
    assert '*' not in ''.join(streamed)


def test_grep_glob_caps_files(mock_jenkins, mocker):
    paths = [f'w_{i}.log' for i in range(1, 41)]
    mock_jenkins.get_build_artifacts.return_value = _artifacts(mocker, *paths)
    _streams(mock_jenkins, {p: ['x'] for p in paths})

    out = bas.grep_build_artifact(mocker.Mock(), fullname='j', relative_path='w_*.log', pattern='x', number=1)

    assert len(out['files']) == 30
    assert out['files_matched'] == 40
    assert out['files_truncated'] is True


def test_grep_glob_reports_a_failing_file_inline(mock_jenkins, mocker):
    from requests.exceptions import HTTPError

    mock_jenkins.get_build_artifacts.return_value = _artifacts(mocker, 'a.log', 'b.log')

    def stream(*, fullname: str, number: int, relative_path: str):  # noqa: ARG001
        if relative_path == 'a.log':
            msg = '404 Client Error'
            raise HTTPError(msg)
        return _stream(_plain('hit'))

    mock_jenkins.stream_build_artifact.side_effect = stream

    out = bas.grep_build_artifact(mocker.Mock(), fullname='j', relative_path='*.log', pattern='hit', number=1)

    assert out['files'][0] == {'relative_path': 'a.log', 'error': 'HTTPError: 404 Client Error'}
    assert out['files'][1]['match_count'] == 1


def test_grep_glob_total_matches_capped(mock_jenkins, mocker):
    mocker.patch.object(bas, '_MAX_MATCHES', 3)
    mock_jenkins.get_build_artifacts.return_value = _artifacts(mocker, 'a.log', 'b.log', 'c.log')
    _streams(mock_jenkins, {p: ['hit', 'hit'] for p in ('a.log', 'b.log', 'c.log')})

    out = bas.grep_build_artifact(mocker.Mock(), fullname='j', relative_path='*.log', pattern='hit', number=1)

    assert out['match_count'] == 3
    assert [f['match_count'] for f in out['files']] == [2, 1]
    assert out['notes'] == ['stopped before c.log: total match, byte or time budget spent']


@pytest.mark.parametrize('glob', ['../*.log', '/etc/*', 'a/%2e%2e/*', 'a\\\\*', '[!a]*.log', 'a/../../*'])
def test_grep_glob_rejects_unsafe_globs(mock_jenkins, mocker, glob):
    with pytest.raises(ValueError):  # noqa: PT011
        bas.grep_build_artifact(mocker.Mock(), fullname='j', relative_path=glob, pattern='x', number=1)
    mock_jenkins.get_build_artifacts.assert_not_called()


def test_grep_glob_rejects_archive_member(mock_jenkins, mocker):
    with pytest.raises(ValueError, match='archive_member cannot be combined'):
        bas.grep_build_artifact(
            mocker.Mock(), fullname='j', relative_path='*.tar.gz', pattern='x', number=1, archive_member='a.log'
        )


def test_grep_glob_skips_archives(mock_jenkins, mocker):
    mock_jenkins.get_build_artifacts.return_value = _artifacts(mocker, 'r/a.log', 'r/logs.tar.gz', 'r/core.gz')
    _streams(mock_jenkins, {'r/a.log': ['hit']})

    out = bas.grep_build_artifact(mocker.Mock(), fullname='j', relative_path='r/*', pattern='hit', number=1)

    assert [f['relative_path'] for f in out['files']] == ['r/a.log']
    assert out['notes'] == ['2 archive(s) skipped; grep one with archive_member']


def test_grep_glob_total_bytes_never_pass_the_cap(mock_jenkins, mocker):
    mocker.patch.object(bas, '_MAX_GLOB_BYTES', 50)
    mock_jenkins.get_build_artifacts.return_value = _artifacts(mocker, 'a.log', 'b.log')
    _streams(mock_jenkins, {'a.log': ['x' * 30], 'b.log': ['y' * 30]})

    out = bas.grep_build_artifact(mocker.Mock(), fullname='j', relative_path='*.log', pattern='z', number=1)

    assert out['bytes_scanned'] <= 50
    assert out['files'][1]['truncated'] is True


def test_grep_glob_reports_a_mid_stream_urllib3_error_inline(mock_jenkins, mocker):
    import urllib3

    mock_jenkins.get_build_artifacts.return_value = _artifacts(mocker, 'a.log', 'b.log')
    broken = _plain('hit')

    def boom(_n: int) -> bytes:
        msg = 'Connection broken'
        raise urllib3.exceptions.ProtocolError(msg)

    broken.raw.read = boom

    def stream(*, fullname: str, number: int, relative_path: str):  # noqa: ARG001
        return _stream(broken if relative_path == 'a.log' else _plain('hit'))

    mock_jenkins.stream_build_artifact.side_effect = stream

    out = bas.grep_build_artifact(mocker.Mock(), fullname='j', relative_path='*.log', pattern='hit', number=1)

    assert out['files'][0]['error'].startswith('ProtocolError')
    assert out['files'][1]['match_count'] == 1


def test_grep_accepts_context_and_regex_aliases(mock_jenkins, mocker):
    _set_stream(mock_jenkins, _plain('a', 'b', 'ERR 1', 'c', 'd'))

    out = bas.grep_build_artifact(
        mocker.Mock(), fullname='j', relative_path='x.log', pattern='ERR [0-9]', number=1, before=2, after=1, regex=True
    )

    assert out['literal'] is False
    assert out['match_count'] == 1
    assert out['matches'][0]['before'] == ['a', 'b']
    assert out['matches'][0]['after'] == ['c', 'd']


def test_regex_matcher_accepts_a_mid_pattern_flag():
    match = bas._build_matcher('ok|(?i)timed out', literal=False, ignore_case=False)

    assert match('Build TIMED OUT')
    assert not match('Build passed')
