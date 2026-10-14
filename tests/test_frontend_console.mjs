/** Console rendering contracts: actual reducer tail edits precede per-stream
 * SGR parsing, and only escaped text plus whitelisted styles reach the DOM.
 * Main caller: node --test tests/test_frontend_console.mjs.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeEvents } from '../web/model.mjs';
import { consoleLines, renderConsole } from '../web/console-view.mjs';

const chunk = (text, stream = 'stdout') => ({ text, stream, timestamp: '2026-10-03T12:00:00Z' });
const plain = line => line.runs.map(run => run.text).join('');

test('split SGR and interleaved streams keep independent styles and line order', () => {
  const lines = consoleLines([chunk('\x1b[31mred '), chunk('warning\n', 'stderr'),
    chunk('\x1b[3'), chunk('2;1mgreen\x1b[0m plain\n')]);
  assert.deepEqual(lines.map(plain), ['red green plain', 'warning']);
  assert.equal(lines[0].runs[0].style.foreground, '#ef7f88');
  const green = lines[0].runs.find(run => run.text === 'green');
  assert.equal(green.style.foreground, '#b9e778'); assert(green.style.bold);
  assert.equal(lines[0].runs.at(-1).style.foreground, null);
  assert.equal(lines[1].runs[0].style.foreground, null);
});

test('Unicode edits inside SGR sequences reconstruct the new style before rendering', () => {
  const event = (index, payload) => ({ event_id: `console-${index}`, run_id: 'styled', project: 'test',
    timestamp: `2026-10-03T12:00:0${index}Z`, type: 'console', payload });
  const { runs, issues } = normalizeEvents([event(0, { text: '\x1b[31m🙂' + '\x1b[0m' }),
    event(1, { text: 'warning\n', stream: 'stderr' }), event(2, { replace_tail: 7, text: '2m🙂\x1b[0m\n' })]);
  assert.deepEqual(issues, []);
  const lines = consoleLines(runs[0].console);
  assert.deepEqual(lines.map(plain), ['🙂', 'warning']);
  assert.equal(lines[0].runs[0].style.foreground, '#b9e778');
  assert(!renderConsole(runs[0].console).includes('\x1b'));
});

test('basic and extended colors plus style resets are safe fixed CSS', () => {
  const lines = consoleLines([chunk('\x1b[91;104;1;3;4;5;7;9mstyled\x1b[39;49;22;23;24;25;27;29mnormal\n'),
    chunk('\x1b[38;5;196;48;2;1;2;3mRGB\x1b[0m')]);
  const styled = lines[0].runs[0].style;
  assert.deepEqual([styled.foreground, styled.background], ['#ffa2ab', '#9ac3ff']);
  for (const effect of ['bold', 'italic', 'underline', 'blink', 'reverse', 'strike']) assert(styled[effect]);
  const normal = lines[0].runs[1].style;
  assert.equal(normal.foreground, null); assert.equal(normal.background, null); assert(!normal.bold);
  assert.equal(lines[1].runs[0].style.foreground, 'rgb(255 0 0)');
  assert.equal(lines[1].runs[0].style.background, 'rgb(1 2 3)');
  const html = renderConsole([chunk('\x1b[7mreverse')]);
  assert(html.includes('color:var(--console-bg);background-color:var(--console-fg)'));
});

test('HTML and OSC hyperlink payloads cannot create markup or executable URLs', () => {
  const chunks = [chunk('\x1b]8;;javascript:alert(1)'), chunk('\x1b\\<img src=x onerror=alert(1)>'),
    chunk('\x1b]0;<script>ignored title</script>\x07\x1b[2K\x1b[31m<script>visible text</script>\x1b[0m')];
  assert.deepEqual(consoleLines(chunks).map(plain), ['<img src=x onerror=alert(1)><script>visible text</script>']);
  const html = renderConsole(chunks);
  assert(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert(!html.includes('<img')); assert(!html.includes('<script>')); assert(!html.includes('javascript:'));
  assert(!html.includes('ignored title')); assert(!html.includes('\x1b'));
});

test('raw split CRLF and unsupported controls preserve visible text without extra lines', () => {
  const lines = consoleLines([chunk('one\r'), chunk('\ntwo\n'), chunk('\x1b[38;2;999;0;0mplain\x07\x1b[?25l')]);
  assert.deepEqual(lines.map(plain), ['one', 'two', 'plain']);
  assert.equal(lines.at(-1).runs[0].style.foreground, null);
});
