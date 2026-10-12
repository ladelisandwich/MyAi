import pytest
from pydantic import ValidationError

from homemesh.protocol import messages as pm
from homemesh.protocol.hardware import CPUInfo, HardwareReport, MemoryInfo, OSInfo
from homemesh.protocol.jobs import validate_payload


def _hello(**kw):
    return pm.Hello(node_id="n1", node_name="pc", software_version="0.1.0", protocol_versions=[1],
                    hardware=HardwareReport(os=OSInfo(system="Linux"), cpu=CPUInfo(), memory=MemoryInfo()),
                    backends=[], models=[], task_types=["chat"], mode="balanced", limits=pm.NodeLimits(), **kw)


def test_round_trip_and_discrimination():
    h = _hello()
    parsed = pm.parse_worker_message(pm.dump(h))
    assert isinstance(parsed, pm.Hello) and parsed.node_id == "n1" and parsed.v == 1
    out = pm.parse_worker_message(pm.dump(pm.JobOutput(job_id="j", seq=1, kind="content", delta="hi")))
    assert isinstance(out, pm.JobOutput) and out.delta == "hi"
    d = pm.parse_coordinator_message(pm.dump(pm.JobDispatch(job_id="j", job_type="chat", priority="interactive",
                                                            payload={}, timeout_s=10)))
    assert isinstance(d, pm.JobDispatch)


def test_unknown_or_malformed_messages_rejected():
    with pytest.raises(ValidationError):
        pm.parse_worker_message('{"type": "exec_shell", "cmd": "rm -rf /"}')
    with pytest.raises(ValidationError):
        pm.parse_worker_message('{"type": "job.output", "job_id": "x"}')
    with pytest.raises(ValidationError):  # workers cannot send coordinator-only messages
        pm.parse_worker_message(pm.dump(pm.JobDispatch(job_id="j", job_type="chat", priority="x", payload={},
                                                       timeout_s=1)))


def test_version_negotiation():
    assert pm.negotiate_version([1, 2], (1,)) == 1
    assert pm.negotiate_version([2, 3], (2, 3)) == 3
    assert pm.negotiate_version([5], (1,)) is None


def test_payload_validation():
    with pytest.raises(ValueError):
        validate_payload("shell", {})
    with pytest.raises(ValidationError):
        validate_payload("embed", {"model": "m", "backend": "b", "inputs": []})
    p = validate_payload("chat", {"model": "m", "backend": "b", "messages": [{"role": "user", "content": "x"}]})
    assert p.messages[0].content == "x"
