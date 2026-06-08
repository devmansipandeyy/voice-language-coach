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
let agentSpeaking = false;   // half-duplex: don't capture mic while the coach talks
let fullDuplex = false;      // set from server "ready"; when true the mic stays live
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
    // Half-duplex: drop mic frames while the coach speaks → no echo loop.
    // Full-duplex: keep the mic live; the server's echo guard distinguishes a
    // real barge-in from the coach hearing itself.
    const gated = !fullDuplex && agentSpeaking;
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
function setStatus(text, color) {
  $("statusText").textContent = text;
  const dot = $("statusDot");
  dot.className = `relative inline-flex w-3 h-3 rounded-full ${color}`;
}
function setSpeaking(on) {
  if (on) {
    if (speakOffTimer) { clearTimeout(speakOffTimer); speakOffTimer = null; }
    agentSpeaking = true;
    setStatus("Coach is speaking…", "text-violet-400 bg-violet-400 pulse");
  } else {
    // keep the mic gated for a short tail so the agent's trailing audio
    // (and speaker echo) isn't re-captured as if the learner spoke.
    if (speakOffTimer) clearTimeout(speakOffTimer);
    speakOffTimer = setTimeout(() => { agentSpeaking = false; }, 300);
    setStatus("Listening — go ahead and talk", "text-emerald-400 bg-emerald-400");
  }
}
function addLine(role, text) {
  const wrap = document.createElement("div");
  const isCoach = role === "coach";
  wrap.className = `flex ${isCoach ? "justify-start" : "justify-end"}`;
  const bubble = document.createElement("div");
  bubble.className = isCoach
    ? "max-w-[80%] bg-slate-800 rounded-2xl rounded-tl-sm px-4 py-2"
    : "max-w-[80%] bg-violet-600 rounded-2xl rounded-tr-sm px-4 py-2";
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
      setStatus("Listening — go ahead and talk", "text-emerald-400 bg-emerald-400");
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
      setStatus(msg.message, "text-sky-400 bg-sky-400");
      break;
    case "error":
      setStatus(msg.message, "text-rose-400 bg-rose-400");
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
    <li class="bg-slate-800/60 rounded-lg p-3">
      <div class="text-rose-300 line-through">${esc(c.you_said)}</div>
      <div class="text-emerald-300 font-medium">${esc(c.better)}</div>
      <div class="text-slate-400 text-sm mt-1">${esc(c.why)}</div>
    </li>`).join("");
  const vocab = (d.new_vocabulary || []).map((v) =>
    `<span class="inline-block bg-slate-800 rounded-full px-3 py-1 m-1 text-sm">
       <b class="text-sky-300">${esc(v.word)}</b> — ${esc(v.meaning)}</span>`).join("");
  const next = (d.next_focus || []).map((t) => `<li>${esc(t)}</li>`).join("");

  el.innerHTML = `
    <div class="flex items-center gap-5">
      <div class="relative w-24 h-24 shrink-0">
        <svg viewBox="0 0 36 36" class="w-24 h-24 -rotate-90">
          <circle cx="18" cy="18" r="16" fill="none" stroke="#1e293b" stroke-width="3"/>
          <circle cx="18" cy="18" r="16" fill="none" stroke="#8b5cf6" stroke-width="3"
            stroke-dasharray="${score} 100" stroke-linecap="round"/>
        </svg>
        <div class="absolute inset-0 flex flex-col items-center justify-center">
          <span class="text-2xl font-bold">${score}</span>
          <span class="text-[10px] text-slate-400">fluency</span>
        </div>
      </div>
      <div>
        <div class="text-xs uppercase tracking-wide text-slate-400">CEFR estimate</div>
        <div class="text-2xl font-bold text-violet-300">${esc(d.cefr_estimate) || "—"}</div>
        <p class="text-slate-300 mt-1">${esc(d.overall_comment)}</p>
      </div>
    </div>
    ${corr ? `<h3 class="mt-6 mb-2 font-semibold text-slate-200">Corrections</h3><ul class="space-y-2">${corr}</ul>` : ""}
    ${vocab ? `<h3 class="mt-6 mb-2 font-semibold text-slate-200">New vocabulary</h3><div>${vocab}</div>` : ""}
    ${next ? `<h3 class="mt-6 mb-2 font-semibold text-slate-200">Practice next</h3><ul class="list-disc list-inside text-slate-300 space-y-1">${next}</ul>` : ""}
    <button onclick="location.reload()" class="mt-6 w-full bg-violet-600 hover:bg-violet-500 transition px-4 py-2.5 rounded-xl font-semibold">
      New session
    </button>`;
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
  ws.onclose = () => setStatus("Disconnected", "text-slate-500 bg-slate-500");
  ws.onerror = () => setStatus("Connection error", "text-rose-400 bg-rose-400");
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
  setStatus("Generating your feedback report…", "text-sky-400 bg-sky-400");
}

$("startBtn").onclick = startSession;
$("interruptBtn").onclick = interrupt;
$("endBtn").onclick = endSession;
