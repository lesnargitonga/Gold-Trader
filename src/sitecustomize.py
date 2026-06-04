"""Test shim: ensure `unittest.mock` is available as an attribute on
the `unittest` package for tests that reference `unittest.mock.patch`.

Placed in `src/` so it's on `sys.path` during test runs.
"""
import unittest
import unittest.mock  # noqa: F401
