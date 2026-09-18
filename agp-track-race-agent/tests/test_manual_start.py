import io
import json
import multiprocessing
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agp_race_agent.manual_start import (
    StartArchive, StartTransport, StartRedactor, PENDING, UNKNOWN,
    candidates, registration_candidate, run_approved, main,
)
from agp_race_agent.track_watcher import SnapshotStore, TrackWatcher, WatchOptions
from agp_race_agent.gateway import McpGateway
from agp_race_agent.write_safety import WriteBlocked, WriteStore
from tests.test_monitor import settings
from tests.test_track_watcher import FakeReader

TRACK = {'id': 'next', 'started': False, 'over': False, 'phase': 'unpublished',
         'registrationClosesAt': '2099-01-01T00:00:00Z', 'viewerEligible': False}


class FakeStart(StartTransport):
    response = {'jsonrpc': '2.0', 'result': {'future': ['waiting', {'unknown': 7}]}}
    failure = None

    def _connect(self):
        self.proc = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO())
        self._id = 0
        self.sent = 0

    def _write(self, message, deadline):
        super()._write(message, deadline)
        self.sent += 1
        if self.failure:
            raise self.failure
        self.proc.stdout = io.StringIO(json.dumps({**self.response, 'id': self._id}) + '\n')

    def close(self):
        pass


def test_new_candidate_is_pending_without_eligibility_or_write(tmp_path):
    reader = FakeReader([])
    watcher = TrackWatcher(lambda: reader, WatchOptions(tmp_path))
    store = SnapshotStore(tmp_path)
    watcher.poll(reader, store)
    reader.tracks = [TRACK]
    assert watcher.poll(reader, store) == ['NEW_TRACK_DETECTED']
    pending = candidates(tmp_path)
    assert pending[0]['status'] == PENDING
    assert pending[0]['evidence']['eligibility'] == 'UNCONFIRMED'
    assert pending[0]['evidence']['auto_join'] == watcher.auto_join_state == 'BLOCKED'
    assert set(reader.calls) <= {'list_tracks', 'my_race', 'track_state', 'sigil_balance'}
    reader.tracks = [{**TRACK, 'over': True}]
    watcher.poll(reader, store)
    assert candidates(tmp_path) == []
    store.close()


@pytest.mark.parametrize('update', [{'started': True}, {'over': True}, {'timedOut': True},
    {'registrationClosesAt': '2020-01-01T00:00:00Z'}, {'registrationClosesAt': 'invalid'}])
def test_no_stale_candidates(update):
    assert not registration_candidate({**TRACK, **update})


def test_unapproved_never_connects_or_sends(tmp_path):
    archive = StartArchive()
    redact = StartRedactor()
    phrase = archive.prepare('next', TRACK, redact)
    factory = Mock()
    for approval in ('', 'yes', 'MAX', phrase.replace('next', 'other')):
        assert run_approved(settings(tmp_path), archive, 'next', approval, TRACK, redact, factory) == PENDING
        with pytest.raises(WriteBlocked):
            FakeStart(settings(tmp_path)).start_once(archive, 'next', approval, TRACK, redact)
    factory.assert_not_called()
    assert archive.status('next') is None
    archive.close()


@pytest.mark.parametrize('response', [
    {'result': {'structuredContent': {'queue': {'future': [1, None, {'x': True}]}}}},
    {'error': {'code': -32000, 'message': 'future failure', 'extra': [1, 2]}},
    {'result': {'isError': True, 'content': [{'type': 'text', 'text': 'rejected'}], 'future': 3}},
    {'result': {'structuredContent': {'run': {'id': 'r', 'trackId': 'next'}}}},
])
def test_entire_unknown_envelope_saved_and_never_solved(tmp_path, response):
    archive = StartArchive()
    redact = StartRedactor()
    phrase = archive.prepare('next', TRACK, redact)
    gateway = FakeStart(settings(tmp_path))
    gateway.response = response
    assert gateway.start_once(archive, 'next', phrase, TRACK, redact) == UNKNOWN
    assert gateway.sent == 1
    assert archive.report('next')['response'] == {**response, 'id': 1}
    assert archive.report('next')['status'] == UNKNOWN
    assert archive.status('next') == 'blocked'
    for name in ('start_track', 'ask', 'guess', 'practice_guess', 'practice_ask'):
        with pytest.raises(WriteBlocked):
            gateway.call(name, {'trackId': 'next'})
    assert gateway.sent == 1
    archive.close()
    restarted = StartArchive()
    with pytest.raises(WriteBlocked):
        restarted.prepare('next', TRACK, redact)
    with pytest.raises(WriteBlocked):
        FakeStart(settings(tmp_path)).start_once(restarted, 'next', phrase, TRACK, redact)
    restarted.close()


def test_response_redaction_and_unknown_fields(tmp_path):
    archive = StartArchive()
    redact = StartRedactor(['configured-value'])
    phrase = archive.prepare('next', TRACK, redact)
    g = FakeStart(settings(tmp_path))
    g.response = {'result': {'token': 'new-token-value', 'alias': 'new-token-value',
        'content': [{'type': 'text', 'text': '{"password":"embedded-value"}'}],
        'unknown': ['configured-value', 'Bearer bearer-value', {'keep': 42}],
        'nested': {'secret': 'secret-value'}}}
    g.start_once(archive, 'next', phrase, TRACK, redact)
    saved = archive.report('next')['response']['result']
    assert saved['unknown'][2] == {'keep': 42}
    assert set(saved) == set(g.response['result'])
    from agp_race_agent.write_safety import SAFETY_DB
    for secret in ('new-token-value', 'configured-value', 'embedded-value', 'bearer-value', 'secret-value'):
        assert secret not in json.dumps(saved)
        assert secret.encode() not in SAFETY_DB.read_bytes()
    archive.close()


@pytest.mark.parametrize('error', [TimeoutError('secret text'), ConnectionResetError('private')])
def test_send_failure_never_retries(tmp_path, error):
    archive = StartArchive()
    redact = StartRedactor()
    phrase = archive.prepare('next', TRACK, redact)
    g = FakeStart(settings(tmp_path))
    g.failure = error
    assert g.start_once(archive, 'next', phrase, TRACK, redact) == UNKNOWN
    assert g.sent == 1
    assert archive.report('next')['response'] is None
    assert archive.report('next')['error_type'] == type(error).__name__
    with pytest.raises(WriteBlocked):
        g.start_once(archive, 'next', phrase, TRACK, redact)
    assert g.sent == 1
    archive.close()


def test_save_failure_does_not_allow_retry(tmp_path):
    archive = StartArchive()
    redact = StartRedactor()
    phrase = archive.prepare('next', TRACK, redact)
    g = FakeStart(settings(tmp_path))
    with patch.object(archive, 'capture', side_effect=OSError()):
        assert g.start_once(archive, 'next', phrase, TRACK, redact) == UNKNOWN
    assert archive.status('next') == 'blocked'
    with pytest.raises(WriteBlocked):
        g.start_once(archive, 'next', phrase, TRACK, redact)
    assert g.sent == 1
    archive.close()


def _concurrent_start(root, phrase, barrier, queue):
    archive = StartArchive()
    g = FakeStart(settings(root))
    barrier.wait(timeout=10)
    try:
        g.start_once(archive, 'next', phrase, TRACK, StartRedactor())
    except WriteBlocked:
        pass
    queue.put(g.sent)
    archive.close()


def test_two_processes_at_most_once(tmp_path):
    archive = StartArchive()
    phrase = archive.prepare('next', TRACK, StartRedactor())
    archive.close()
    ctx = multiprocessing.get_context('fork')
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_concurrent_start, args=(tmp_path, phrase, barrier, queue)) for _ in range(2)]
    try:
        for p in processes:
            p.start()
        assert sum(queue.get(timeout=15) for _ in processes) == 1
        for p in processes:
            p.join(timeout=5)
            assert p.exitcode == 0
    finally:
        for p in processes:
            if p.is_alive():
                p.terminate()
                p.join()
        queue.close()


def test_legacy_write_store_blocks_manual_start(tmp_path):
    archive = StartArchive()
    phrase = archive.prepare('next', TRACK, StartRedactor())
    archive.reserve('next', 'start_track', 'old-agent', None, 0)
    g = FakeStart(settings(tmp_path))
    with pytest.raises(WriteBlocked):
        g.start_once(archive, 'next', phrase, TRACK, StartRedactor())
    assert g.sent == 0
    archive.close()


def test_cli_pipe_cannot_approve(tmp_path):
    with patch('agp_race_agent.manual_start.sys.stdin.isatty', return_value=False), patch('agp_race_agent.manual_start.ObservationReader') as factory:
        assert main(['--review', '--track-id', 'next']) == 1
        factory.assert_not_called()


@pytest.mark.parametrize('tool', ['start_track', 'ask', 'guess', 'practice_guess'])
def test_direct_wire_without_permit_blocked(tmp_path, tool):
    g = FakeStart(settings(tmp_path))
    with patch.object(McpGateway, '_write') as wire:
        with pytest.raises(WriteBlocked):
            g._write({'method': 'tools/call', 'params': {'name': tool, 'arguments': {}}}, 0)
        wire.assert_not_called()


def test_watcher_announces_pending_with_audit(tmp_path):
    from agp_race_agent.watch_audit import CURRENT, WatchAudit
    reader = FakeReader([TRACK])
    notify = Mock()
    audit = WatchAudit(tmp_path, None, 30)
    binding = CURRENT.set(audit)
    try:
        watcher = TrackWatcher(lambda: reader, WatchOptions(tmp_path))
        assert watcher.run(notify=notify, max_polls=1) == 0
        assert [call.args[0] for call in notify.call_args_list] == ['BASELINE_TRACK', PENDING]
        assert watcher.auto_join_state == 'BLOCKED'
    finally:
        CURRENT.reset(binding)
        audit.close()


@pytest.mark.parametrize('approve,changed,expected', [(False, False, 0), (True, True, 0), (True, False, 1)])
def test_cli_confirmation_and_stale_evidence(tmp_path, approve, changed, expected):
    reader = Mock()
    reader.list_tracks.side_effect = [{'tracks': [TRACK]}, {'tracks': [{**TRACK, 'over': True} if changed else TRACK]}]
    reader.my_race.return_value = {'run': None}
    reader.sigil_balance.return_value = {'sigilBalance': {'creditStatus': 'unconfirmed'}}
    created = []
    def factory(config):
        g = FakeStart(config)
        created.append(g)
        return g
    original = run_approved
    def fake_run(*args):
        return original(*args, factory=factory)
    def confirmation(prompt):
        with_archive = StartArchive()
        row = with_archive.db.execute('SELECT nonce FROM manual_starts WHERE track=?', ('next',)).fetchone()
        with_archive.close()
        return f'MAX APPROVE START next {row[0]}' if approve else ''
    with patch('agp_race_agent.manual_start.load', return_value=settings(tmp_path)), \
         patch('agp_race_agent.manual_start.sys.stdin.isatty', return_value=True), \
         patch('agp_race_agent.manual_start.credential_values', return_value=[]), \
         patch('agp_race_agent.manual_start.ObservationReader', return_value=reader), \
         patch('agp_race_agent.manual_start.run_approved', side_effect=fake_run), \
         patch('builtins.input', side_effect=confirmation):
        assert main(['--review', '--track-id', 'next']) == 0
    assert sum(g.sent for g in created) == expected
    assert len(created) == expected
