"""Make the project root importable so tests can `import config` etc.

pytest's default (prepend) import mode inserts the *test file's* directory on
sys.path; this ensures the project root is there too, regardless of where
tests live.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
