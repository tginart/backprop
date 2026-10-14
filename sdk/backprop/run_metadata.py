"""Reproducibility metadata with explicit code and Git privacy controls.

Architecture: Settings selects machine/program/Git fields -> read-only local
inspection -> one JSON run file and metadata event. Git reads are bounded and
disabled entirely when requested. Credentials embedded in HTTP remote URLs are
removed before anything is persisted.
Main callers: Run initialization; no background process or network is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
from urllib.parse import urlsplit, urlunsplit


def _git(root, *arguments, raw=False):
    try:
        result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True,
                                text=True, timeout=2, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return (result.stdout if raw else result.stdout.strip()) if result.returncode == 0 else None


def upstream_reference(root, disable_fork_point):
    if disable_fork_point:
        return _git(root, "rev-parse", "@{upstream}")
    if not _git(root, "symbolic-ref", "HEAD"):
        return None
    current = _git(root, "rev-parse", "--abbrev-ref", "@{upstream}")
    references = [current] if current else (_git(root, "for-each-ref", "--format=%(upstream:short)", "refs/heads/") or "").splitlines()
    references = sorted({value for value in references if value})
    return _git(root, "merge-base", "HEAD", *references) if references else None


def collect_metadata(settings):
    """Collect reproducibility fields without copying source files implicitly."""
    result = {"os": platform.platform(), "python": platform.python_version(),
              "host": settings.host, "cpu_count": os.cpu_count()}
    for name in ("docker", "email", "username", "notebook_name"):
        value = getattr(settings, name)
        if value is not None:
            result[name] = value
    if not settings.disable_code:
        notebook = getattr(settings, "_notebook_path", None)
        program = notebook or settings.program
        absolute = settings.program_abspath or notebook or (str(Path(program).resolve()) if program and Path(program).is_file() else None)
        relative = settings.program_relpath
        if notebook and relative is None:
            relative = str(Path(notebook).relative_to(Path(settings._notebook_root)))
        if notebook and not settings.notebook_name:
            result["notebook_name"] = notebook
        if absolute and relative is None:
            try:
                relative = str(Path(absolute).relative_to(Path(settings.git_root or os.getcwd()).resolve()))
            except ValueError:
                relative = Path(absolute).name
        settings.program_abspath, settings.program_relpath = absolute, relative
        result.update(program=program, codePath=relative, codePathLocal=absolute)
    if not settings.disable_code and getattr(settings, '_notebook_provider', None) and not getattr(settings, '_notebook_path', None):
        name = settings._notebook_export_name or settings._notebook_provider + '.ipynb'
        result.update(program=name, codePath=name, codePathLocal=None)
        if not settings.notebook_name:
            result['notebook_name'] = name
        if settings.colab_url:
            result['colab'] = settings.colab_url
    if not settings.disable_git:
        settings.infer_git_root()
        root = settings.git_root
        commit = settings.git_commit or (_git(root, "rev-parse", "HEAD") if root else None)
        remote = settings.git_remote_url or (_git(root, "remote", "get-url", settings.git_remote) if root else None)
        if remote and "://" in remote:
            parsed = urlsplit(remote)
            remote = urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
        if commit or remote:
            result["git"] = {"commit": commit, "remote": remote}
        if root and not settings.disable_git_fork_point:
            fork_point = upstream_reference(root, False)
            if fork_point:
                result.setdefault("git", {})["forkPoint"] = fork_point
    return {key: value for key, value in result.items() if value is not None}


def save_metadata(run):
    """Save metadata as a normal run file, honoring file upload exclusions."""
    metadata = collect_metadata(run._settings)
    metadata["code_labels"] = dict(run._code_labels)
    path = Path(run.dir) / "wandb-metadata.json"
    path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    run._files.save(path, base_path=run.dir, policy="now", glob=False)
    run._emit("run_metadata", {"values": {"environment": metadata, "code_labels": dict(run._code_labels)}})
    save_git_patches(run)


def save_git_patches(run):
    """Capture tracked worktree/staged changes only when code saving is enabled.

    Main callers: init metadata setup. Like pinned gitops.SavePatch, diff.patch
    compares tracked files against HEAD; an upstream patch is separate. File
    upload exclusions are applied by the ordinary RunFiles boundary.
    """
    settings = run._settings
    if not settings.save_code or settings.disable_code or settings.disable_git:
        return
    settings.infer_git_root()
    root = settings.git_root
    if not root or not _git(root, "rev-parse", "--verify", "HEAD"):
        return
    references = [("HEAD", "diff.patch")]
    upstream = upstream_reference(root, settings.disable_git_fork_point)
    if upstream:
        references.append((upstream, f"diff_{upstream}.patch"))
    for reference, name in references:
        patch = _git(root, "diff", "--no-ext-diff", reference, "--submodule=diff", raw=True)
        if patch is None:
            run._warn(f"Git diff could not be captured for {reference}")
            continue
        path = Path(run.dir) / name
        path.write_text(patch, encoding="utf-8")
        run._files.save(path, base_path=run.dir, policy="now", glob=False)
