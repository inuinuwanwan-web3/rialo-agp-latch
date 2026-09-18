"""Production Join Window gate, deliberately WITHOUT fake_join_window fixture."""
from unittest.mock import Mock, patch

import pytest

from agp_race_agent.agent import is_participable_track, join_window_confirmed
from agp_race_agent.monitor import TrackMonitor
from tests.test_agent import NOW, available_track
from tests.test_monitor import settings
from tests.test_write_safety import BALANCE, FakeVerifiedContract, make, writes


@pytest.mark.parametrize('updates', [
    {'over': True},                              # A
    {'phase': 'unknown'},                        # B
    {'status': 'unknown'},                       # D
    {'over': True, 'phase': 'open'},              # F
    {'started': True, 'phase': 'upcoming'},       # F
    {'phase': 'future-agp-value-v999'},           # G
    {'phase': {'unexpected': 'schema'}},
    {'status': ['unexpected']},
    {'phase': 'open', 'status': 'open'},
    {'phase': 'joinable', 'status': 'joinable'},
    {'phase': 'upcoming'},
    {},                                         # H
])
def test_production_candidate_rejects_without_official_contract(updates):
    track = available_track(**updates)
    assert join_window_confirmed(track, NOW) is False
    assert is_participable_track(track, NOW) is False


@pytest.mark.parametrize('field', ['phase', 'status', 'startsAt', 'registrationClosesAt', 'endsAt'])
def test_missing_phase_status_or_window_information_is_rejected(field):
    track = available_track(status='synthetic-only')
    track.pop(field)
    assert not is_participable_track(track, NOW)


def test_production_denial_reaches_no_start_even_with_fake_funding_approved(tmp_path):
    contract = FakeVerifiedContract()
    assert contract.authorized('start_track', BALANCE, None) is True
    gateway = make(tmp_path, contract=contract)
    # Would raise on any attempted write, even before the fake wire is reached.
    original = gateway.call
    def read_only_call(name, arguments=None):
        assert name in {'list_tracks', 'my_race', 'sigil_balance', 'track_state'}
        return original(name, arguments)
    gateway.call = Mock(side_effect=read_only_call)
    with patch('agp_race_agent.write_safety.WriteSafety.perform') as write_gate:
        TrackMonitor(settings(tmp_path), gateway).run(auto_join=True,
            sleep=Mock(side_effect=KeyboardInterrupt), notify=Mock())
        write_gate.assert_not_called()
    assert writes(gateway) == []
    assert any(call.args[0] == 'list_tracks' for call in gateway.call.call_args_list)


def test_fake_permission_is_not_loaded_by_production():
    assert join_window_confirmed(available_track(), NOW) is False
