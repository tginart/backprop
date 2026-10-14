"""Verify fixture cleanup semantics independently of unittest version helpers."""
from contextlib import contextmanager
import unittest
from runtime_test_case import ContextTestCase, change_directory
import os
import tempfile


class ContextFixtureTests(unittest.TestCase):
    def test_successful_enter_registers_cleanup_and_returns_value(self):
        events=[]
        @contextmanager
        def fixture():
            events.append('enter')
            try:yield 42
            finally:events.append('exit')
        case=ContextTestCase()
        self.assertEqual(case.enterContext(fixture()),42)
        self.assertEqual(events,['enter']);case.doCleanups()
        self.assertEqual(events,['enter','exit'])

    def test_failed_enter_registers_no_exit(self):
        events=[]
        class Fixture:
            def __enter__(self):raise RuntimeError('fixture failed')
            def __exit__(self,*args):events.append('exit')
        case=ContextTestCase()
        with self.assertRaisesRegex(RuntimeError,'fixture failed'):case.enterContext(Fixture())
        case.doCleanups();self.assertEqual(events,[])

    def test_test_failure_still_runs_context_cleanup(self):
        events=[]
        @contextmanager
        def fixture():
            try:yield
            finally:events.append('exit')
        class Broken(ContextTestCase):
            def runTest(self):
                self.enterContext(fixture());self.fail('deliberate fixture-owner failure')
        result=unittest.TestResult();Broken().run(result)
        self.assertEqual(len(result.failures),1);self.assertEqual(events,['exit'])


    def test_directory_is_restored_after_failure(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'deliberate'):
                with change_directory(directory):
                    self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath(directory))
                    raise RuntimeError('deliberate')
            self.assertEqual(os.getcwd(), original)
