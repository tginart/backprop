/**
 * Architecture: replayed console chunks -> per-stream ANSI state -> styled lines
 * -> escaped HTML. Model replay applies Unicode tail edits before this parser.
 * Only SGR colors/effects become fixed CSS; OSC, cursor and unknown controls are
 * discarded. Raw logs stay unchanged in events, files and Python readback.
 * Main callers: app.mjs run detail and console display contract tests.
 */
import { escapeHtml as e } from './model.mjs';

const PALETTE = ['#161b1d', '#ef7f88', '#b9e778', '#f2bd75', '#72a9ef', '#b09be9', '#66cabe', '#dce5e3',
  '#849391', '#ffa2ab', '#d3f5a0', '#ffda9b', '#9ac3ff', '#d2b8ff', '#94e8dd', '#ffffff'];
const defaults = () => ({ foreground: null, background: null, bold: false, dim: false, italic: false,
  underline: false, blink: false, reverse: false, strike: false });
const parserState = () => ({ style: defaults(), mode: 'text', parameters: '', overflow: false, afterCR: false });

function indexedColor(index) {
  if (index < 16) return PALETTE[index];
  if (index >= 232) { const value = 8 + (index - 232) * 10; return `rgb(${value} ${value} ${value})`; }
  const offset = index - 16, level = value => value ? 55 + value * 40 : 0;
  return `rgb(${level(Math.floor(offset / 36))} ${level(Math.floor(offset / 6) % 6)} ${level(offset % 6)})`;
}

/** Convert validated SGR numbers into a new style; no input becomes raw CSS.
 * Main callers: parseChunk when a complete CSI m sequence arrives.
 */
function applySgr(previous, parameters) {
  if (!/^[0-9;]*$/.test(parameters)) return previous;
  const values = parameters.split(';').map(value => value === '' ? 0 : Number(value));
  let style = { ...previous };
  const on = { 1: 'bold', 2: 'dim', 3: 'italic', 4: 'underline', 5: 'blink', 7: 'reverse', 9: 'strike' };
  const off = { 23: 'italic', 24: 'underline', 25: 'blink', 27: 'reverse', 29: 'strike' };
  for (let index = 0; index < values.length; index++) {
    const value = values[index];
    if (value === 0) style = defaults();
    else if (value === 38 || value === 48) {
      const mode = values[++index], count = mode === 5 ? 1 : mode === 2 ? 3 : 0;
      const channels = values.slice(index + 1, index + 1 + count);
      if (!count || channels.length !== count || channels.some(channel => !Number.isInteger(channel) || channel < 0 || channel > 255)) break;
      style[value === 38 ? 'foreground' : 'background'] = mode === 5 ? indexedColor(channels[0]) : `rgb(${channels.join(' ')})`;
      index += count;
    } else if (value >= 30 && value <= 37) style.foreground = PALETTE[value - 30];
    else if (value >= 90 && value <= 97) style.foreground = PALETTE[value - 90 + 8];
    else if (value >= 40 && value <= 47) style.background = PALETTE[value - 40];
    else if (value >= 100 && value <= 107) style.background = PALETTE[value - 100 + 8];
    else if (value === 39) style.foreground = null;
    else if (value === 49) style.background = null;
    else if (value === 22) { style.bold = false; style.dim = false; }
    else if (Object.hasOwn(on, value)) style[on[value]] = true;
    else if (Object.hasOwn(off, value)) style[off[value]] = false;
  }
  return style;
}

function parseChunk(state, text) {
  const runs = [];
  for (const character of String(text)) {
    if (state.mode === 'osc') {
      if (character === '\x07' || character === '\x9c') state.mode = 'text';
      else if (character === '\x1b') state.mode = 'osc-escape';
      continue;
    }
    if (state.mode === 'osc-escape') { state.mode = character === '\\' || character === '\x07' ? 'text' : 'osc'; continue; }
    if (state.mode === 'escape') {
      state.mode = character === '[' ? 'csi' : character === ']' ? 'osc' : 'text';
      state.parameters = ''; state.overflow = false;
      continue;
    }
    if (state.mode === 'csi') {
      const code = character.codePointAt(0);
      if (code >= 0x40 && code <= 0x7e) {
        if (character === 'm' && !state.overflow) state.style = applySgr(state.style, state.parameters);
        state.mode = 'text'; state.parameters = '';
      } else if (state.parameters.length < 256) state.parameters += character;
      else state.overflow = true;
      continue;
    }
    if (character === '\x1b') { state.mode = 'escape'; continue; }
    if (character === '\x9b') { state.mode = 'csi'; state.parameters = ''; state.overflow = false; continue; }
    if (character === '\x9d') { state.mode = 'osc'; continue; }
    const code = character.codePointAt(0);
    if (code < 32 && !'\n\r\t'.includes(character) || code >= 127 && code <= 159) continue;
    const last = runs.at(-1);
    if (last?.style === state.style) last.text += character;
    else runs.push({ text: character, style: state.style });
  }
  return runs;
}

/** Join split writes into lines without mixing stdout/stderr style or content.
 * Main callers: renderConsole and tests. A line keeps its first fragment timestamp;
 * later fragments append to that same stream's unfinished line. This matches
 * PublicApi console line ordering while keeping raw/redirect SGR bytes intact.
 */
export function consoleLines(chunks) {
  const streams = new Map(), pending = new Map(), lines = [];
  for (const chunk of chunks) {
    const stream = chunk.stream || 'stdout';
    if (!streams.has(stream)) streams.set(stream, parserState());
    const state = streams.get(stream);
    // A tail edit may retain only the start of an SGR escape in the original
    // chunk. Keep that line's position while its visible suffix arrives later.
    if (chunk.text && !pending.has(stream)) {
      const line = { stream, timestamp: chunk.timestamp, runs: [], terminated: false };
      pending.set(stream, line); lines.push(line);
    }
    for (const run of parseChunk(state, chunk.text)) for (const character of run.text) {
      if (character === '\n' && state.afterCR) { state.afterCR = false; continue; }
      state.afterCR = character === '\r';
      if (!pending.has(stream)) {
        const line = { stream, timestamp: chunk.timestamp, runs: [], terminated: false };
        pending.set(stream, line); lines.push(line);
      }
      const line = pending.get(stream);
      if (character === '\n' || character === '\r') { line.terminated = true; pending.delete(stream); continue; }
      const last = line.runs.at(-1);
      if (last?.style === run.style) last.text += character;
      else line.runs.push({ text: character, style: run.style });
    }
  }
  return lines.filter(line => line.runs.length || line.terminated);
}

function renderSpan({ text, style }) {
  const classes = ['bold', 'dim', 'italic', 'underline', 'blink', 'strike'].filter(name => style[name]).map(name => `ansi-${name}`);
  let foreground = style.foreground, background = style.background;
  if (style.reverse) [foreground, background] = [background || 'var(--console-bg)', foreground || 'var(--console-fg)'];
  const css = `${foreground ? `color:${foreground};` : ''}${background ? `background-color:${background};` : ''}`;
  return `<span${classes.length ? ` class="${classes.join(' ')}"` : ''}${css ? ` style="${css}"` : ''}>${e(text)}</span>`;
}

export function renderConsole(chunks) {
  const lines = consoleLines(chunks);
  if (!lines.length) return '<div class="notice">No visible console text yet.</div>';
  return `<div class="console-heading"><span>${lines.length} lines</span><span>ANSI colors and styles preserved</span></div><div class="console-log" tabindex="0" aria-label="Captured console">${lines.map((line, index) => `<div class="console-row"><span class="console-number" aria-hidden="true">${index + 1}</span><time class="console-time">${e(new Date(line.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }))}</time><span class="console-stream ${line.stream === 'stderr' ? 'console-stderr' : ''}">${e(line.stream)}</span><pre class="console-text" data-console-text>${line.runs.map(renderSpan).join('') || ' '}</pre></div>`).join('')}</div>`;
}
