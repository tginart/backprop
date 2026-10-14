"""Public export result types; no W&B background-service machinery is exposed."""
from .downloads import DownloadHistoryResult, IncompleteRunHistoryError

__all__ = ["DownloadHistoryResult", "IncompleteRunHistoryError"]
