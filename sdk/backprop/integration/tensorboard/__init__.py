"""TensorBoard writers -> discovered directories -> portable history and files.

Architecture: reversible hooks cover TensorFlow 1/2, TensorBoard, PyTorch and
TensorBoardX writers. Registered runs share automatically installed hooks until
the last run finishes. Each run retains its announced sources independently of
the current hook session, so unpatching cannot discard a live stream's tail.
The reader reconstructs plugin metadata and durable
source identities before journaling each new summary through ordinary Run.log.
Main callers: init(sync_tensorboard=True), patch/unpatch and Api.sync_tensorboard.
Why this exists: framework writers stay in charge of their event files; Backprop
only observes them and reuses its existing durable history/media transport.
"""
from __future__ import annotations

from functools import wraps
import importlib
import importlib.util
import os
import posixpath
from pathlib import Path
import re
import socket
import threading
import time
import warnings
import weakref
from urllib.parse import urlsplit, urlunsplit

from .codec import values

__all__ = ["patch", "unpatch"]
_patches = []
_directories = set()
_directory_options = {}
_inferred_roots = {}
_registered_runs = weakref.WeakSet()
_run_sources = weakref.WeakKeyDictionary()
_automatic_patch = False
_save = True
_root_logdir = ""
_lock = threading.RLock()
_directory_changed = threading.Condition(_lock)


_MODULES = (
    ("tensorflow.python.ops.gen_summary_ops", "create_summary_file_writer"),
    ("tensorflow.python.summary.writer.writer", "EventFileWriter"),
    ("tensorboard.summary.writer.event_file_writer", "EventFileWriter"),
    ("torch.utils.tensorboard.writer", "EventFileWriter"),
    ("tensorboardX.writer", "EventFileWriter"),
)


def _path(value):
    """Turn a TensorFlow string tensor or filesystem path into one source path."""
    if hasattr(value, "numpy"):
        value = value.numpy()
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    value = os.fspath(value)
    if "://" in value:
        from .cloud import canonical_path
        return canonical_path(value)
    return str(Path(value).resolve())


def _within(directory, root):
    if "://" in directory or "://" in root:
        return directory == root or directory.startswith(root.rstrip("/") + "/")
    return Path(directory).is_relative_to(root)


def _discovery_root(directory):
    """Resolve a hook's effective root without publishing a source or warning.

    Main callers: preflight and discovery while holding _lock. Generated writer
    paths override the configured root; unrelated paths drop it as pinned does.
    Why this exists: policy validation must happen before a writer allocates
    resources, while successful discovery remains responsible for warnings.
    """
    root = _root_logdir
    generated = re.search(r"-\d+_" + re.escape(socket.gethostname()), directory)
    if generated:
        return directory[:generated.end()], False
    if root and not _within(directory, root):
        return "", True
    return root, False


def _reject_changed_policy(directory, root):
    """Reject an extra policy for the same live durable source identity.

    Main callers: writer preflight and _discover, while holding _lock. Pinned
    re-patching can add another watcher; native currently retains one namespace
    and save policy per run/source. Why this exists: keeping the old policy
    silently would ignore the caller's request, so the unsupported transition
    must fail before changing discovery, options or ownership.
    """
    for run in _registered_runs:
        watched = _run_sources.get(run, {}).get(directory)
        if watched is not None and _receives_discoveries(run):
            previous = watched["options"]
            same_root = previous["root"] == root or bool(root) and previous.get("resolved_root") == root
            if not same_root or previous["save"] != _save:
                from ...errors import UnsupportedFeatureError
                raise UnsupportedFeatureError("TensorBoard cannot re-announce the same live source "
                    "with a different root_logdir or save policy; finish its owner before changing policy")


def _preflight_source(logdir):
    """Check existing live policies before a constructor starts writer workers.

    Main callers: class and TF2 hooks. First discovery still follows successful
    writer creation. Why this exists: rejecting after allocation would leave an
    unusable live writer because its constructor never returned to the caller.
    Discovery rechecks policy after construction before publishing its source.
    """
    with _lock:
        if any(_run_sources.values()):
            directory = _path(logdir)
            root, _ = _discovery_root(directory)
            _reject_changed_policy(directory, root)


def _discover(logdir):
    """Remember the writer directory and the root used to name its metrics.

    Main callers: all writer hooks. Explicit roots separate train/validation
    metrics; unrelated writers drop the root with a visible warning, as upstream
    does. Framework-generated hostname directories define their own root.
    """
    directory = _path(logdir)
    with _lock:
        root, outside = _discovery_root(directory)
        _reject_changed_policy(directory, root)
        if outside:
            warnings.warn("Found log directory outside of given root_logdir, "
                          f"dropping given root_logdir for event file in {directory}",
                          RuntimeWarning, stacklevel=3)
        if _directories and directory not in _directories and not _root_logdir:
            warnings.warn("When using several event log directories, call "
                          "tensorboard.patch(root_logdir='...') before init",
                          RuntimeWarning, stacklevel=3)
        _directories.add(directory)
        _directory_options.setdefault(directory, {"root": root, "save": _save, "discovered_at": time.monotonic()})
        for run in _registered_runs:
            _remember_sources(run, directory)
        _directory_changed.notify_all()
        installed = bool(_patches)
    if installed:
        _adopt_current_run()


def _adopt_current_run():
    """Attach a live global run when manual patching starts after init.

    Main caller: _discover after releasing the directory lock. The pinned
    writer hook notifies wandb.run directly, even if its initial sync option was
    false. Register that same current handle without changing public settings;
    preexisting create_new handles have no global binding and are not adopted.
    Why this exists: Run polling depends on hook ownership rather than its
    original init flag. Adoption defines metrics through normal Run methods,
    outside the discovery lock to avoid reversing Run/module lock order.
    """
    import backprop
    current = backprop.run
    if current is not None and not current.disabled and not current._closed and not current._tensorboard_registered:
        from ...init_integrations import adopt_tensorboard
        adopt_tensorboard(current)


def _optional_module(name):
    """Import an installed framework, and expose failures inside that framework.

    Main caller: patch. Missing optional top-level packages are skipped, while a
    broken installed package is an error so a partially patched process cannot
    silently lose one writer family.
    """
    package = name.split(".", 1)[0]
    if importlib.util.find_spec(package) is None:
        return None
    return importlib.import_module(name)


def _hook_class(cls):
    original = cls.__init__

    @wraps(original)
    def initialize(self, logdir, *args, **kwargs):
        _preflight_source(logdir)
        original(self, logdir, *args, **kwargs)
        _discover(logdir)

    cls.__init__ = initialize
    _patches.append((cls, "__init__", original))


def _hook_tensorflow2(module):
    """Observe TF2's native writer constructor, which bypasses EventFileWriter.

    Main caller: patch. TensorFlow passes logdir as a Tensor in eager and graph
    writer paths. The original constructor remains responsible for all output.
    """
    original = module.create_summary_file_writer

    @wraps(original)
    def create_summary_file_writer(*args, **kwargs):
        logdir = kwargs.get("logdir", args[1] if len(args) > 1 else None)
        if logdir is not None:
            _preflight_source(logdir)
        result = original(*args, **kwargs)
        if logdir is None:
            raise TypeError("TensorFlow create_summary_file_writer requires logdir")
        _discover(logdir)
        return result

    module.create_summary_file_writer = create_summary_file_writer
    _patches.append((module, "create_summary_file_writer", original))


def patch(save: bool = True, tensorboard_x: bool | None = None,
          pytorch: bool | None = None, root_logdir: str = "") -> None:
    """Install all available writer hooks using the selected upstream switches.

    Main callers: explicit tensorboard.patch and register_run. pytorch and
    tensorboard_x select a writer family by disabling the TF2 native hook; other
    installed Python writers are still observed, matching W&B 0.30.0. A failure
    restores every hook already installed, so retrying is safe.
    """
    global _save, _root_logdir
    with _lock:
        if _patches:
            raise ValueError("Tensorboard already patched. Call `wandb.tensorboard.unpatch()` first; "
                             "remove `sync_tensorboard=True` from `wandb.init`; "
                             "or only call `wandb.tensorboard.patch` once.")
        if importlib.util.find_spec("tensorboard") is None:
            from ...errors import Error
            raise Error("Please install tensorboard package")
        resolved_root = _path(root_logdir) if root_logdir else ""
        _save = bool(save)
        _root_logdir = resolved_root
        try:
            hooked_classes = set()
            for name, attribute in _MODULES:
                if attribute == "create_summary_file_writer" and (pytorch or tensorboard_x):
                    continue
                module = _optional_module(name)
                if module is None:
                    continue
                target = getattr(module, attribute)
                if attribute == "create_summary_file_writer":
                    _hook_tensorflow2(module)
                elif target not in hooked_classes:
                    _hook_class(target)
                    hooked_classes.add(target)
            if not _patches:
                raise RuntimeError("Unsupported tensorboard configuration")
        except Exception:
            unpatch()
            raise


def unpatch() -> None:
    """Restore constructors while leaving announced run streams alive.

    Main callers: explicit unpatch and release_run. Repeated calls are harmless;
    registered runs remain registered so a later manual patch can serve them.
    Why this exists: pinned unpatch removes client hooks, while its already
    notified core streams keep reading until run finish. Retain the old session
    objects for those runs; a new patch starts fresh discovery and inference.
    """
    global _automatic_patch, _save, _root_logdir, _directories, _directory_options, _inferred_roots
    with _lock:
        for target, attribute, original in reversed(_patches):
            setattr(target, attribute, original)
        _patches.clear()
        _directories = set()
        _directory_options = {}
        _inferred_roots = {}
        _directory_changed.notify_all()
        _automatic_patch = False
        _save = True
        _root_logdir = ""


def register_run(run):
    """Keep automatic hooks alive until all syncing runs have finished.

    Main caller: Run initialization. Explicitly installed hooks belong to the
    caller and survive run finish; automatic hooks belong to registered runs.
    """
    global _automatic_patch
    with _lock:
        if not _patches:
            patch()
            _automatic_patch = True
        _registered_runs.add(run)
        _remember_sources(run)


def _receives_discoveries(run):
    """Apply the existing global/create_new rule to new writer announcements.

    Main callers: _remember_sources and directories. Previously announced
    streams already belong to their run, even if its global binding changes.
    Direct Run handles retain their explicit native polling behavior.
    """
    if getattr(run, "_tensorboard_nonglobal_init", False):
        import backprop
        return backprop.run is run
    return True


def _remember_sources(run, directory=None):
    """Bind announced paths and their original policy to one weak run owner.

    Main callers: register_run and _discover, while holding _lock. Keep session
    directory/inference objects by reference until unpatch replaces them; this
    preserves train/validation roots without exposing retired paths to new runs.
    First ownership wins for repeated discovery of one canonical source.
    """
    if not _receives_discoveries(run):
        return
    watched = _run_sources.setdefault(run, {})
    selected = _directory_options if directory is None else [directory]
    for source in selected:
        watched.setdefault(source, {"options": _directory_options[source],
                                   "directories": _directories,
                                   "inferred_roots": _inferred_roots})


def is_patched():
    """Report installed hook ownership for Run initialization preflight.

    Main caller: parent-owned init integration wiring. An explicit patch also
    requests writer interception even when the new run has no sync flag.
    """
    with _lock:
        return bool(_patches)


def release_run(run):
    """Release one run's hook ownership without disrupting another live run.

    Main caller: Run.finish. This is distinct from unpatch, which is the user's
    explicit request to remove the hooks immediately regardless of live runs.
    """
    with _lock:
        _registered_runs.discard(run)
        _run_sources.pop(run, None)
        if _automatic_patch and not _registered_runs:
            unpatch()


def directories(run=None):
    """Route automatic discoveries away from nonglobal public create_new runs.

    Main caller: Run polling/finalization. Pinned writer notification targets
    wandb.run; create_new retains its hook lease but deliberately does not bind
    that global. Direct Run construction and explicit sync have separate native
    ownership and keep their existing behavior. A manually bound handle can
    receive discoveries even if it originated as create_new.
    """
    with _lock:
        if run is None:
            return sorted(_directories)
        # A create_new handle can become explicitly global after a writer was
        # discovered. Polling transfers those current streams to its durable
        # run ownership before unpatch can retire this session. Closed or
        # unregistered inspection handles must not resurrect released sources.
        if run in _registered_runs and _receives_discoveries(run):
            _remember_sources(run)
        watched = set(_run_sources.get(run, {}))
        if _receives_discoveries(run):
            watched.update(_directories)
        return sorted(watched)


def source_options(directory, run=None):
    """Choose the stable metric root and save policy for one import operation.

    Main caller: sync. An explicit API directory is its own root. When patch
    observes several directories without a root, their common parent supplies
    train/validation namespace inference. Freeze each source after its first
    import, after upstream's ten-second inference window if necessary. A live
    run keeps the policy from its announced stream after explicit unpatch;
    ordinary explicit imports use only the current patch session.
    """
    directory = _path(directory)
    with _lock:
        watched = _run_sources.get(run, {}).get(directory) if run is not None else None
        options = watched["options"] if watched is not None else _directory_options.get(directory)
        sources = watched["directories"] if watched is not None else _directories
        inferred_roots = watched["inferred_roots"] if watched is not None else _inferred_roots
        if options is None:
            if _root_logdir and _within(directory, _root_logdir):
                return _root_logdir, _save
            return directory, True
        if "resolved_root" in options:
            return options["resolved_root"], options["save"]
        root = options["root"]
        if not root:
            # Upstream gives concurrent train/validation writers ten seconds to
            # reveal a common root, then fixes the stream's namespace. Waiting
            # releases _lock so another constructor can notify this reader.
            def related_peers():
                current = urlsplit(directory) if "://" in directory else None
                return [item for item in sources
                        if ("://" in item) == ("://" in directory)
                        and (current is None or (urlsplit(item).scheme, urlsplit(item).netloc)
                             == (current.scheme, current.netloc))]
            peers = related_peers()
            deadline = options["discovered_at"] + 10.0
            while len(peers) < 2 and sources is _directories and directory in _directory_options:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _directory_changed.wait(timeout=remaining)
                peers = related_peers()
            if not peers:
                return directory, options["save"]
            location = urlsplit(directory) if "://" in directory else None
            cluster = (location.scheme, location.netloc) if location else ("local", "")
            inferred = inferred_roots.get(cluster)
            if inferred is not None:
                root = inferred
            elif len(peers) > 1 and "://" in directory:
                current = urlsplit(directory)
                related = [urlsplit(item).path for item in peers
                           if (urlsplit(item).scheme, urlsplit(item).netloc) == (current.scheme, current.netloc)]
                root = urlunsplit((current.scheme, current.netloc, posixpath.commonpath(related), "", ""))
            elif inferred is None:
                root = os.path.commonpath(peers) if len(peers) > 1 else directory
            if len(peers) > 1 and inferred is None:
                inferred_roots[cluster] = root
            if not _within(directory, root):
                warnings.warn("Found log directory outside of inferred TensorBoard root, "
                              f"dropping inferred root for event file in {directory}",
                              RuntimeWarning, stacklevel=2)
                root = directory
        options["resolved_root"] = root
        return root, options["save"]


def sync(run, root_dir, *, final=None):
    """Import newly visible summaries with durable identities and plugin state.

    Main callers: Run.flush and Api.sync_tensorboard. The implementation lives
    separately from patching so offline import does not load ML frameworks.
    """
    from .reader import sync as read
    return read(run, root_dir, final=final)


def live_filter(run, directory):
    """Restrict live polling to files from this machine and run start time.

    Main caller: reader.sync. Historical API imports include every writer; live
    integrations observe only current training, matching upstream TFEventsFileFilter.
    """
    with _lock:
        return (int(run.start_time), socket.gethostname()) if run in _registered_runs else None
