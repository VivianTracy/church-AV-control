from __future__ import annotations

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from time import sleep
from urllib.parse import urlparse

from church_av.a2ui import SURFACE_ID, build_messages
from church_av.config import Settings
from church_av.detector import Detector
from church_av.models import Fault, Snapshot
from church_av.obs_read import ObsReader
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

    def tick(self) -> None:
        snapshot = self.reader.read()
        faults = self.detector.detect(snapshot, self.runbook)
        with self._lock:
            self.snapshot = snapshot
            self.faults = faults

    def state(self) -> dict:
        with self._lock:
            snapshot = self.snapshot
            faults = list(self.faults)
            last_action = self.last_action
        return {
            "connected": snapshot.connected,
            "programScene": snapshot.program_scene,
            "streaming": snapshot.streaming,
            "allowApply": self.settings.allow_apply,
            "lastAction": last_action,
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
        try:
            self._apply(fault)
        except Exception as exc:
            message_text = str(exc).strip() or exc.__class__.__name__
            with self._lock:
                self.last_action = message_text
            return {"ok": False, "message": message_text}
        with self._lock:
            self.last_action = fault.action.summary
        self.tick()
        return {"ok": True, "message": fault.action.summary}

    def _apply(self, fault: Fault) -> None:
        action = fault.action
        if action is None or action.kind != "set_input_mute" or not action.input_name:
            raise RuntimeError("This booth only applies an unmute of the house mix.")
        if action.muted is not False:
            raise RuntimeError("Muting the house mix from here is disabled.")
        self.reader.set_input_mute(action.input_name, False)


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


def _poll(booth: Booth) -> None:
    while True:
        booth.tick()
        sleep(booth.settings.poll_seconds)


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Church stream booth</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font: 18px/1.4 "Iowan Old Style", Palatino, Georgia, serif;
         background: #1b1916; color: #f3efe6; }
  main { max-width: 46rem; margin: 0 auto; padding: 1.5rem 1.25rem 3rem; }
  h1 { font-size: 1.6rem; font-weight: 650; margin: 0 0 0.4rem; }
  .status { color: #c8c0b0; margin-bottom: 1rem; }
  article { background: #28241f; border: 1px solid #3c362e; padding: 0.9rem 1rem; margin: 0.7rem 0; }
  article h2 { font-size: 1.15rem; margin: 0 0 0.35rem; }
  button { font: inherit; background: #efe6d4; color: #1b1916; border: 0; padding: 0.45rem 0.8rem; margin-top: 0.6rem; }
  .layouts { margin-top: 1.6rem; color: #c8c0b0; }
  .layouts p { margin: 0.2rem 0; font-size: 0.95rem; }
  .raw { margin-top: 1.5rem; color: #9c9488; font-size: 0.85rem; }
  pre { white-space: pre-wrap; }
</style>
</head>
<body>
<main>
  <div id="screen">Waiting for the service machine…</div>
  <details class="raw"><summary>A2UI messages</summary><pre id="raw"></pre></details>
</main>
<script>
const SURFACE = "booth";
function textOf(components, id) {
  const found = components.find((item) => item.id === id);
  return found && typeof found.text === "string" ? found.text : "";
}
function render(messages) {
  const created = messages.find((item) => item.createSurface && item.createSurface.surfaceId === SURFACE);
  const components = created ? created.createSurface.components : [];
  const byId = Object.fromEntries(components.map((item) => [item.id, item]));
  const root = byId.root;
  const screen = document.getElementById("screen");
  screen.replaceChildren();
  if (!root) return;
  for (const id of root.children) {
    const node = byId[id];
    if (!node) continue;
    if (node.component === "Text") {
      const tag = id === "title" ? "h1" : (id.endsWith("heading") ? "h2" : "p");
      const el = document.createElement(tag);
      if (id.startsWith("layout_")) el.className = "layouts";
      else if (id.endsWith("_line") || id === "clear") el.className = "status";
      el.textContent = node.text;
      screen.appendChild(el);
    } else if (node.component === "Card") {
      const column = byId[node.child];
      const article = document.createElement("article");
      const heading = document.createElement("h2");
      heading.textContent = textOf(components, column.children[0]);
      article.appendChild(heading);
      for (const childId of column.children.slice(1)) {
        const child = byId[childId];
        if (!child) continue;
        if (child.component === "Text") {
          const p = document.createElement("p");
          p.textContent = child.text;
          article.appendChild(p);
        } else if (child.component === "Button") {
          const button = document.createElement("button");
          button.textContent = textOf(components, child.child);
          button.addEventListener("click", () => sendAction(child, childId));
          article.appendChild(button);
        }
      }
      screen.appendChild(article);
    }
  }
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
  const response = await fetch("/api/state", {cache: "no-store"});
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
