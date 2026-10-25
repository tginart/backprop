"""
Purpose: Expose the backend package mapping in a source-only checkout.
Input: The checkout SDK path on sys.path.
Output: Backend imports from the shared backend source directory.
Note: Wheels map this package directly to backend and omit this bridge.
"""
from pathlib import Path


__path__ = [str(Path(__file__).resolve().parents[2] / "backend")]
