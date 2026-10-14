# Adapted from W&B 0.30.0 (MIT); see THIRD_PARTY_NOTICES.md.
# Architecture: framework callbacks -> Backprop public logging -> Foundry.
# Main callers: the corresponding optional training framework.
"""W&B callback for CatBoost."""

from .catboost import WandbCallback, log_summary

__all__ = ["log_summary", "WandbCallback"]
