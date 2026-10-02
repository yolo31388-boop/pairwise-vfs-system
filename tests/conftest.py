"""Pytest fixtures/helpers for the VFS test suite.

The test classes are plain classes (not unittest.TestCase) but use
``self.assertIsNotNone``; provide compatible assertion helpers here so
the test files themselves stay untouched.
"""


def _assert_is_not_none(self, value, msg=None):
    assert value is not None, msg or f"expected non-None value, got {value!r}"


def _assert_is_none(self, value, msg=None):
    assert value is None, msg or f"expected None, got {value!r}"


def pytest_collection_modifyitems(items):
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is None:
            continue
        if not hasattr(cls, "assertIsNotNone"):
            cls.assertIsNotNone = _assert_is_not_none
        if not hasattr(cls, "assertIsNone"):
            cls.assertIsNone = _assert_is_none
