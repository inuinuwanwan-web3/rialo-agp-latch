from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from agp_race_agent.agent import RaceAgent, is_participable_track
from agp_race_agent.audit import AuditLog
from agp_race_agent.config import Settings


NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)


def available_track(**updates):
    # Field names/types come from the observed list_tracks response. Future
    # values below are synthetic; no live registration phase is assumed.
    track = dict(id="test-track", over=False, phase="future-test-phase",
                 timedOut=False, invitedOnly=False, teamOnly=False, started=False,
                 startsAt="2099-09-06T00:00:00Z", registrationClosesAt=None,
                 endsAt=None, racerCount=2, maxRacers=50)
    track.update(updates)
    return track


def local_settings(tmp_path):
    return Settings("unused", [], [], {}, "direct", None, "2025-03-26",
                    1.0, 12, 3, "unused-solver", tmp_path)


def test_agent_starts_available_track_without_real_mcp(tmp_path: Path):
    settings = local_settings(tmp_path)

    gateway = Mock()
    gateway.call.side_effect = [
        {"run": None},
        {"tracks": [available_track()]},
        {"creditRemainingUsd": 1, "balanceUsd": 0},
        {"run": {"id": "fake-run", "trackId": "test-track"}},
        {"finished": True, "trackId": "test-track"},
    ]

    audit = AuditLog(tmp_path)
    agent = RaceAgent(settings, gateway, audit)

    result = agent.run()

    assert result == "レースを完走しました。"
    gateway.call.assert_any_call("start_track", {"trackId": "test-track"})


@pytest.mark.parametrize("updates", [
    {}, {"maxRacers": None}, {"racerCount": 49},
    {"registrationClosesAt": "2026-09-05T10:00:00+09:00"},
    {"endsAt": "2099-09-07T00:00:00Z"},
])
def test_registration_candidates(updates):
    assert is_participable_track(available_track(**updates), NOW)


@pytest.mark.parametrize("updates", [
    {"over": True}, {"timedOut": True}, {"phase": "over"}, {"phase": "closed"},
    {"invitedOnly": True}, {"teamOnly": True}, {"started": True},
    {"startsAt": "2026-09-05T00:00:00Z"},
    {"startsAt": "2026-09-04T23:59:59Z"},
    {"registrationClosesAt": "2026-09-05T09:00:00+09:00"},
    {"registrationClosesAt": "2026-09-04T23:59:59Z"},
    {"endsAt": "2026-09-05T00:00:00Z"},
    {"racerCount": 50}, {"racerCount": 51}, {"maxRacers": 0},
    {"racerCount": -1}, {"racerCount": "2"}, {"racerCount": True},
    {"maxRacers": "50"}, {"maxRacers": True},
    {"startsAt": None}, {"startsAt": "invalid"},
    {"startsAt": "2099-09-06T00:00:00"},
    {"registrationClosesAt": "invalid"}, {"endsAt": 123},
    {"invitedOnly": "false"}, {"started": 0}, {"id": ""}, {"phase": None},
])
def test_ineligible_or_malformed_tracks(updates):
    assert not is_participable_track(available_track(**updates), NOW)


@pytest.mark.parametrize("field", list(available_track()))
def test_missing_eligibility_fields_are_rejected(field):
    track = available_track()
    del track[field]
    assert not is_participable_track(track, NOW)


@pytest.mark.parametrize("track", [None, [], "invalid"])
def test_non_object_tracks_are_rejected(track):
    assert not is_participable_track(track, NOW)


@pytest.mark.parametrize("preferred", [None, "blocked"])
def test_no_eligible_track_stops_before_balance_or_registration(tmp_path, preferred):
    gateway = Mock()
    gateway.call.side_effect = [
        {"run": None},
        {"tracks": [available_track(id="blocked", invitedOnly=True)]},
    ]
    agent = RaceAgent(local_settings(tmp_path), gateway, AuditLog(tmp_path))
    assert agent.run(preferred) == "参加可能なTrackがないため終了しました。"
    assert [call.args[0] for call in gateway.call.call_args_list] == ["my_race", "list_tracks"]


def test_skips_ineligible_track_and_selects_next_candidate(tmp_path):
    gateway = Mock()
    gateway.call.side_effect = [
        {"run": None},
        {"tracks": [available_track(id="full", racerCount=50), available_track()]},
        {"creditRemainingUsd": 1}, {"run": {"id": "fake-run", "trackId": "test-track"}}, {"finished": True, "trackId": "test-track"},
    ]
    agent = RaceAgent(local_settings(tmp_path), gateway, AuditLog(tmp_path))
    assert agent.run() == "レースを完走しました。"
    gateway.call.assert_any_call("start_track", {"trackId": "test-track"})


@pytest.mark.parametrize("registration", [
    None, {}, [], "unconfirmed", True,
    {"run": None}, {"run": {}}, {"run": []}, {"run": "fake-run"},
    {"run": {"id": None}}, {"run": {"id": ""}}, {"run": {"id": "  "}},
    {"run": {"id": 123}}, {"run": {"id": True}},
])
def test_unconfirmed_registration_stops_before_state_or_solver(tmp_path, registration):
    gateway = Mock()
    gateway.call.side_effect = [
        {"run": None}, {"tracks": [available_track()]},
        {"creditRemainingUsd": 1}, registration,
    ]
    audit = AuditLog(tmp_path)
    agent = RaceAgent(local_settings(tmp_path), gateway, audit)
    agent.solver = Mock()

    assert agent.run() == "参加登録の応答を確認できないため終了しました。"
    assert [call.args[0] for call in gateway.call.call_args_list] == [
        "my_race", "list_tracks", "sigil_balance", "start_track",
    ]
    agent.solver.decide.assert_not_called()
    assert '"reason": "registration_error"' in audit.path.read_text()


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, ValueError])
def test_registration_error_stops_without_retry_or_exposing_details(tmp_path, error_type):
    gateway = Mock()
    gateway.call.side_effect = [
        {"run": None}, {"tracks": [available_track()]},
        {"creditRemainingUsd": 1}, error_type("private-test-diagnostic"),
    ]
    audit = AuditLog(tmp_path)
    agent = RaceAgent(local_settings(tmp_path), gateway, audit)
    agent.solver = Mock()

    result = agent.run()
    assert result == "参加登録の応答を確認できないため終了しました。"
    assert [call.args[0] for call in gateway.call.call_args_list] == [
        "my_race", "list_tracks", "sigil_balance", "start_track",
    ]
    agent.solver.decide.assert_not_called()
    log = audit.path.read_text()
    assert '"reason": "registration_error"' in log
    assert "private-test-diagnostic" not in result + log


def test_existing_race_still_skips_registration(tmp_path):
    gateway = Mock()
    gateway.call.side_effect = [{"run": {"id": "existing-run"}}, {"finished": True, "trackId": "test-track"}]
    agent = RaceAgent(local_settings(tmp_path), gateway, AuditLog(tmp_path))
    assert agent.run() == "レースを完走しました。"
    assert [call.args[0] for call in gateway.call.call_args_list] == ["my_race", "track_state"]


# Explicit Fake Join Window proof keeps downstream lifecycle coverage active.
import pytest as _pytest
pytestmark = _pytest.mark.usefixtures("fake_join_window")
