from unittest.mock import Mock, patch
import pytest
from agp_race_agent.readonly_probe import ProbeGateway, run_probe
from tests.test_monitor import settings


@pytest.mark.parametrize('tool', ['start_track','ask','guess','bet','practice_guess','practice_ask','unknown'])
def test_wire_guard_blocks_writes(tool):
    probe = ProbeGateway.__new__(ProbeGateway)
    with patch('agp_race_agent.gateway.McpGateway.request') as wire:
        with pytest.raises(RuntimeError):
            probe.request('tools/call', {'name':tool,'arguments':{}})
        wire.assert_not_called()


def test_monitor_only_reads_and_does_not_log_payload(tmp_path):
    gateway = Mock()
    gateway.call.side_effect = [ {'sigilBalance':{'secret':'private-value'}}, {'tracks':[{'id':'private-value'}]}, {'tracks':[]} ]
    output=Mock()
    with patch('agp_race_agent.readonly_probe.auth_check', return_value=True):
        run_probe(settings(tmp_path), factory=lambda _:gateway, output=output,
                  sleep=Mock(side_effect=[None,KeyboardInterrupt]))
    assert [c.args[0] for c in gateway.call.call_args_list] == ['sigil_balance','list_tracks','list_tracks']
    assert 'private-value' not in str(output.call_args_list)
    gateway.close.assert_called_once()


def test_failure_diagnostics_never_include_error_payload(tmp_path):
    from agp_race_agent.gateway import McpResponseError
    gateway = Mock(spec=['call','close'])
    gateway.call.side_effect = [{'sigilBalance':{}}, McpResponseError({'code':-32603,'message':'fetch failed Bearer private-secret'})]
    output = Mock()
    with patch('agp_race_agent.readonly_probe.auth_check', return_value=True):
        run_probe(settings(tmp_path), factory=lambda _:gateway, output=output, sleep=Mock(side_effect=KeyboardInterrupt))
    assert 'private-secret' not in str(output.call_args_list)
    assert 'fetch_failed' in str(output.call_args_list)
