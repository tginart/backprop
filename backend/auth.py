"""
Purpose: Represent a backend authentication result without credential disclosure.
Input: A backend credential selection.
Output: An immutable session result for SDK callers.
"""
from dataclasses import dataclass


@dataclass(frozen=True, repr=False)
class AuthCandidate:
    """A resolved token or explicit local-mode choice; repr never prints tokens."""
    token: str | None
    host: str | None
    source: str = 'explicit'
    mode: str = 'online'
    backend: str = 'foundry'
