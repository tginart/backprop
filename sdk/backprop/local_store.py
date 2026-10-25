"""
Purpose: Preserve the legacy module path for the backend implementation.
Input: Existing imports from backprop.
Output: The same backend module, including its patch and class identities.
"""
import sys
from backprop_backend.local import store as _implementation

sys.modules[__name__] = _implementation
