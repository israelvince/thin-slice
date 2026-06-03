import os
import sys

# When pytest runs from the project root, ensure the `eng` folder is on sys.path
# so `from ai...` imports resolve to the local package under eng/ai.
ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
