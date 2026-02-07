"""Configuration for the pytest test suite."""

import os

# Python 3.14t (freethreaded) workaround: lupa (transitive dep via fakeredis)
# doesn't declare GIL-free safety, causing sporadic RuntimeError in async tests.
os.environ.setdefault("PYTHON_GIL", "0")
