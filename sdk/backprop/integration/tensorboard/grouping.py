"""Consecutive source events -> closed groups with immutable member evidence.

Architecture: source positions and decoded event values -> consecutive exact
step groups -> last-value history plus versioned provenance. Main callers:
reader.sync and Python/browser replay validation. A live EOF leaves its tail
uncommitted; a different step or explicit final scan closes it.

Why this exists: final grouped values alone lose overwritten source values.
Keeping every member allows delayed importers and replay to reconcile prefixes,
new barriers and independent visibility without rewriting physical journals.
Empty leading groups are prefix evidence, rather than empty history rows.
"""
from __future__ import annotations

import hashlib
import math
import re

from .origin import record_identity_from_origin, validate_record_origin

_MEMBER_FIELDS = {"origin", "index", "step", "wall_time", "metrics"}
_NEXT_FIELDS = _MEMBER_FIELDS - {"metrics"}
_GROUP_FIELDS = {"version", "prefix", "members", "next", "revision"}


def source_position(member):
    return member["origin"]["filename"], member["index"]


def _validate_member(member, *, boundary=False):
    """Validate one physical source position without interpreting rich values.

    Main callers: group_revision and replay. Decimal step strings preserve the
    signed-int64 comparison in browser JSON, whose numeric display can round.
    Rich descriptors retain their ordinary encoder and integrity checks.
    """
    fields = _NEXT_FIELDS if boundary else _MEMBER_FIELDS
    if not isinstance(member, dict) or set(member) != fields:
        raise ValueError("TensorBoard group member has unexpected fields")
    record_identity_from_origin(member["origin"])
    index = member["index"]
    if type(index) is not int or not 0 <= index <= 2**53 - 1:
        raise ValueError("TensorBoard event index must be an exact nonnegative JSON integer")
    step = member["step"]
    if not isinstance(step, str) or re.fullmatch(r"0|-?[1-9][0-9]*", step) is None or not -(2**63) <= int(step) < 2**63:
        raise ValueError("TensorBoard source step must be a canonical signed-int64 string")
    wall_time = member["wall_time"]
    if type(wall_time) not in {int, float} or not math.isfinite(wall_time):
        raise ValueError("TensorBoard source wall time must be finite")
    if not boundary and (not isinstance(member["metrics"], dict) or any(not isinstance(key, str) for key in member["metrics"])):
        raise ValueError("TensorBoard member metrics must be a string-keyed map")


def _ordered_evidence(prefix, members, next_event):
    """Check one group's contiguous physical coverage and explicit barrier.

    Main callers: group_revision and validate_group. Replay still must detect
    missing coverage before the first position and across separate observations.
    A boundary has no metrics field because its values have not been observed
    by this group; it must not fabricate an empty decoded source observation.
    """
    if not isinstance(prefix, list) or not isinstance(members, list) or not members:
        raise ValueError("TensorBoard groups need list prefixes and nonempty member lists")
    for member in [*prefix, *members]:
        _validate_member(member)
    if any(member["metrics"] for member in prefix):
        raise ValueError("TensorBoard group prefix must contain only empty metrics")
    if not any(member["metrics"] for member in members):
        raise ValueError("TensorBoard group must contain converted metrics")
    if any(member["step"] != members[0]["step"] for member in members):
        raise ValueError("TensorBoard group members must share one exact source step")
    if prefix and prefix[-1]["step"] == members[0]["step"]:
        raise ValueError("TensorBoard same-step prefix belongs in the member group")
    evidence = [*prefix, *members]
    if next_event is not None:
        _validate_member(next_event, boundary=True)
        if next_event["step"] == members[0]["step"]:
            raise ValueError("TensorBoard next event must prove a different-step barrier")
        evidence.append(next_event)
    namespace = members[0]["origin"]["namespace"]
    for item in evidence:
        if item["origin"]["namespace"] != namespace:
            raise ValueError("TensorBoard group crosses source namespaces")
    for previous, current in zip(evidence, evidence[1:]):
        before, after = source_position(previous), source_position(current)
        if after <= before or (before[0] == after[0] and after[1] != before[1] + 1) or (before[0] != after[0] and after[1] != 0):
            raise ValueError("TensorBoard group evidence has a missing or reordered source position")


def group_revision(prefix, members, next_event):
    """Compute the exact revision hash from source evidence, without paths.

    Main callers: closed_groups and strict Python/browser protocol validation.
    Prefix/member/next separators distinguish a header prefix from a real
    group and a final EOF from a known next-step boundary.
    """
    _ordered_evidence(prefix, members, next_event)
    digest = hashlib.sha256(b"tensorboard-group-v1\0")
    for member in prefix:
        digest.update(bytes.fromhex(record_identity_from_origin(member["origin"])))
    digest.update(b"\0members\0")
    for member in members:
        digest.update(bytes.fromhex(record_identity_from_origin(member["origin"])))
    digest.update(b"\0next\0")
    if next_event is not None:
        digest.update(bytes.fromhex(record_identity_from_origin(next_event["origin"])))
    return digest.hexdigest()


def validate_group(group):
    """Reject malformed or contradictory revisions before trusting members.

    Main callers: durable importer recovery and conflict-preserving replay.
    This validates source structure and its hash; comparing immutable decoded
    content for duplicate positions remains the replay layer's responsibility.
    """
    if not isinstance(group, dict) or set(group) != _GROUP_FIELDS:
        raise ValueError("TensorBoard group has unexpected fields")
    if type(group["version"]) is not int or group["version"] != 1:
        raise ValueError("TensorBoard group version must be 1")
    revision = group_revision(group["prefix"], group["members"], group["next"])
    if group["revision"] != revision:
        raise ValueError("TensorBoard group revision does not match its source evidence")
    return revision


def validate_group_origin(record, origin, group):
    """Keep the released v1 marker as the group's first member anchor.

    Main callers: importer recovery and replay. Old standalone v1 observations
    can be matched to actual group members while retaining their physical events.
    """
    validate_record_origin(record, origin)
    revision = validate_group(group)
    if origin != group["members"][0]["origin"]:
        raise ValueError("TensorBoard group anchor does not match its first member")
    return revision


def closed_groups(observations, *, final):
    """Combine consecutive steps, keeping every overwritten member value.

    Main caller: reader.sync after source decoding in filename/index order.
    A step change closes the preceding group. Zero-metric groups accumulate as
    the next real group's prefix. A live tail is rebuilt from source next pass;
    final EOF explicitly closes it, so restart needs no volatile pending values.
    """
    prefix, pending = [], []

    def close(next_event):
        nonlocal prefix, pending
        if not any(member["metrics"] for member in pending):
            prefix.extend(pending)
            pending = []
            return None
        boundary = {key: value for key, value in next_event.items() if key != "metrics"} if next_event else None
        group = {"version": 1, "prefix": prefix, "members": pending, "next": boundary}
        group["revision"] = group_revision(prefix, pending, boundary)
        prefix, pending = [], []
        return group

    for observation in observations:
        _validate_member(observation)
        if pending and pending[-1]["step"] != observation["step"]:
            group = close(observation)
            if group is not None:
                yield group
        pending.append(observation)
    if final and pending:
        group = close(None)
        if group is not None:
            yield group


def history_metrics(group):
    """Build pinned last-value metrics and timestamp for one closed group.

    Main caller: reader.sync immediately before ordinary Run.log. The rich
    objects are shared between winning values and member evidence, so repeated
    encoding uses the same deterministic source table identity and manifests.
    """
    validate_group(group)
    metrics = {}
    for member in group["members"]:
        metrics.update(member["metrics"])
    anchor = group["members"][0]["origin"]
    last = group["members"][-1]
    return {**metrics, "global_step": int(last["step"]), "_timestamp": last["wall_time"],
            "_tensorboard_record": record_identity_from_origin(anchor),
            "_tensorboard_origin": anchor, "_tensorboard_source": anchor["filename"],
            "_tensorboard_group": group}
