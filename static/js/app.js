/* ══════════════════════════════════════════════════════════════════════
   J.A.R.V.I.S. Live — Frontend Audio + HUD Logic
   ══════════════════════════════════════════════════════════════════════
   Audio pipeline:
     MIC  → AudioWorklet → Int16 PCM → b64 → WebSocket → server
     server → WebSocket → b64 → Int16 PCM → Float32 → AudioBuffer → speaker
   ══════════════════════════════════════════════════════════════════════ */

'use strict';

// ── Constants ────────────────────────────────────────────────────────────────
const MIC_SAMPLE_RATE = 16000;
const SPK_SAMPLE_RATE = 24000;
const CHUNK_SAMPLES   = 1024;

// ── DOM refs ─────────────────────────────────────────────────────────────────
const btnConnect     = document.getElementById('btn-connect');
const btnDisconnect  = document.getElementById('btn-disconnect');
const connDot        = document.getElementById('conn-dot');
const connLabel      = document.getElementById('conn-label');
const statusText     = document.getElementById('status-text');
const hudTime        = document.getElementById('hud-time');
const uptimeVal      = document.getElementById('uptime-val');
const latencyVal     = document.getElementById('latency-val');
const micDb          = document.getElementById('mic-db');
const spkDb          = document.getElementById('spk-db');
const vrBar          = document.getElementById('vr-bar');
const vrPct          = document.getElementById('vr-pct');
const bargeCount     = document.getElementById('barge-count');
const micMeter       = document.getElementById('mic-meter');
const spkMeter       = document.getElementById('spk-meter');
const coreCanvas     = document.getElementById('core-canvas');
const chatMessages   = document.getElementById('chat-messages');
const voiceSelect    = document.getElementById('voice-select');
const activeVoiceLbl = document.getElementById('active-voice-label');
const voiceBarName   = document.getElementById('voice-bar-name');
const voiceBarDot    = document.querySelector('.voice-bar-dot');

// ── State ─────────────────────────────────────────────────────────────────────
let ws           = null;
let micCtx       = null;
let spkCtx       = null;
let micStream    = null;
let workletNode  = null;
let sessionStart = null;
let bargeIns     = 0;
let nextPlayTime = 0;
let isPlaying    = false;
let vrConfidence = 0;
let fakeLatency  = 0;
let micVolume    = 0;
let spkVolume    = 0;

// ── Canvas core ───────────────────────────────────────────────────────────────
const ctx2d = coreCanvas.getContext('2d');
let coreState = 'idle';
let pulsePhase = 0;

function sizeCanvas() {
  const wrap = coreCanvas.parentElement;
  const side = Math.min(wrap.clientWidth, wrap.clientHeight) * 0.80;
  coreCanvas.width  = side;
  coreCanvas.height = side;
}

const PARTICLE_COUNT = 80;
const particles = Array.from({ length: PARTICLE_COUNT }, (_, i) => ({
  angle:  (i / PARTICLE_COUNT) * Math.PI * 2,
  radius: 0.28 + Math.random() * 0.26,
  speed:  (0.002 + Math.random() * 0.004) * (Math.random() < 0.5 ? 1 : -1),
  size:   0.8 + Math.random() * 1.8,
  alpha:  0.3 + Math.random() * 0.7,
}));

const RINGS = [
  { r: 0.38, speed:  0.003, dashes: 60,  gapRatio: 0.35, lw: 1.2 },
  { r: 0.30, speed: -0.005, dashes: 0,   gapRatio: 0,    lw: 0.8 },
  { r: 0.22, speed:  0.008, dashes: 24,  gapRatio: 0.5,  lw: 1.0 },
  { r: 0.46, speed: -0.002, dashes: 120, gapRatio: 0.5,  lw: 0.5 },
  { r: 0.54, speed:  0.001, dashes: 8,   gapRatio: 0.6,  lw: 0.6 },
];
const ringAngles = RINGS.map(() => 0);

function drawCore() {
  const W = coreCanvas.width, H = coreCanvas.height;
  if (!W || !H) { requestAnimationFrame(drawCore); return; }
  const cx = W / 2, cy = H / 2, R = W / 2;

  ctx2d.clearRect(0, 0, W, H);
  pulsePhase += 0.02;

  const pulseMag =
    coreState === 'listening' ? 0.10 + micVolume * 0.18 :
    coreState === 'speaking'  ? 0.08 + spkVolume * 0.14 : 0.04;
  const pulse = 1 + Math.sin(pulsePhase) * pulseMag;

  const [c1, c2] =
    coreState === 'listening' ? ['#00ff9d', '#00d4ff'] :
    coreState === 'speaking'  ? ['#00eaff', '#1a6fff'] :
                                ['#00a8cc', '#1a3fff'];

  // Ambient glow
  const grd = ctx2d.createRadialGradient(cx, cy, R * 0.1, cx, cy, R * 0.55 * pulse);
  grd.addColorStop(0,   coreState === 'idle' ? 'rgba(0,120,200,0.22)' : 'rgba(0,200,255,0.32)');
  grd.addColorStop(0.6, 'rgba(0,40,100,0.10)');
  grd.addColorStop(1,   'transparent');
  ctx2d.fillStyle = grd;
  ctx2d.beginPath(); ctx2d.arc(cx, cy, R, 0, Math.PI * 2); ctx2d.fill();

  // Rings
  RINGS.forEach((ring, i) => {
    ringAngles[i] = (ringAngles[i] + ring.speed) % (Math.PI * 2);
    const rr = R * ring.r * pulse;
    ctx2d.save();
    ctx2d.translate(cx, cy); ctx2d.rotate(ringAngles[i]);
    ctx2d.strokeStyle = c1; ctx2d.lineWidth = ring.lw;
    ctx2d.shadowBlur = 6; ctx2d.shadowColor = c1;
    ctx2d.globalAlpha = 0.65;
    if (ring.dashes > 0) {
      const circ = 2 * Math.PI * rr;
      ctx2d.setLineDash([circ / ring.dashes * (1 - ring.gapRatio), circ / ring.dashes * ring.gapRatio]);
    } else { ctx2d.setLineDash([]); }
    ctx2d.beginPath(); ctx2d.arc(0, 0, rr, 0, Math.PI * 2); ctx2d.stroke();
    ctx2d.restore();
  });

  // Particles
  particles.forEach(p => {
    p.angle += p.speed * (coreState === 'idle' ? 0.7 : 1.4);
    const pr = R * p.radius * pulse;
    const px = cx + Math.cos(p.angle) * pr;
    const py = cy + Math.sin(p.angle) * pr;
    ctx2d.beginPath(); ctx2d.arc(px, py, p.size, 0, Math.PI * 2);
    ctx2d.fillStyle = c2;
    ctx2d.globalAlpha = p.alpha * (0.5 + 0.5 * Math.sin(pulsePhase + p.angle));
    ctx2d.shadowBlur = 8; ctx2d.shadowColor = c2;
    ctx2d.fill();
  });
  ctx2d.globalAlpha = 1; ctx2d.shadowBlur = 0;

  // Core glow
  const coreR = R * 0.12 * pulse;
  const cGrd  = ctx2d.createRadialGradient(cx, cy, 0, cx, cy, coreR * 2.5);
  cGrd.addColorStop(0,   coreState === 'speaking' ? 'rgba(0,234,255,0.9)' : 'rgba(0,180,255,0.7)');
  cGrd.addColorStop(0.4, 'rgba(0,100,200,0.25)');
  cGrd.addColorStop(1,   'transparent');
  ctx2d.fillStyle = cGrd;
  ctx2d.beginPath(); ctx2d.arc(cx, cy, coreR * 2.5, 0, Math.PI * 2); ctx2d.fill();

  ctx2d.beginPath(); ctx2d.arc(cx, cy, coreR, 0, Math.PI * 2);
  ctx2d.fillStyle  = coreState === 'speaking' ? '#00eaff' : '#00c8ff';
  ctx2d.shadowBlur = 22; ctx2d.shadowColor = '#00d4ff';
  ctx2d.fill(); ctx2d.shadowBlur = 0;

  // Cross-hairs
  ctx2d.strokeStyle = 'rgba(0,212,255,0.15)'; ctx2d.lineWidth = 0.5;
  ctx2d.setLineDash([4, 8]);
  ctx2d.beginPath(); ctx2d.moveTo(cx, 0); ctx2d.lineTo(cx, H); ctx2d.stroke();
  ctx2d.beginPath(); ctx2d.moveTo(0, cy); ctx2d.lineTo(W, cy); ctx2d.stroke();
  ctx2d.setLineDash([]);

  requestAnimationFrame(drawCore);
}

// ── Waveform meters ───────────────────────────────────────────────────────────
const micMeterCtx = micMeter.getContext('2d');
const spkMeterCtx = spkMeter.getContext('2d');
const micHistory  = new Float32Array(micMeter.width);
const spkHistory  = new Float32Array(spkMeter.width);

function drawMeter(mctx, history, vol, color) {
  const W = mctx.canvas.width, H = mctx.canvas.height;
  history.copyWithin(0, 1); history[W - 1] = vol;
  mctx.clearRect(0, 0, W, H);
  mctx.strokeStyle = color; mctx.lineWidth = 1;
  mctx.shadowBlur = 4; mctx.shadowColor = color;
  mctx.beginPath();
  for (let i = 0; i < W; i++) {
    const y = H / 2 - history[i] * (H / 2 - 2);
    i === 0 ? mctx.moveTo(i, y) : mctx.lineTo(i, y);
  }
  mctx.stroke(); mctx.shadowBlur = 0;
}

// ── AudioWorklet (inline blob) ────────────────────────────────────────────────
const WORKLET_SRC = `
class PCMCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf = new Int16Array(${CHUNK_SAMPLES});
    this._pos = 0;
  }
  process(inputs) {
    const ch = inputs[0]?.[0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this._buf[this._pos++] = Math.max(-32768, Math.min(32767, ch[i] * 32768));
      if (this._pos === ${CHUNK_SAMPLES}) {
        this.port.postMessage(this._buf.buffer, [this._buf.buffer]);
        this._buf = new Int16Array(${CHUNK_SAMPLES});
        this._pos = 0;
      }
    }
    return true;
  }
}
registerProcessor('pcm-capture', PCMCapture);
`;

// ── Encoding helpers ──────────────────────────────────────────────────────────
function bufferToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

function base64ToInt16(b64) {
  const bin = atob(b64);
  const buf = new ArrayBuffer(bin.length);
  const u8  = new Uint8Array(buf);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return new Int16Array(buf);
}

function int16ToFloat32(i16) {
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
  return f32;
}

// ── Smooth playback ───────────────────────────────────────────────────────────
function enqueueAudio(f32) {
  if (!spkCtx) return;
  const buf = spkCtx.createBuffer(1, f32.length, SPK_SAMPLE_RATE);
  buf.copyToChannel(f32, 0);
  const src = spkCtx.createBufferSource();
  src.buffer = buf; src.connect(spkCtx.destination);
  const now = spkCtx.currentTime;
  if (nextPlayTime < now) nextPlayTime = now + 0.04;
  src.start(nextPlayTime);
  nextPlayTime += buf.duration;
  spkVolume = Math.min(1, spkVolume * 0.7 + 0.3 * Math.max(...Array.from(f32).map(Math.abs)));
  isPlaying = true;
}

function stopPlayback() {
  if (!spkCtx) return;
  spkCtx.close().then(() => {
    spkCtx = new AudioContext({ sampleRate: SPK_SAMPLE_RATE });
  });
  nextPlayTime = 0; isPlaying = false; spkVolume = 0;
}

// ── Tool labels ───────────────────────────────────────────────────────────────
const TOOL_LABELS = {
  // File system
  list_directory:   '📂 LIST DIR',
  read_file:        '📄 READ FILE',
  write_file:       '✏️ WRITE FILE',
  delete_file:      '🗑 DELETE',
  create_directory: '📁 CREATE DIR',
  search_files:     '🔍 SEARCH',
  get_file_info:    'ℹ️ FILE INFO',
  move_file:        '↪️ MOVE',
  // System monitoring
  get_system_info:   '🖥️ SYSTEM INFO',
  get_cpu_status:    '⚙️ CPU STATUS',
  get_memory_status: '🧠 MEMORY',
  get_gpu_status:    '🎮 GPU STATUS',
  get_disk_status:   '💾 DISK STATUS',
  get_top_processes: '📊 PROCESSES',
  get_system_uptime: '⏱️ UPTIME',
  // Browser
  open_url:   '🌐 OPEN URL',
  open_gmail: '📧 GMAIL',
};

// ── Chat panel ────────────────────────────────────────────────────────────────
function clearEmpty() {
  const empty = chatMessages.querySelector('.chat-empty');
  if (empty) empty.remove();
}

function appendMessage(role, text) {
  clearEmpty();
  const div     = document.createElement('div');
  div.className = `chat-msg ${role}`;

  const roleEl       = document.createElement('div');
  roleEl.className   = 'chat-msg-role';
  roleEl.textContent = role === 'user' ? 'YOU' : 'J.A.R.V.I.S.';

  const textEl       = document.createElement('div');
  textEl.className   = 'chat-msg-text';
  textEl.textContent = text;

  div.appendChild(roleEl);
  div.appendChild(textEl);
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

function appendToolMessage(tool, args, done, result) {
  clearEmpty();
  const div     = document.createElement('div');
  div.className = `chat-msg tool${done ? ' tool-done' : ''}`;

  const roleEl       = document.createElement('div');
  roleEl.className   = 'chat-msg-role';
  roleEl.textContent = done ? 'TOOL COMPLETE' : 'TOOL CALL';

  const textEl = document.createElement('div');
  textEl.className = 'chat-msg-text';

  const label = TOOL_LABELS[tool] || tool.toUpperCase();
  if (!done) {
    // Show what's being called
    const mainArg = args.path || args.directory || args.source || Object.values(args)[0] || '';
    textEl.textContent = `${label}\n${mainArg}`;
  } else {
    // Show concise result summary
    let summary = `${label}`;
    if (result && !result.error) {
      if (result.count !== undefined) summary += `\n${result.count} item(s)`;
      else if (result.status === 'success') summary += '\n✓ Done';
      else if (result.size) summary += `\n${result.size}`;
    } else if (result && result.error) {
      summary += `\n✗ ${result.error}`;
    }
    textEl.textContent = summary;
  }

  div.appendChild(roleEl);
  div.appendChild(textEl);
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return div;
}

// ── Connection ────────────────────────────────────────────────────────────────
async function connect() {
  btnConnect.disabled    = true;
  btnDisconnect.disabled = false;
  setStatus('CONNECTING', '');

  const selectedVoice = voiceSelect.value;
  setActiveVoice(selectedVoice);
  if (voiceBarDot) voiceBarDot.classList.add('active');

  // Speaker context
  spkCtx = new AudioContext({ sampleRate: SPK_SAMPLE_RATE });

  // Mic context at 16 kHz
  micCtx = new AudioContext({ sampleRate: MIC_SAMPLE_RATE });
  const blobURL = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'application/javascript' }));
  await micCtx.audioWorklet.addModule(blobURL);
  URL.revokeObjectURL(blobURL);

  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, sampleRate: MIC_SAMPLE_RATE, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
  });

  const micSource = micCtx.createMediaStreamSource(micStream);
  workletNode     = new AudioWorkletNode(micCtx, 'pcm-capture');

  workletNode.port.onmessage = (e) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const i16 = new Int16Array(e.data);
    let sum = 0; for (const v of i16) sum += Math.abs(v);
    micVolume = Math.min(1, (sum / i16.length) / 8192);
    ws.send(JSON.stringify({ type: 'audio', data: bufferToBase64(e.data) }));
  };

  micSource.connect(workletNode);
  workletNode.connect(micCtx.destination);

  // Open WebSocket
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    // First message: send voice config
    ws.send(JSON.stringify({ type: 'config', voice: selectedVoice }));
    setStatus('LISTENING', 'listening');
    connDot.classList.add('connected');
    connLabel.textContent = 'CONNECTED';
    sessionStart  = Date.now();
    fakeLatency   = Math.floor(Math.random() * 40) + 55;
    latencyVal.textContent = fakeLatency + ' ms';
  };

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);

    if (msg.type === 'audio') {
      const f32 = int16ToFloat32(base64ToInt16(msg.data));
      enqueueAudio(f32);
      setStatus('SPEAKING', 'speaking');
    }

    if (msg.type === 'transcript') {
      appendMessage(msg.role, msg.text);
    }

    if (msg.type === 'tool_start') {
      appendToolMessage(msg.tool, msg.args || {}, false, null);
      setStatus('TOOL: ' + (TOOL_LABELS[msg.tool] || msg.tool), 'speaking');
    }

    if (msg.type === 'tool_done') {
      appendToolMessage(msg.tool, msg.args || {}, true, msg.result);
      if (ws && ws.readyState === WebSocket.OPEN) setStatus('LISTENING', 'listening');
    }

    if (msg.type === 'interrupted') {
      stopPlayback();
      bargeIns++;
      bargeCount.textContent = bargeIns;
      setStatus('LISTENING', 'listening');
    }

    if (msg.type === 'error') {
      console.error('Server error:', msg.message);
      setStatus('ERR: ' + (msg.message || 'unknown').slice(0, 55), '');
      connDot.classList.add('error');
    }

    if (msg.type === 'voice_changed') {
      voiceSelect.value = msg.voice;
      voiceSelect.disabled = false;
      voiceSelect.classList.remove('switching');
      setActiveVoice(msg.voice);
      setStatus('LISTENING', 'listening');
    }
  };

  ws.onclose = (event) => {
    stopPlayback();
    connDot.classList.remove('connected', 'error');
    voiceSelect.disabled = false;
    voiceSelect.classList.remove('switching');
    if (voiceBarDot) voiceBarDot.classList.remove('active');

    // 1011 = Gemini internal crash, 1006 = abnormal close — reconnect automatically
    if (event.code === 1011 || event.code === 1006) {
      connDot.classList.add('error');
      connLabel.textContent = 'RECONNECTING...';
      setStatus('RECONNECTING', '');
      appendMessage('jarvis', '[Session interrupted — reconnecting in 3 s…]');
      // Full teardown then fresh connect
      setTimeout(() => { disconnect(); setTimeout(connect, 200); }, 3000);
      return;
    }

    setStatus('DISCONNECTED', '');
    connLabel.textContent    = 'DISCONNECTED';
    btnConnect.disabled      = false;
    btnDisconnect.disabled   = true;
  };

  ws.onerror = (err) => {
    console.error('WS error', err);
    setStatus('CONNECTION ERROR', '');
    connDot.classList.add('error');
  };
}

function disconnect() {
  if (ws)          { ws.close(); ws = null; }
  if (micStream)   micStream.getTracks().forEach(t => t.stop());
  if (workletNode) workletNode.disconnect();
  if (micCtx)      micCtx.close();
  if (spkCtx)      spkCtx.close();
  micCtx = spkCtx = micStream = workletNode = null;
  micVolume = spkVolume = 0;
  nextPlayTime = 0; isPlaying = false; sessionStart = null;
}

function setStatus(text, cls) {
  statusText.textContent = text;
  statusText.className   = 'core-status' + (cls ? ` ${cls}` : '');
  coreState              = cls || 'idle';
}

function setActiveVoice(name) {
  const upper = name.toUpperCase();
  if (activeVoiceLbl) activeVoiceLbl.textContent = upper;
  if (voiceBarName)   voiceBarName.textContent   = upper;
}

// ── UI ticker ─────────────────────────────────────────────────────────────────
setInterval(() => {
  // Clock
  const n = new Date();
  hudTime.textContent =
    String(n.getHours()).padStart(2,'0') + ':' +
    String(n.getMinutes()).padStart(2,'0') + ':' +
    String(n.getSeconds()).padStart(2,'0');

  // Uptime
  if (sessionStart) {
    const d  = Math.floor((Date.now() - sessionStart) / 1000);
    uptimeVal.textContent =
      String(Math.floor(d / 3600)).padStart(2,'0') + ':' +
      String(Math.floor((d % 3600) / 60)).padStart(2,'0') + ':' +
      String(d % 60).padStart(2,'0');
  }

  // VR confidence drift
  if (sessionStart) {
    vrConfidence = Math.min(99, vrConfidence + (Math.random() * 2 - 0.5));
    if (vrConfidence < 0) vrConfidence = 0;
  } else {
    vrConfidence = Math.max(0, vrConfidence - 1);
  }
  vrBar.style.width   = vrConfidence + '%';
  vrPct.textContent   = Math.round(vrConfidence) + '%';

  // Latency jitter
  if (sessionStart) {
    fakeLatency = Math.max(30, fakeLatency + (Math.random() * 6 - 3));
    latencyVal.textContent = Math.round(fakeLatency) + ' ms';
  }

  // Mic meter
  micDb.textContent = micVolume > 0.001
    ? (20 * Math.log10(micVolume + 1e-6)).toFixed(1) + ' dB' : '— dB';
  drawMeter(micMeterCtx, micHistory, micVolume, '#00ff9d');

  // Speaker meter
  spkDb.textContent = spkVolume > 0.001
    ? (20 * Math.log10(spkVolume + 1e-6)).toFixed(1) + ' dB' : '— dB';
  drawMeter(spkMeterCtx, spkHistory, spkVolume, '#00d4ff');
  spkVolume *= 0.92;

  // Detect end of playback
  if (isPlaying && spkCtx && spkCtx.currentTime >= nextPlayTime - 0.05) {
    isPlaying = false;
    if (ws && ws.readyState === WebSocket.OPEN) setStatus('LISTENING', 'listening');
  }

}, 1000 / 30);

// ── Bind UI ───────────────────────────────────────────────────────────────────
btnConnect.addEventListener('click', connect);
btnDisconnect.addEventListener('click', disconnect);
window.addEventListener('resize', sizeCanvas);

voiceSelect.addEventListener('change', () => {
  const newVoice = voiceSelect.value;
  if (ws && ws.readyState === WebSocket.OPEN) {
    // Mid-session voice switch: stop audio, signal backend, show switching state
    stopPlayback();
    setStatus('SWITCHING VOICE', '');
    voiceSelect.disabled = true;
    voiceSelect.classList.add('switching');
    ws.send(JSON.stringify({ type: 'voice_change', voice: newVoice }));
  } else {
    // Not connected — just update labels for the next session
    setActiveVoice(newVoice);
  }
});

sizeCanvas();
requestAnimationFrame(drawCore);
