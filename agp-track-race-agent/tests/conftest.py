"""Keep the durable safety store out of the real account in every unit test."""
import pytest


@pytest.fixture(autouse=True)
def isolated_write_store(tmp_path, monkeypatch):
    monkeypatch.setattr('agp_race_agent.write_safety.SAFETY_DB', tmp_path / 'account-state' / 'writes.sqlite3')


@pytest.fixture
def fake_join_window(monkeypatch):
    """Explicit synthetic permission for downstream lifecycle tests only.

    Not autouse: production eligibility tests must exercise the real deny gate.
    This does not assert any live AGP phase/status contract.
    """
    monkeypatch.setattr('agp_race_agent.agent.join_window_confirmed', lambda track, now: True)
