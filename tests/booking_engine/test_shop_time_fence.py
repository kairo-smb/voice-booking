"""Fence: shop time logic lives in `booking_engine/time/shop_time.py` and nowhere else.

Fails when code under booking_engine/ (outside that module):
  * has a string literal that is an IANA zone name ('Europe/Rome'), or SQL
    `AT TIME ZONE '<literal>'`, or `CURRENT_DATE` / `CURRENT_TIME`;
  * calls `ZoneInfo(...)`, a bare `datetime.now()` / `datetime.today()` /
    `date.today()` / `utcnow()` / `.astimezone()` with no argument (naive, i.e.
    host-local).
Docstrings and comments are not code and are ignored. Kairo-internal uses go in
ALLOW with the reason. Shop SQL reads `shops.timezone` (`AT TIME ZONE s.timezone`).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from zoneinfo import available_timezones

ROOT = Path(__file__).resolve().parents[2] / "booking_engine"
MODULE = ROOT / "time" / "shop_time.py"

# (relative path, rule) -> reason. Empty on purpose: every use here is shop-facing.
ALLOW: dict[tuple[str, str], str] = {}

# SQL: (file, line text fragment) -> reason.
ALLOW_SQL = {
    ("04_voice_agent_v2.sql", "effective_from date NOT NULL DEFAULT current_date"):
        "voice_agent.system_policy is Kairo's platform-wide policy text, not a shop's; "
        "a column default in an applied migration (UTC server date is fine, nobody shows it per shop)",
}

_ZONES = {z for z in available_timezones() if "/" in z}
_SQL_BAD = re.compile(r"AT\s+TIME\s+ZONE\s+'|CURRENT_DATE|CURRENT_TIME\b", re.IGNORECASE)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _violations(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    docs = _docstring_nodes(tree)
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if node.value in _ZONES:
                found.append(("iana-literal", f"{path.name}:{node.lineno} {node.value!r}"))
            elif _SQL_BAD.search(node.value):
                found.append(("sql-local-time", f"{path.name}:{node.lineno}"))
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
            if name == "ZoneInfo":
                found.append(("zoneinfo", f"{path.name}:{node.lineno}"))
            elif name == "utcnow":
                found.append(("naive-now", f"{path.name}:{node.lineno} utcnow()"))
            elif name == "now" and owner == "datetime" and not node.args and not node.keywords:
                found.append(("naive-now", f"{path.name}:{node.lineno} datetime.now()"))
            elif name == "today" and owner in ("date", "datetime"):
                found.append(("naive-now", f"{path.name}:{node.lineno} {owner}.today()"))
            elif name == "astimezone" and not node.args and not node.keywords:
                found.append(("naive-now", f"{path.name}:{node.lineno} .astimezone()"))
    return found


def _sources():
    files = [p for p in ROOT.rglob("*.py") if p != MODULE]
    return files


def test_no_shop_time_logic_outside_the_module():
    bad = []
    for p in _sources():
        rel = str(p.relative_to(ROOT.parent))
        for rule, where in _violations(p):
            if (rel, rule) not in ALLOW:
                bad.append(f"{rel}: {rule} ({where})")
    assert not bad, "shop time belongs in booking_engine/time/shop_time.py:\n" + "\n".join(bad)


def test_sql_files_have_no_local_time_literals():
    bad = []
    for p in (ROOT / "db" / "sql").glob("*.sql"):
        for i, line in enumerate(p.read_text().splitlines(), 1):
            code = line.split("--", 1)[0]
            if _SQL_BAD.search(code) and not any(
                p.name == f and frag in code for (f, frag) in ALLOW_SQL
            ):
                bad.append(f"{p.name}:{i}")
    assert not bad, "\n".join(bad)


def test_the_fence_catches_a_violation(tmp_path):
    f = tmp_path / "x.py"
    f.write_text('from datetime import datetime\nTZ = "Europe/Rome"\nn = datetime.now()\n'
                 'q = "SELECT CURRENT_DATE"\n')
    rules = {r for r, _ in _violations(f)}
    assert rules == {"iana-literal", "naive-now", "sql-local-time"}
