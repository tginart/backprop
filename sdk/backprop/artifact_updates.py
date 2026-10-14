"""Mutable names and descriptions over immutable artifact manifests.

Architecture: validate/deduplicate original manifests -> replay alias ownership
and descriptive edits -> return materialized copies. Stored file lists and
content-based versions never change. Alias removal only removes that artifact's
own binding, so a stale editor cannot delete a name reassigned elsewhere.
Main callers: Public Api artifact lookup and Artifact.save.
"""
import copy
import re

from .artifact_events import iter_artifact_events
from .event_order import ordered_events


def validate_aliases(values):
    """Validate property assignment without deduplicating its ordered list.

    Main callers: Artifact.aliases and logging preparation. The pinned Python
    boundary rejects the artifact path separators, while retaining empty names
    and version-shaped strings. Native immutable content versions are resolved
    separately from user-assigned aliases.
    """
    values = [values] if isinstance(values, (str, bytes)) else list(values)
    if any(set("/:").intersection(value) for value in values):
        raise ValueError("Aliases must not contain any of the following characters: '/', ':'")
    return values


def logging_aliases(values):
    """Add latest for every publication, including explicit empty alias lists.

    Main callers: artifact and framework checkpoint publication. Property edits
    can remove latest; publishing a new version assigns it again. Assignment
    preserves list duplicates, whereas published alias ownership is a set.
    """
    values = ["latest"] if values is None else validate_aliases(values)
    try:
        return sorted(set(values) | {"latest"}, key=repr)
    except TypeError as error:
        raise ValueError("`aliases` must be Iterable or None") from error


def validate_tags(values):
    """Validate both assignment and in-place list edits before persistence.

    Main callers: Artifact.tags, save_artifact and public artifact attachment.
    """
    values = [values] if isinstance(values, str) else list(values)
    if any(not isinstance(v, str) for v in values):
        raise TypeError("tags must contain strings")
    if any(re.fullmatch(r"[-\w]+( +[-\w]+)*", value) is None for value in values):
        raise ValueError("Tags require alphanumeric words, hyphens, underscores or spaces")
    return list(dict.fromkeys(values))


def artifact_views(events):
    """Fold updates once per project while retaining each run relationship.

    Main callers: Api._artifact_events. Embedded Table artifacts retain their
    synthetic event IDs; edits apply to their identity just like explicit ones.
    """
    ordered = ordered_events(events)
    from .artifact_collections import collection_catalog
    collections, membership = collection_catalog(ordered)
    originals = list(iter_artifact_events(ordered))
    records, bindings = {}, {}
    operations = ordered_events([*originals, *(e for e in ordered if e["type"] == "artifact_update")])
    for event in operations:
        p = event["payload"]
        key = event.get("entity", ""), event["project"], p["artifact_id"]
        scope = key[:2] + (membership.get(key),)
        if event["type"] == "artifact":
            if key in records: continue
            records[key] = copy.deepcopy(p)
            for alias in p.get("aliases", []): bindings[(*scope, alias)] = key
        else:
            if key not in records:
                raise ValueError("Artifact update precedes its manifest")
            records[key].update(copy.deepcopy(p.get("values", {})))
            for alias in p.get("alias_remove", []):
                if bindings.get((*scope, alias)) == key: bindings.pop((*scope, alias))
            for alias in p.get("alias_add", []): bindings[(*scope, alias)] = key
    for key, payload in records.items():
        payload["aliases"] = sorted(alias[-1] for alias, owner in bindings.items() if owner == key)
        collection = collections[membership[key]]
        payload.update(name=collection['name'], artifact_type=collection['artifact_type'],
                       _collection_id=collection['id'])
    return [{**event, "payload": copy.deepcopy(records[(event.get("entity", ""), event["project"], event["payload"]["artifact_id"])])}
            for event in originals]


def save_artifact(artifact):
    """Publish only changed metadata and alias membership, retaining retry IDs.

    Main callers: Artifact.save on a logged artifact. The baseline advances only
    after acknowledgement; file contents and their version remain immutable.
    """
    from .apis.public.mutations import MutationWriter
    from .artifacts import _thaw, _validate_metadata
    if artifact._store is None:
        raise RuntimeError("Saving a committed artifact requires an authenticated store")
    aliases = validate_aliases(artifact.aliases)
    values = {"metadata": _validate_metadata(artifact.metadata), "description": artifact.description,
              "tags": validate_tags(artifact.tags)}
    before = artifact._saved_descriptive
    changed = {key: value for key, value in values.items() if value != before[key]}
    old = artifact._saved_aliases
    added, removed = [a for a in aliases if a not in old], [a for a in old if a not in aliases]
    if not changed and not added and not removed: return
    if getattr(artifact, "_edit_writer", None) is None:
        artifact._edit_writer = MutationWriter(artifact._store, artifact.entity, artifact.project, artifact._run_id)
    artifact._edit_writer.write("artifact_update", {"artifact_id": artifact.id, "name": artifact._name,
        "values": changed, "alias_add": added, "alias_remove": removed})
    artifact._saved_aliases = list(aliases)
    artifact._saved_descriptive = copy.deepcopy(values)


def enable_edits(artifact):
    """Separate editable descriptions from the finalized manifest's files."""
    from .artifacts import _thaw
    object.__setattr__(artifact, "metadata", _thaw(artifact.metadata))
    artifact._saved_aliases = list(artifact._aliases)
    artifact._saved_descriptive = copy.deepcopy({"metadata": artifact.metadata,
        "description": artifact.description, "tags": artifact.tags})


def add_artifact_aliases(artifact, aliases):
    """Publish explicit alias ownership after a queued artifact is committed.

    Main caller: use_artifact on an already queued draft. Unlike save(), every
    requested alias is reassigned even if the local object has a stale list.
    Immutable files/provenance stay unchanged and retries reuse the edit journal.
    """
    from .apis.public.mutations import MutationWriter
    if artifact._store is None:
        raise RuntimeError("Saving a committed artifact requires an authenticated store")
    if getattr(artifact, "_edit_writer", None) is None:
        artifact._edit_writer = MutationWriter(artifact._store, artifact.entity, artifact.project, artifact._run_id)
    artifact._pending_use_aliases = list(aliases)
    artifact._aliases = list(dict.fromkeys([*artifact._aliases, *aliases]))
    artifact._edit_writer.write("artifact_update", {"artifact_id": artifact.id, "name": artifact._name,
        "values": {}, "alias_add": list(aliases), "alias_remove": []})
    flush_artifact_edits(artifact)


def flush_artifact_edits(artifact):
    """Acknowledge edits and then advance the local alias baseline.

    Main caller: use_artifact, including a retry after a lost acknowledgement.
    The mutation journal owns durable recovery. This pending alias list only
    reconciles the live Python object; a new reader reconstructs it from events.
    Locally requested aliases stay editable while their publication is pending.
    """
    writer = getattr(artifact, '_edit_writer', None)
    if writer is not None:
        writer.flush()
    pending = getattr(artifact, '_pending_use_aliases', None)
    if pending is not None:
        artifact._saved_aliases = list(dict.fromkeys([*artifact._saved_aliases, *pending]))
        del artifact._pending_use_aliases
