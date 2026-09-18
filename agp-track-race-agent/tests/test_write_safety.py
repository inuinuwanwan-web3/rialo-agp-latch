"""Exercise the real write gate and wire guard using an in-memory transport only."""
import io
import json
import sqlite3
import subprocess
import sys
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agp_race_agent.gateway import McpGateway
from agp_race_agent.monitor import TrackMonitor
from agp_race_agent.readonly_probe import ProbeGateway
from agp_race_agent.write_safety import AgpWriteContract, WriteBlocked, WriteStore, WRITE_TOOLS
from tests.test_agent import available_track
from tests.test_monitor import settings

TRACK = 'test-track'
BALANCE = {'sigilBalance': {'creditStatus': 'fake-confirmed', 'creditRemainingUsd': 1,
                          'creditUsedUsd': 0, 'creditCapUsd': 1, 'balanceUsd': 1}}
STATE = {'trackId': TRACK, 'started': True, 'finished': False, 'remainingUsd': 1,
         'spentUsd': 0, 'questionCostUsd': .01, 'guessCostUsd': .01}
START = {'run': {'id': 'fake-run', 'trackId': TRACK, 'finished': False}}


class FakeVerifiedContract:
    """Test-only proof; fake-confirmed is NOT an asserted AGP credit value.

    Allowance/credit proofs are Fake inputs, never invented AGP JSON fields.
    Production AgpWriteContract never returns True.
    """
    def __init__(self, *, allowance=True, credit=True):
        self.allowance, self.credit = allowance, credit

    def authorized(self, tool, balance, state):
        return self.allowance is True and self.credit is True and balance == BALANCE


class FakeWireGateway(McpGateway):
    def __init__(self, config, *, contract=None, allow_writes=True, replies=None):
        self.wire = []
        self.replies = {key: deque(values) for key, values in (replies or {}).items()}
        super().__init__(config, contract=contract, allow_writes=allow_writes)
        self.sleep = Mock()

    def _connect(self):
        self.proc = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(), poll=lambda: None)
        self._id = 0

    def _write(self, message, deadline):
        if message['method'] != 'tools/call':
            self.next_line = json.dumps({'id': message.get('id'), 'result': {}}) + '\n'
            return
        name = message['params']['name']
        self.wire.append((name, message['params']['arguments']))
        defaults = {'my_race': {'run': getattr(self, 'current_run', None)}, 'sigil_balance': BALANCE,
                    'list_tracks': {'tracks': [available_track()]}, 'track_state': STATE,
                    'start_track': START, 'ask': {'answer': 'yes'}, 'guess': {'correct': True}}
        value = self.replies[name].popleft() if self.replies.get(name) else defaults[name]
        if isinstance(value, BaseException):
            raise value
        if name == 'start_track' and isinstance(value, dict) and isinstance(value.get('run'), dict):
            self.current_run = value['run']
        self.next_line = json.dumps({'id': message['id'], 'result': {'structuredContent': value}}) + '\n'

    def _readline(self, deadline):
        return self.next_line

    def close(self):
        pass


def make(tmp_path, **kwargs):
    kwargs.setdefault('contract', FakeVerifiedContract())
    return FakeWireGateway(settings(tmp_path), **kwargs)


def writes(g):
    return [name for name, _ in g.wire if name in WRITE_TOOLS]


def prepare(g):
    g.call('my_race')
    g.call('sigil_balance')


def start(g):
    prepare(g)
    g.call('start_track', {'trackId': TRACK})
    g.call('track_state')


def blocked():
    store = WriteStore()
    try:
        assert store.status(TRACK) == 'blocked'
    finally:
        store.close()


def assert_no_more_writes(g):
    before = writes(g)
    for tool, args in [('start_track', {'trackId': TRACK}), ('ask', {'question': 'next'}), ('guess', {'guess': 'next'})]:
        with pytest.raises(WriteBlocked):
            g.call(tool, args)
    assert writes(g) == before


@pytest.mark.parametrize('failure', [TimeoutError(), ConnectionResetError(), {}, {'run': None}, {'run': {'id': 'r'}}, {'success': False}])
def test_start_uncertain_persistently_blocks_all_writes(tmp_path, failure):
    g = make(tmp_path, replies={'start_track': [failure]})
    prepare(g)
    with pytest.raises(WriteBlocked):
        g.call('start_track', {'trackId': TRACK})
    blocked()
    assert writes(g) == ['start_track']
    assert_no_more_writes(g)
    restarted = make(tmp_path)
    prepare(restarted)
    assert_no_more_writes(restarted)
    assert writes(restarted) == []


@pytest.mark.parametrize('tool,bad', [
    ('ask', TimeoutError()), ('ask', ConnectionResetError()), ('ask', {}),
    ('ask', {'answer': ''}), ('ask', {'answer': None}), ('ask', {'success': False}),
    ('guess', TimeoutError()), ('guess', {}), ('guess', {'correct': None}),
    ('guess', {'correct': 0}), ('guess', {'success': False}),
])
def test_paid_write_failure_blocks_next_write_and_restart(tmp_path, tool, bad):
    g = make(tmp_path, replies={tool: [bad]})
    start(g)
    args = {'question': 'q'} if tool == 'ask' else {'guess': 'g'}
    with pytest.raises(WriteBlocked):
        g.call(tool, args)
    blocked()
    assert writes(g) == ['start_track', tool]
    assert_no_more_writes(g)
    again = make(tmp_path)
    prepare(again)
    assert_no_more_writes(again)
    assert writes(again) == []


def test_block_survives_a_real_python_process_restart(tmp_path):
    g = make(tmp_path, replies={'start_track': [TimeoutError()]})
    prepare(g)
    with pytest.raises(WriteBlocked):
        g.call('start_track', {'trackId': TRACK})
    from agp_race_agent import write_safety
    code = "from pathlib import Path; from agp_race_agent import write_safety as w; import sys; w.SAFETY_DB=Path(sys.argv[1]); s=w.WriteStore(); print(s.status('test-track')); s.close()"
    result = subprocess.run([sys.executable, '-B', '-c', code, str(write_safety.SAFETY_DB)], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'blocked'


def test_commit_before_transport_and_interruption(tmp_path):
    g = make(tmp_path)
    prepare(g)
    original = g._write
    def interrupted(message, deadline):
        if message['params']['name'] == 'start_track':
            blocked()
            raise KeyboardInterrupt
        return original(message, deadline)
    g._write = interrupted
    with pytest.raises(KeyboardInterrupt):
        g.call('start_track', {'trackId': TRACK})
    blocked()
    assert_no_more_writes(make(tmp_path))


@pytest.mark.parametrize('another_gateway', [False, True])
def test_duplicate_registration_never_reaches_wire(tmp_path, another_gateway):
    g = make(tmp_path)
    start(g)
    other = make(tmp_path) if another_gateway else g
    prepare(other)
    with pytest.raises(WriteBlocked):
        other.call('start_track', {'trackId': TRACK})
    assert writes(g).count('start_track') == 1
    if another_gateway:
        assert writes(other) == []


def test_alternate_monitor_database_cannot_repeat_registration(tmp_path):
    g = make(tmp_path)
    start(g)
    other = FakeWireGateway(replace(settings(tmp_path), monitor_seen_file=tmp_path / 'other.sqlite'), contract=FakeVerifiedContract())
    prepare(other)
    with pytest.raises(WriteBlocked):
        other.call('start_track', {'trackId': TRACK})
    assert writes(other) == []


@pytest.mark.parametrize('contract,balance', [
    (AgpWriteContract(), BALANCE),
    (FakeVerifiedContract(allowance=None), BALANCE),
    (FakeVerifiedContract(credit=None), BALANCE),
    (FakeVerifiedContract(), {}),
    (FakeVerifiedContract(), {'sigilBalance': {}}),
    (FakeVerifiedContract(), {'sigilBalance': {'creditStatus': 'failed', 'balanceUsd': 0}}),
])
def test_unknown_funding_allows_zero_writes(tmp_path, contract, balance):
    g = make(tmp_path, contract=contract, replies={'sigil_balance': [balance]})
    prepare(g)
    with pytest.raises(WriteBlocked):
        g.call('start_track', {'trackId': TRACK})
    blocked()
    assert writes(g) == []


@pytest.mark.parametrize('tool,args', [('start_track', {'trackId': TRACK}), ('ask', {'question': 'q'}), ('guess', {'guess': 'g'})])
def test_readonly_denies_every_write_even_with_fake_contract(tmp_path, tool, args):
    g = make(tmp_path, allow_writes=False)
    with pytest.raises(WriteBlocked):
        g.call(tool, args)
    with pytest.raises(WriteBlocked):
        g.request('tools/call', {'name': tool, 'arguments': args})
    assert writes(g) == []


def test_unknown_tools_cannot_bypass_wire_guard(tmp_path):
    g = make(tmp_path)
    with pytest.raises(WriteBlocked):
        g.call('bet', {})
    with pytest.raises(WriteBlocked):
        g.request('tools/call', {'name': 'bet', 'arguments': {}})
    assert g.wire == []


def test_normal_fake_monitor_can_start_ask_guess(tmp_path):
    g = make(tmp_path, replies={'track_state': [STATE, {'trackId': TRACK, 'finished': True}]})
    with patch('agp_race_agent.agent.Solver') as solver:
        solver.return_value.decide.side_effect = [{'question': 'q'}, {'guess': 'g'}]
        result = TrackMonitor(settings(tmp_path), g).run(auto_join=True, sleep=Mock(side_effect=KeyboardInterrupt), notify=Mock())
    assert writes(g) == ['start_track', 'ask', 'guess']
    assert '監視を停止' in result


def test_reconcile_and_restart_cannot_unblock_registration(tmp_path):
    from agp_race_agent.registration import Registration
    config = settings(tmp_path)
    g = make(tmp_path, replies={'start_track': [TimeoutError()]})
    db = sqlite3.connect(config.monitor_seen_file)
    reg = Registration(config, g, db, Mock(), Mock())
    assert 'blocked' in reg.join(TRACK)
    count = len(g.wire)
    assert 'blocked' in reg.reconcile(TRACK)
    assert 'blocked' in reg.recover()
    reg.set(TRACK, 'detected')
    assert reg.status(TRACK) == 'blocked'
    reg.join(TRACK)
    assert len(g.wire) == count
    db.close()
    blocked()


def test_legacy_attempt_is_imported_to_canonical_blocklist(tmp_path):
    config = settings(tmp_path)
    with sqlite3.connect(config.monitor_seen_file) as db:
        db.execute('CREATE TABLE registration_state(id TEXT, status TEXT)')
        db.execute("INSERT INTO registration_state VALUES (?, 'registration_attempted')", (TRACK,))
    g = make(tmp_path)
    blocked()
    prepare(g)
    assert_no_more_writes(g)
    assert writes(g) == []


def test_unlabelled_stderr_is_not_put_in_exception():
    from agp_race_agent.codex_solver import _safe_stderr_tail
    assert 'x' not in _safe_stderr_tail('Bearer ' + 'x' * 3000)
    assert _safe_stderr_tail('unlabelled-credential') == '<redacted>'


@pytest.mark.parametrize('tool,args', [('ask', {'question': 'q'}), ('guess', {'guess': 'g'})])
def test_no_paid_write_before_confirmed_start(tmp_path, tool, args):
    g = make(tmp_path)
    prepare(g)
    g.call('track_state')
    with pytest.raises(WriteBlocked):
        g.call(tool, args)
    assert writes(g) == []


@pytest.mark.parametrize('state', [
    {**STATE, 'trackId': 'other'}, {**STATE, 'trackId': None},
    {**STATE, 'started': False}, {**STATE, 'status': 'waiting'},
    {**STATE, 'success': False}, {**STATE, 'remainingUsd': None},
    {**STATE, 'remainingUsd': 0}, {**STATE, 'questionCostUsd': True},
])
def test_unknown_or_conflicting_state_blocks_paid_write(tmp_path, state):
    g = make(tmp_path, replies={'track_state': [state]})
    start(g)
    with pytest.raises(WriteBlocked):
        g.call('ask', {'question': 'q'})
    blocked()
    assert writes(g) == ['start_track']


def test_run_identity_change_blocks_paid_write(tmp_path):
    g = make(tmp_path)
    start(g)
    g.current_run = {**START['run'], 'id': 'different-run'}
    with pytest.raises(WriteBlocked):
        g.call('ask', {'question': 'q'})
    blocked()
    assert writes(g) == ['start_track']


def test_normal_registered_state_cannot_be_adopted_by_new_process(tmp_path):
    g = make(tmp_path)
    start(g)
    again = make(tmp_path)
    again._write_safety.track = TRACK
    again._write_safety.run = START['run']['id']
    again.current_run = START['run']
    again.call('sigil_balance')
    again.call('track_state')
    with pytest.raises(WriteBlocked):
        again.call('guess', {'guess': 'repeat'})
    blocked()
    assert writes(again) == []


def test_failed_post_send_commit_leaves_blocked(tmp_path):
    g = make(tmp_path)
    prepare(g)
    with patch.object(WriteStore, 'complete', side_effect=sqlite3.OperationalError('private-credential')):
        with pytest.raises(WriteBlocked) as error:
            g.call('start_track', {'trackId': TRACK})
    assert 'private-credential' not in str(error.value)
    blocked()
    assert_no_more_writes(g)


def test_failed_pre_send_commit_sends_nothing(tmp_path):
    g = make(tmp_path)
    prepare(g)
    with patch.object(WriteStore, 'reserve', side_effect=sqlite3.OperationalError('private-credential')):
        with pytest.raises(WriteBlocked):
            g.call('start_track', {'trackId': TRACK})
    assert writes(g) == []
    blocked()


def test_simultaneous_registration_has_only_one_send(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    gate = Barrier(2)
    gateways = []
    def enter():
        g = make(tmp_path)
        gateways.append(g)
        prepare(g)
        gate.wait(timeout=5)
        try:
            g.call('start_track', {'trackId': TRACK})
        except WriteBlocked:
            pass
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(enter) for _ in range(2)]
        for job in jobs:
            job.result(timeout=10)
    assert sum(writes(g).count('start_track') for g in gateways) == 1


def test_single_race_entry_cannot_bypass_existing_block(tmp_path):
    from agp_race_agent.agent import RaceAgent
    from agp_race_agent.audit import AuditLog
    g = make(tmp_path, replies={'start_track': [TimeoutError()]})
    prepare(g)
    with pytest.raises(WriteBlocked):
        g.call('start_track', {'trackId': TRACK})
    again = make(tmp_path)
    agent = RaceAgent(settings(tmp_path), again, AuditLog(tmp_path / 'audit'))
    agent.run(TRACK)
    assert writes(again) == []
    blocked()


def test_audit_logger_drops_unknown_values(tmp_path):
    from agp_race_agent.audit import AuditLog
    audit = AuditLog(tmp_path)
    audit.write('tool_result', tool='ask', result_type='dict', token='private-credential')
    audit.write('private-credential', reason='private-credential')
    assert 'private-credential' not in audit.path.read_text()


@pytest.mark.parametrize('args,environment', [
    (['--token=private-credential'], {}),
    (['Authorization: Bearer private-credential'], {}),
    (['private-credential'], {'AUTH': 'private-credential'}),
])
def test_credential_arguments_are_rejected_without_echo(args, environment):
    from agp_race_agent.config import reject_secret_argv
    with pytest.raises(ValueError) as error:
        reject_secret_argv(args, environment)
    assert 'private-credential' not in str(error.value)


def test_wire_guard_cannot_be_bypassed_via_private_transport_method(tmp_path):
    g = make(tmp_path)
    with pytest.raises(WriteBlocked):
        g._call('start_track', {'trackId': TRACK})
    assert writes(g) == []


# Explicit Fake Join Window proof keeps downstream lifecycle coverage active.
import pytest as _pytest
pytestmark = _pytest.mark.usefixtures("fake_join_window")
