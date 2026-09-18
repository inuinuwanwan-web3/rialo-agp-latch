from collections import deque
from datetime import datetime, timezone, timedelta
import io
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agp_race_agent.gateway import McpGateway
from agp_race_agent.observation_gateway import ObservationReader, ObservationTransport
from agp_race_agent.track_watcher import TrackWatcher, WatchOptions, Redactor, SnapshotStore, manual_session
from agp_race_agent.watch_audit import WatchAudit, CURRENT, report
from tests.test_monitor import settings


class Clock:
    def __init__(self):
        self.t = 0
        self.suspend = 0

    def monotonic(self):
        return self.t

    def boot(self):
        return self.t + self.suspend

    def utc(self):
        return (datetime(2026, 1, 1, tzinfo=timezone.utc)+timedelta(seconds=self.t)).isoformat()

    def event(self):
        clock = self
        class Event:
            stopped = False
            def set(self): self.stopped = True
            def is_set(self): return self.stopped
            def wait(self, seconds): clock.t += seconds
        return Event()


class FakeTransport(ObservationTransport):
    def _connect(self):
        self.proc = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(), poll=lambda: None)
        self._id = 0

    def _readline(self, deadline):
        return self.next_line

    def close(self):
        pass


def exercise(tmp_path, outcomes=(), *, duration=4, interval=1, hook=None, secret='never-log-this-value'):
    clock = Clock()
    outcomes = deque(outcomes)
    calls = []
    config = settings(tmp_path)
    watcher = TrackWatcher(lambda: ObservationReader(config), WatchOptions(tmp_path, interval, max(4,interval)), Redactor([secret]))
    def wire(transport, message, deadline):
        params = message['params']; name = params['name']; calls.append(name)
        assert name in {'list_tracks','my_race','sigil_balance','track_state'}
        if hook:
            hook(watcher, clock, name)
        value = {'tracks': []} if name == 'list_tracks' else {'run': None, 'secret': secret}
        if name == 'list_tracks' and outcomes:
            outcome = outcomes.popleft()
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == '429':
                transport.next_line = json.dumps({'id':message['id'],'error':{'code':429,'message':secret}})
                return
        transport.next_line = json.dumps({'id':message['id'],'result':{'structuredContent':value}})
    with patch('agp_race_agent.track_watcher.time.monotonic',clock.monotonic), \
         patch('agp_race_agent.watch_audit.boot_clock',clock.boot), \
         patch('agp_race_agent.watch_audit.utc',clock.utc), \
         patch('agp_race_agent.track_watcher.threading.Event',clock.event), \
         patch('agp_race_agent.observation_gateway.ObservationTransport',FakeTransport), \
         patch.object(McpGateway,'_write',wire):
        manual_session(watcher,hours=duration/3600)
    return report(tmp_path,required_seconds=duration), calls


def test_normal_polls_and_duration_proved_by_wire_evidence(tmp_path):
    result,calls = exercise(tmp_path)
    assert result['verdict']=='PASS'
    assert result['reason']=='duration_completed'
    assert result['elapsed_seconds']==4
    assert result['poll_total']==result['poll_success']==4
    assert result['poll_failed']==0
    assert sum(result['write_attempts_at_wire'].values())==0
    assert len(calls)==12


@pytest.mark.parametrize('error,field', [
    (ConnectionResetError('never-log-this-value'),'read_error'),
    (TimeoutError('never-log-this-value'),'timeout'), ('429','429'),
])
def test_transient_error_backoff_then_measured_recovery(tmp_path,error,field):
    result,_=exercise(tmp_path,[error])
    assert result['verdict']=='PASS'
    assert result[field]==1
    assert result['backoff']==1
    assert result['recovered_error_polls']==[1]
    assert result['unrecovered_error_polls']==[]
    assert result['max_consecutive_failed']==1


def test_unrecovered_failure_is_fail_even_on_duration_exit(tmp_path):
    result,_=exercise(tmp_path,[TimeoutError()]*10)
    assert result['reason']=='duration_completed'
    assert result['verdict']=='FAIL'
    assert result['unrecovered_error_polls']


def test_manual_stop_is_not_duration_success(tmp_path):
    done=False
    def stop(watcher,clock,name):
        nonlocal done
        if not done:
            done=True;os.kill(os.getpid(),signal.SIGINT)
    result,_=exercise(tmp_path,hook=stop)
    assert result['reason']=='manual_stop'
    assert result['verdict']=='FAIL'
    assert result['poll_success']==1


def _crash(directory):
    audit=WatchAudit(Path(directory),86400,120)
    audit.acquired_lock()
    audit.begin_poll()
    os._exit(7)


def test_abnormal_process_exit_and_restart_history(tmp_path):
    ctx=multiprocessing.get_context('fork')
    child=ctx.Process(target=_crash,args=(str(tmp_path),))
    child.start();child.join(timeout=10)
    assert child.exitcode==7
    old=report(tmp_path)
    assert old['verdict']=='UNVERIFIED'
    assert old['end_utc'] is None
    new=WatchAudit(tmp_path,86400,120)
    new.acquired_lock()
    new.finish('manual_stop');new.close()
    previous=report(tmp_path,old['session_id'])
    assert previous['reason']=='abnormal_exit'
    assert previous['verdict']=='UNVERIFIED'
    assert previous['poll_total']==1


@pytest.mark.parametrize('tool',['start_track','ask','guess','practice_guess','secret-unknown-write'])
def test_write_attempt_persisted_and_blocked_before_wire(tmp_path,tool):
    audit=WatchAudit(tmp_path,1,1)
    token=CURRENT.set(audit)
    transport=ObservationTransport.__new__(ObservationTransport)
    try:
        with patch.object(McpGateway,'_write') as wire:
            with pytest.raises(RuntimeError):
                transport._write({'method':'tools/call','params':{'name':tool,'arguments':{'token':'never-log-this-value'}}},0)
            wire.assert_not_called()
        audit.finish('error')
    finally:
        CURRENT.reset(token);audit.close()
    result=report(tmp_path,required_seconds=1)
    assert result['boundary_violations']==1
    assert sum(result['write_attempts_at_wire'].values())==0
    assert result['verdict']=='FAIL'
    assert b'secret-unknown-write' not in (tmp_path/'watch_audit.sqlite3').read_bytes()


def test_auto_join_change_recorded_even_if_exception_is_caught(tmp_path):
    def change(watcher,clock,name):
        if name=='list_tracks':
            with pytest.raises(RuntimeError):watcher.auto_join_state='private-ENABLED-value'
    result,_=exercise(tmp_path,hook=change)
    assert result['auto_join_all_blocked'] is False
    assert result['verdict']=='FAIL'


def test_secrets_never_enter_audit_database(tmp_path):
    exercise(tmp_path,[TimeoutError('never-log-this-value'),'429'])
    for path in tmp_path.glob('watch_audit.sqlite3*'):
        data=path.read_bytes()
        assert b'never-log-this-value' not in data
        assert b'Bearer' not in data


def test_duplicate_poll_end_and_event_not_counted_twice(tmp_path):
    audit=WatchAudit(tmp_path,1,1)
    audit.begin_poll();audit.end_poll();audit.end_poll(TimeoutError())
    assert audit.notification(1,0,'NEW_TRACK_DETECTED') is True
    assert audit.notification(1,0,'NEW_TRACK_DETECTED') is False
    audit.finish('manual_stop');audit.close()
    result=report(tmp_path,required_seconds=1)
    assert result['poll_total']==result['poll_success']==1
    assert result['read_error']==0


def test_suspend_or_large_evidence_gap_cannot_pass(tmp_path):
    def suspend(watcher,clock,name):
        if name=='list_tracks':clock.suspend+=10
    result,_=exercise(tmp_path,hook=suspend)
    assert result['suspend_detected'] is True
    assert result['verdict']=='FAIL'


def test_missing_poll_history_never_passes(tmp_path):
    exercise(tmp_path)
    with sqlite3.connect(tmp_path/'watch_audit.sqlite3') as db:
        db.execute("DELETE FROM evidence WHERE kind='poll_end' AND poll=2")
    assert report(tmp_path,required_seconds=4)['verdict']=='FAIL'


def test_short_run_never_passes_default_24hour_report(tmp_path):
    exercise(tmp_path)
    assert report(tmp_path)['verdict']=='FAIL'


def test_audit_failure_prevents_transport_send(tmp_path):
    audit=WatchAudit(tmp_path,1,1)
    token=CURRENT.set(audit)
    transport=ObservationTransport.__new__(ObservationTransport)
    try:
        with patch.object(audit,'before_send',side_effect=sqlite3.OperationalError()),patch.object(McpGateway,'_write') as wire:
            with pytest.raises(sqlite3.Error):
                transport._write({'method':'tools/call','params':{'name':'list_tracks','arguments':{}}},0)
            wire.assert_not_called()
    finally:
        CURRENT.reset(token);audit.close()


def test_24hour_virtual_clock_full_report_uses_actual_recorded_polls(tmp_path):
    result,calls=exercise(tmp_path,duration=86400,interval=120)
    assert result['verdict']=='PASS'
    assert report(tmp_path)['verdict']=='PASS'
    assert result['poll_total']==720
    assert result['poll_success']==720
    assert len(calls)==2160
    assert result['max_evidence_gap_seconds']<=30


def test_evidence_gap_and_skipped_poll_time_fail(tmp_path):
    done=False
    def pause(watcher,clock,name):
        nonlocal done
        if not done:
            done=True;clock.t+=200
    result,_=exercise(tmp_path,hook=pause)
    assert result['max_evidence_gap_seconds']>=200
    assert result['verdict']=='FAIL'


def test_audit_report_does_not_modify_database(tmp_path):
    exercise(tmp_path)
    path=tmp_path/'watch_audit.sqlite3'
    before=path.read_bytes()
    report(tmp_path)
    assert path.read_bytes()==before


def test_inline_transport_retry_without_backoff_is_not_certified(tmp_path):
    done=False
    def error_inside_success(watcher,clock,name):
        nonlocal done
        if not done:
            done=True
            CURRENT.get().transport_error(TimeoutError('private-value'))
    result,_=exercise(tmp_path,hook=error_inside_success)
    assert result['timeout']==1
    assert result['unbacked_transport_error_polls']==[1]
    assert result['verdict']=='FAIL'
