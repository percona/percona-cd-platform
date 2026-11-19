"""Inline reads from a master are bounded in what they hold and what they return."""

import json

import pytest

from mcp_jenkins.jenkins import Jenkins, rest_client
from mcp_jenkins.jenkins.rest_client import (
    MAX_CONSOLE_BYTES,
    MAX_INLINE_ARTIFACT_BYTES,
    PayloadTooLargeError,
    pattern_narrows,
    require_narrow_pattern,
)

CAP = 1000
CHUNK = 64


class _Body:
    """A streamed body that counts how many bytes the client pulled from it."""

    def __init__(self, data: bytes, chunk: int = CHUNK) -> None:
        self.data = data
        self.chunk = chunk
        self.pulled = 0

    def iter_content(self, chunk_size: int = 1):  # noqa: ARG002
        for start in range(0, len(self.data), self.chunk):
            piece = self.data[start : start + self.chunk]
            self.pulled += len(piece)
            yield piece


@pytest.fixture
def session(mocker):
    session = mocker.Mock()
    mocker.patch('mcp_jenkins.jenkins.rest_client.requests.Session', autospec=True, return_value=session)
    return session


@pytest.fixture
def client(session):
    return Jenkins(url='https://example.com/', username='u', password='p')


def _serve(session, mocker, data: bytes, headers: dict | None = None) -> tuple[object, _Body]:
    body = _Body(data)
    response = mocker.Mock(headers=headers or {})
    response.iter_content.side_effect = body.iter_content
    session.get.return_value = response
    return response, body


# ---- get_build_artifact ----
def _artifact(client, max_bytes: int = CAP) -> bytes:
    return client.get_build_artifact(fullname='job', number=1, relative_path='big.bin', max_bytes=max_bytes)


@pytest.mark.parametrize('with_length', [True, False], ids=['content-length', 'no-content-length'])
@pytest.mark.parametrize('size', [CAP - 1, CAP], ids=['under', 'at'])
def test_artifact_at_or_under_the_cap_is_returned(client, session, mocker, size, with_length):
    data = b'a' * size
    response, _ = _serve(session, mocker, data, {'Content-Length': str(size)} if with_length else None)
    assert _artifact(client) == data
    response.close.assert_called_once()
    session.get.assert_called_once_with(
        'https://example.com/job/job/1/artifact/big.bin', timeout=client.timeout, stream=True
    )


def test_artifact_over_the_cap_with_content_length_is_refused_before_any_read(client, session, mocker):
    response, body = _serve(session, mocker, b'a' * (CAP + 1), {'Content-Length': str(CAP + 1)})
    with pytest.raises(PayloadTooLargeError, match='export_build_artifact') as refused:
        _artifact(client)
    assert 'grep_build_artifact' in str(refused.value)
    assert body.pulled == 0
    response.close.assert_called_once()


@pytest.mark.parametrize(
    'headers',
    [None, {'Content-Length': '10'}, {'Content-Length': 'nonsense'}],
    ids=['no-content-length', 'lying-content-length', 'bad-content-length'],
)
def test_artifact_over_the_cap_is_refused_while_reading(client, session, mocker, headers):
    response, body = _serve(session, mocker, b'a' * (50 * CAP), headers)
    with pytest.raises(PayloadTooLargeError, match=f'more than {CAP} bytes'):
        _artifact(client)
    assert CAP < body.pulled <= CAP + CHUNK
    response.close.assert_called_once()


def test_artifact_one_byte_over_the_cap_is_refused(client, session, mocker):
    response, _ = _serve(session, mocker, b'a' * (CAP + 1))
    with pytest.raises(PayloadTooLargeError):
        _artifact(client)
    response.close.assert_called_once()


def test_artifact_default_cap_is_ten_mib_and_named(client, session, mocker):
    assert MAX_INLINE_ARTIFACT_BYTES == 10 * 1024 * 1024
    _serve(session, mocker, b'', {'Content-Length': str(MAX_INLINE_ARTIFACT_BYTES + 1)})
    with pytest.raises(PayloadTooLargeError, match='inline limit of 10 MiB'):
        client.get_build_artifact(fullname='job', number=1, relative_path='big.bin')


def test_artifact_refusal_is_a_value_error(client, session, mocker):
    _serve(session, mocker, b'a' * (CAP + 1))
    with pytest.raises(ValueError, match='inline limit'):
        _artifact(client)


def test_artifact_http_error_closes_the_response(client, session, mocker):
    response, _ = _serve(session, mocker, b'')
    response.raise_for_status.side_effect = rest_client.HTTPError('404')
    with pytest.raises(rest_client.HTTPError):
        _artifact(client)
    response.close.assert_called_once()


# ---- console output ----
def _console(lines: list[str]) -> bytes:
    return ''.join(f'{line}\n' for line in lines).encode()


def test_unfiltered_console_over_the_cap_returns_the_head_and_a_marker(client, session, mocker):
    lines = [f'line {index:04d} ' + 'x' * 90 for index in range(100)]  # 100 lines of 101 bytes
    response, body = _serve(session, mocker, _console(lines))
    out = client.get_build_console_output(fullname='job', number=1, max_bytes=CAP).split('\n')
    assert out[:-1] == lines[:9]
    assert out[-1].startswith('[output truncated: one call returns at most 1000 bytes')
    assert 'offset=9' in out[-1]
    assert 'export_build_log' in out[-1]
    assert body.pulled < len(body.data)
    response.close.assert_called_once()


def test_console_offset_from_the_marker_continues_the_read(client, session, mocker):
    lines = [f'line {index:04d} ' + 'x' * 90 for index in range(30)]
    _serve(session, mocker, _console(lines))
    out = client.get_build_console_output(fullname='job', number=1, offset=9, max_bytes=CAP).split('\n')
    assert out[0] == lines[9]
    assert 'offset=18' in out[-1]


def test_console_at_the_cap_has_no_marker(client, session, mocker):
    lines = ['y' * 99] * 10  # exactly 1000 bytes with newlines
    _serve(session, mocker, _console(lines))
    assert client.get_build_console_output(fullname='job', number=1, max_bytes=CAP) == '\n'.join(lines)


def test_filtered_console_scans_the_whole_log_as_a_stream(client, session, mocker):
    lines = ['noise ' + 'n' * 200] * 5000 + ['ERROR at the end']
    _, body = _serve(session, mocker, _console(lines))
    out = client.get_build_console_output(fullname='job', number=1, pattern='ERROR', max_bytes=CAP)
    assert out == 'ERROR at the end'
    assert body.pulled == len(body.data)


def test_filtered_console_answer_is_capped_too(client, session, mocker):
    lines = ['ERROR ' + 'e' * 94] * 100
    _serve(session, mocker, _console(lines))
    out = client.get_build_console_output(fullname='job', number=1, pattern='ERROR', max_bytes=CAP)
    assert len(out.encode()) < CAP + 300
    assert out.split('\n')[-1].startswith('[output truncated')


def test_console_line_without_newline_is_cut_and_never_buffered_whole(client, session, mocker):
    blob = b'z' * (rest_client.MAX_CONSOLE_LINE_BYTES * 8)
    response = mocker.Mock(headers={})
    buffered = []

    def chunks(chunk_size):  # noqa: ARG001
        for start in range(0, len(blob), 4096):
            buffered.append(start)
            yield blob[start : start + 4096]
        yield b'\nnext line\n'

    response.iter_content.side_effect = chunks
    session.get.return_value = response
    out = client.get_build_console_output(fullname='job', number=1).split('\n')
    assert (
        out[0]
        == 'z' * rest_client.MAX_CONSOLE_LINE_BYTES + f' [line cut at {rest_client.MAX_CONSOLE_LINE_BYTES} bytes]'
    )
    assert out[1] == 'next line'


def test_iter_console_lines_holds_at_most_one_line_and_one_chunk():
    seen = []

    def chunks():
        for _ in range(100):
            yield b'q' * 50

    for line, size in rest_client._iter_console_lines(chunks(), max_line=200):
        seen.append((line, size))
    assert len(seen) == 1
    assert seen[0][0].startswith('q' * 200 + ' [line cut at 200 bytes]')


def test_iter_console_lines_keeps_crlf_and_empty_lines():
    out = [line for line, _ in rest_client._iter_console_lines([b'a\r\n', b'\nb', b'c\n', b'tail'])]
    assert out == ['a', '', 'bc', 'tail']


def test_console_default_cap_is_one_mib():
    assert MAX_CONSOLE_BYTES == 1024 * 1024


# ---- console tail ----
def test_tail_under_the_byte_cap_returns_the_last_lines(client, session, mocker):
    lines = [f'l{index}' for index in range(50)]
    response, _ = _serve(session, mocker, _console(lines))
    assert client.get_build_console_tail(fullname='job', number=1, lines=5, max_bytes=CAP) == '\n'.join(lines[-5:])
    response.close.assert_called_once()


def test_tail_over_the_byte_cap_keeps_the_newest_lines_and_says_so(client, session, mocker):
    lines = [f'{index:03d}' + 't' * 96 for index in range(100)]  # 100 bytes per line
    _serve(session, mocker, _console(lines))
    out = client.get_build_console_tail(fullname='job', number=1, lines=50, max_bytes=CAP).split('\n')
    assert out[0].startswith('[output truncated: the last 50 lines are more than 1000 bytes, only the last 10')
    assert out[1:] == lines[-10:]


def test_tail_of_a_short_log_has_no_marker(client, session, mocker):
    _serve(session, mocker, _console(['a', 'b']))
    assert client.get_build_console_tail(fullname='job', number=1, lines=50, max_bytes=CAP) == 'a\nb'


# ---- test report ----
def test_test_report_sends_the_tree_and_parses_the_body(client, session, mocker):
    report = {'failCount': 0, 'suites': []}
    response, _ = _serve(session, mocker, json.dumps(report).encode())
    assert client.get_build_test_report(fullname='job', number=1, tree='failCount') == report
    session.get.assert_called_once_with(
        'https://example.com/job/job/1/testReport/api/json?depth=0',
        timeout=client.timeout,
        stream=True,
        params={'tree': 'failCount'},
    )
    response.close.assert_called_once()


def test_test_report_over_the_cap_is_refused(client, session, mocker):
    response, body = _serve(session, mocker, b'{"suites": [' + b'{}, ' * 1000 + b'{}]}')
    with pytest.raises(PayloadTooLargeError, match='larger than the 1000 bytes read limit') as refused:
        client.get_build_test_report(fullname='job', number=1, max_bytes=CAP)
    assert body.pulled <= CAP + CHUNK
    assert 'get_build_failure_summary' not in str(refused.value)
    response.close.assert_called_once()


def test_capped_read_returns_the_buffer_without_a_copy(client, session, mocker):
    _serve(session, mocker, b'abc')
    assert isinstance(_artifact(client), bytearray)


# ---- narrowing patterns ----
@pytest.mark.parametrize('pattern', ['.', '.+', '^.*$', r'[\s\S]', '.*', 'a|.*', '', None, '(?s).'])
def test_catch_all_patterns_do_not_narrow(pattern):
    assert not pattern_narrows(pattern)


@pytest.mark.parametrize('pattern', ['.', '.+', '^.*$', r'[\s\S]'])
def test_require_narrow_pattern_refuses_catch_alls(pattern):
    with pytest.raises(ValueError, match='must not match every name'):
        require_narrow_pattern(pattern, 'find_items')


@pytest.mark.parametrize('pattern', ['pxc', '^ps80-', 'job', '.{3}x'])
def test_narrow_patterns_pass(pattern):
    assert pattern_narrows(pattern)
    require_narrow_pattern(pattern, 'find_items')


def test_an_invalid_pattern_raises_from_the_helper():
    with pytest.raises(ValueError, match='invalid regex pattern'):
        pattern_narrows('(unclosed')
