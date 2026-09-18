"""Race completion through the wire gate and a real subprocess, all AGP Fake."""
import shlex
import sqlite3
import sys
from dataclasses import replace
from unittest.mock import Mock

from agp_race_agent.registration import Registration
from tests.test_write_safety import STATE, TRACK, make, writes


def test_start_ask_external_solver_guess_finished(tmp_path, fake_join_window):
    solver = tmp_path / 'fake_solver.py'
    solver.write_text(
        'import json, sys\n'
        'p = json.load(sys.stdin)\n'
        'assert p["state"]["trackId"] == "test-track"\n'
        'if not p["history"]:\n'
        '    print(json.dumps({"question": "What is the answer?"}))\n'
        'else:\n'
        '    assert p["history"] == [{"question": "What is the answer?", '
        '"answer": {"answer": "ORBIT"}}]\n'
        '    print(json.dumps({"guess": p["history"][0]["answer"]["answer"]}))\n'
    )
    gateway = make(tmp_path, replies={
        'track_state': [STATE, {'trackId': TRACK, 'finished': True}],
        'ask': [{'answer': 'ORBIT'}],
    })
    settings = replace(gateway.settings, solver_command=shlex.join([sys.executable, str(solver)]))
    with sqlite3.connect(':memory:') as db:
        registration = Registration(settings, gateway, db, Mock(), Mock())
        assert registration.join(TRACK) is None
        assert registration.status(TRACK) == 'completed'
    assert writes(gateway) == ['start_track', 'ask', 'guess']
    assert ('guess', {'guess': 'ORBIT'}) in gateway.wire
    assert gateway.wire[-1][0] == 'track_state'
