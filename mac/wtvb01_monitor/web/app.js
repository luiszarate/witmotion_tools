'use strict';

// --- configuration -----------------------------------------------------------

const AXES = ['x', 'y', 'z'];
const AXIS_COLORS = { x: '#ff6b6b', y: '#4ade80', z: '#4aa3ff' };
const MAX_SAMPLES = 20000;
const RECONNECT_MS = 1500;

// `live` is the mode's channel-group name, which differs from the chart key
// where the register keys are prefixed differently (fast_disp_* vs "fast").
const GROUPS = {
  velocity:     { label: 'Velocidad',    unit: 'mm/s', decimals: 1, live: 'velocity' },
  displacement: { label: 'Desplazam.',   unit: 'µm',   decimals: 0, live: 'displacement' },
  fast_disp:    { label: 'Despl. rápido', unit: 'µm',  decimals: 0, live: 'fast' },
  frequency:    { label: 'Frecuencia',   unit: 'Hz',   decimals: 1, live: 'frequency' },
  angle:        { label: 'Ángulo',       unit: '°',    decimals: 3, live: 'angle' },
  accel:        { label: 'Aceleración',  unit: 'g',    decimals: 3, live: 'accel' },
  gyro:         { label: 'Giro',         unit: '°/s',  decimals: 2, live: 'gyro' },
};

const TILE_GROUPS = ['velocity', 'displacement', 'frequency', 'angle'];

// --- state -------------------------------------------------------------------

const state = {
  samples: [],
  seen: new Set(),
  connected: false,
  recording: false,
  paused: false,
  windowSec: 30,
  registers: [],
  modes: [],
  returnRates: {},
  latest: null,
  source: {},
};

const $ = (id) => document.getElementById(id);

// --- charts ------------------------------------------------------------------

class Chart {
  constructor(figure) {
    this.figure = figure;
    this.group = figure.dataset.group;
    this.canvas = figure.querySelector('canvas');
    this.ctx = this.canvas.getContext('2d');
    this.legend = figure.querySelector('.legend');
    this.legend.innerHTML = AXES
      .map((a) => `<span style="color:${AXIS_COLORS[a]}">${a.toUpperCase()}</span>`)
      .join('');
    this.resize();
  }

  resize() {
    const ratio = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    if (!rect.width) return;
    this.canvas.width = Math.round(rect.width * ratio);
    this.canvas.height = Math.round(rect.height * ratio);
    this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    this.w = rect.width;
    this.h = rect.height;
  }

  // Returns [min, max] over the visible window, padded and never degenerate.
  bounds(series) {
    let lo = Infinity;
    let hi = -Infinity;
    for (const points of series) {
      for (const p of points) {
        if (p.v < lo) lo = p.v;
        if (p.v > hi) hi = p.v;
      }
    }
    if (!isFinite(lo)) return [0, 1];
    if (hi - lo < 1e-9) { lo -= 0.5; hi += 0.5; }
    const pad = (hi - lo) * 0.12;
    return [lo - pad, hi + pad];
  }

  draw(samples, tEnd, windowSec) {
    if (!this.w) this.resize();
    const ctx = this.ctx;
    const padL = 44;
    const padR = 8;
    const padT = 6;
    const padB = 16;
    const plotW = Math.max(1, this.w - padL - padR);
    const plotH = Math.max(1, this.h - padT - padB);
    const tStart = tEnd - windowSec;

    const series = AXES.map((axis) => {
      const key = `${this.group}_${axis}`;
      const points = [];
      for (let i = samples.length - 1; i >= 0; i--) {
        const s = samples[i];
        if (s.t < tStart) break;
        const v = s.values[key];
        if (v !== undefined) points.push({ t: s.t, v });
      }
      points.reverse();
      return points;
    });

    const hasData = series.some((p) => p.length > 0);
    this.figure.hidden = !hasData && !state.paused;
    const freshness = hasData ? groupFreshness(this.group) : 'live';
    this.figure.classList.toggle('stale', freshness === 'stale');
    this.figure.classList.toggle('aliased', freshness === 'aliased');
    ctx.clearRect(0, 0, this.w, this.h);
    if (!hasData) return;

    const [lo, hi] = this.bounds(series);
    const xOf = (t) => padL + ((t - tStart) / windowSec) * plotW;
    const yOf = (v) => padT + plotH - ((v - lo) / (hi - lo)) * plotH;

    // grid + y labels
    ctx.strokeStyle = '#2a3240';
    ctx.fillStyle = '#8b95a7';
    ctx.lineWidth = 1;
    ctx.font = '10px -apple-system, system-ui, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let i = 0; i <= 4; i++) {
      const v = lo + ((hi - lo) * i) / 4;
      const y = Math.round(yOf(v)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.fillText(formatTick(v, hi - lo), padL - 6, y);
    }
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (let i = 0; i <= 3; i++) {
      const secondsAgo = windowSec * (1 - i / 3);
      const x = Math.round(padL + (plotW * i) / 3) + 0.5;
      if (i > 0 && i < 3) {
        ctx.beginPath();
        ctx.moveTo(x, padT);
        ctx.lineTo(x, padT + plotH);
        ctx.stroke();
      }
      ctx.fillText(secondsAgo === 0 ? 'ahora' : `-${Math.round(secondsAgo)}s`, x, padT + plotH + 3);
    }

    // series
    ctx.lineWidth = 1.4;
    ctx.lineJoin = 'round';
    series.forEach((points, index) => {
      if (points.length === 0) return;
      ctx.strokeStyle = AXIS_COLORS[AXES[index]];
      ctx.beginPath();
      points.forEach((p, i) => {
        const x = xOf(p.t);
        const y = yOf(p.v);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });
  }
}

// How trustworthy this group's trace is under the current capture mode:
// 'live' (refreshed at a useful rate), 'aliased' (refreshed, but far slower
// than the signal it represents) or 'stale' (not refreshed at all).
function groupFreshness(group) {
  const mode = state.modes.find((m) => m.key === state.source.mode);
  if (!mode) return 'live';
  const name = GROUPS[group] ? GROUPS[group].live : group;
  if (!mode.live_groups.includes(name)) return 'stale';
  return mode.undersampled_groups.includes(name) ? 'aliased' : 'live';
}

function formatTick(value, span) {
  const decimals = span >= 100 ? 0 : span >= 10 ? 1 : span >= 1 ? 2 : 3;
  return value.toFixed(decimals);
}

const charts = Array.from(document.querySelectorAll('.chart')).map((f) => new Chart(f));

// --- tiles -------------------------------------------------------------------

function buildTiles() {
  const html = TILE_GROUPS.map((group) => {
    const meta = GROUPS[group];
    return `<div class="tile" data-tile="${group}">
      <div class="name">${meta.label}</div>
      <div class="value">–<small>${meta.unit}</small></div>
      <div class="axes">
        ${AXES.map((a) => `<span class="${a}" data-axis="${a}">–</span>`).join('')}
      </div>
    </div>`;
  }).join('');
  const extra = `
    <div class="tile" data-tile="temperature">
      <div class="name">Temperatura módulo</div>
      <div class="value">–<small>°C</small></div>
      <div class="axes"><span>sensor interno</span></div>
    </div>
    <div class="tile" data-tile="rate">
      <div class="name">Trama continua</div>
      <div class="value">–<small>Hz</small></div>
      <div class="axes"><span id="rateDetail">—</span></div>
    </div>`;
  $('tiles').innerHTML = html + extra;
}

function updateTiles(sample) {
  for (const group of TILE_GROUPS) {
    const tile = document.querySelector(`[data-tile="${group}"]`);
    const meta = GROUPS[group];
    const values = AXES.map((a) => sample.values[`${group}_${a}`]);
    const present = values.filter((v) => v !== undefined);
    const peak = present.length ? Math.max(...present.map(Math.abs)) : null;
    tile.querySelector('.value').innerHTML =
      (peak === null ? '–' : peak.toFixed(meta.decimals)) + `<small>${meta.unit}</small>`;
    AXES.forEach((axis, i) => {
      const cell = tile.querySelector(`[data-axis="${axis}"]`);
      cell.textContent = values[i] === undefined ? '–' : `${axis.toUpperCase()} ${values[i].toFixed(meta.decimals)}`;
    });
  }
  const temp = sample.values.temperature;
  document.querySelector('[data-tile="temperature"] .value').innerHTML =
    (temp === undefined ? '–' : temp.toFixed(2)) + '<small>°C</small>';
}

function updateRate() {
  const rate = state.source.rate_hz;
  document.querySelector('[data-tile="rate"] .value').innerHTML =
    (rate ? rate.toFixed(1) : '–') + '<small>Hz</small>';
  const detail = $('rateDetail');
  if (!detail) return;
  const poll = state.source.poll_rate_hz;
  detail.textContent = poll ? `sondeo ${poll.toFixed(1)} Hz` : 'sin sondeo';
}

// --- raw register table ------------------------------------------------------

function updateRaw(sample) {
  if (!document.querySelector('.raw').open) return;
  const byAddress = new Map(state.registers.map((r) => [r.address, r]));
  const rows = Object.entries(sample.raw).map(([addr, raw]) => {
    const meta = byAddress.get(addr);
    const value = meta ? sample.values[meta.key] : undefined;
    return `<tr>
      <td class="addr">${addr}</td>
      <td class="label">${meta ? meta.label : 'sin documentar'}</td>
      <td class="num">${raw}</td>
      <td class="num">${value === undefined ? '' : value.toFixed(3) + ' ' + meta.unit}</td>
    </tr>`;
  });
  $('rawTable').querySelector('tbody').innerHTML = rows.join('');
}

// --- data intake -------------------------------------------------------------

function pushSample(sample) {
  const last = state.samples[state.samples.length - 1];
  if (last && sample.t < last.t) return;
  state.samples.push(sample);
  if (state.samples.length > MAX_SAMPLES) state.samples.splice(0, state.samples.length - MAX_SAMPLES);
  state.latest = sample;
}

async function loadHistory() {
  try {
    const { samples } = await api('/api/history');
    state.samples = [];
    samples.forEach(pushSample);
  } catch (err) {
    console.warn('history unavailable', err);
  }
}

let eventSource = null;

function openStream() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/api/stream');
  eventSource.onmessage = (event) => {
    try {
      pushSample(JSON.parse(event.data));
    } catch (err) {
      console.warn('sample parse failed', err);
    }
  };
  eventSource.onerror = () => {
    eventSource.close();
    eventSource = null;
    setTimeout(openStream, RECONNECT_MS);
  };
}

// --- API ---------------------------------------------------------------------

async function api(path, options) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({ ok: false, error: 'respuesta ilegible' }));
  if (!response.ok || payload.ok === false) throw new Error(payload.error || response.statusText);
  return payload;
}

const post = (path, body) =>
  api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });

function notify(message) {
  const el = $('notice');
  el.textContent = message;
  el.hidden = !message;
}

// --- capture mode ------------------------------------------------------------

function currentMode() {
  return state.modes.find((m) => m.key === $('modeSelect').value) || state.modes[0];
}

function buildModeSelect() {
  $('modeSelect').innerHTML = state.modes
    .map((m) => `<option value="${m.key}">${m.label}</option>`)
    .join('');
  applyModeToControls();
}

// Keep the poll-rate box consistent with the selected mode: disabled for a
// mode that never polls, and bounded by the fastest interval the sensor
// actually answers.
let shownMode = null;

function applyModeToControls() {
  const mode = currentMode();
  const field = $('pollField');
  const input = $('pollRate');
  $('modeHint').textContent = mode ? mode.description : '';
  if (!mode) return;
  const polls = mode.blocks.length > 0;
  field.classList.toggle('off', !polls);
  input.disabled = !polls;
  input.max = polls ? Math.round(1 / mode.min_interval) : '';
  // A different mode means a different sensible poll rate, so reset to its
  // default rather than carrying the previous mode's number across.
  if (mode.key !== shownMode) {
    shownMode = mode.key;
    input.value = polls ? Math.round(1 / mode.default_interval) : '';
  } else if (polls && Number(input.value) > Number(input.max)) {
    input.value = input.max;
  }
}

function requestedPollInterval() {
  const mode = currentMode();
  if (!mode || mode.blocks.length === 0) return null;
  const hz = Number($('pollRate').value);
  return hz > 0 ? 1 / hz : null;
}

async function applyMode() {
  applyModeToControls();
  if (!state.connected) return;
  try {
    await post('/api/mode', { mode: $('modeSelect').value, poll_interval: requestedPollInterval() });
    notify('');
  } catch (err) {
    notify(err.message);
  }
  refreshStatus();
}

// --- sensor configuration ----------------------------------------------------

// Each entry writes one register through /api/setting. `cutoff` is split
// across two registers, so it sends both.
const SETTINGS = [
  {
    id: 'cutoff',
    name: 'Frecuencia de corte',
    note: 'Filtra el ruido por encima de esta frecuencia. Por defecto 10.0 Hz.',
    input: () => '<input type="number" min="1" max="100" step="0.1" value="10.0">',
    writes: (raw) => {
      const hz = Number(raw);
      if (!(hz >= 1 && hz <= 100)) throw new Error('La frecuencia de corte debe estar entre 1 y 100 Hz');
      return [
        { name: 'cutoff_int', value: Math.floor(hz) },
        { name: 'cutoff_frac', value: Math.round((hz - Math.floor(hz)) * 10) },
      ];
    },
  },
  {
    id: 'sample_freq',
    name: 'Ciclo de detección',
    note: 'Paquetes de medición por segundo dentro del sensor. Por defecto 100 Hz.',
    input: () => '<input type="number" min="1" max="100" step="1" value="100">',
    writes: (raw) => [{ name: 'sample_freq', value: Number(raw) }],
  },
  {
    id: 'return_rate',
    name: 'Tasa de retorno',
    note: 'Frecuencia de la trama continua que emite el sensor.',
    input: () => {
      const options = Object.entries(state.returnRates)
        .map(([code, hz]) => `<option value="${parseInt(code, 16)}"${hz === 100 ? ' selected' : ''}>${hz} Hz</option>`)
        .join('');
      return `<select>${options}</select>`;
    },
    writes: (raw) => [{ name: 'return_rate', value: Number(raw) }],
  },
];

function buildSettings() {
  $('settings').innerHTML = SETTINGS.map(
    (setting) => `<div class="setting" data-setting="${setting.id}">
      <div class="name">${setting.name}</div>
      <div class="row">${setting.input()}<button class="apply">Aplicar</button></div>
      <div class="note">${setting.note}</div>
    </div>`
  ).join('');

  for (const setting of SETTINGS) {
    const card = document.querySelector(`[data-setting="${setting.id}"]`);
    card.querySelector('.apply').addEventListener('click', () => applySetting(setting, card));
  }
}

async function applySetting(setting, card) {
  if (!state.connected) {
    notify('Conecta el sensor antes de cambiar su configuración.');
    return;
  }
  const field = card.querySelector('input, select');
  let writes;
  try {
    writes = setting.writes(field.value);
  } catch (err) {
    notify(err.message);
    return;
  }
  const summary = writes.map((w) => `${w.name} = ${w.value}`).join(', ');
  if (!window.confirm(`Se escribirá en el sensor y quedará guardado:\n\n${summary}\n\n¿Continuar?`)) return;
  try {
    for (const write of writes) await post('/api/setting', write);
    notify(`Escrito en el sensor: ${summary}`);
  } catch (err) {
    notify(err.message);
  }
}

// --- UI wiring ---------------------------------------------------------------

async function loadPorts() {
  const select = $('portSelect');
  try {
    const { ports } = await api('/api/ports');
    const previous = select.value;
    select.innerHTML = ports.length
      ? ports.map((p) => `<option value="${p.device}">${p.device}${p.likely ? ' ★' : ''} — ${p.description}</option>`).join('')
      : '<option value="">sin puertos</option>';
    if (previous && ports.some((p) => p.device === previous)) select.value = previous;
  } catch (err) {
    notify(`No se pudieron listar los puertos: ${err.message}`);
  }
}

async function toggleConnection() {
  const button = $('connectBtn');
  button.disabled = true;
  try {
    if (state.connected) {
      await post('/api/disconnect');
    } else {
      await post('/api/connect', {
        port: $('portSelect').value,
        mode: $('modeSelect').value,
        poll_interval: requestedPollInterval(),
      });
      state.samples = [];
      state.latest = null;
    }
    notify('');
  } catch (err) {
    notify(err.message);
  } finally {
    button.disabled = false;
    refreshStatus();
  }
}

async function toggleRecording() {
  try {
    const payload = await post('/api/record', { action: state.recording ? 'stop' : 'start' });
    notify(state.recording ? `Grabación guardada en ${payload.path}` : '');
  } catch (err) {
    notify(err.message);
  }
  refreshStatus();
}

async function refreshStatus() {
  try {
    const status = await api('/api/status');
    state.source = status.source || {};
    state.connected = !!state.source.connected;
    state.recording = !!(status.recording && status.recording.active);
    applyStatus(status);
  } catch (err) {
    notify(`Sin conexión con el servidor: ${err.message}`);
  }
}

function applyStatus(status) {
  const dot = $('statusDot');
  dot.className = 'dot' + (state.source.error ? ' error' : state.connected ? ' live' : '');
  $('statusText').textContent = state.source.error
    ? state.source.error
    : state.connected
      ? `${state.source.port} · ${state.source.layout || 'detectando…'}`
      : 'Desconectado';

  const connectBtn = $('connectBtn');
  connectBtn.textContent = state.connected ? 'Desconectar' : 'Conectar';
  connectBtn.classList.toggle('on', state.connected);

  const recordBtn = $('recordBtn');
  recordBtn.disabled = !state.connected;
  recordBtn.classList.toggle('on', state.recording);
  recordBtn.textContent = state.recording ? '■ Detener' : '● Grabar';

  $('footStats').textContent = state.connected
    ? `${status.source.frames} tramas · ${status.source.bytes_read} bytes · ${status.source.dropped_bytes} descartados · `
      + `sondeo ${status.source.poll_rate_hz.toFixed(1)} Hz · ${state.samples.length} muestras en memoria`
    : 'Selecciona un puerto y conecta el sensor.';
  if (state.connected && state.source.mode && $('modeSelect').value !== state.source.mode) {
    $('modeSelect').value = state.source.mode;
    applyModeToControls();
  }

  $('recordStats').textContent = state.recording
    ? `Grabando → ${status.recording.path} (${status.recording.rows} filas)`
    : '';
  updateRate();
}

function render() {
  if (!state.paused && state.latest) {
    const tEnd = state.latest.t;
    for (const chart of charts) chart.draw(state.samples, tEnd, state.windowSec);
    updateTiles(state.latest);
    updateRaw(state.latest);
  }
  requestAnimationFrame(render);
}

async function boot() {
  buildTiles();
  try {
    const catalog = await api('/api/registers');
    state.registers = catalog.registers;
    state.modes = catalog.modes;
    state.returnRates = catalog.return_rates;
    buildModeSelect();
    buildSettings();
  } catch (err) {
    notify(`No se pudo leer el catálogo de registros: ${err.message}`);
  }
  await loadPorts();
  await refreshStatus();
  await loadHistory();
  openStream();

  $('refreshPorts').addEventListener('click', loadPorts);
  $('connectBtn').addEventListener('click', toggleConnection);
  $('modeSelect').addEventListener('change', applyMode);
  $('pollRate').addEventListener('change', applyMode);
  $('recordBtn').addEventListener('click', toggleRecording);
  $('windowSelect').addEventListener('change', (e) => { state.windowSec = Number(e.target.value); });
  $('pauseBtn').addEventListener('click', (e) => {
    state.paused = !state.paused;
    e.target.classList.toggle('on', state.paused);
    e.target.textContent = state.paused ? '▶' : '❚❚';
  });
  window.addEventListener('resize', () => charts.forEach((c) => c.resize()));

  setInterval(refreshStatus, 1000);
  requestAnimationFrame(render);
}

boot();
