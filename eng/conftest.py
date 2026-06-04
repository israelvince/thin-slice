import os
import sys

# Add eng/ to sys.path so `from ai import ...` works
ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Add the parent of eng/ so `from eng import runner` works in tests
PARENT = os.path.dirname(ROOT)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)
