"""Caller-supplied regex patterns compile with the linear-time RE2 engine on every path."""

import re
import time

import pytest

from mcp_jenkins.jenkins import Jenkins
from mcp_jenkins.jenkins.rest_client import (
    MAX_USER_PATTERN_LEN,
    _compile_query_pattern,
    compile_user_regex,
    hoist_inline_flags,
    require_narrow_pattern,
)
from mcp_jenkins.server.build_artifact_search import _build_matcher

CATASTROPHIC = '(=+)+$x'
TEXT = '=' * 5000
BUDGET_SECONDS = 0.1


def _timed(call):
    started = time.perf_counter()
    result = call()
    return result, time.perf_counter() - started


@pytest.mark.parametrize(
    'compile_path',
    [
        pytest.param(lambda p: compile_user_regex(p).search, id='compile_user_regex'),
        pytest.param(lambda p: compile_user_regex(p, re.IGNORECASE).search, id='compile_user_regex-ignorecase'),
        pytest.param(lambda p: _compile_query_pattern(p).search, id='query_items-patterns'),
        pytest.param(lambda p: _build_matcher(p, literal=False, ignore_case=False), id='grep-matcher'),
        pytest.param(lambda p: _build_matcher(p, literal=False, ignore_case=True), id='grep-matcher-ignorecase'),
    ],
)
def test_catastrophic_pattern_is_linear_on_every_compile_path(re2_engine, compile_path):
    matcher = compile_path(CATASTROPHIC)
    result, seconds = _timed(lambda: matcher(TEXT))
    assert not result
    assert seconds < BUDGET_SECONDS, f'{seconds:.3f}s'


def test_catastrophic_pattern_through_narrow_pattern_guard_is_fast(re2_engine):
    _, seconds = _timed(lambda: require_narrow_pattern(CATASTROPHIC, 'find_items'))
    assert seconds < BUDGET_SECONDS


def test_console_output_filter_is_linear(re2_engine, mocker):
    session = mocker.Mock()
    mocker.patch('mcp_jenkins.jenkins.rest_client.requests.Session', autospec=True, return_value=session)
    session.get.return_value.iter_content.return_value = iter([f'{TEXT}\n{TEXT}x\n'.encode()])
    client = Jenkins(url='https://example.com/', username='u', password='p')

    out, seconds = _timed(lambda: client.get_build_console_output(fullname='job', number=1, pattern=CATASTROPHIC))

    assert out == ''
    assert seconds < BUDGET_SECONDS * 2
    session.get.return_value.iter_content.return_value = iter([b'a\nb==\n'])
    assert client.get_build_console_output(fullname='job', number=1, pattern='=+$') == 'b=='


def test_query_items_patterns_are_linear(re2_engine, mocker):
    from mcp_jenkins.jenkins.model.item import Job

    session = mocker.Mock()
    mocker.patch('mcp_jenkins.jenkins.rest_client.requests.Session', autospec=True, return_value=session)
    client = Jenkins(url='https://example.com/', username='u', password='p')
    job = Job(fullname=TEXT, name=TEXT, url='u', class_=TEXT, color=TEXT)
    mocker.patch.object(client, 'get_items', return_value=[job])

    for name in ('class_pattern', 'fullname_pattern', 'color_pattern', 'label_pattern'):
        out, seconds = _timed(lambda name=name: client.query_items(**{name: CATASTROPHIC}))
        assert out == []
        assert seconds < BUDGET_SECONDS, name


@pytest.mark.parametrize(
    ('pattern', 'construct'),
    [
        (r'(a)\1', r'backreference \1'),
        (r'(?P<x>a)(?P=x)', 'named backreference (?P=name)'),
        (r'foo(?=bar)', 'lookahead (?=...)'),
        (r'foo(?!bar)', 'negative lookahead (?!...)'),
        (r'(?<=foo)bar', 'lookbehind (?<=...)'),
        (r'(?<!foo)bar', 'negative lookbehind (?<!...)'),
        (r'(?>a+)b', 'atomic group (?>...)'),
        (r'a++b', 'possessive quantifier ++'),
        (r'a*+b', 'possessive quantifier *+'),
        (r'a{2}+b', 'possessive quantifier }+'),
        (r'end\Z', r'\Z (use \z for the end of the text)'),
        (r'(a)?(?(1)b|c)', 'conditional group'),
    ],
)
def test_unsupported_construct_is_named_in_the_error(pattern, construct):
    with pytest.raises(ValueError, match='invalid regex pattern') as info:
        compile_user_regex(pattern)
    message = str(info.value)
    assert construct in message
    assert 'Simplify the pattern' in message
    assert 'literal=True' in message


@pytest.mark.parametrize('pattern', [r'(?x)a b', r'a(?x)b'])
def test_verbose_flag_is_refused_by_name(pattern):
    with pytest.raises(ValueError, match=r'inline flag \(\?x\) is not supported'):
        compile_user_regex(pattern)


def test_unsupported_construct_inside_a_class_is_not_reported():
    # [*+] and [\1] are plain characters, the error is the unclosed group.
    with pytest.raises(ValueError, match='invalid regex pattern') as info:
        compile_user_regex(r'[*+](')
    assert 'possessive' not in str(info.value)
    assert compile_user_regex(r'a[*+]b').search('a+b')


def test_other_errors_keep_the_engine_text():
    with pytest.raises(ValueError, match=r'invalid regex pattern: missing \)'):
        compile_user_regex('(unclosed')


def test_pattern_length_cap_applies_to_every_path():
    long = 'a' * (MAX_USER_PATTERN_LEN + 1)
    with pytest.raises(ValueError, match='pattern too long'):
        compile_user_regex(long)
    with pytest.raises(ValueError, match='pattern too long'):
        _compile_query_pattern(long)
    assert compile_user_regex('a' * MAX_USER_PATTERN_LEN).search('a' * MAX_USER_PATTERN_LEN)


def test_flags_ignorecase_multiline_dotall():
    assert compile_user_regex('error', re.IGNORECASE).search('An ERROR here')
    assert not compile_user_regex('error').search('An ERROR here')
    assert compile_user_regex('^two$', re.MULTILINE).search('one\ntwo\nthree')
    assert not compile_user_regex('^two$').search('one\ntwo\nthree')
    assert compile_user_regex('a.b', re.DOTALL).search('a\nb')
    assert not compile_user_regex('a.b').search('a\nb')
    with pytest.raises(ValueError, match='not supported'):
        compile_user_regex('a', re.VERBOSE)


@pytest.mark.parametrize(
    ('pattern', 'text'),
    [
        ('(?i)error', 'ERROR'),
        ('ok|(?i)timed out', 'TIMED OUT'),
        ('(?m)^b$', 'a\nb\nc'),
        ('(?s)a.c', 'a\nc'),
        ('z|(?im)^Y$', 'q\ny\n'),
        ('(?i:scoped)', 'SCOPED'),
        ('(?u)plain', 'plain'),
        ('(?a)plain', 'plain'),
    ],
)
def test_inline_flags_keep_their_python_meaning(pattern, text):
    assert compile_user_regex(pattern).search(text)


def test_mid_pattern_flag_applies_to_the_whole_pattern():
    # RE2 alone would apply a mid-pattern (?i) only after it. The hoist keeps the Python meaning.
    assert compile_user_regex('error(?i)').search('ERROR')


@pytest.mark.parametrize(
    ('pattern', 'expected'),
    [
        ('[(?i)]x', '[(?i)]x'),
        (r'\(?i)x', r'\(?i)x'),
        ('[]a(?i)]b', '[]a(?i)]b'),
        ('(?i)(?m)ab', '(?im)ab'),
    ],
)
def test_hoist_ignores_classes_and_escapes(pattern, expected):
    assert hoist_inline_flags(pattern) == expected


def test_compiled_object_supports_the_methods_callers_use():
    rx = compile_user_regex(r'(\w+)=(\d+)')
    assert rx.search('a=1').group(1) == 'a'
    assert rx.match('b=2').group(2) == '2'
    assert [m.group(0) for m in rx.finditer('a=1 b=2')] == ['a=1', 'b=2']
    assert rx.sub('x', 'a=1') == 'x'
    assert rx.search('') is None


def test_ascii_classes_differ_from_python_re():
    # RE2 \d, \w and \b are ASCII only. Python re matches Unicode digits and letters in str patterns.
    assert re.search(r'\d', '٣')
    assert not compile_user_regex(r'\d').search('٣')
    assert not compile_user_regex(r'\w').search('é')
    assert compile_user_regex(r'\pL').search('é')
    assert compile_user_regex('café').search('un café')
    assert compile_user_regex('(?i)CAFÉ').search('café')


def test_pattern_and_text_types():
    # Matching runs on str lines everywhere. Bytes input still works with a str pattern.
    assert compile_user_regex('err').search('an err')
    assert compile_user_regex('err').search(b'an err')
