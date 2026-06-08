/* Voice Language Coach — browser client.
 *
 * Captures mic audio as 16 kHz PCM16 (via an AudioWorklet) and streams it over a
 * WebSocket. Receives streamed TTS PCM back and plays it on a gapless schedule.
 * Falls back to the browser's SpeechSynthesis when the server has no TTS key.
 */

const $ = (id) => document.getElementById(id);
const LANG_BCP47 = { es: "es-ES", fr: "fr-FR", de: "de-DE", en: "en-US" };

let ws = null;
let micCtx = null;
let micStream = null;
let player = null;
let usingBrowserTTS = false;
let speakingSources = 0; // active TTS audio sources (Cartesia path)
let currentAgentEl = null;
let agentSpeaking = false;   // drives status display only
let fullDuplex = false;      // set from server "ready"; when true the mic stays live
let micAllowed = true;       // server-authoritative half-duplex gate (mic on/off)
let speakOffTimer = null;

// ----- gapless PCM playback (24 kHz mono s16le) ------------------------------
class PCMPlayer {
  constructor(sampleRate) {
    this.ctx = new AudioContext({ sampleRate });
    this.sr = sampleRate;
    this.cursor = 0;
    this.sources = new Set();
  }
  async resume() { if (this.ctx.state !== "running") await this.ctx.resume(); }
  play(int16) {
    const f = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) f[i] = int16[i] / 0x8000;
    const buf = this.ctx.createBuffer(1, f.length, this.sr);
    buf.copyToChannel(f, 0);
    const node = this.ctx.createBufferSource();
    node.buffer = buf;
    node.connect(this.ctx.destination);
    const start = Math.max(this.ctx.currentTime, this.cursor);
    node.start(start);
    this.cursor = start + buf.duration;
    this.sources.add(node);
    speakingSources++;
    setSpeaking(true);
    node.onended = () => {
      this.sources.delete(node);
      speakingSources = Math.max(0, speakingSources - 1);
      if (speakingSources === 0) setSpeaking(false);
    };
  }
  clear() {
    for (const s of this.sources) { try { s.stop(); } catch (_) {} }
    this.sources.clear();
    this.cursor = 0;
    speakingSources = 0;
    setSpeaking(false);
  }
}

// ----- mic capture worklet (inline, no separate file) ------------------------
const WORKLET = `
class Cap extends AudioWorkletProcessor {
  constructor(){ super(); this.buf=[]; this.target=1600; } // ~100ms @16kHz
  process(inputs){
    const ch = inputs[0][0];
    if(ch){
      for(let i=0;i<ch.length;i++) this.buf.push(ch[i]);
      while(this.buf.length>=this.target){
        const frame=this.buf.splice(0,this.target);
        const pcm=new Int16Array(this.target);
        for(let i=0;i<this.target;i++){ let s=Math.max(-1,Math.min(1,frame[i])); pcm[i]=s<0?s*0x8000:s*0x7FFF; }
        this.port.postMessage(pcm.buffer,[pcm.buffer]);
      }
    }
    return true;
  }
}
registerProcessor('cap', Cap);
`;

let framesSent = 0, framesGated = 0;
async function startMic() {
  micCtx = new AudioContext({ sampleRate: 16000 });
  await micCtx.resume(); // ensure the context is running, not suspended
  console.log("[mic] AudioContext state:", micCtx.state, "sampleRate:", micCtx.sampleRate);
  const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
  await micCtx.audioWorklet.addModule(url);
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
  });
  console.log("[mic] got stream, tracks:", micStream.getAudioTracks().map(t => t.label));
  const src = micCtx.createMediaStreamSource(micStream);
  const node = new AudioWorkletNode(micCtx, "cap");
  node.port.onmessage = (e) => {
    // Half-duplex: the SERVER tells us when to mute (mic off for the whole reply,
    // back on when it's done) — authoritative, so there's no gap and no way to
    // get stuck muted. Full-duplex: keep the mic live; the server's echo guard
    // distinguishes a real barge-in from the coach hearing itself.
    const gated = !fullDuplex && !micAllowed;
    if (ws && ws.readyState === 1 && !gated) {
      ws.send(e.data);
      if (++framesSent % 25 === 0) console.log("[mic] sent", framesSent, "frames to server");
    } else {
      if (++framesGated % 25 === 0)
        console.log("[mic] GATED", framesGated, "frames (agentSpeaking=" + agentSpeaking + ", fullDuplex=" + fullDuplex + ")");
    }
  };
  // keep the graph pulling without making sound
  const sink = micCtx.createGain();
  sink.gain.value = 0;
  src.connect(node); node.connect(sink); sink.connect(micCtx.destination);
}

function stopMic() {
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  if (micCtx) micCtx.close();
  micStream = micCtx = null;
}

// ----- UI helpers ------------------------------------------------------------
function setStatus(text, state) {
  $("statusText").textContent = text;
  $("statusDot").className = `dot ${state || "idle"}`;
}
function setSpeaking(on) {
  if (on) {
    if (speakOffTimer) { clearTimeout(speakOffTimer); speakOffTimer = null; }
    agentSpeaking = true;
    setStatus("Coach is speaking…", "speaking");
  } else {
    // keep the mic gated for a short tail so the agent's trailing audio
    // (and speaker echo) isn't re-captured as if the learner spoke.
    if (speakOffTimer) clearTimeout(speakOffTimer);
    speakOffTimer = setTimeout(() => { agentSpeaking = false; }, 300);
    setStatus("Listening — go ahead and talk", "listening");
  }
}
function addLine(role, text) {
  const wrap = document.createElement("div");
  const isCoach = role === "coach";
  wrap.className = `msg ${isCoach ? "msg--coach" : "msg--learner"}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  $("log").appendChild(wrap);
  $("log").scrollTop = $("log").scrollHeight;
  return bubble;
}
function browserSpeak(text) {
  const u = new SpeechSynthesisUtterance(text);
  u.lang = LANG_BCP47[$("language").value] || "en-US";
  u.onstart = () => setSpeaking(true);
  u.onend = () => { if (!speechSynthesis.speaking) setSpeaking(false); };
  speechSynthesis.speak(u);
}

// ----- WebSocket message handling --------------------------------------------
function handleMessage(msg) {
  switch (msg.type) {
    case "ready":
      usingBrowserTTS = msg.tts === "browser";
      fullDuplex = !!msg.full_duplex;
      setStatus("Listening — go ahead and talk", "listening");
      break;
    case "mic":            // server-authoritative half-duplex gate
      micAllowed = !!msg.on;
      break;
    case "transcript":
      console.log("[stt]", msg.final ? "FINAL:" : "interim:", msg.text);
      if (msg.final) {
        $("interimWrap").textContent = "";
        addLine("learner", msg.text);
      } else {
        $("interimWrap").textContent = msg.text;
      }
      break;
    case "agent_text":
      if (!currentAgentEl) currentAgentEl = addLine("coach", "");
      currentAgentEl.textContent = msg.text;
      $("log").scrollTop = $("log").scrollHeight;
      if (msg.done) currentAgentEl = null;
      break;
    case "speak":            // browser-TTS fallback path
      browserSpeak(msg.text);
      break;
    case "audio_start": break; // Cartesia path; chunks arrive as binary frames
    case "audio_end": break;
    case "clear":            // barge-in: flush whatever is playing
      if (player) player.clear();
      if (usingBrowserTTS) speechSynthesis.cancel();
      currentAgentEl = null;
      break;
    case "latency":
      $("ttBadge").textContent = `first-word ${msg.ttft_ms}ms · first-audio ${msg.ttfa_ms}ms`;
      break;
    case "report":
      renderReport(msg.data);
      break;
    case "info":
      setStatus(msg.message, "info");
      break;
    case "error":
      setStatus(msg.message, "error");
      break;
  }
}

// Escape LLM/user-derived text before it goes into innerHTML. The report
// fields come from the model (which echoes learner speech), so treating them
// as trusted HTML is an injection risk — render them as text.
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ----- report card -----------------------------------------------------------
function renderReport(d) {
  $("live").classList.add("hidden");
  const el = $("report");
  el.classList.remove("hidden");
  const score = Math.max(0, Math.min(100, d.fluency_score ?? 0));
  const corr = (d.corrections || []).map((c) => `
    <li>
      <div class="c-said">${esc(c.you_said)}</div>
      <div class="c-better">${esc(c.better)}</div>
      <div class="c-why">${esc(c.why)}</div>
    </li>`).join("");
  const vocab = (d.new_vocabulary || []).map((v) =>
    `<span class="chip"><b>${esc(v.word)}</b> — ${esc(v.meaning)}</span>`).join("");
  const next = (d.next_focus || []).map((t) => `<li>${esc(t)}</li>`).join("");

  el.innerHTML = `
    <div class="report-top">
      <div class="ring">
        <svg viewBox="0 0 36 36">
          <circle cx="18" cy="18" r="16" fill="none" stroke="var(--line)" stroke-width="3"/>
          <circle cx="18" cy="18" r="16" fill="none" stroke="var(--accent)" stroke-width="3"
            stroke-dasharray="${score} 100" stroke-linecap="round"/>
        </svg>
        <div class="num"><b>${score}</b><small>fluency</small></div>
      </div>
      <div>
        <div class="report-cefr-label">CEFR estimate</div>
        <div class="report-cefr">${esc(d.cefr_estimate) || "—"}</div>
        <p class="report-comment">${esc(d.overall_comment)}</p>
      </div>
    </div>
    ${corr ? `<h3>Corrections</h3><ul class="corrections">${corr}</ul>` : ""}
    ${vocab ? `<h3>New vocabulary</h3><div class="vocab">${vocab}</div>` : ""}
    ${next ? `<h3>Practice next</h3><ul class="focus">${next}</ul>` : ""}
    <button onclick="location.reload()" class="btn btn--accent btn--block" style="margin-top:26px">New session</button>`;
  el.scrollIntoView({ behavior: "smooth" });
}

// ----- session lifecycle -----------------------------------------------------
async function startSession() {
  $("startBtn").disabled = true;
  $("startBtn").textContent = "Starting…";
  try {
    player = new PCMPlayer(24000);
    await player.resume();
    await startMic();
  } catch (err) {
    alert("Microphone access is required: " + err.message);
    $("startBtn").disabled = false;
    $("startBtn").textContent = "Start session";
    return;
  }

  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    ws.send(JSON.stringify({
      type: "start",
      language: $("language").value,
      level: $("level").value,
      scenario: $("scenario").value,
      corrections: $("corrections").checked,
    }));
    $("setup").classList.add("hidden");
    $("live").classList.remove("hidden");
  };
  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) {
      const n = Math.floor(ev.data.byteLength / 2);
      if (n > 0 && player) player.play(new Int16Array(ev.data, 0, n));
    } else {
      handleMessage(JSON.parse(ev.data));
    }
  };
  ws.onclose = () => setStatus("Disconnected", "idle");
  ws.onerror = () => setStatus("Connection error", "error");
}

function interrupt() {
  if (player) player.clear();
  if (usingBrowserTTS) speechSynthesis.cancel();
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "interrupt" }));
}

function endSession() {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "end_session" }));
  if (player) player.clear();
  if (usingBrowserTTS) speechSynthesis.cancel();
  stopMic();
  setStatus("Generating your feedback report…", "info");
}

$("startBtn").onclick = startSession;
$("interruptBtn").onclick = interrupt;
$("endBtn").onclick = endSession;
