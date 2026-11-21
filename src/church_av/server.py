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
  :root { color-scheme: dark; }
  body { margin: 0; background: #141210; color: #f4efe6;
         font: 17px/1.35 ui-sans-serif, system-ui, sans-serif; }
  main { max-width: 64rem; margin: 0 auto; padding: 1.25rem 1rem 3rem; }
  h1 { font-size: 1.35rem; font-weight: 650; margin: 0 0 0.8rem; letter-spacing: 0.01em; }
  h2 { font-size: 0.78rem; font-weight: 650; letter-spacing: 0.08em; text-transform: uppercase;
       color: #b7aa98; margin: 1.2rem 0 0.45rem; }
  article { border-radius: 12px; }
  .row { display: flex; gap: 0.75rem; align-items: stretch; }
  .column { display: flex; flex-direction: column; gap: 0.35rem; min-width: 0; }
  #status { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.75rem; }
  #status > article { background: #221e1a; border: 1px solid #3a332c; padding: 0.85rem 0.9rem 1rem; }
  #mix_body { display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: 0.75rem; align-items: center; }
  #status .kicker { color: #b7aa98; font-size: 0.75rem; letter-spacing: 0.08em; text-transform: uppercase; }
  #status .value { font-size: 1.7rem; font-weight: 680; line-height: 1.1; margin-top: 0.15rem; }
  #status .detail { color: #d5cbbd; font-size: 0.92rem; }
  #stream_card[data-state="Live"] { box-shadow: inset 3px 0 0 #7dbe7a; }
  #stream_card[data-state="Stopped"] { box-shadow: inset 3px 0 0 #8d8478; }
  #stream_card[data-state="Offline"], #stream_card[data-state="Reconnecting"] { box-shadow: inset 3px 0 0 #e0a15a; }
  #mix_card[data-state="Open"] { box-shadow: inset 3px 0 0 #7dbe7a; }
  #mix_card[data-state="Muted"], #mix_card[data-state="Missing"] { box-shadow: inset 3px 0 0 #e06c5a; }
  svg.icon { width: 1.7rem; height: 1.7rem; display: block; }
  #meter { grid-column: 2; grid-row: 1 / span 4; margin: 0; flex-direction: column; align-items: center; gap: 0.3rem; }
  #meter_caption { font-size: 0.68rem; letter-spacing: 0.06em; text-transform: uppercase; color: #b7aa98; }
  .strip { display: flex; align-items: flex-end; gap: 0.4rem; height: 6.8rem; }
  .meter { display: flex; flex-direction: column-reverse; gap: 2px; height: 6.8rem; width: 0.85rem; }
  .meter i { display: block; flex: 1; width: 100%; background: #3a332c; }
  .meter i.lit { background: #7dbe7a; }
  .meter i.hot { background: #e0a15a; }
  .meter.is-muted i { background: #5a3030; }
  .readout { font-size: 0.72rem; color: #b7aa98; text-align: center; }
  #clear { background: #1c241c; border: 1px solid #314233; margin-top: 0.85rem; }
  #clear .row { align-items: center; padding: 0.8rem 0.9rem; gap: 0.7rem; }
  article[id^="fault_"] { background: #2a211c; border: 1px solid #5a4034; margin-top: 0.7rem; }
  .x32 { margin: 0.85rem 0.85rem 0; background: #14110f; border-radius: 10px; padding: 0.7rem 0.65rem 0.6rem; }
  .x32-name { color: #e0a15a; font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 0.45rem; }
  .x32-strips { display: flex; align-items: flex-end; gap: 0.35rem; }
  .x32-gap { width: 0.7rem; flex: 0 0 0.7rem; }
  .x32-strip { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; gap: 0.25rem; }
  .x32-arrow { height: 1.05rem; color: #e06c5a; font-size: 1.15rem; font-weight: 700; line-height: 1; }
  .x32-fader { position: relative; width: 0.5rem; height: 5.4rem; background: #2a261f; border-radius: 3px; }
  .x32-strip.is-hot .x32-fader { box-shadow: 0 0 0 1px #e06c5a; }
  .x32-knob { position: absolute; left: -0.28rem; width: 1.05rem; height: 0.42rem; background: #8d8478; border-radius: 2px; }
  .x32-strip.is-hot .x32-knob { background: #f4efe6; }
  .x32-label { font-size: 0.68rem; line-height: 1.2; text-align: center; color: #b7aa98; }
  .x32-strip.is-hot .x32-label { color: #f4efe6; font-weight: 650; }
  article[id^="fault_"] > .row { align-items: stretch; }
  .mixer, .badge { width: 6.5rem; flex: 0 0 6.5rem; background: #14110f; align-items: center;
                  justify-content: flex-end; padding: 0.8rem 0.4rem 0.7rem; gap: 0.45rem; }
  .badge { justify-content: center; }
  .track { width: 0.55rem; height: 6.8rem; background: #3a332c; position: relative; }
  .knob { position: absolute; left: -0.28rem; width: 1.1rem; height: 0.45rem; background: #f4efe6; }
  .mixer .speaker, .badge .speaker { width: 2rem; height: 2rem; border: 2px solid #f4efe6; border-radius: 99px;
                    display: grid; place-items: center; }
  .mixer.is-muted .speaker { border-color: #e06c5a; color: #e06c5a; }
  .place { color: #e0a15a; font-size: 0.75rem; letter-spacing: 0.08em; text-transform: uppercase; margin: 0; }
  .control { font-size: 1.45rem; font-weight: 680; margin: 0; }
  .copy { padding: 0.85rem 1rem 1rem; gap: 0.35rem; flex: 1; }
  .copy .steps { margin: 0.2rem 0 0; padding-left: 0; list-style: none; counter-reset: step; }
  .copy .steps li { counter-increment: step; margin: 0.28rem 0; color: #f4efe6; }
  .copy .steps li::before { content: counter(step); display: inline-grid; place-items: center;
       width: 1.3rem; height: 1.3rem; margin-right: 0.45rem; background: #3a332c; font-size: 0.8rem; }
  button { font: inherit; background: #f4efe6; color: #141210; border: 0; padding: 0.5rem 0.85rem; margin-top: 0.45rem; }
  #guide { background: #241c18; border: 1px solid #5a4034; margin-top: 0.85rem; }
  #guide_body { padding: 0.9rem 1rem 1rem; }
  #guide_icon { color: #e06c5a; width: 1.7rem; height: 1.7rem; border: 2px solid #e06c5a; border-radius: 99px; padding: 0.35rem; }
  #guide_reply { margin: 0.25rem 0 0; font-size: 1.05rem; }
  #guide_asked { margin: 0; color: #b7aa98; }
  #chat { align-items: end; margin-top: 0.75rem; }
  #question { flex: 1; display: flex; flex-direction: column; gap: 0.3rem; color: #b7aa98;
              font-size: 0.75rem; letter-spacing: 0.08em; text-transform: uppercase; }
  #question input { font: 17px/1.35 ui-sans-serif, system-ui, sans-serif; text-transform: none;
                    letter-spacing: 0; color: #f4efe6; background: #1c1916; border: 1px solid #3a332c;
                    border-radius: 8px; padding: 0.55rem 0.7rem; }
  #scene_strip { display: grid; grid-template-columns: repeat(auto-fill, minmax(9.5rem, 1fr)); gap: 0.4rem; }
  #scene_strip article { background: #1c1916; border: 1px solid #3a332c; padding: 0.45rem 0.55rem; }
  #scene_current { background: #2a261f; border-color: #f4efe6; }
  #scene_strip p { margin: 0; color: #d5cbbd; font-size: 0.85rem; }
  #scene_current p { color: #f4efe6; font-weight: 650; }
  .swatch { height: 0.55rem; margin: -0.45rem -0.55rem 0.4rem; }
  .raw { margin-top: 1.5rem; color: #8d8478; font-size: 0.8rem; }
  pre { white-space: pre-wrap; }
  @media (max-width: 720px) {
    #status { grid-template-columns: 1fr; }
    article[id^="fault_"] > .row { flex-direction: column; }
    .mixer, .badge { width: auto; flex-basis: auto; flex-direction: row; justify-content: flex-start; }
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
  restoreDraft();
}

async function sendAction(component, sourceId) {
  const event = component.action && component.action.event;
  if (!event) return;
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
