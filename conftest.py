"""Shared pytest fixtures.

The bundled test classes are plain objects that reuse ``unittest`` style
``self.assert*`` helpers; provide them automatically without touching the
test files.
"""

import pytest


@pytest.fixture(autouse=True)
def _provide_unittest_assertions(request):
    cls = getattr(request, "cls", None)
    if cls is not None and not hasattr(cls, "assertIsNotNone"):
        def assertIsNotNone(self, obj, msg=None):
            assert obj is not None, msg

        cls.assertIsNotNone = assertIsNotNone
