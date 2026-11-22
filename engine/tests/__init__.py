"""The tests run the engine as the release it is becoming (see `govern._as_release`)."""
import os

from .source_version import SOURCE_VERSION

# The variable holds the exact source version to rewrite; a fixture engine at another beta is left alone.
os.environ.setdefault("GOVERN_TEST_AS_RELEASE", SOURCE_VERSION)
