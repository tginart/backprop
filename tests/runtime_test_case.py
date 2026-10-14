"""Portable context-managed test fixtures for the minimum Python 3.10 runtime."""
import unittest
import os
from contextlib import contextmanager


class ContextTestCase(unittest.TestCase):
    def enterContext(self, manager):
        """Enter once and register exit with unittest's failure-safe cleanup stack."""
        cls = type(manager)
        try:
            enter, exit = cls.__enter__, cls.__exit__
        except AttributeError:
            raise TypeError('Test fixture does not support the context manager protocol') from None
        result = enter(manager)
        self.addCleanup(exit, manager, None, None, None)
        return result


@contextmanager
def change_directory(path):
    """Portable contextlib.chdir equivalent for temporary fixture directories."""
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)
