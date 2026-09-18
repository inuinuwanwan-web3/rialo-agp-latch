"""Start at-most-once and read-only reconciliation through the real wire gate."""
import multiprocessing
from unittest.mock import Mock, patch

import pytest

from agp_race_agent.monitor import TrackMonitor
from agp_race_agent.write_safety import WriteBlocked
from tests.test_monitor import settings
from tests.test_write_safety import (
    TRACK, START, STATE, make, prepare, writes, blocked, assert_no_more_writes,
)


@pytest.mark.parametrize('outcome', [START, TimeoutError(), ConnectionResetError(), {}, {'run': None}])
def test_start_confirmed_by_read_continues_without_resend(tmp_path, outcome):
    g = make(tmp_path, replies={'start_track': [outcome]})
    prepare(g)
    # The server registered the run even if its write response was lost.
    original = g._write
    def wire(message, deadline):
        if message['params']['name'] == 'start_track':
            g.current_run = START['run']
        return original(message, deadline)
    g._write = wire
    assert g.call('start_track', {'trackId': TRACK}) == START
    names = [name for name, _ in g.wire]
    index = names.index('start_track')
    assert names[index - 1] == 'my_race'
    assert names[index + 1:] == ['my_race']
    g.call('track_state')
    g.call('ask', {'question': 'q'})
    g.call('guess', {'guess': 'g'})
    assert writes(g) == ['start_track', 'ask', 'guess']
    with pytest.raises(WriteBlocked):
        g.call('start_track', {'trackId': TRACK})
    assert writes(g).count('start_track') == 1


@pytest.mark.parametrize('confirmation', [
    {'run': None}, {}, {'registration': {'trackId': TRACK}},
    {'run': {'id': 'r', 'trackId': 'other', 'finished': False}},
    {'run': {**START['run'], 'finished': True}},
    {'run': {**START['run'], 'status': 'unknown'}},
    {'run': {'id': 'r', 'trackId': TRACK}},
])
def test_timeout_unconfirmed_blocks_paid_calls_and_restart(tmp_path, confirmation):
    g = make(tmp_path, replies={'start_track': [TimeoutError()],
                               'my_race': [{'run': None}, {'run': None}, confirmation]})
    prepare(g)
    with pytest.raises(WriteBlocked):
        g.call('start_track', {'trackId': TRACK})
    blocked()
    assert [name for name, _ in g.wire][-1] == 'my_race'
    assert_no_more_writes(g)
    assert writes(g) == ['start_track']
    restarted = make(tmp_path)
    assert_no_more_writes(restarted)
    assert writes(restarted) == []


def test_success_response_requires_matching_my_race(tmp_path):
    g = make(tmp_path, replies={'my_race': [{'run': None}, {'run': None},
                                         {'run': {**START['run'], 'id': 'other'}}]})
    prepare(g)
    with pytest.raises(WriteBlocked):
        g.call('start_track', {'trackId': TRACK})
    blocked()
    assert_no_more_writes(g)
    assert writes(g) == ['start_track']


def test_fresh_existing_race_prevents_start_despite_stale_cache(tmp_path):
    g = make(tmp_path)
    prepare(g)
    g.current_run = START['run']
    with pytest.raises(WriteBlocked):
        g.call('start_track', {'trackId': TRACK})
    assert writes(g) == []


@pytest.mark.parametrize('outcome', [START, TimeoutError()])
def test_confirmation_read_failure_never_resends(tmp_path, outcome):
    g = make(tmp_path, replies={'start_track': [outcome],
        'my_race': [{'run': None}, {'run': None},
                    TimeoutError(), TimeoutError(), TimeoutError()]})
    prepare(g)
    with pytest.raises(WriteBlocked):
        g.call('start_track', {'trackId': TRACK})
    blocked()
    assert_no_more_writes(g)
    assert writes(g) == ['start_track']


def test_confirmed_grid_does_not_allow_paid_calls_before_green_flag(tmp_path):
    g = make(tmp_path, replies={'track_state': [{**STATE, 'started': False}]})
    prepare(g)
    g.call('start_track', {'trackId': TRACK})
    g.call('track_state')
    with pytest.raises(WriteBlocked):
        g.call('ask', {'question': 'q'})
    assert_no_more_writes(g)
    assert writes(g) == ['start_track']


def test_timeout_confirmation_in_monitor_continues_after_green_flag(tmp_path):
    g = make(tmp_path, replies={'start_track': [TimeoutError()],
                               'track_state': [STATE, {'trackId': TRACK, 'finished': True}]})
    original = g._write
    def wire(message, deadline):
        if message['params']['name'] == 'start_track':
            g.current_run = START['run']
        return original(message, deadline)
    g._write = wire
    with patch('agp_race_agent.agent.Solver') as solver:
        solver.return_value.decide.side_effect = [{'question': 'q'}, {'guess': 'g'}]
        TrackMonitor(settings(tmp_path), g).run(auto_join=True,
            sleep=Mock(side_effect=KeyboardInterrupt), notify=Mock())
    assert writes(g) == ['start_track', 'ask', 'guess']


def _process_start(root, barrier, results):
    g = make(root)
    prepare(g)
    barrier.wait(timeout=10)
    try:
        g.call('start_track', {'trackId': TRACK})
    except WriteBlocked:
        pass
    results.put(writes(g).count('start_track'))


def test_two_real_processes_share_at_most_once_store(tmp_path):
    # Linux fork inherits only the test's isolated DB path; all MCP is Fake.
    ctx = multiprocessing.get_context('fork')
    barrier, results = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_process_start, args=(tmp_path, barrier, results)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        counts = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
        assert sum(counts) == 1
        # A third, restarted process cannot resend the same Track.
        again = ctx.Process(target=_process_start, args=(tmp_path, ctx.Barrier(1), results))
        processes.append(again)
        again.start()
        assert results.get(timeout=15) == 0
        again.join(timeout=5)
        assert again.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()


# Explicit Fake Join Window proof keeps downstream lifecycle coverage active.
import pytest as _pytest
pytestmark = _pytest.mark.usefixtures("fake_join_window")
