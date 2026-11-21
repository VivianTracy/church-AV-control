from __future__ import annotations

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from time import sleep
from urllib.parse import urlparse

from church_av.agent import Guide, explain
from church_av.a2ui import SURFACE_ID, build_messages
from church_av.config import Settings
from church_av.detector import Detector
from church_av.fixtures import FIXTURE_NAMES, load_fixture
from church_av.models import Fault, Snapshot
from church_av.obs_read import ObsReader
from church_av.policy import decide
from church_av.runbook import Runbook, load_runbook


class Booth:
    def __init__(self, settings: Settings, reader: ObsReader, runbook: Runbook) -> None:
        self.settings = settings
        self.reader = reader
        self.runbook = runbook
        self.detector = Detector()
        self._lock = Lock()
        self.snapshot = Snapshot(connected=False, error="Waiting for the first reading")
        self.faults: list[Fault] = []
        self.last_action = ""
        self.guide = Guide("Waiting for the first reading.", "detector")
        self._guide_fault_ids: tuple[str, ...] | None = None

    def tick(self) -> None:
        snapshot = self.reader.read()
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
            "a2ui": build_messages(snapshot, self.runbook, faults, self.settings.allow_apply),
        }

    def handle_action(self, message: dict) -> dict:
        action = message.get("action") if isinstance(message, dict) else None
        if not isinstance(action, dict):
            return {"ok": False, "message": "Missing action."}
        name = action.get("name")
        context = action.get("context") if isinstance(action.get("context"), dict) else {}
        fault_id = str(context.get("faultId", ""))
        with self._lock:
            fault = next((item for item in self.faults if item.id == fault_id), None)
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
    try:
        server.serve_forever()
    finally:
        booth.reader.close()
        server.server_close()


def _preview_state(booth: Booth, name: str) -> dict:
    detector = Detector()
    snapshot = load_fixture(name)[0]
    faults: list[Fault] = []
    for item in load_fixture(name):
        snapshot = item
        faults = detector.detect(item, booth.runbook)
    return {
        "connected": snapshot.connected,
        "programScene": snapshot.program_scene,
        "streaming": snapshot.streaming,
        "allowApply": False,
        "preview": name,
        "a2ui": build_messages(snapshot, booth.runbook, faults, allow_apply=False),
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
  #stage_heading { font-size: 1.15rem; letter-spacing: 0; text-transform: none; color: #f4efe6; margin: 0 0 0.4rem; }
  #stage { position: relative; aspect-ratio: 16 / 9; min-height: 11rem; background: #0a0908;
           border: 1px solid #3a332c; border-radius: 12px; padding: 0.45rem; gap: 0.35rem; }
  .tally { position: absolute; z-index: 2; top: 0.85rem; left: 0.85rem; background: #e06c5a; color: #fff;
           font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em; padding: 0.12rem 0.4rem; }
  #stage[data-connected="false"] .tally { background: #5a4034; }
  #stage > article { position: relative; display: flex; align-items: flex-end; padding: 0.7rem;
                     min-height: 0; color: #f4efe6; overflow: hidden; border-radius: 6px; }
  #stage > article p { position: relative; z-index: 1; margin: 0; font-size: 1.2rem; font-weight: 650;
                       text-shadow: 0 1px 2px #000; }
  #stage .art { position: absolute; inset: 0; width: 100%; height: 100%; }
  #stage > article[data-source="Wall Cam"] { background: #243044; }
  #stage > article[data-source="Slide PC"] { background: #3d3428; }
  #stage > article[data-source="Mobile Cam"] { background: #24362c; }
  #stage > article[data-source="Acer"] { background: #2c3034; }
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
    #stage { min-height: 9rem; }
    article[id^="fault_"] > .row { flex-direction: column; }
    .mixer, .badge { width: auto; flex-basis: auto; flex-direction: row; justify-content: flex-start; }
    .mixer .track { height: 4.5rem; }
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

function stageArt(source) {
  const pictures = {
    "Wall Cam": '<rect width="320" height="180" fill="#1b2838"/><rect x="36" y="28" width="28" height="88" fill="#31445c"/><rect x="256" y="28" width="28" height="88" fill="#31445c"/><path d="M160 34v58M132 62h56" stroke="#f4efe6" stroke-width="7"/><rect x="104" y="112" width="112" height="26" fill="#5c4632"/><path d="M20 156h280" stroke="#8d8478" stroke-width="3"/>',
    "Slide PC": '<rect width="320" height="180" fill="#2a241c"/><rect x="36" y="18" width="248" height="144" fill="#f4efe6"/><rect x="56" y="38" width="120" height="14" fill="#3d3428"/><rect x="56" y="68" width="200" height="6" fill="#c4b8a8"/><rect x="56" y="84" width="176" height="6" fill="#c4b8a8"/><rect x="56" y="100" width="150" height="6" fill="#c4b8a8"/>',
    "Mobile Cam": '<rect width="320" height="180" fill="#1c2a22"/><rect x="124" y="10" width="72" height="160" rx="10" fill="#121815" stroke="#f4efe6" stroke-width="3"/><circle cx="160" cy="68" r="16" fill="#d5cbbd"/><path d="M136 132c6-22 42-22 48 0" fill="#d5cbbd"/>',
    "Acer": '<rect width="320" height="180" fill="#22262a"/><rect x="28" y="16" width="264" height="132" fill="#141210" stroke="#8d8478" stroke-width="6"/><rect x="140" y="148" width="40" height="12" fill="#8d8478"/><rect x="112" y="160" width="96" height="6" fill="#8d8478"/>'
  };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 320 180");
  svg.setAttribute("preserveAspectRatio", "xMidYMid slice");
  svg.classList.add("art");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = pictures[source] || '<rect width="320" height="180" fill="#1a1816"/><circle cx="160" cy="90" r="28" fill="none" stroke="#8d8478" stroke-width="4"/><path d="M142 108l36-36" stroke="#8d8478" stroke-width="4"/>';
  return svg;
}

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
  if (node.component === "Button") {
    const button = document.createElement("button");
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
    if (id === "stage") {
      el.dataset.connected = data && data.connected === false ? "false" : "true";
      const tally = document.createElement("div");
      tally.className = "tally";
      tally.textContent = data && data.connected === false ? "OFF" : "PGM";
      el.appendChild(tally);
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
  if (node.component === "Card") {
    const el = document.createElement("article");
    el.id = id;
    const child = renderNode(node.child, byId, data);
    if (node.weight) el.style.flex = node.weight + " 1 0";
    el.appendChild(child);
    const label = textOf(byId, node.child);
    if (id.startsWith("pane_")) {
      el.dataset.source = label;
      el.insertBefore(stageArt(label), el.firstChild);
    }
    const colors = SCENE_COLORS[label];
    if (colors && (id === "scene_current" || /^scene_\d+$/.test(id))) el.insertBefore(swatch(colors), el.firstChild);
    if (id === "stream_card") el.dataset.state = textOf(byId, "stream_value");
    if (id === "mix_card") el.dataset.state = textOf(byId, "mix_value");
    return el;
  }
  return document.createElement("span");
}

function childToLi(node) {
  const li = document.createElement("li");
  li.textContent = node.textContent || "";
  return li;
}

function render(messages) {
  const created = messages.find((item) => item.createSurface && item.createSurface.surfaceId === SURFACE);
  const surface = created ? created.createSurface : {components: [], dataModel: {}};
  const byId = Object.fromEntries((surface.components || []).map((item) => [item.id, item]));
  const screen = document.getElementById("screen");
  screen.replaceChildren();
  if (!byId.root) return;
  screen.appendChild(renderNode("root", byId, surface.dataModel || {}));
}

async function sendAction(component, sourceId) {
  const event = component.action && component.action.event;
  if (!event) return;
  const body = {
    version: "1.0",
    action: {
      name: event.name,
      surfaceId: SURFACE,
      sourceComponentId: sourceId,
      timestamp: new Date().toISOString(),
      context: event.context || {}
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
