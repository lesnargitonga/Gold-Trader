"""Pytest conftest to ensure `unittest.mock` attribute exists on `unittest`.

Some tests reference `unittest.mock.patch` via attribute access; ensure the
submodule is imported and attached to the `unittest` package before tests run.
"""
import unittest
import unittest.mock  # noqa: F401

# Explicitly attach the submodule to the unittest package for attribute access
setattr(unittest, "mock", unittest.mock)
