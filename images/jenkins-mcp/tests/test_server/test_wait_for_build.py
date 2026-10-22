import asyncio
import time

import pytest
from fastmcp import Client
from requests.exceptions import HTTPError

from mcp_jenkins.jenkins.model.build import Build
from mcp_jenkins.jenkins.model.queue import QueueExecutable, QueueItem, QueueItemTask
from mcp_jenkins.server import mcp, queue


@pytest.fixture
def mock_jenkins(mocker):
    mock_jenkins = mocker.Mock()
    mocker.patch('mcp_jenkins.server.queue.jenkins', return_value=mock_jenkins)
    mocker.patch('mcp_jenkins.server.queue._POLL_INTERVAL_SECONDS', 0)
    yield mock_jenkins


def _item(**kwargs: object) -> QueueItem:
    return QueueItem(
        **{'id': 45, 'inQueueSince': 1, 'url': 'queue/item/45/', 'why': None, 'task': QueueItemTask(), **kwargs}
    )


def _http_error(status: int) -> HTTPError:
    error = HTTPError(f'{status} Client Error')
    error.response = type('Response', (), {'status_code': status})()
    return error


@pytest.mark.asyncio
async def test_queue_id_resolves_to_number_and_waits_until_finished(mock_jenkins, mocker):
    mock_jenkins.get_queue_item.side_effect = [_item(why='Waiting'), _item(executable=QueueExecutable(number=7))]
    mock_jenkins.get_build.side_effect = [
        Build(number=7, url='u7', building=True),
        Build(number=7, url='u7', building=False, result='SUCCESS'),
    ]

    result = await queue.wait_for_build(mocker.Mock(), fullname='job1', queue_id=45)

    assert result['state'] == 'finished'
    assert result['number'] == 7
    assert result['result'] == 'SUCCESS'
    assert result['timed_out'] is False
    mock_jenkins.get_queue_item.assert_called_with(id=45, depth=1)


@pytest.mark.asyncio
async def test_cancelled_queue_item(mock_jenkins, mocker):
    mock_jenkins.get_queue_item.return_value = _item(cancelled=True)

    result = await queue.wait_for_build(mocker.Mock(), fullname='job1', queue_id=45)

    assert result['state'] == 'cancelled'
    mock_jenkins.get_build.assert_not_called()


@pytest.mark.asyncio
async def test_forgotten_queue_item_falls_back_to_history(mock_jenkins, mocker):
    mock_jenkins.get_queue_item.side_effect = _http_error(404)
    mock_jenkins.get_build_history.return_value = [
        Build(number=9, url='u9', queueId=46),
        Build(number=8, url='u8', queueId=45),
    ]
    mock_jenkins.get_build.return_value = Build(number=8, url='u8', building=False, result='FAILURE')

    result = await queue.wait_for_build(mocker.Mock(), fullname='job1', queue_id=45)

    assert (result['state'], result['number'], result['result']) == ('finished', 8, 'FAILURE')


@pytest.mark.asyncio
async def test_forgotten_queue_item_not_in_history(mock_jenkins, mocker):
    mock_jenkins.get_queue_item.side_effect = _http_error(404)
    mock_jenkins.get_build_history.return_value = []

    result = await queue.wait_for_build(mocker.Mock(), fullname='job1', queue_id=45)

    assert result['state'] == 'not_found'


@pytest.mark.asyncio
async def test_timeout_returns_state_instead_of_raising(mock_jenkins, mocker):
    mocker.patch('mcp_jenkins.server.queue._POLL_INTERVAL_SECONDS', 2)
    mock_jenkins.get_queue_item.return_value = _item(why='Waiting for next available executor')

    result = await queue.wait_for_build(mocker.Mock(), fullname='job1', queue_id=45, timeout_seconds=1)

    assert result['state'] == 'queued'
    assert result['timed_out'] is True


@pytest.mark.asyncio
async def test_needs_queue_id_or_number(mock_jenkins, mocker):
    with pytest.raises(ValueError, match='needs queue_id'):
        await queue.wait_for_build(mocker.Mock(), fullname='job1')


@pytest.mark.asyncio
async def test_wait_keeps_the_event_loop_responsive(mock_jenkins, mocker):
    """Each Jenkins request runs on a worker thread, so a slow master never freezes /healthz."""
    mock_jenkins.get_build.side_effect = lambda **kw: time.sleep(0.1) or Build(number=3, url='u3', building=False)
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:  # noqa: ASYNC110 - polls loop liveness, which is the property under test
            await asyncio.sleep(0.005)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        async with Client(mcp) as client:
            result = await client.call_tool('wait_for_build', {'fullname': 'job1', 'number': 3})
    finally:
        beat.cancel()

    assert result.structured_content['state'] == 'finished'
    assert ticks >= 5


@pytest.mark.asyncio
async def test_queue_item_for_another_job_is_refused(mock_jenkins, mocker):
    item = _item(task=QueueItemTask(url='https://ps80.cd/job/other-job/'))
    mock_jenkins.get_queue_item.return_value = item

    with pytest.raises(ValueError, match="not job 'job1'"):
        await queue.wait_for_build(mocker.Mock(), fullname='job1', queue_id=45)


@pytest.mark.asyncio
async def test_queue_item_for_the_named_folder_job_is_accepted(mock_jenkins, mocker):
    mock_jenkins.get_queue_item.return_value = _item(
        task=QueueItemTask(url='https://ps80.cd/job/f/job/job%201/'),
        executable=QueueExecutable(number=4, url='https://ps80.cd/job/f/job/job%201/4/'),
    )
    mock_jenkins.get_build.return_value = Build(number=4, url='u4', building=False, result='SUCCESS')

    result = await queue.wait_for_build(mocker.Mock(), fullname='f/job 1', queue_id=45)

    assert (result['state'], result['number']) == ('finished', 4)


@pytest.mark.asyncio
async def test_a_slow_request_cannot_push_the_wait_past_its_deadline(mock_jenkins, mocker):
    mock_jenkins.get_build.side_effect = lambda **kw: time.sleep(3) or Build(number=3, url='u3', building=False)
    started = time.monotonic()

    result = await queue.wait_for_build(mocker.Mock(), fullname='job1', number=3, timeout_seconds=1)

    assert time.monotonic() - started < 2
    assert (result['state'], result['timed_out']) == ('running', True)
