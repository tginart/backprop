"""Optional training adapters. Import a framework module only when using it."""

import sys as _sys
from backprop.unsupported import module_getattr as _guard_module
_guard_module(_sys.modules[__name__], 'wandb.integration')
