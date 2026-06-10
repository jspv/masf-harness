from harness import HarnessConfig, Session
from harness.spill import make_spill_parser
from harness.status import StatusBus, bind_bus
from harness.tools.code import run_python
from harness.tools.documents import read_document


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r", spill_threshold_bytes=64))


def _collect():
    bus = StatusBus()
    events = []
    bus.subscribe(events.append)
    return bus, events


def test_read_document_emits_converting_status(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "d.pdf").write_bytes(b"x")
    bus, events = _collect()
    with bind_bus(bus):
        read_document(sess, "d.pdf", convert=lambda src: "# ok")
    assert any(e.tool == "read_document" and "converting" in e.message.lower() for e in events)


def test_run_python_emits_running_status(tmp_path):
    sess = _session(tmp_path)
    bus, events = _collect()
    with bind_bus(bus):
        run_python(sess, code="from harness_sandbox import emit\nemit(1)\n")
    assert any(e.tool == "run_python" for e in events)


def test_spill_emits_stored_status(tmp_path):
    sess = _session(tmp_path)
    parse = make_spill_parser(sess, "big_tool")
    bus, events = _collect()
    with bind_bus(bus):
        parse({"rows": list(range(500))})              # over the 64-byte threshold -> spills
    stored = [e for e in events if e.tool == "big_tool" and "stored" in e.message.lower()]
    assert stored and "h1" in stored[0].message
