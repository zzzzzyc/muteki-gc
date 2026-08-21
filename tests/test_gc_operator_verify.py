"""Operator verification of an existing gc_gate coordinate candidate.

Operator input can only upgrade an exact normalized, unverified, witnessed
``gc_gate`` candidate. It cannot create a coordinate fact or bypass provenance.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from muteki.control import (
    AdmissionError,
    ControlAction,
    ControlAdmission,
    ControlCommand,
    ControlActor,
    EffectState,
    RunControlMode,
    RunControlState,
    SQLiteControlJournal,
)
from muteki.core.event_bus import EventBus
from muteki.core.events import EventType
from muteki.models.solve_graph import Challenge
from muteki.vendor.geocaching_cli.coord import format_dmm, parse_coord

from tests.test_control_plane import _RecordingPort
from tests.test_gc_gate import _SLOT_DMM, _gc
from tests.test_swarm import _coordinator_swarm


ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "apps" / "web" / "ui"
_ALT_INPUT = "N 51 30.123 E 000 00.456"
_OPERATOR_ACTOR = "operator-coordinate-verifier"
_CANDIDATE_FACT = f"坐标候选 {_SLOT_DMM}"
_WITNESS = "本地形状与距离校验通过（距 posted 12 米）"


def _admission(mode: str = "geocache") -> ControlAdmission:
    return ControlAdmission(challenge_id="gc-op", challenge_mode=mode)


def _state(**kw) -> RunControlState:
    return RunControlState(run_id="run-1", **kw)


def _cmd(**kw) -> ControlCommand:
    values = dict(
        run_id="run-1",
        action=ControlAction.VERIFY_COORD,
        payload={"text": _SLOT_DMM},
    )
    values.update(kw)
    return ControlCommand(**values)


def _seed_candidate(
    graph,
    *,
    source: str = "gc_gate",
    fact: str = _CANDIDATE_FACT,
    verified: bool = False,
    witness: str = _WITNESS,
    actor: str = "cli-1",
) -> None:
    graph.add_evidence(
        actor=actor, source=source, fact=fact, verified=verified, witness=witness,
    )


async def _drain_ack(sw, cmd: dict) -> dict:
    loop = asyncio.get_running_loop()
    ack: asyncio.Future = loop.create_future()
    envelope = dict(cmd)
    envelope["_control_ack"] = ack
    sw.hitl_inbox = sw.hitl_inbox or asyncio.Queue()
    if sw._operator_event is None:
        sw._operator_event = asyncio.Event()
    await sw.hitl_inbox.put(envelope)
    task = asyncio.create_task(sw._drain_hitl())
    try:
        return await asyncio.wait_for(asyncio.shield(ack), timeout=1.0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _gc_swarm(tmp_path, **kw):
    bus = kw.pop("bus", EventBus())
    return _coordinator_swarm(_gc(id="gc-op"), tmp_path, bus=bus), bus


def _flag_found_events(graph) -> list[dict]:
    return [ev for ev in graph.events() if ev["kind"] == "flag_found"]


def _bb_kinds(events) -> list[str]:
    return [
        (ev.payload or {}).get("kind")
        for ev in events
        if ev.event_type is EventType.BLACKBOARD_DELTA
    ]


# ── enum / admission ─────────────────────────────────────────────────────────


def test_verify_coord_is_control_action():
    assert ControlAction.VERIFY_COORD is ControlAction("verify_coord")
    assert ControlAction.VERIFY_COORD.value == "verify_coord"


def test_admission_accepts_run_global_challenge_scope_with_coord_or_text():
    admission = _admission()
    state = _state()
    for scope in ("global", "run:run-1", "challenge:gc-op"):
        assert admission.admit(
            _cmd(scope=scope, payload={"text": _SLOT_DMM}), state,
        ).accepted
        assert admission.admit(
            _cmd(scope=scope, payload={"coord": _SLOT_DMM}), state,
        ).accepted


def test_admission_rejects_worker_scope_missing_content_and_oversize():
    admission = _admission()
    state = _state()
    with pytest.raises(AdmissionError) as scope_err:
        admission.admit(_cmd(scope="solver:w-1"), state)
    assert scope_err.value.code == "invalid_scope"

    with pytest.raises(AdmissionError) as missing:
        admission.admit(_cmd(payload={}), state)
    assert missing.value.code == "missing_content"

    with pytest.raises(AdmissionError) as blank:
        admission.admit(_cmd(payload={"text": "   ", "coord": ""}), state)
    assert blank.value.code == "missing_content"

    with pytest.raises(AdmissionError) as multiline:
        admission.admit(_cmd(payload={"text": f"{_SLOT_DMM}\nextra"}), state)
    assert multiline.value.code == "coord_invalid"

    with pytest.raises(AdmissionError) as huge:
        admission.admit(_cmd(payload={"text": "N " + ("1" * 260)}), state)
    assert huge.value.code == "coord_invalid"


def test_admission_rejects_terminated_and_non_geocache():
    live = _admission()
    terminated = _state(mode=RunControlMode.TERMINATED)
    with pytest.raises(AdmissionError) as term:
        live.admit(_cmd(), terminated)
    assert term.value.code == "run_terminated"

    ctf = _admission("ctf")
    with pytest.raises(AdmissionError) as mode_err:
        ctf.admit(_cmd(), _state())
    assert mode_err.value.code == "coord_rejected"

    pentest = _admission("pentest")
    with pytest.raises(AdmissionError) as pentest_err:
        pentest.admit(_cmd(), _state())
    assert pentest_err.value.code == "coord_rejected"


def test_hint_mark_false_and_submit_admission_unchanged():
    admission = ControlAdmission(challenge_id="challenge-1")
    state = _state()
    assert admission.admit(
        ControlCommand(run_id="run-1", action=ControlAction.HINT,
                       payload={"text": "try /admin"}),
        state,
    ).accepted
    assert admission.admit(
        ControlCommand(run_id="run-1", action=ControlAction.MARK_FALSE,
                       scope="global"),
        state,
    ).accepted
    assert admission.admit(
        ControlCommand(run_id="run-1", action=ControlAction.SUBMIT,
                       payload={"text": "flag{x}"}),
        state,
    ).accepted
    terminated = _state(mode=RunControlMode.TERMINATED)
    assert admission.admit(
        ControlCommand(run_id="run-1", action=ControlAction.MARK_FALSE),
        terminated,
    ).accepted
    with pytest.raises(AdmissionError, match="run-scoped action"):
        admission.admit(
            ControlCommand(run_id="run-1", action=ControlAction.MARK_FALSE,
                           scope="solver:w-1"),
            state,
        )


# ── drain authority ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_rejects_non_geocache_and_does_not_finish(tmp_path):
    challenge = Challenge(
        id="c-ctf", name="web", category="web", mode="ctf",
        flag_format=r"flag\{.*?\}",
    )
    sw = _coordinator_swarm(challenge, tmp_path, bus=EventBus())
    sw._operator_event = asyncio.Event()
    ack = await _drain_ack(sw, {
        "target": "global", "action": "verify_coord", "text": _SLOT_DMM,
    })
    assert ack["state"] == "failed"
    assert ack["metadata"]["code"] == "coord_rejected"
    assert _flag_found_events(sw.shared_graph) == []
    assert sw._found_flags == []
    assert sw._flags_complete() is False


@pytest.mark.asyncio
async def test_drain_rejects_arbitrary_coord_without_candidate(tmp_path):
    sw, bus = _gc_swarm(tmp_path)
    events: list = []
    bus.add_sink(lambda ev: events.append(ev) or asyncio.sleep(0))
    ack = await _drain_ack(sw, {
        "target": "global", "action": "verify_coord", "text": _SLOT_DMM,
    })
    assert ack["state"] == "failed"
    assert ack["metadata"]["code"] == "coord_candidate_not_found"
    assert _flag_found_events(sw.shared_graph) == []
    assert "coord_found" not in _bb_kinds(events)
    assert sw._flags_complete() is False


@pytest.mark.asyncio
async def test_drain_rejects_unparseable_coord(tmp_path):
    sw, _bus = _gc_swarm(tmp_path)
    _seed_candidate(sw.shared_graph)
    ack = await _drain_ack(sw, {
        "target": "global", "action": "verify_coord", "text": "not-a-coordinate",
    })
    assert ack["state"] == "failed"
    assert ack["metadata"]["code"] == "coord_invalid"
    assert _flag_found_events(sw.shared_graph) == []


@pytest.mark.asyncio
async def test_drain_rejects_wrong_source_verified_or_missing_witness(tmp_path):
    cases = [
        dict(source="operator", verified=False, witness=_WITNESS),
        dict(source="gc_gate", verified=True, witness=_WITNESS),
        dict(source="gc_gate", verified=False, witness=""),
        dict(source="gc_gate", verified=False, witness=None),
        dict(source="gc_gate", verified=False, witness=_WITNESS,
             fact="坐标候选 somewhere else"),
    ]
    for kwargs in cases:
        sw, bus = _gc_swarm(tmp_path / str(hash(frozenset(
            (k, str(v)) for k, v in kwargs.items()))))
        events: list = []
        bus.add_sink(lambda ev: events.append(ev) or asyncio.sleep(0))
        _seed_candidate(sw.shared_graph, **kwargs)
        ack = await _drain_ack(sw, {
            "target": "global", "action": "verify_coord", "text": _SLOT_DMM,
        })
        assert ack["state"] == "failed", kwargs
        assert ack["metadata"]["code"] == "coord_candidate_not_found", kwargs
        assert _flag_found_events(sw.shared_graph) == []
        assert "coord_found" not in _bb_kinds(events)


@pytest.mark.asyncio
async def test_drain_rejects_invalidated_candidate(tmp_path):
    sw, bus = _gc_swarm(tmp_path)
    events: list = []
    bus.add_sink(lambda ev: events.append(ev) or asyncio.sleep(0))
    _seed_candidate(sw.shared_graph)
    sw.shared_graph.reopen_after_false_positive(actor="operator", flag=_SLOT_DMM)
    ack = await _drain_ack(sw, {
        "target": "global", "action": "verify_coord", "text": _SLOT_DMM,
    })
    assert ack["state"] == "failed"
    assert ack["metadata"]["code"] == "coord_rejected"
    assert _SLOT_DMM in sw.shared_graph.invalidated_flags()
    assert _flag_found_events(sw.shared_graph) == []
    assert "coord_found" not in _bb_kinds(events)
    assert sw._flags_complete() is False


@pytest.mark.asyncio
async def test_drain_accepts_normalized_candidate_and_completes(tmp_path):
    sw, bus = _gc_swarm(tmp_path)
    events: list = []
    bus.add_sink(lambda ev: events.append(ev) or asyncio.sleep(0))
    _seed_candidate(sw.shared_graph)
    sw._operator_event = asyncio.Event()

    assert format_dmm(parse_coord(_ALT_INPUT)) == _SLOT_DMM
    ack = await _drain_ack(sw, {
        "target": "global",
        "action": "verify_coord",
        "text": _ALT_INPUT,
        "command_id": "C-verify-1",
    })

    assert ack["state"] == "effect_observed"
    assert ack["metadata"].get("verification") == "operator"
    assert _ALT_INPUT not in str(ack.get("detail") or "")
    assert _ALT_INPUT not in str(ack["metadata"])
    assert sw._operator_event.is_set()
    assert sw._found_flags == [_SLOT_DMM]
    assert sw._flags_complete() is True

    found = _flag_found_events(sw.shared_graph)
    assert len(found) == 1
    assert found[0]["actor"] == _OPERATOR_ACTOR
    assert found[0]["payload"]["flag"] == _SLOT_DMM
    snap = sw.shared_graph.snapshot()
    assert snap.flags == [_SLOT_DMM]
    verified_facts = [ev for ev in snap.evidence if ev.verified]
    assert verified_facts == []
    candidates = [ev for ev in snap.evidence if ev.fact == _CANDIDATE_FACT]
    assert candidates and candidates[0].verified is False
    assert candidates[0].source == "gc_gate"
    for ev in snap.evidence:
        assert _ALT_INPUT not in (ev.fact or "")
        assert _ALT_INPUT not in str(ev.witness or "")

    kinds = _bb_kinds(events)
    assert kinds.count("coord_found") == 1
    coord_ev = next(
        ev for ev in events
        if ev.event_type is EventType.BLACKBOARD_DELTA
        and (ev.payload or {}).get("kind") == "coord_found"
    )
    assert coord_ev.payload.get("coord") == _SLOT_DMM
    assert coord_ev.payload.get("verification") == "operator"
    assert any(
        ev.event_type is EventType.INSIGHT_BUS_EVENT
        and (ev.payload or {}).get("kind") == "FlagFound"
        and (ev.payload or {}).get("flag") == _SLOT_DMM
        for ev in events
    )


@pytest.mark.asyncio
async def test_duplicate_command_id_and_coordinate_are_idempotent(tmp_path):
    sw, bus = _gc_swarm(tmp_path)
    events: list = []
    bus.add_sink(lambda ev: events.append(ev) or asyncio.sleep(0))
    _seed_candidate(sw.shared_graph)

    first = await _drain_ack(sw, {
        "target": "global", "action": "verify_coord",
        "text": _SLOT_DMM, "command_id": "C-dup",
    })
    second = await _drain_ack(sw, {
        "target": "global", "action": "verify_coord",
        "text": _ALT_INPUT, "command_id": "C-dup",
    })
    third = await _drain_ack(sw, {
        "target": "global", "action": "verify_coord",
        "coord": _SLOT_DMM, "command_id": "C-dup-2",
    })
    assert first["state"] == "effect_observed"
    assert second["state"] == "effect_observed"
    assert third["state"] == "effect_observed"
    assert len(_flag_found_events(sw.shared_graph)) == 1
    assert _bb_kinds(events).count("coord_found") == 1
    assert sum(
        1 for ev in events
        if ev.event_type is EventType.INSIGHT_BUS_EVENT
        and (ev.payload or {}).get("kind") == "FlagFound"
    ) == 1
    assert sw.shared_graph.snapshot().flags == [_SLOT_DMM]


@pytest.mark.asyncio
async def test_actor_duplicate_command_id_does_not_reapply(tmp_path):
    journal = SQLiteControlJournal(tmp_path / "ctl.db", run_id="run-1")
    port = _RecordingPort()
    actor = ControlActor(
        run_id="run-1", journal=journal, port=port,
        admission=_admission(),
    )
    command = _cmd(command_id="C-fixed")
    first = await actor.submit_and_wait(command)
    second = await actor.submit_and_wait(
        command.model_copy(update={"created_at": command.created_at + 1}))
    assert first.state is EffectState.EFFECT_OBSERVED
    assert second.state is EffectState.EFFECT_OBSERVED
    assert len(port.calls) == 1
    await actor.close()
    journal.close()


# ── frontend slash path ──────────────────────────────────────────────────────


def test_frontend_slash_sends_verify_coord_text_through_control_api():
    convo = (UI_ROOT / "components" / "Conversation.tsx").read_text(encoding="utf-8")
    use_run = (UI_ROOT / "lib" / "useRun.ts").read_text(encoding="utf-8")
    chat = (UI_ROOT / "components" / "Chat.tsx").read_text(encoding="utf-8")
    bar = (UI_ROOT / "components" / "CommandBar.tsx").read_text(encoding="utf-8")
    assert 'raw.startsWith("/")' in convo
    assert "onCommand(cmdTarget, a, payload)" in convo
    assert "verify_coord" not in convo.split("NO_ARG")[1].split("]")[0]
    assert "`/api/runs/${runId}/control`" in use_run
    assert "body: JSON.stringify(body)" in use_run
    assert "const body: Record<string, unknown> = { target, action, text }" in use_run
    assert "shared_graph.flag_found" not in use_run
    assert "add_evidence" not in use_run
    for src in (chat, bar):
        assert 'text.startsWith("/")' in src or 'raw.startsWith("/")' in src
        assert "onCommand" in src or "onSend" in src


def test_skill_tells_operator_to_verify_and_forbids_worker_simulation():
    skill = (ROOT / "skills" / "gc-blackboard" / "SKILL.md").read_text(encoding="utf-8")
    assert "/verify_coord" in skill
    assert "checksum" in skill.lower() or "checker" in skill.lower()
    assert "不要" in skill or "never" in skill.lower()
    assert "simulate" in skill.lower() or "模拟" in skill or "伪造" in skill
