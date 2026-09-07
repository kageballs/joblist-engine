"""Loading boards that live only on this machine.

The rule these pin down: a private source joins the run exactly like a
committed one, and every way it can go wrong is REPORTED rather than raised or
swallowed. Both of those failure modes are worse than they look — a raise lets
one typo in a private file kill a run three working boards would have survived,
and a swallow lets a source stop running forever while the output still looks
like a quiet market.

The loader is public; what anyone loads with it is not. So these tests build
their own throwaway modules in tmp_path and never touch sources/local/.
"""

from __future__ import annotations

import textwrap

import pytest

from sources import discovery

SOURCE = textwrap.dedent('''
    class {cls}:
        name = "{name}"
        {extra}
        def fetch(self, since):
            return iter(())
''')


def write_source(directory, filename, cls="Board", name="board", extra=""):
    path = directory / filename
    path.write_text(SOURCE.format(cls=cls, name=name, extra=extra), encoding="utf-8")
    return path


def test_missing_directory_is_not_an_error(tmp_path):
    """A clean clone has no sources/local/, and that is the normal case."""
    found, errors = discovery.discover(tmp_path / "nope")
    assert found == []
    assert errors == []


def test_discovers_a_source_in_the_main_py_tuple_shape(tmp_path):
    write_source(tmp_path, "mine.py", cls="Mine", name="mine")
    found, errors = discovery.discover(tmp_path)

    assert errors == []
    assert len(found) == 1
    instance, max_pages, setting = found[0]
    assert instance.name == "mine"
    assert max_pages == 0
    # main.py prints this in the page-cap warning, so it has to name something
    # the operator can actually go and change.
    assert "Mine" in setting


def test_max_pages_is_read_off_the_class(tmp_path):
    """A private board's cap belongs on the class, not in the committed config.py."""
    write_source(tmp_path, "mine.py", extra="max_pages = 7")
    (found, _) = discovery.discover(tmp_path)
    assert found[0][1] == 7


def test_underscore_and_dunder_files_are_skipped(tmp_path):
    write_source(tmp_path, "_helper.py", cls="Helper", name="helper")
    write_source(tmp_path, "__init__.py", cls="Init", name="init")
    write_source(tmp_path, "real.py", cls="Real", name="real")

    found, errors = discovery.discover(tmp_path)
    assert [f[0].name for f in found] == ["real"]
    assert errors == []


def test_a_broken_file_is_reported_and_the_others_still_load(tmp_path):
    """The whole point: one bad private file must not end the run."""
    (tmp_path / "broken.py").write_text("this is not python(", encoding="utf-8")
    write_source(tmp_path, "good.py", cls="Good", name="good")

    found, errors = discovery.discover(tmp_path)
    assert [f[0].name for f in found] == ["good"]
    assert len(errors) == 1
    assert "broken.py" in errors[0]


def test_a_constructor_that_raises_is_reported_not_raised(tmp_path):
    (tmp_path / "boom.py").write_text(textwrap.dedent('''
        class Boom:
            name = "boom"
            def __init__(self):
                raise RuntimeError("no api key")
            def fetch(self, since):
                return iter(())
    '''), encoding="utf-8")

    found, errors = discovery.discover(tmp_path)
    assert found == []
    assert len(errors) == 1
    assert "no api key" in errors[0]


def test_classes_that_are_not_sources_are_ignored(tmp_path):
    (tmp_path / "mixed.py").write_text(textwrap.dedent('''
        class Helper:
            """No name, no fetch."""
        class Config:
            name = "config"          # a name but no fetch
        class Real:
            name = "real"
            def fetch(self, since):
                return iter(())
    '''), encoding="utf-8")

    found, errors = discovery.discover(tmp_path)
    assert [f[0].name for f in found] == ["real"]
    assert errors == []


def test_an_imported_source_is_not_registered_twice(tmp_path):
    """Importing a repo source for reference must not re-register that board.

    Without the __module__ check this fetches himalayas twice in one run.
    """
    (tmp_path / "borrows.py").write_text(textwrap.dedent('''
        from sources.himalayas import Himalayas  # noqa: F401
        class Mine:
            name = "mine"
            def fetch(self, since):
                return iter(())
    '''), encoding="utf-8")

    found, errors = discovery.discover(tmp_path)
    assert [f[0].name for f in found] == ["mine"]
    assert errors == []


def test_duplicate_names_are_refused(tmp_path):
    """Two boards with one name would collide in the board-policy lookup."""
    write_source(tmp_path, "a.py", cls="A", name="same")
    write_source(tmp_path, "b.py", cls="B", name="same")

    found, errors = discovery.discover(tmp_path)
    assert len(found) == 1
    assert len(errors) == 1
    assert "already taken" in errors[0]


def test_discovery_order_is_deterministic(tmp_path):
    for letter in "cab":
        write_source(tmp_path, f"{letter}.py", cls=letter.upper(), name=letter)
    found, _ = discovery.discover(tmp_path)
    assert [f[0].name for f in found] == ["a", "b", "c"]


@pytest.mark.parametrize("shadow", ["himalayas", "onlinejobs", "indeed"])
def test_a_local_source_cannot_shadow_a_committed_one(tmp_path, shadow):
    """Silent shadowing would apply a measured board's policy to a different feed."""
    write_source(tmp_path, "sneaky.py", cls="Sneaky", name=shadow)
    found, _ = discovery.discover(tmp_path)
    kept, errors = discovery.reject_shadowing(found)

    assert kept == []
    assert len(errors) == 1
    assert shadow in errors[0]


def test_shadow_check_keeps_everything_else(tmp_path):
    write_source(tmp_path, "fine.py", cls="Fine", name="fine")
    found, _ = discovery.discover(tmp_path)
    kept, errors = discovery.reject_shadowing(found)
    assert [k[0].name for k in kept] == ["fine"]
    assert errors == []


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("YES", True),
    ("0", False), ("", False), ("no", False),
])
def test_env_disabled(monkeypatch, value, expected):
    monkeypatch.setenv("JOBLIST_NO_LOCAL", value)
    assert discovery.env_disabled() is expected


def test_env_disabled_defaults_to_loading(monkeypatch):
    monkeypatch.delenv("JOBLIST_NO_LOCAL", raising=False)
    assert discovery.env_disabled() is False
