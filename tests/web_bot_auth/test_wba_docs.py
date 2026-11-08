"""WEB_BOT_AUTH.md states the API, the refusals, and the interop result as the code has them."""

from __future__ import annotations

import ast
import enum
import importlib
import inspect
import pkgutil
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import ForwardRef, TypeVar

from iab_agentic_primitives.web_bot_auth import Refusal

DOC = (Path(__file__).resolve().parents[2] / "WEB_BOT_AUTH.md").read_text()


def _section(heading: str) -> str:
    start = DOC.index(heading)
    return DOC[start : DOC.index("\n## ", start + len(heading))]


def test_the_module_table_lists_each_modules_all() -> None:
    rows = re.findall(r"^\| `([a-z_]+)` \| [^|]* \| (.*) \|$", _section("## Modules"), re.M)
    assert rows
    for module, names in rows:
        listed = re.findall(r"`([A-Za-z_0-9]+)`", names)
        code = importlib.import_module(f"iab_agentic_primitives.web_bot_auth.{module}").__all__
        assert listed == list(code), module


def test_the_root_export_list_is_the_roots_all() -> None:
    block = _section("## Modules").split("```text\n", 1)[1].split("\n```", 1)[0]
    root = importlib.import_module("iab_agentic_primitives.web_bot_auth")
    assert block.splitlines() == list(root.__all__)


def test_every_public_module_sorts_its_all() -> None:
    rows = re.findall(r"^\| `([a-z_]+)` \|", _section("## Modules"), re.M)
    for module in ["", *rows]:
        name = "iab_agentic_primitives.web_bot_auth" + (f".{module}" if module else "")
        exported = list(importlib.import_module(name).__all__)
        constants = [n for n in exported if n.isupper()]
        classes = [n for n in exported if n[0].isupper() and not n.isupper()]
        rest = [n for n in exported if not n[0].isupper()]
        assert exported == sorted(constants) + sorted(classes) + sorted(rest), name


def test_the_refusal_table_is_the_refusal_enum() -> None:
    table = _section("### Verification procedure")
    by_status: dict[int, set[str]] = {}
    for status, cell in re.findall(r"^\| `(\d{3})` \| (.*) \|$", table, re.M):
        by_status[int(status)] = set(re.findall(r"`([a-z-]+)`", cell))
    expected: dict[int, set[str]] = {}
    for refusal in Refusal:
        expected.setdefault(refusal.status, set()).add(refusal.code)
    assert by_status == expected


# ── every type a public signature names is exported ───────────────────────

PACKAGE = "iab_agentic_primitives.web_bot_auth"


def test_the_verifier_names_the_steps_of_the_procedure() -> None:
    from iab_agentic_primitives.web_bot_auth import verifier

    procedure = " ".join(_section("### Verification procedure").split())
    docstring = " ".join((verifier.__doc__ or "").split())
    in_guide = set(re.findall(r"\*\*(\w[^*]*?)\.\*\*", procedure))
    in_code = set(re.findall(r'"([A-Z][a-z]+ the [a-z ]+)"', docstring))
    assert in_guide == in_code, in_guide ^ in_code


def _modules() -> dict[str, ModuleType]:
    """Every module of the package, private ones included, by name; not the CLI entry point."""
    root = importlib.import_module(PACKAGE)
    found = {PACKAGE: root}
    for info in pkgutil.walk_packages(root.__path__, f"{PACKAGE}."):
        if not info.name.endswith(".__main__"):
            found[info.name] = importlib.import_module(info.name)
    return found


MODULES = _modules()
#: The public modules: no part of the name after the package starts with ``_``.
PUBLIC = {
    name: module
    for name, module in MODULES.items()
    if not any(part.startswith("_") for part in name.removeprefix(PACKAGE).split("."))
}


def _top_level_names(module: ModuleType) -> set[str]:
    """The names a module's own source defines at the top level."""
    assert module.__file__ is not None
    names: set[str] = set()
    for node in ast.parse(Path(module.__file__).read_text()).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


DEFINED = {name: _top_level_names(module) for name, module in MODULES.items()}
EXPORTED = {id(getattr(m, n)) for m in PUBLIC.values() for n in getattr(m, "__all__", ())}


def _home(module: ModuleType, name: str) -> str | None:
    """The package module that defines what ``name`` is in ``module``, if the package does."""
    value = vars(module).get(name)
    if value is None or isinstance(value, TypeVar):
        return None
    for home, defined in DEFINED.items():
        if name in defined and vars(MODULES[home]).get(name) is value:
            return home
    return None


def _members(cls: type) -> Iterator[tuple[str, object]]:
    """A class's own public functions and properties, unwrapped to their functions."""
    for name, attribute in vars(cls).items():
        if name.startswith("_") and name not in ("__init__", "__call__"):
            continue
        if isinstance(attribute, (classmethod, staticmethod)):
            yield name, attribute.__func__
        elif isinstance(attribute, property):
            yield name, attribute.fget
        else:
            yield name, attribute


def _arguments(where: str, function: object) -> Iterator[tuple[str, object]]:
    """``(where, annotation)`` for each public argument of ``function``."""
    for argument, annotation in getattr(function, "__annotations__", {}).items():
        if not argument.startswith("_"):
            yield f"{where}({argument})", annotation


def _annotations(value: object) -> Iterator[tuple[str, object]]:
    """``(where, annotation)`` for each annotation of a public function or class."""
    if inspect.isfunction(value):
        yield from _arguments(value.__name__, value)
    if not isinstance(value, type) or issubclass(value, enum.Enum):
        return
    for field, annotation in vars(value).get("__annotations__", {}).items():
        if not field.startswith("_"):
            yield f"{value.__name__}.{field}", annotation
    for name, member in _members(value):
        if inspect.isfunction(member):
            yield from _arguments(f"{value.__name__}.{name}", member)


def _names(annotation: object) -> Iterator[str]:
    """The names an annotation's text refers to (every module defers annotations).

    A generated ``__init__``, such as a dataclass's, annotates its return as ``None``.
    The strings inside an annotation are ``Literal`` values, not names. Before
    Python 3.14, a ``NamedTuple`` keeps a field's deferred annotation as a ``ForwardRef``.
    """
    if annotation is None:
        return
    if isinstance(annotation, ForwardRef):
        annotation = annotation.__forward_arg__
    assert isinstance(annotation, str), annotation
    for node in ast.walk(ast.parse(annotation, mode="eval")):
        if isinstance(node, ast.Name):
            yield node.id


def test_every_type_a_public_signature_names_is_exported() -> None:
    unexported = set()
    for module_name, module in PUBLIC.items():
        for name in module.__all__:
            home = MODULES[_home(module, name) or module_name]
            for where, annotation in _annotations(getattr(module, name)):
                for used in _names(annotation):
                    source = _home(home, used)
                    if source is not None and id(vars(home)[used]) not in EXPORTED:
                        unexported.add(f"{module_name}.{name}: {where} names {source}.{used}")
    assert sorted(unexported) == []


def test_the_module_table_lists_every_public_module() -> None:
    rows = re.findall(r"^\| `([a-z_]+)` \|", _section("## Modules"), re.M)
    top = {name.removeprefix(f"{PACKAGE}.") for name in PUBLIC if name.count(".") == 2}
    assert sorted(rows) == sorted(top)
