"""Public artifact references after durable publication is acknowledged.

Architecture: check the artifact lifecycle -> expose the portable reference
schema or the immutable Foundry commit identity. Main callers: Artifact's
json_encode() and commit_hash property. Draft references must not look usable;
ordinary run/config logging retains its separate pending-journal representation.
"""

from functools import wraps
from .errors import Error


class WaitTimeoutError(Error, TimeoutError):
    """A publication wait expired; retains the native TimeoutError catch path.

    Main caller: Artifact.wait. The named W&B exception and SDK Error handler
    both work, while existing native deadline callers can still catch TimeoutError.
    """


class ArtifactStatusError(AttributeError):
    """An artifact is in the wrong lifecycle state for a public operation."""


class ArtifactNotLoggedError(ArtifactStatusError):
    """Match the pinned error identity, message and AttributeError metadata."""

    def __init__(self, fullname, obj):
        message = (f"{fullname!r} used prior to logging artifact or while in offline mode. "
                   "Call Artifact.wait() before accessing logged artifact properties.")
        super().__init__(message, name=fullname.rsplit('.', 1)[-1], obj=obj)


def require_logged(artifact, member):
    """Reject unacknowledged drafts without forcing a network write or wait.

    Main callers: public identity readers. is_draft() already observes durable
    acknowledgements, including another process syncing the original journal.
    """
    if artifact.is_draft():
        raise ArtifactNotLoggedError('Artifact.' + member, artifact)


def ensure_logged(method):
    """Apply the pinned state gate before Python binds the method arguments.

    Main callers: Artifact identity methods. The ordering is observable: a
    draft raises ArtifactNotLoggedError even when extra arguments were supplied.
    wraps preserves the declared callable signature for introspection.
    """
    @wraps(method)
    def wrapper(artifact, *args, **kwargs):
        require_logged(artifact, method.__name__)
        return method(artifact, *args, **kwargs)
    return wrapper


def json_encode(artifact):
    """Return W&B's reference schema with Foundry's opaque artifact identity.

    Main caller: Artifact.json_encode. `_version` identifies this JSON schema;
    `version` identifies immutable artifact contents and remains content based.
    No file contents or descriptive metadata are copied into a reference.
    """
    return {'_type': 'artifactVersion', '_version': 'v0', 'id': artifact.id,
            'version': artifact.source_version,
            'sequenceName': artifact.source_name.split(':')[0], 'usedAs': artifact.use_as}


def commit_hash(artifact):
    """Use immutable content identity as Foundry's equivalent of a server commit.

    Main caller: Artifact.commit_hash. Descriptive edits and retries keep this
    identity; different immutable manifests receive different digests.
    """
    return artifact.digest
