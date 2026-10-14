"""Exercise real background diagnostics without replacing warning emission.

Architecture: isolated subprocess streams -> live Run upload worker -> injected
permanent/transient sink failures -> warnings, stop callback and durable events.
Why subprocesses: native redirect changes process descriptors and warning hooks
are process-global. These checks must not alter the test runner's own console.
Main callers: unittest discovery.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


WORKER = r'''
import json, os, sys, threading, time, warnings
import backprop as w

mode, policy, fatal = sys.argv[1], sys.argv[2], sys.argv[3] == 'fatal'
sink, stopped = w.MemorySink(), threading.Event()
thread_errors = []
threading.excepthook = lambda args: thread_errors.append(str(args.exc_value))
run = w.Run(project='diagnostics', dir=os.environ['TEST_DIR'],
    settings={'x_disable_stats':True,'console':mode, 'quiet':True, 'summary_warnings':0, 'flush_interval':.02,
              'stop_on_fatal_error':True, 'stop_fn':stopped.set},
    _sink=sink, _deployment={})
original_publish, original_stderr = sink.publish, sys.stderr
sys.stdout.write('ordinary Python output\n')
sys.stdout.flush()
if mode == 'redirect': os.write(1, b'ordinary native output\n')

def wait_for(check):
    end = time.monotonic() + 3
    while not check() and time.monotonic() < end: time.sleep(.005)
    assert check(), 'worker did not reach expected state'

if mode == 'redirect':
    wait_for(lambda: 'ordinary native output' in ''.join(record[1] for record in run._console.records))
run.flush()
warnings.simplefilter('error' if policy in {'error', 'broken-stderr'} else 'always', RuntimeWarning)
seen = []
if policy == 'hook':
    def broken_hook(*args, **kwargs): raise RuntimeError('warning hook failed')
    warnings.showwarning = broken_hook
elif policy == 'record':
    warnings.showwarning = lambda message, *args, **kwargs: seen.append(str(message))
elif policy == 'broken-stderr':
    class BrokenStderr:
        def write(self, text): raise OSError('stderr unavailable')
        def flush(self): pass
    sys.stderr = BrokenStderr()

attempts = 0
def publish(events):
    global attempts
    attempts += 1
    if fatal or attempts == 1:
        raise ValueError('permanent rejection') if fatal else ConnectionError('temporary interruption')
    return original_publish(events)
sink.publish = publish
run.log({'x': 1})
if fatal:
    assert stopped.wait(3), 'fatal-stop callback did not execute'
    run._thread.join(timeout=1)
    assert not run._thread.is_alive()
else:
    wait_for(lambda: any(event['type'] == 'history' for event in sink.events))
    assert run._thread.is_alive(), 'recoverable failure killed worker'
    assert not stopped.is_set()
sys.stderr = original_stderr
sink.publish = original_publish
run.finish()
console = ''.join(event['payload']['text'] for event in sink.events if event['type'] == 'console')
assert console.count('ordinary Python output\n') == 1, repr(console)
if mode == 'redirect': assert console.count('ordinary native output\n') == 1, repr(console)
assert 'upload failed' not in console, repr(console)
assert 'warning reporting failed' not in console, repr(console)
assert not thread_errors, thread_errors
assert any('upload failed' in item for item in run._warnings), run._warnings
if policy in {'error', 'hook'}:
    assert any('warning reporting failed' in item for item in run._warnings)
if policy == 'broken-stderr':
    assert any('could not write its diagnostic' in item for item in run._warnings)
if policy == 'record': assert len(seen) == 1, seen
assert len(sink.events) == len({event['event_id'] for event in sink.events})
print(json.dumps({'callback':stopped.is_set(), 'attempts':attempts, 'thread_errors':thread_errors}))
'''


class CaptureDiagnosticTests(unittest.TestCase):
    def worker(self, mode, policy='default', failure='fatal'):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c', WORKER, mode, policy, failure],
                                    env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'sdk'),
                                         'TEST_DIR': directory}, capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr + '\n' + result.stdout)
        self.assertNotIn('Exception in thread', result.stderr)
        outcome = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(outcome['callback'], failure == 'fatal')
        return result, outcome

    def test_real_default_warnings_do_not_reenter_capture_or_prevent_fatal_stop(self):
        for mode in ('auto', 'wrap', 'wrap_emu', 'redirect'):
            with self.subTest(mode=mode):
                result, outcome = self.worker(mode)
                self.assertIn('RuntimeWarning: Backprop upload failed', result.stderr)
                self.assertEqual(outcome['attempts'], 1)

    def test_warnings_as_errors_do_not_prevent_fatal_stop_or_transient_recovery(self):
        for mode in ('wrap', 'redirect'):
            for failure in ('fatal', 'transient'):
                with self.subTest(mode=mode, failure=failure):
                    result, outcome = self.worker(mode, 'error', failure)
                    self.assertIn('warning reporting failed (RuntimeWarning:', result.stderr)
                    if failure == 'transient': self.assertEqual(outcome['attempts'], 2)

    def test_broken_warning_hook_is_reported_without_losing_stop_callback(self):
        for mode in ('wrap', 'redirect'):
            with self.subTest(mode=mode):
                result, _ = self.worker(mode, 'hook')
                self.assertIn('warning hook failed', result.stderr)

    def test_warning_recording_hook_keeps_its_normal_behavior(self):
        for mode in ('wrap', 'redirect'):
            with self.subTest(mode=mode):
                result, _ = self.worker(mode, 'record')
                self.assertEqual(result.stderr, '')

    def test_unavailable_stderr_retains_diagnostic_and_still_stops(self):
        self.worker('wrap', 'broken-stderr')


if __name__ == '__main__': unittest.main()
