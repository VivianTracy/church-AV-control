from __future__ import annotations

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from time import sleep
from urllib.parse import urlparse

from church_av.agent import Guide, explain, local_reply
from church_av.a2ui import SURFACE_ID, build_messages
from church_av.config import Settings
from church_av.detector import Detector
from church_av.fixtures import FIXTURE_NAMES, load_fixture
from church_av.models import BoardReading, Fault, Snapshot
from church_av.obs_read import ObsReader
from church_av.policy import decide, decide_mapping
from church_av.runbook import Runbook, load_runbook, write_strip
from church_av.x32_read import X32Reader


class Booth:
    def __init__(
        self,
        settings: Settings,
        reader: ObsReader,
        runbook: Runbook,
        board_reader: X32Reader | None = None,
    ) -> None:
        self.settings = settings
        self.reader = reader
        self.board_reader = board_reader
        self.runbook = runbook
        self.detector = Detector()
        self._lock = Lock()
        self.snapshot = Snapshot(connected=False, error="Waiting for the first reading")
        self.faults: list[Fault] = []
        self.last_action = ""
        self.guide = Guide("Waiting for the first reading.", "detector")
        self._guide_fault_ids: tuple[str, ...] | None = None
        self.chat_guide: Guide | None = None
        self.chat_question = ""
        self.chat_scope = ""

    def tick(self) -> None:
        snapshot = self.reader.read()
        snapshot.board = self._board_reading()
        faults = self.detector.detect(snapshot, self.runbook)
        fault_ids = tuple(fault.id for fault in faults)
        with self._lock:
            changed = fault_ids != self._guide_fault_ids
            guide = self.guide
        if changed:
            guide = explain(
                snapshot,
                self.runbook,
                faults,
                api_key=self.settings.google_api_key,
            )
        with self._lock:
            self.snapshot = snapshot
            self.faults = faults
            self.guide = guide
            self._guide_fault_ids = fault_ids

    def state(self) -> dict:
        with self._lock:
            snapshot = self.snapshot
            faults = list(self.faults)
            last_action = self.last_action
            guide, asked = self._chat_for("live")
            if guide is None:
                guide = self.guide
        return {
            "connected": snapshot.connected,
            "programScene": snapshot.program_scene,
            "streaming": snapshot.streaming,
            "allowApply": self.settings.allow_apply,
            "lastAction": last_action,
            "guide": {"reply": guide.reply, "source": guide.source},
            "error": snapshot.error,
            "snapshot": asdict(snapshot),
            "faults": [_fault_dict(fault) for fault in faults],
            "a2ui": build_messages(
                snapshot,
                self.runbook,
                faults,
                self.settings.allow_apply,
                guide=guide.reply,
                asked=asked,
            ),
        }

    def ask(self, question: str, preview: str = "") -> Guide:
        """Answer one operator question. A preview name reads a saved snapshot, not OBS."""
        preview = preview.strip()
        if preview:
            if preview not in FIXTURE_NAMES:
                raise ValueError("Unknown preview")
            snapshot, faults = _fixture_reading(self.runbook, preview)
            scope = preview
        else:
            with self._lock:
                snapshot = self.snapshot
                faults = list(self.faults)
            scope = "live"
        guide = explain(
            snapshot,
            self.runbook,
            faults,
            question=question,
            api_key=self.settings.google_api_key,
        )
        with self._lock:
            self.chat_guide = guide
            self.chat_question = question
            self.chat_scope = scope
        return guide

    def _chat_for(self, scope: str) -> tuple[Guide | None, str]:
        if self.chat_guide is not None and self.chat_scope == scope:
            return self.chat_guide, self.chat_question
        return None, ""

    def handle_action(self, message: dict) -> dict:
        action = message.get("action") if isinstance(message, dict) else None
        if not isinstance(action, dict):
            return {"ok": False, "message": "Missing action."}
        name = action.get("name")
        context = action.get("context") if isinstance(action.get("context"), dict) else {}
        fault_id = str(context.get("faultId", ""))
        with self._lock:
            fault = next((item for item in self.faults if item.id == fault_id), None)
        if name == "map_channel":
            channel_id = str(context.get("channelId", "")).strip()
            strip = str(context.get("strip", "")).strip()
            decision = decide_mapping(channel_id, strip, self.runbook)
            if not decision.allowed:
                return {"ok": False, "message": decision.reason}
            try:
                write_strip(self.settings.runbook_path, channel_id, strip)
            except (OSError, ValueError) as exc:
                return {"ok": False, "message": str(exc)}
            self.runbook = self.runbook.with_strip(channel_id, strip)
            return {"ok": True, "message": decision.reason}
        if name == "ask":
            question = str(context.get("question", "")).strip()
            preview = str(context.get("preview", "")).strip()
            if not question:
                return {"ok": False, "message": "Type a question first."}
            try:
                guide = self.ask(question, preview)
            except ValueError as exc:
                return {"ok": False, "message": str(exc)}
            return {
                "ok": True,
                "message": guide.reply,
                "guide": {"reply": guide.reply, "source": guide.source},
            }
        if name == "dismiss_fault":
            self.detector.dismiss(fault_id)
            self.tick()
            return {"ok": True, "message": "Dismissed."}
        if name != "apply_fix":
            return {"ok": False, "message": f"Unknown action {name}."}
        if fault is None or fault.action is None:
            return {"ok": False, "message": "That fault has no change to apply."}
        if not self.settings.allow_apply:
            return {
                "ok": False,
                "message": "Apply is off. This computer is watching only. Set ALLOW_APPLY=true to let a button change OBS.",
            }
        with self._lock:
            snapshot = self.snapshot
        try:
            summary = self._apply(fault, snapshot)
        except Exception as exc:
            message_text = str(exc).strip() or exc.__class__.__name__
            with self._lock:
                self.last_action = message_text
            return {"ok": False, "message": message_text}
        with self._lock:
            self.last_action = summary
        self.tick()
        return {"ok": True, "message": summary}

    def _apply(self, fault: Fault, snapshot: Snapshot) -> str:
        decision = decide(fault.action, snapshot, self.runbook, confirmed=True)
        if not decision.allowed or decision.action is None:
            raise RuntimeError(decision.reason)
        action = decision.action
        if action.kind == "set_input_mute" and action.input_name is not None:
            self.reader.set_input_mute(action.input_name, False)
        elif action.kind == "set_input_volume" and action.input_name is not None and action.volume_db is not None:
            self.reader.set_input_volume_db(action.input_name, action.volume_db)
        elif action.kind == "set_program_scene" and action.scene_name is not None:
            self.reader.set_program_scene(action.scene_name)
        else:
            raise RuntimeError("That change is not available from the booth.")
        return decision.reason

    def _board_reading(self) -> BoardReading | None:
        if self.board_reader is None:
            return None
        try:
            return self.board_reader.read(self.runbook)
        except Exception as exc:
            return BoardReading(connected=False, error=str(exc).strip() or exc.__class__.__name__)


def _fault_dict(fault: Fault) -> dict:
    return {
        "id": fault.id,
        "severity": fault.severity,
        "title": fault.title,
        "evidence": fault.evidence,
        "steps": fault.steps,
        "action": None
        if fault.action is None
        else {
            "kind": fault.action.kind,
            "summary": fault.action.summary,
            "inputName": fault.action.input_name,
        },
    }


class _Handler(BaseHTTPRequestHandler):
    booth: Booth

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._bytes(200, "text/html; charset=utf-8", PAGE.encode())
            return
        if path == "/api/state":
            self._bytes(200, "application/json", json.dumps(self.booth.state()).encode())
            return
        if path == "/api/a2ui":
            body = json.dumps(self.booth.state()["a2ui"]).encode()
            self._bytes(200, "application/json", body)
            return
        if path.startswith("/api/preview/"):
            name = path.removeprefix("/api/preview/")
            if name not in FIXTURE_NAMES:
                self._bytes(404, "text/plain", b"Unknown preview")
                return
            self._bytes(200, "application/json", json.dumps(_preview_state(self.booth, name)).encode())
            return
        self._bytes(404, "text/plain", b"Not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/a2ui/action":
            self._bytes(404, "text/plain", b"Not found")
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            message = json.loads(raw.decode())
        except json.JSONDecodeError:
            self._bytes(400, "application/json", b'{"ok": false, "message": "Invalid JSON"}')
            return
        if "version" not in message:
            message = {"version": "1.0", "action": message.get("action", message)}
        result = self.booth.handle_action(message)
        status = 200 if result.get("ok") else 409
        self._bytes(status, "application/json", json.dumps(result).encode())

    def _bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(booth: Booth) -> None:
    _Handler.booth = booth
    server = ThreadingHTTPServer((booth.settings.booth_host, booth.settings.booth_port), _Handler)
    thread = Thread(target=_poll, args=(booth,), daemon=True)
    thread.start()
    host = booth.settings.booth_host
    port = booth.settings.booth_port
    print(f"Booth at http://{host}:{port}", flush=True)
    print(f"Reading OBS at {booth.settings.obs_host}:{booth.settings.obs_port}", flush=True)
    if booth.settings.x32_host:
        print(f"Reading X32 at {booth.settings.x32_host}:{booth.settings.x32_port}", flush=True)
    else:
        print("X32 OSC is off. OBS still shows the one X-USB feed.", flush=True)
    try:
        server.serve_forever()
    finally:
        booth.reader.close()
        if booth.board_reader is not None:
            booth.board_reader.close()
        server.server_close()


def _fixture_reading(runbook: Runbook, name: str) -> tuple[Snapshot, list[Fault]]:
    detector = Detector()
    snapshot = load_fixture(name)[0]
    faults: list[Fault] = []
    for item in load_fixture(name):
        snapshot = item
        faults = detector.detect(item, runbook)
    return snapshot, faults


def _preview_state(booth: Booth, name: str) -> dict:
    snapshot, faults = _fixture_reading(booth.runbook, name)
    with booth._lock:
        guide, asked = booth._chat_for(name)
    if guide is None:
        guide = Guide(local_reply(snapshot, booth.runbook, faults), "detector")
        asked = ""
    return {
        "connected": snapshot.connected,
        "programScene": snapshot.program_scene,
        "streaming": snapshot.streaming,
        "allowApply": False,
        "preview": name,
        "guide": {"reply": guide.reply, "source": guide.source},
        "a2ui": build_messages(
            snapshot,
            booth.runbook,
            faults,
            allow_apply=False,
            guide=guide.reply,
            asked=asked,
        ),
    }


def _poll(booth: Booth) -> None:
    while True:
        booth.tick()
        sleep(booth.settings.poll_seconds)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Church stream booth</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0c0d10;
    --card: #16181d;
    --inset: #101114;
    --line: #2a2e36;
    --text: #f4f5f7;
    --muted: #9aa1ab;
    --ok: #3dd68c;
    --warn: #e8b15a;
    --bad: #f07167;
  }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 16px/1.5 ui-sans-serif, system-ui, sans-serif; }
  main { max-width: none; margin: 0; padding: 1.1rem 1.25rem 2.5rem; }
  h1 { font-size: 1.25rem; font-weight: 600; margin: 0; letter-spacing: -0.02em; }
  h2 { font-size: 0.8rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
       color: var(--muted); margin: 0.4rem 0 0; }
  #root { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(18rem, 0.85fr); gap: 0.85rem; align-items: start; }
  #title, #path_card, #chat, #fault_heading { grid-column: 1 / -1; }
  #root > article[id^="fault_"], #root > #clear { grid-column: 1; }
  #guide { grid-column: 1 / -1; }
  #root:has(> article[id^="fault_"]) #guide, #root:has(> #clear) #guide { grid-column: 2; }
  article { border-radius: 16px; background: var(--card); border: 1px solid var(--line); }
  .row { display: flex; gap: 0.75rem; align-items: stretch; }
  .column { display: flex; flex-direction: column; gap: 0.35rem; min-width: 0; }
  #status { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.75rem; }
  #status > article { padding: 1.05rem 1.15rem 1.15rem; }
  #mix_body { display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: 0.85rem; align-items: center; }
  #status .kicker { color: var(--muted); font-size: 0.72rem; letter-spacing: 0.06em; text-transform: uppercase; margin: 0; }
  #status .value { font-size: 1.85rem; font-weight: 600; letter-spacing: -0.03em; line-height: 1.1; margin: 0.15rem 0 0; }
  #status .detail { color: var(--muted); font-size: 0.92rem; margin: 0.2rem 0 0; }
  #status svg.icon { color: var(--muted); }
  #stream_card[data-state="Live"] .value,
  #mix_card[data-state="Open"] .value { color: var(--ok); }
  #stream_card[data-state="Stopped"] .value { color: var(--muted); }
  #stream_card[data-state="Offline"] .value,
  #stream_card[data-state="Reconnecting"] .value,
  #mix_card[data-state="Muted"] .value,
  #mix_card[data-state="Missing"] .value { color: var(--bad); }
  svg.icon { width: 1.35rem; height: 1.35rem; display: block; }
  #meter { grid-column: 2; grid-row: 1 / span 4; margin: 0; flex-direction: column; align-items: center; gap: 0.35rem; }
  #meter_caption { font-size: 0.65rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }
  .strip { display: flex; align-items: flex-end; gap: 0.45rem; height: 5.6rem; }
  .meter { display: flex; flex-direction: column-reverse; gap: 2px; height: 5.6rem; width: 0.45rem; }
  .meter i { display: block; flex: 1; width: 100%; background: #2a2e36; border-radius: 2px; }
  .meter i.lit { background: var(--ok); }
  .meter i.hot { background: var(--warn); }
  .meter.is-muted i { background: #5a3030; }
  .readout { font-size: 0.7rem; color: var(--muted); text-align: center; }
  #clear { background: #121a16; border-color: #1e3a2c; }
  #clear .row { align-items: center; padding: 1rem 1.15rem; gap: 0.75rem; }
  #clear svg.icon { color: var(--ok); }
  #clear p { margin: 0; font-size: 1.02rem; }
  article[id^="fault_"] { background: #1c1614; border-color: #3d2c28; }
  article[id^="fault_"] > .row,
  article[id^="fault_"] > .column { align-items: stretch; }
  .x32 { margin: 1rem 1rem 0; background: var(--inset); border-radius: 12px; padding: 0.75rem 0.7rem 0.65rem; }
  .x32-name { color: var(--warn); font-size: 0.7rem; letter-spacing: 0.06em; text-transform: uppercase; margin-bottom: 0.45rem; }
  .x32-strips { display: flex; align-items: flex-end; gap: 0.35rem; }
  .x32-gap { width: 0.7rem; flex: 0 0 0.7rem; }
  .x32-strip { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; gap: 0.25rem; }
  .x32-arrow { height: 1.05rem; color: var(--bad); font-size: 1.05rem; font-weight: 650; line-height: 1; }
  .x32-fader { position: relative; width: 0.4rem; height: 5.2rem; background: #2a2e36; border-radius: 99px; }
  .x32-strip.is-hot .x32-fader { background: #3a3030; }
  .x32-knob { position: absolute; left: -0.28rem; width: 0.95rem; height: 0.38rem; background: #8b919a; border-radius: 2px; }
  .x32-strip.is-hot .x32-knob { background: var(--text); }
  .x32-label { font-size: 0.68rem; line-height: 1.25; text-align: center; color: var(--muted); }
  .x32-strip.is-hot .x32-label { color: var(--text); font-weight: 600; }
  .mixer, .badge { width: 6.25rem; flex: 0 0 6.25rem; background: var(--inset); align-items: center;
                  justify-content: flex-end; padding: 1rem 0.45rem; gap: 0.45rem; border-radius: 16px 0 0 16px; }
  .badge { justify-content: center; }
  .track { width: 0.4rem; height: 6.2rem; background: #2a2e36; position: relative; border-radius: 99px; }
  .knob { position: absolute; left: -0.28rem; width: 0.95rem; height: 0.38rem; background: var(--text); border-radius: 2px; }
  .mixer .speaker, .badge .speaker { width: 2rem; height: 2rem; border: 1.5px solid var(--text); border-radius: 99px;
                    display: grid; place-items: center; }
  .mixer.is-muted .speaker { border-color: var(--bad); color: var(--bad); }
  .place { color: var(--warn); font-size: 0.72rem; letter-spacing: 0.06em; text-transform: uppercase; margin: 0; }
  .control { font-size: 1.35rem; font-weight: 600; letter-spacing: -0.02em; margin: 0; }
  .copy { padding: 1rem 1.15rem 1.1rem; gap: 0.35rem; flex: 1; }
  .copy .steps { margin: 0.35rem 0 0; padding-left: 0; list-style: none; counter-reset: step; }
  .copy .steps li { counter-increment: step; margin: 0.4rem 0; color: var(--text); }
  .copy .steps li::before { content: counter(step); display: inline-grid; place-items: center;
       width: 1.35rem; height: 1.35rem; margin-right: 0.5rem; background: #2a2422; border-radius: 99px; font-size: 0.75rem; }
  button { font: inherit; background: var(--text); color: #111214; border: 0; border-radius: 10px;
           padding: 0.55rem 0.9rem; margin: 0; cursor: pointer; }
  #path_card { padding: 1.05rem 1.15rem 1.1rem; }
  #path_kicker { color: var(--muted); font-size: 0.72rem; letter-spacing: 0.06em; text-transform: uppercase; margin: 0; }
  #guide { }
  #guide_body { padding: 1.05rem 1.15rem 1.15rem; gap: 0.35rem; }
  #guide_kicker { color: var(--muted); font-size: 0.72rem; letter-spacing: 0.06em; text-transform: uppercase; margin: 0; }
  #guide_icon { color: var(--bad); width: 1.35rem; height: 1.35rem; }
  #guide_reply { margin: 0.15rem 0 0; font-size: 1.05rem; line-height: 1.5; }
  .setup { display: flex; flex-direction: column; gap: 0.85rem; margin: 0.85rem 0 0; }
  .setup-columns { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.15fr); gap: 0.75rem; align-items: start; }
  .picture-column, .audio-column { background: var(--inset); border: 1px solid var(--line); border-radius: 16px; padding: 0.85rem 0.9rem 0.95rem; min-width: 0; }
  .panel-title { margin: 0 0 0.7rem; font-size: 1rem; font-weight: 650; letter-spacing: -0.02em; text-transform: none; }
  .scene-dock { margin-top: 0.8rem; }
  .scene-dock #scene_heading { margin: 0 0 0.45rem; }
  .scene-dock #scene_strip { display: flex; flex-wrap: wrap; gap: 0.4rem; }
  .scene-dock #scene_strip button { width: auto; margin: 0; background: #1a1d24; color: var(--text);
                                    border: 1px solid var(--line); border-radius: 999px; padding: 0.4rem 0.85rem; cursor: pointer; }
  .scene-dock #scene_strip #scene_current { background: #14663d; border-color: #1f8a52; color: #fff; font-weight: 650; }
  .scene-dock #scene_strip #scene_current.is-selected { outline: 2px solid #fff; outline-offset: 2px; }
  .scene-dock .swatch { display: none; }
  .path-row { display: flex; flex-wrap: nowrap; align-items: stretch; gap: 0.35rem; }
  .picture-column .path-row { flex-wrap: wrap; }
  button.path-node { position: relative; flex: 1 1 0; min-width: 0; min-height: 4.6rem; margin: 0; padding: 0.7rem 0.55rem;
                      border-radius: 12px; border: 1px solid #2a2e36; background: #1a1d24; color: var(--text);
                      text-align: center; cursor: pointer; font: inherit; }
  button.path-node.is-selected { border-color: var(--text); }
  button.path-node .path-dot { display: block; width: 0.48rem; height: 0.48rem; margin: 0 auto 0.28rem; border-radius: 99px; background: #6b7280; }
  button.path-node[data-state="ok"] .path-dot { background: var(--ok); box-shadow: 0 0 0 3px rgba(61, 214, 140, 0.22); }
  button.path-node[data-state="bad"] .path-dot { background: var(--bad); box-shadow: 0 0 0 3px rgba(240, 113, 103, 0.22); }
  button.path-node .kicker { display: block; color: var(--muted); font-size: 0.68rem; letter-spacing: 0.04em; text-transform: uppercase; }
  button.path-node strong { display: block; font-size: 0.92rem; font-weight: 650; line-height: 1.25; overflow-wrap: anywhere; }
  button.path-node .status { display: block; margin-top: 0.12rem; font-size: 0.7rem; color: var(--muted); overflow-wrap: anywhere; }
  button.path-node[data-state="ok"] .status { color: var(--ok); }
  button.path-node[data-state="bad"] .status { color: var(--bad); }
  button.path-node .reading { display: block; margin-top: 0.08rem; font-size: 0.66rem; color: #8b939e; overflow-wrap: anywhere; }
  .setup-arrow { flex: 0 0 auto; align-self: center; color: #5c6370; font-size: 0.9rem; }
  .meter-row { display: flex; align-items: center; gap: 0.7rem; margin-top: 0.85rem; }
  .meter-row p { margin: 0; color: var(--muted); font-size: 0.82rem; white-space: nowrap; }
  .segments { display: flex; gap: 3px; flex: 1; height: 0.8rem; min-width: 0; }
  .segments i { flex: 1; background: #2a2e36; border-radius: 2px; }
  .segments i.lit { background: var(--ok); }
  .segments i.hot { background: var(--warn); }
  .setup-detail { margin: 0; padding: 0.75rem 0.85rem; background: var(--inset); border-radius: 10px; color: var(--text); font-size: 0.95rem; }
  .setup-aside { margin: 0; padding-left: 1.1rem; color: var(--muted); font-size: 0.88rem; }
  .setup-aside li { margin: 0.2rem 0; }
  #guide_asked { margin: 0; color: var(--muted); }
  #chat { align-items: end; gap: 0.6rem; background: var(--card); border: 1px solid var(--line);
          border-radius: 16px; padding: 1rem 1.15rem 1.1rem; }
  #question { flex: 1; display: flex; flex-direction: column; gap: 0.35rem; color: var(--muted);
              font-size: 0.72rem; letter-spacing: 0.06em; text-transform: uppercase; }
  #question input { font: 16px/1.4 ui-sans-serif, system-ui, sans-serif; text-transform: none;
                    letter-spacing: 0; color: var(--text); background: var(--card); border: 1px solid var(--line);
                    border-radius: 12px; padding: 0.7rem 0.85rem; }
  #question input:focus { outline: 1px solid #5c6370; outline-offset: 1px; }
  #scene_strip { display: grid; grid-template-columns: repeat(auto-fill, minmax(12.5rem, 1fr)); gap: 0.5rem; }
  #scene_strip article { background: var(--card); border: 1px solid var(--line); padding: 0.55rem 0.7rem 0.6rem; border-radius: 12px; }
  #scene_current { background: #1c1f26; border-color: #8b919a; }
  #scene_strip p { margin: 0; color: var(--muted); font-size: 0.84rem; }
  #scene_current p { color: var(--text); font-weight: 600; }
  .swatch { height: 3px; margin: -0.55rem -0.7rem 0.5rem; border-radius: 12px 12px 0 0; }
  .raw { margin-top: 1.75rem; color: #6d7380; font-size: 0.8rem; }
  pre { white-space: pre-wrap; }
  @media (max-width: 720px) {
    main { padding: 1.15rem 0.85rem 3rem; }
    #root { grid-template-columns: 1fr; }
    #title, #path_card, #chat, #guide, #clear, #fault_heading, article[id^="fault_"] { grid-column: 1; }
    .setup-columns { grid-template-columns: 1fr; }
    .path-row { flex-wrap: wrap; }
    button.path-node { flex: 1 1 5.2rem; min-width: 5.2rem; }
    article[id^="fault_"] > .row,
    article[id^="fault_"] > .column { flex-direction: column; }
    .mixer, .badge { width: auto; flex-basis: auto; flex-direction: row; justify-content: flex-start;
                     border-radius: 16px 16px 0 0; }
    .mixer .track { height: 4.5rem; }
    #chat { flex-direction: column; align-items: stretch; }
  }
</style>
</head>
<body>
<main>
  <div id="screen">Waiting for the service machine…</div>
  <details class="raw"><summary>A2UI messages</summary><pre id="raw"></pre></details>
</main>
<script>
const SURFACE = "booth";
const preview = new URLSearchParams(location.search).get("preview");
const drafts = {};
let selectedBox = "";
let pathNotes = {};

function questionInput() {
  const field = document.getElementById("question");
  if (!field) return null;
  return field.tagName === "INPUT" ? field : field.querySelector("input");
}

function rememberDraft() {
  const input = questionInput();
  if (!input) return;
  drafts.question = input.value;
  drafts.focused = document.activeElement === input;
  drafts.caret = input.selectionStart;
}

function restoreDraft() {
  const input = questionInput();
  if (!input || drafts.question == null) return;
  input.value = drafts.question;
  if (drafts.focused) {
    input.focus();
    const caret = drafts.caret == null ? input.value.length : drafts.caret;
    input.setSelectionRange(caret, caret);
  }
}

function textOf(byId, id) {
  const found = byId[id];
  return found && typeof found.text === "string" ? found.text : "";
}

function icon(name) {
  const paths = {
    camera: '<rect x="3" y="7" width="18" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="13" r="3" fill="none" stroke="currentColor" stroke-width="1.8"/>',
    play: '<path d="M8 6l10 6-10 6z" fill="currentColor"/>',
    stop: '<rect x="7" y="7" width="10" height="10" fill="currentColor"/>',
    warning: '<path d="M12 4l9 16H3z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 10v4" stroke="currentColor" stroke-width="1.8"/>',
    check: '<path d="M5 12l5 5 9-10" fill="none" stroke="currentColor" stroke-width="2"/>',
    volumeUp: '<path d="M4 10h3l5-4v12l-5-4H4z" fill="currentColor"/><path d="M16 9a4 4 0 010 6" fill="none" stroke="currentColor" stroke-width="1.6"/>',
    volumeMute: '<path d="M4 10h3l5-4v12l-5-4H4z" fill="currentColor"/><path d="M16 10l5 5M21 10l-5 5" stroke="currentColor" stroke-width="1.6"/>'
  };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = paths[name] || paths.warning;
  return svg;
}

function meter(data) {
  const wrap = document.createElement("div");
  const muted = Boolean(data && data.muted);
  wrap.className = "meter" + (muted ? " is-muted" : "");
  let lit = 0;
  const db = data ? data.meterDb : null;
  if (!muted && typeof db === "number" && db > -90) {
    const clamped = Math.max(-60, Math.min(0, db));
    lit = Math.round(((clamped + 60) / 60) * 16);
  }
  for (let i = 0; i < 16; i += 1) {
    const seg = document.createElement("i");
    if (i < lit) seg.className = i >= 13 ? "hot" : "lit";
    wrap.appendChild(seg);
  }
  return wrap;
}

function readout(data) {
  const el = document.createElement("div");
  el.className = "readout";
  const db = data ? data.meterDb : null;
  if (data && data.muted) el.textContent = "mute";
  else if (typeof db === "number" && db > -90) el.textContent = Math.round(db) + " dB";
  else el.textContent = "quiet";
  return el;
}

function fader(data) {
  const track = document.createElement("div");
  track.className = "track";
  const knob = document.createElement("div");
  knob.className = "knob";
  const db = data && typeof data.faderDb === "number" ? data.faderDb : -60;
  const clamped = Math.max(-60, Math.min(0, db));
  const fraction = (clamped + 60) / 60;
  knob.style.bottom = (fraction * 88) + "%";
  track.appendChild(knob);
  return track;
}

const SCENE_COLORS = {
  "1-Stage Only": ["#243044"],
  "2-Slide Only": ["#3d3428"],
  "3-Stage 50/Slide 50": ["#243044", "#3d3428"],
  "4-Stage 100/Slide 30": ["#243044", "#243044", "#243044", "#3d3428"],
  "5-Stage 30/Slide 70": ["#243044", "#3d3428", "#3d3428"],
  "6-Stage 30/Slide 100": ["#3d3428", "#3d3428", "#3d3428", "#243044"],
  "7-Mobile Cam 50/Slide 50": ["#24362c", "#3d3428"],
  "8-Mobile Cam Only": ["#24362c"],
  "9-Studio (Wall Cam + Mobile Cam)": ["#243044", "#24362c"],
  "10-Acer Monitor": ["#2c3034"],
  "11-Acer 70 + Slide 30": ["#2c3034", "#2c3034", "#3d3428"]
};

function swatch(colors) {
  const el = document.createElement("div");
  el.className = "swatch";
  el.style.background = colors.length === 1 ? colors[0] : "linear-gradient(90deg," + colors.map((color, index) => {
    const start = (index / colors.length) * 100;
    const end = ((index + 1) / colors.length) * 100;
    return color + " " + start + "% " + end + "%";
  }).join(",") + ")";
  return el;
}

function renderNode(id, byId, data) {
  const node = byId[id];
  if (!node) return document.createElement("span");
  if (node.component === "Text") {
    const el = document.createElement(id === "title" ? "h1" : (id.endsWith("heading") ? "h2" : "p"));
    el.id = id;
    el.textContent = node.text || "";
    if (id.endsWith("_kicker") || id.endsWith("_detail") || id.endsWith("_where")) el.className = id.endsWith("_where") ? "place" : (id.endsWith("_kicker") ? "kicker" : "detail");
    if (id.endsWith("_value") || id.endsWith("_control")) el.className = id.endsWith("_control") ? "control" : "value";
    return el;
  }
  if (node.component === "Icon") {
    const svg = icon(node.name);
    svg.id = id;
    return svg;
  }
  if (node.component === "TextField") {
    const field = document.createElement("label");
    field.id = id;
    field.textContent = node.label || "Ask";
    const input = document.createElement("input");
    input.type = node.textFieldType === "obscured" ? "password" : "text";
    input.value = drafts.question || node.text || "";
    input.placeholder = "What should I adjust?";
    input.addEventListener("input", () => { drafts.question = input.value; });
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const button = document.getElementById("ask");
      if (button) button.click();
    });
    field.appendChild(input);
    return field;
  }
  if (node.component === "Button") {
    const button = document.createElement("button");
    button.id = id;
    button.textContent = textOf(byId, node.child);
    button.addEventListener("click", () => sendAction(node, id));
    return button;
  }
  if (node.component === "Row" || node.component === "Column") {
    const el = document.createElement("div");
    el.id = id;
    el.className = node.component === "Row" ? "row" : "column";
    const stepList = id.endsWith("_copy") ? document.createElement("ol") : null;
    if (stepList) stepList.className = "steps";
    for (const childId of node.children || []) {
      const child = renderNode(childId, byId, data);
      const spec = byId[childId];
      if (spec && spec.weight) child.style.flex = spec.weight + " 1 0";
      if (stepList && childId.includes("_step_")) stepList.appendChild(childToLi(child));
      else el.appendChild(child);
    }
    if (stepList && stepList.childElementCount) el.appendChild(stepList);
    if (id === "meter") {
      const cluster = document.createElement("div");
      cluster.className = "strip";
      cluster.appendChild(meter(data));
      cluster.appendChild(fader(data));
      el.insertBefore(cluster, el.firstChild);
      el.appendChild(readout(data));
    }
    if (id.endsWith("_fader") || id.endsWith("_badge")) {
      const speaker = document.createElement("div");
      speaker.className = "speaker";
      speaker.appendChild(el.querySelector("svg") || icon("warning"));
      if (id.endsWith("_fader")) {
        el.classList.add("mixer");
        if (data && data.muted) el.classList.add("is-muted");
        el.insertBefore(fader(data), el.firstChild);
      } else {
        el.classList.add("badge");
      }
      el.appendChild(speaker);
    }
    if (id.endsWith("_copy")) el.classList.add("copy");
    return el;
  }
  if (node.component === "X32Diagram") return x32Diagram(node);
  if (node.component === "SetupDiagram") return setupDiagram(node, data);
  if (node.component === "Card") {
    const el = document.createElement("article");
    el.id = id;
    const child = renderNode(node.child, byId, data);
    if (node.weight) el.style.flex = node.weight + " 1 0";
    el.appendChild(child);
    const label = textOf(byId, node.child);
    const colors = SCENE_COLORS[label];
    if (colors && (id === "scene_current" || /^scene_\d+$/.test(id))) el.insertBefore(swatch(colors), el.firstChild);
    if (id === "stream_card") el.dataset.state = textOf(byId, "stream_value");
    if (id === "mix_card") el.dataset.state = textOf(byId, "mix_value");
    return el;
  }
  return document.createElement("span");
}

function setupNotes(node) {
  const notes = {};
  for (const lane of node.lanes || []) {
    for (const stage of lane.stages || []) {
      for (const item of stage) {
        if (item.id) notes[item.id] = item.note || item.title || "";
      }
    }
  }
  return notes;
}

function paintSelection() {
  document.querySelectorAll("button.path-node").forEach((box) => {
    box.classList.toggle("is-selected", box.dataset.boxId === selectedBox);
  });
  const scene = document.getElementById("scene_current");
  if (scene) scene.classList.toggle("is-selected", selectedBox === "scene-help");
  const detail = document.getElementById("setup_detail");
  if (!detail) return;
  detail.textContent = pathNotes[selectedBox] || "Select a box to read that step.";
}

function bindBox(box, item, notes) {
  box.type = "button";
  box.dataset.boxId = item.id || "";
  if (item.id && item.id === selectedBox) box.classList.add("is-selected");
  if (item.state) box.dataset.state = item.state;
  box.addEventListener("click", () => {
    selectedBox = selectedBox === item.id ? "" : item.id;
    paintSelection();
  });
}

function segmentMeter(data) {
  const wrap = document.createElement("div");
  wrap.className = "segments";
  const count = 28;
  const db = data ? data.meterDb : null;
  const muted = Boolean(data && data.muted);
  let lit = 0;
  if (!muted && typeof db === "number" && db > -90) {
    const clamped = Math.max(-60, Math.min(0, db));
    lit = Math.round(((clamped + 60) / 60) * count);
  }
  for (let i = 0; i < count; i += 1) {
    const seg = document.createElement("i");
    if (i < lit) seg.className = i >= count - 3 ? "hot" : "lit";
    wrap.appendChild(seg);
  }
  return wrap;
}

function meterText(data) {
  const db = data ? data.meterDb : null;
  if (data && data.muted) return "mute";
  if (typeof db === "number" && db > -90) return Math.round(db) + " dB";
  return "quiet";
}

function pictureColumn(lane, notes) {
  const card = document.createElement("section");
  card.className = "picture-column";
  const title = document.createElement("h2");
  title.className = "panel-title";
  title.textContent = "On screen";
  const row = document.createElement("div");
  row.className = "path-row";
  (lane && lane.stages || []).forEach((stage, index) => {
    if (index > 0) row.appendChild(pathArrow());
    for (const item of stage) row.appendChild(pathNode(item, notes));
  });
  const dock = document.createElement("div");
  dock.className = "scene-dock";
  card.append(title, row, dock);
  return card;
}

function pathArrow() {
  const arrow = document.createElement("span");
  arrow.className = "setup-arrow";
  arrow.setAttribute("aria-hidden", "true");
  arrow.textContent = "→";
  return arrow;
}

function pathNode(item, notes) {
  const box = document.createElement("button");
  box.className = "path-node";
  bindBox(box, item, notes);
  const dot = document.createElement("i");
  dot.className = "path-dot";
  const title = document.createElement("strong");
  title.textContent = item.title || "";
  box.append(dot, title);
  if (item.kicker) {
    const kicker = document.createElement("span");
    kicker.className = "kicker";
    kicker.textContent = item.kicker;
    box.insertBefore(kicker, dot);
  }
  if (item.status) {
    const status = document.createElement("span");
    status.className = "status";
    status.textContent = item.status;
    box.appendChild(status);
  }
  if (item.detail) {
    const reading = document.createElement("span");
    reading.className = "reading";
    reading.textContent = item.detail;
    box.appendChild(reading);
  }
  return box;
}

function audioColumn(lane, data, notes) {
  const card = document.createElement("section");
  card.className = "audio-column";
  const title = document.createElement("h2");
  title.className = "panel-title";
  title.textContent = (lane && lane.name) || "Audio";
  const row = document.createElement("div");
  row.className = "path-row";
  let meter = false;
  (lane && lane.stages || []).forEach((stage, index) => {
    if (index > 0) row.appendChild(pathArrow());
    for (const item of stage) {
      if (item.meter) meter = true;
      row.appendChild(pathNode(item, notes));
    }
  });
  card.append(title, row);
  if (meter) {
    const line = document.createElement("div");
    line.className = "meter-row";
    const label = document.createElement("p");
    label.textContent = "X-USB meter";
    const value = document.createElement("p");
    value.textContent = meterText(data);
    line.append(label, segmentMeter(data), value);
    card.appendChild(line);
  }
  return card;
}

function setupDiagram(node, data) {
  const sceneHelp = pathNotes["scene-help"];
  pathNotes = setupNotes(node);
  if (sceneHelp) pathNotes["scene-help"] = sceneHelp;
  if (selectedBox && !pathNotes[selectedBox]) selectedBox = "";
  const el = document.createElement("div");
  el.id = node.id;
  el.className = "setup";
  const columns = document.createElement("div");
  columns.className = "setup-columns";
  const lanes = node.lanes || [];
  const picture = lanes.find((lane) => lane.name === "Picture") || lanes[1];
  const audio = lanes.find((lane) => lane.name === "Audio") || lanes[0];
  columns.append(pictureColumn(picture, pathNotes), audioColumn(audio, data, pathNotes));
  el.appendChild(columns);
  const detail = document.createElement("p");
  detail.id = "setup_detail";
  detail.className = "setup-detail";
  detail.textContent = pathNotes[selectedBox] || "Select a box to read that step.";
  el.appendChild(detail);
  const lines = node.aside || [];
  if (lines.length) {
    const aside = document.createElement("ul");
    aside.className = "setup-aside";
    for (const line of lines) {
      const item = document.createElement("li");
      item.textContent = line;
      aside.appendChild(item);
    }
    el.appendChild(aside);
  }
  return el;
}

function x32Diagram(node) {
  const el = document.createElement("div");
  el.id = node.id;
  el.className = "x32";
  const title = document.createElement("div");
  title.className = "x32-name";
  title.textContent = node.board || "Behringer X32";
  const strips = document.createElement("div");
  strips.className = "x32-strips";
  for (const channel of node.channels || []) {
    if (channel.id === "main") {
      const gap = document.createElement("div");
      gap.className = "x32-gap";
      strips.appendChild(gap);
    }
    const hot = channel.id === node.highlight;
    const strip = document.createElement("div");
    strip.className = hot ? "x32-strip is-hot" : "x32-strip";
    const arrow = document.createElement("div");
    arrow.className = "x32-arrow";
    arrow.textContent = hot ? (node.direction === "up" ? "↑" : "↓") : "";
    const fader = document.createElement("div");
    fader.className = "x32-fader";
    const knob = document.createElement("div");
    knob.className = "x32-knob";
    knob.style.top = hot && node.direction === "up" ? "16%" : (hot ? "66%" : "40%");
    fader.appendChild(knob);
    const label = document.createElement("div");
    label.className = "x32-label";
    label.textContent = (channel.name || "").trim() || channel.role || channel.id;
    strip.append(arrow, fader, label);
    strips.appendChild(strip);
  }
  el.append(title, strips);
  return el;
}

function childToLi(node) {
  const li = document.createElement("li");
  li.textContent = node.textContent || "";
  return li;
}

function render(messages) {
  rememberDraft();
  const created = messages.find((item) => item.createSurface && item.createSurface.surfaceId === SURFACE);
  const surface = created ? created.createSurface : {components: [], dataModel: {}};
  const byId = Object.fromEntries((surface.components || []).map((item) => [item.id, item]));
  const screen = document.getElementById("screen");
  screen.replaceChildren();
  if (!byId.root) return;
  screen.appendChild(renderNode("root", byId, surface.dataModel || {}));
  const dock = document.querySelector(".scene-dock");
  const heading = document.getElementById("scene_heading");
  const strip = document.getElementById("scene_strip");
  if (dock && strip) {
    if (heading) dock.appendChild(heading);
    dock.appendChild(strip);
  }
  paintSelection();
  restoreDraft();
}

async function sendAction(component, sourceId) {
  const event = component.action && component.action.event;
  if (!event) return;
  if (event.name === "read_scene") {
    selectedBox = selectedBox === "scene-help" ? "" : "scene-help";
    const note = event.context && event.context.note;
    if (note) pathNotes["scene-help"] = note;
    paintSelection();
    return;
  }
  const context = Object.assign({}, event.context || {});
  if (event.name === "ask") {
    const field = questionInput();
    context.question = field ? field.value : "";
    if (preview) context.preview = preview;
    const askButton = document.getElementById("ask");
    if (askButton) askButton.textContent = "Asking…";
  }
  const body = {
    version: "1.0",
    action: {
      name: event.name,
      surfaceId: SURFACE,
      sourceComponentId: sourceId,
      timestamp: new Date().toISOString(),
      context: context
    }
  };
  const response = await fetch("/api/a2ui/action", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });
  const result = await response.json();
  if (!result.ok) alert(result.message);
  refresh();
}

async function refresh() {
  const url = preview ? "/api/preview/" + preview : "/api/state";
  const response = await fetch(url, {cache: "no-store"});
  const state = await response.json();
  document.getElementById("raw").textContent = JSON.stringify(state.a2ui, null, 2);
  render(state.a2ui);
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""
