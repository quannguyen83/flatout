// VIO acquisition view. Existing RaceView owns the vehicle controls and race feed.
import { h, setText } from "../dom.js";
import { authHeaders, readJson, writeJson } from "../store.js";
import { RaceView } from "./race.js";

const KEY = "racelab.vio";
const prefs = readJson(KEY, {}) || {};
function field(label, key, placeholder = "", type = "text", remember = true) {
  const input = h("input.input", { type, placeholder, value: remember ? prefs[key] || "" : "",
    autocomplete: type === "password" ? "current-password" : "off" });
  if (remember) input.addEventListener("change", () => {
    prefs[key] = input.value; writeJson(KEY, prefs);
  });
  return { input, el: h("label.vio-field", {}, h("span", {}, label), input) };
}
function button(label, fn, cls = "") {
  return h("button.btn" + (cls ? "." + cls : ""), { type: "button", onclick: fn }, label);
}
function panel(title, ...children) {
  return h("section.panel.vio-panel", {}, h("div.panel-head", {}, h("h2", {}, title)), ...children);
}
async function api(path, data) {
  const res = await fetch("/api/vio/" + path, { cache: "no-store",
    headers: { ...authHeaders(), ...(data === undefined ? {} : {"Content-Type": "application/json"}) },
    ...(data === undefined ? {} : {method: "POST", body: JSON.stringify(data)}) });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw Error(json.error || "VIO unavailable (localhost only)");
  return json;
}
function mediaUrl(path) {
  const token = authHeaders().Authorization?.replace(/^Bearer /, "");
  const query = new URLSearchParams({t: String(Date.now())});
  if (token) query.set("token", token);
  return "/api/vio/" + path + "?" + query;
}
class Poll {
  constructor(update, error) {
    this.closed = false;
    const tick = async () => {
      try { const state = await api("state"); if (!this.closed) update(state); }
      catch (e) { if (!this.closed) error(e.message); }
      finally { if (!this.closed) this.timer = setTimeout(tick, 1000); }
    };
    tick();
  }
  destroy() { this.closed = true; clearTimeout(this.timer); }
}

export class RpiPanel {
  constructor() {
    const host = field("RPi IP", "host"), user = field("SSH username", "user");
    const port = field("SSH port", "port", "22", "number");
    const key = field("SSH private key (optional)", "key");
    const pass = field("Password (not saved)", "password", "", "password", false);
    const status = h("p.note", {}, "Disconnected");
    const error = h("p.warn-line");
    let busy = false, connected = false, running = false;
    const refresh = () => { connect.disabled = busy || connected; disconnect.disabled = busy || !connected || running; };
    const act = async (op, data) => {
      busy = true; refresh(); setText(error, "");
      try { render(await api("action/" + op, data)); }
      catch (e) { setText(error, e.message); }
      finally { busy = false; pass.input.value = ""; refresh(); }
    };
    const connect = button("Connect SSH", () => act("connect", {
      host: host.input.value, user: user.input.value, port: port.input.value || 22,
      key: key.input.value, password: pass.input.value
    }), "primary");
    const disconnect = button("Disconnect", () => act("disconnect", {}));
    const render = state => {
      connected = state.connected; running = state.pi_running; refresh();
      setText(status, connected ? "Connected · " + state.host : "Disconnected");
    };
    this.el = panel("RPi Connection", h("div.vio-fields", {}, host.el, user.el, port.el, key.el),
      pass.el, h("div.vio-actions", {}, connect, disconnect, status), error,
      h("p.note", {}, "SSH host key must already be trusted (connect once using ssh in your terminal)."));
    refresh();
    this.poll = new Poll(render, msg => setText(error, msg));
  }
  destroy() { this.poll.destroy(); }
}

export class VioView extends RaceView {
  constructor(root) {
    super(root);
    const dir = field("Remote directory", "directory", "~/carrera");
    const cmd = field("Run command", "command", "python3 pi_send.py LAPTOP_IP");
    if (!dir.input.value) dir.input.value = "~/carrera";
    if (!cmd.input.value) cmd.input.value = "python3 pi_send.py LAPTOP_IP";
    const specs = [
      ["Width", "width", 1280, 1], ["Height", "height", 720, 1],
      ["FPS", "fps", 30, 1], ["Exposure (µs)", "exposure-us", 5000, 1],
      ["Analogue gain", "gain", 6, 0.1], ["Lens position (D)", "lens-position", 2.45, 0.01],
      ["IMU (Hz)", "imu-rate", 100, 1], ["Mag (Hz)", "mag-rate", 10, 1],
      ["Light (Hz)", "light-rate", 10, 1], ["MJPEG bitrate", "mjpeg-bitrate", 16000000, 1],
      ["Video port", "video-port", 5000, 1], ["Data port", "data-port", 5001, 1],
      ["RTP MTU", "rtp-mtu", 1200, 1], ["AF log interval (s)", "af-log-interval", 1, 0.1],
      ["IMU poll sleep (s)", "imu-ready-poll-sleep", 0.001, 0.001],
    ];
    const piFields = specs.map(([label, key, initial, step]) => {
      const f = field(label, "pi-" + key, String(initial), "number");
      f.input.value = prefs["pi-" + key] ?? String(initial);
      f.input.step = String(step); f.input.min = "0"; f.input.required = true;
      return {key, ...f};
    });
    const af = h("select.input", {}, ...["auto", "manual", "continuous"].map(v => h("option", {value:v}, v)));
    af.value = prefs["pi-af-mode"] || "auto";
    af.addEventListener("change", () => { prefs["pi-af-mode"] = af.value; writeJson(KEY, prefs); });
    const afField = h("label.vio-field", {}, h("span", {}, "Autofocus"), af);
    const piParams = () => {
      for (const f of piFields) {
        if (!f.input.reportValidity()) return null;
      }
      return Object.fromEntries([...piFields.map(f => [f.key, f.input.value]), ["af-mode", af.value]]);
    };
    const file = field("Receiver file", "file", "C:\\...\\laptop_receiver.py");
    const args = field("Arguments (optional)", "args", "--output dataset");
    const python = field("Python executable (optional)", "python", "RaceLab Python");
    const piStatus = h("span.dim", {}, "Idle"), receiverStatus = h("span.dim", {}, "Idle");
    const datasetRoot = h("p.note");
    this.error = h("p.warn-line");
    this.vioState = null;
    this.pending = new Set();
    const act = async (op, data, group) => {
      if (this.pending.has(group)) return;
      this.pending.add(group); this.syncButtons(); setText(this.error, "");
      try { this.renderVio(await api("action/" + op, data)); }
      catch(e) { setText(this.error, e.message); }
      finally { this.pending.delete(group); this.syncButtons(); }
    };
    this.piRun = button("Run", () => {
      const params = piParams();
      if (!params) return;
      prefs.directory = dir.input.value; prefs.command = cmd.input.value;
      for (const f of piFields) prefs["pi-" + f.key] = f.input.value;
      prefs["pi-af-mode"] = af.value; writeJson(KEY, prefs);
      act("pi-start", {directory: dir.input.value, command: cmd.input.value, params}, "pi");
    }, "go");
    this.piStop = button("Stop", () => act("pi-stop", {}, "pi"), "danger");
    this.pcRun = button("Run", () => act("receiver-start", {file: file.input.value, args: args.input.value, python: python.input.value}, "receiver"), "go");
    this.pcStop = button("Stop", () => { if (this.mode === "live") this.clearMedia(); act("receiver-stop", {}, "receiver"); }, "danger");
    const configure = () => {
      if (!file.input.value || this.vioState?.receiver_running) return;
      api("action/receiver-config", {file: file.input.value, args: args.input.value})
        .then(state => this.renderVio(state)).catch(e => setText(this.error, e.message));
    };
    file.input.addEventListener("change", configure);
    args.input.addEventListener("change", configure);
    this.browse = button("Browse", async () => {
      this.browse.disabled = true;
      try {
        const result = await api("action/browse", {});
        if (result.file) { file.input.value = result.file; file.input.dispatchEvent(new Event("change")); }
      } catch(e) { setText(this.error, e.message); }
      finally { this.browse.disabled = false; }
    });
    this.piStatus = piStatus; this.receiverStatus = receiverStatus; this.datasetRoot = datasetRoot;
    const configuration = h("div.vio-config", {},
      panel("Pi-send Configuration",
        h("details", {}, h("summary", {}, "Remote directory / command"), dir.el, cmd.el),
        h("div.vio-fields", {}, ...piFields.slice(0,9).map(f => f.el), afField),
        h("details", {}, h("summary", {}, "Advanced parameters"), h("div.vio-fields", {}, ...piFields.slice(9).map(f => f.el))),
        h("div.vio-actions.vio-module-footer", {}, this.piRun, this.piStop, piStatus)),
      panel("Laptop Receiver", h("div.vio-file", {}, file.el, this.browse), args.el, python.el,
        datasetRoot, h("div.vio-actions.vio-module-footer", {}, this.pcRun, this.pcStop, receiverStatus)));
    this.dataset = h("select.input", {"aria-label": "Recorded dataset"});
    this.image = h("img.vio-media.hidden", {alt: "Live preview"});
    this.video = h("video.vio-media.hidden", {controls: true, preload: "metadata", playsinline: true});
    this.previewNote = h("p.note", {}, "Start receiver for live preview; FFmpeg is only required for replay.");
    this.liveButton = button("Live", () => this.showLive());
    this.replayButton = button("Replay", () => this.showReplay());
    this.video.addEventListener("error", () => setText(this.previewNote, "Replay failed; check FFmpeg and dataset."));
    this.image.addEventListener("error", () => { if (this.mode === "live") setText(this.previewNote, "Embedded live preview unavailable; check receiver logs and updated receiver version."); });
    const preview = panel("Video Preview", h("div.vio-actions", {}, this.liveButton, this.dataset, this.replayButton),
      h("div.vio-video", {}, this.image, this.video), this.previewNote);
    this.piLog = h("pre.vio-log", {role: "log"});
    this.pcLog = h("pre.vio-log", {role: "log"});
    const logs = panel("Process Logs", h("div.eyebrow", {}, "Pi-send"), this.piLog,
      h("div.eyebrow", {}, "Laptop Receiver"), this.pcLog);
    // Move the original DOM nodes, do not recreate BotCard or Race Feed.
    root.replaceChildren(h("div.race.vio", {}, configuration, this.error, this.shortBanner, this.bar,
      h("div.vio-grid", {}, h("div.vio-column", {}, preview, logs),
        h("div.vio-column", {}, this.best, this.bots, this.feedBox))));
    this.syncButtons();
    this.poll = new Poll(state => this.renderVio(state), msg => setText(this.error, msg));
    configure();
  }
  updateMap() {} // VIO does not render a track map.
  destroy() {
    this.poll?.destroy(); this.clearMedia(); super.destroy();
  }
  syncButtons() {
    const s = this.vioState || {};
    this.piRun.disabled = !s.connected || !!s.pi_running || this.pending.has("pi");
    this.piStop.disabled = !s.pi_running || this.pending.has("pi");
    this.pcRun.disabled = !this.vioState || !Object.hasOwn(s, "preview_port") || !!s.receiver_running || this.pending.has("receiver");
    this.pcStop.disabled = !s.receiver_running || this.pending.has("receiver");
  }
  renderVio(s) {
    if (!s || !s.logs) return;
    this.vioState = s;
    this.syncButtons();
    if (!Object.hasOwn(s, "preview_port")) {
      setText(this.error, "The running RaceLab backend is outdated. Stop acquisition, quit RaceLab completely and reopen the installed app.");
    }
    setText(this.piStatus, s.pi_running ? "Running" : s.connected ? "SSH connected" : "SSH disconnected");
    setText(this.receiverStatus, s.receiver_running ? "Running" : s.receiver_exit == null ? "Idle" : "Exit " + s.receiver_exit);
    setText(this.datasetRoot, s.dataset_root ? "Dataset: " + s.dataset_root : "Dataset detected from receiver output");
    setText(this.piLog, s.logs.pi.join("\n")); setText(this.pcLog, s.logs.receiver.join("\n"));
    const selection = this.dataset.value;
    const names = s.datasets || [];
    const signature = JSON.stringify(names);
    if (signature !== this.datasetSig) {
      this.datasetSig = signature;
      this.dataset.replaceChildren(h("option", {value: ""}, "Select dataset"),
        ...names.map(name => h("option", {value: name}, name)));
      this.dataset.value = names.includes(selection) ? selection : names[0] || "";
    }
    this.liveButton.disabled = !s.receiver_running || !s.preview_port;
    if (s.receiver_running && s.preview_port && this.autoPreviewPort !== s.preview_port) {
      this.autoPreviewPort = s.preview_port;
      this.showLive();
    }
    if (!s.receiver_running) this.autoPreviewPort = null;
    this.replayButton.disabled = !names.length || !s.ffmpeg;
    if (!s.receiver_running && this.mode === "live") {
      this.clearMedia(); setText(this.previewNote, "Receiver stopped; select dataset to replay.");
    }
  }
  clearMedia() {
    if (!this.video || !this.image) return;
    this.mode = null;
    this.image.removeAttribute("src"); this.image.classList.add("hidden");
    this.video.pause(); this.video.removeAttribute("src"); this.video.load(); this.video.classList.add("hidden");
  }
  showLive() {
    this.clearMedia(); this.mode = "live";
    this.image.classList.remove("hidden");
    this.image.src = mediaUrl("live");
    setText(this.previewNote, "Live JPEG preview from receiver.");
  }
  showReplay() {
    if (!this.dataset.value) return;
    if (this.vioState.receiver_running && this.vioState.active_dataset === this.dataset.value) {
      setText(this.previewNote, "Stop the receiver before replaying the active dataset."); return;
    }
    this.clearMedia(); this.mode = "replay";
    this.video.classList.remove("hidden");
    this.video.src = mediaUrl("replay/" + encodeURIComponent(this.dataset.value));
    setText(this.previewNote, "Preparing browser-compatible replay…");
    this.video.onloadedmetadata = () => setText(this.previewNote, "Recorded dataset");
  }
}
