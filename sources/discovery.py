"""Find sources that live on this machine and are not in the repo.

The privacy split says the repo is the engine and `profile.yaml` is the person.
That works for targeting rules but not for code: a board you do not want to
publish — a private feed, a company board, an experiment, a paid API whose
existence you would rather not advertise — had nowhere to live, because
`main.py` held a hardcoded list of three.

So `sources/local/` is gitignored and loaded if it exists. Drop a module in it
that satisfies the `Source` protocol and it joins the run. Nothing else changes:
`profile.require_boards()` still demands a `boards.<name>` block, and that block
lives in the gitignored `profile.yaml`, so a local source's policy is private
for free.

Discovery is by file rather than by registry on purpose. A registry is one more
thing to edit and forget, and the failure mode of forgetting is a source that
silently never runs.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
from pathlib import Path

LOCAL_DIR = Path(__file__).resolve().parent / "local"


def looks_like_source(obj) -> bool:
    """Does this class satisfy the parts of `Source` that `main.py` actually uses?

    Structural, not `isinstance`. `Source` is a Protocol and the existing three
    sources do not inherit from it, so requiring a base class would mean a local
    source had to be written differently from the ones in the repo.
    """
    return (
        inspect.isclass(obj)
        and not inspect.isabstract(obj)
        and isinstance(getattr(obj, "name", None), str)
        and callable(getattr(obj, "fetch", None))
    )


def _load_module(path: Path):
    """Import one file without requiring `sources/local/` to be a package.

    spec_from_file_location rather than a package import, so the directory needs
    no `__init__.py`: dropping a single .py file in is the whole install step.
    """
    spec = importlib.util.spec_from_file_location(f"sources._local_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover(directory=None) -> tuple[list[tuple], list[str]]:
    """Instantiate every local source. Returns (sources, errors).

    The tuple shape matches the hardcoded list in `main.py` —
    `(instance, max_pages, setting_name)` — so a local source is not a second
    class of thing the fetch loop has to special-case.

    Errors are RETURNED, not raised and not swallowed. Raising would let a typo
    in one private file kill a run that three working boards would have
    survived; swallowing would let a private source quietly stop running
    forever, which is the failure this repo keeps warning about — it looks
    exactly like a quiet market. The caller logs them as warnings and carries on,
    the same contract the per-source try/except in `main.py` already uses.
    """
    directory = Path(directory) if directory else LOCAL_DIR
    if not directory.is_dir():
        return [], []

    found: list[tuple] = []
    errors: list[str] = []
    seen_names: dict[str, str] = {}

    # Sorted, so two machines with the same files fetch in the same order and a
    # run is reproducible in the one way this stage can be.
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            module = _load_module(path)
        except Exception as exc:  # noqa: BLE001 - one bad file must not end the run
            errors.append(f"local source {path.name} failed to load: {exc}")
            continue

        for _, obj in inspect.getmembers(module, looks_like_source):
            # Only classes defined in this file. Without this, importing
            # Himalayas for reference inside a local module would register a
            # second copy of it and fetch that board twice.
            if getattr(obj, "__module__", None) != module.__name__:
                continue
            try:
                instance = obj()
            except Exception as exc:  # noqa: BLE001 - same reasoning as above
                errors.append(f"local source {path.name}: {obj.__name__}() failed: {exc}")
                continue

            if instance.name in seen_names:
                errors.append(
                    f"local source {path.name} declares name {instance.name!r}, "
                    f"already taken by {seen_names[instance.name]} — skipped, because a "
                    "duplicate name would collide in the board policy lookup"
                )
                continue
            seen_names[instance.name] = path.name

            # A local source declares its own cap as an attribute rather than a
            # config constant: config.py is committed, and a private board's
            # tuning does not belong in a public file.
            found.append((instance, int(getattr(instance, "max_pages", 0) or 0),
                          f"max_pages on {obj.__name__}"))

    return found, errors


def names_in_repo() -> set[str]:
    """Source names that ship with the repo, so a local one cannot shadow them."""
    return {"himalayas", "onlinejobs", "indeed"}


def reject_shadowing(found: list[tuple]) -> tuple[list[tuple], list[str]]:
    """Drop any local source claiming a committed source's name.

    Shadowing would be silent and awful: `--source himalayas` would run
    something else, and the board policy for himalayas would be applied to a
    feed it was never measured against.
    """
    builtin = names_in_repo()
    kept, errors = [], []
    for entry in found:
        if entry[0].name in builtin:
            errors.append(
                f"local source {entry[0].name!r} shadows a source in the repo — skipped"
            )
            continue
        kept.append(entry)
    return kept, errors


def env_disabled() -> bool:
    """`JOBLIST_NO_LOCAL=1` runs the repo's sources only.

    Useful for reproducing a problem exactly as someone with a clean clone would
    see it, without moving the directory out of the way.
    """
    return os.environ.get("JOBLIST_NO_LOCAL", "").strip().lower() in {"1", "true", "yes"}
