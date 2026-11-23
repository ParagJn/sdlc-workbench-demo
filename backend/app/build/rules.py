"""The automatic code rules (S18b): plain checks over the saved code, with no model. Each finding has a rule, a severity, a file and a line.

These are computed from the saved code whenever they are asked for and never stored, so they are always current and cannot drift from the code.
They are deliberately SIMPLE (a line is read as text, not parsed as a program), so they will miss things and will sometimes flag harmless code. They
exist to catch the common, serious mistakes cheaply and the same way every time; the two model reviewers read the code for everything else.

The rules are advice about a PROTOTYPE. A clean result means "none of these patterns were found", never "this is safe to run in production".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from ..guardrails.scan import Scanner

HIGH, MEDIUM, LOW = "high", "medium", "low"
MAX_LINE_CHARS = 600           # a longer line is read only this far: a minified file must not make a rule take for ever
MAX_FINDINGS_PER_RULE = 20     # one noisy rule must not bury the others
MAX_FINDINGS = 200
PY, JS, HTML, ANY = ("py",), ("js",), ("html",), ("py", "js", "html", "css", "sql")


@dataclass(frozen=True)
class CodeRule:
    id: str
    severity: str
    what: str                      # one plain sentence about the problem, shown to a person
    kinds: tuple[str, ...]         # file types the rule applies to
    pattern: re.Pattern
    comment: str = "#"             # a line that starts with this (after spaces) is a comment and is skipped


def _p(text: str) -> re.Pattern:
    return re.compile(text)


RULES: tuple[CodeRule, ...] = (
    # ---- high: a person who ships this is exposed
    CodeRule("eval-exec", HIGH, "runs text as code (eval or exec), which lets whoever controls the text run anything", PY, _p(r"(?<![\w.])(eval|exec)\s*\(")),
    CodeRule("js-eval", HIGH, "runs text as code (eval or new Function)", JS, _p(r"(?<![\w.])eval\s*\(|new\s+Function\s*\("), "//"),
    CodeRule("shell-true", HIGH, "starts a shell from text (shell=True), so an input can add its own commands", PY, _p(r"shell\s*=\s*True")),
    CodeRule("os-system", HIGH, "starts a shell command from text (os.system or os.popen)", PY, _p(r"\bos\.(system|popen)\s*\(")),
    CodeRule("unsafe-pickle", HIGH, "loads data with pickle, which can run code hidden in the data", PY, _p(r"\bpickle\.loads?\s*\(|\bcPickle\.loads?\s*\(")),
    CodeRule("unsafe-yaml", HIGH, "loads YAML without the safe loader, which can run code hidden in the file", PY, _p(r"\byaml\.load\s*\((?![^)]*Loader\s*=\s*yaml\.SafeLoader)(?![^)]*SafeLoader)")),
    CodeRule("sql-by-formatting", HIGH, "builds an SQL statement by joining text, which lets an input change the statement (SQL injection)", PY,
             _p(r"""(?ix)\.execute(?:script|many)?\s*\(\s*(?:f["']|["'][^"']*["']\s*(?:%|\+|\.format\s*\())""")),
    CodeRule("sql-concatenated", HIGH, "builds an SQL statement by joining text, which lets an input change the statement (SQL injection)", PY,
             _p(r"""(?ix)(?:select|insert|update|delete)\b[^"'\n]*["']\s*\+\s*\w""")),
    # ---- medium: risky defaults and habits
    CodeRule("debug-on", MEDIUM, "leaves debug mode on, which can show internals to anyone who triggers an error", PY, _p(r"\bdebug\s*=\s*True\b")),
    CodeRule("bind-all", MEDIUM, "listens on every network address (0.0.0.0), not only this machine", PY + JS, _p(r"""["']0\.0\.0\.0["']"""), "#"),
    CodeRule("cors-open", MEDIUM, "lets every website call this service (CORS allows every origin)", PY, _p(r"""allow_origins\s*=\s*\[\s*["']\*["']\s*\]|Access-Control-Allow-Origin["']?\s*[:,]\s*["']\*""")),
    CodeRule("innerhtml", MEDIUM, "writes text into the page as HTML (innerHTML), so data containing markup can run script", JS + HTML, _p(r"\.innerHTML\s*[+]?=|\binsertAdjacentHTML\s*\("), "//"),
    CodeRule("http-address", MEDIUM, "uses a plain http:// address, which sends data without protecting it", PY + JS + HTML, _p(r"""["']http://(?!localhost|127\.0\.0\.1|0\.0\.0\.0)[\w.-]+"""), "#"),
    CodeRule("verify-off", MEDIUM, "switches off the check that a secure connection is to the right server (verify=False)", PY, _p(r"\bverify\s*=\s*False\b")),
    # ---- low: tidiness a reader would want fixed
    CodeRule("bare-except", LOW, "catches every error without saying which (a bare except), which hides real faults", PY, _p(r"^\s*except\s*:")),
    CodeRule("todo-left", LOW, "leaves a TODO or FIXME in the code", ANY, _p(r"\b(TODO|FIXME|XXX)\b")),
)
BY_ID = {r.id: r for r in RULES}
SEVERITY_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}


@dataclass(frozen=True)
class CodeFinding:
    rule: str                     # a CodeRule id, or "secret" for the scanner
    severity: str
    file: str
    line: int                     # 1-based
    what: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity, "file": self.file, "line": self.line, "what": self.what}


def _kind(path: str) -> str:
    dot = path.rfind(".")
    return path[dot + 1:].lower() if dot > 0 else ""


def _is_comment(line: str, marker: str) -> bool:
    return line.lstrip().startswith(marker)


def check_file(path: str, text: str) -> list[CodeFinding]:
    """Every rule that applies to this file's type, on every line that is not a comment. A rule fires at most once per line."""
    kind = _kind(path)
    rules = [r for r in RULES if kind in r.kinds]
    found: list[CodeFinding] = []
    per_rule: dict[str, int] = {}
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw[:MAX_LINE_CHARS]
        for rule in rules:
            if per_rule.get(rule.id, 0) >= MAX_FINDINGS_PER_RULE:
                continue
            if rule.id != "todo-left" and _is_comment(line, rule.comment):
                continue
            if rule.pattern.search(line):
                found.append(CodeFinding(rule.id, rule.severity, path, number, rule.what))
                per_rule[rule.id] = per_rule.get(rule.id, 0) + 1
    return found


def check_secrets(path: str, text: str, scanner: Scanner) -> list[CodeFinding]:
    """The scanner's SECRET rules, as code findings: a secret in code is high. The value itself is never copied into a finding."""
    out: list[CodeFinding] = []
    for f in scanner.scan(text):
        if f.klass == "secret":
            out.append(CodeFinding("secret", HIGH, path, f.line, f"contains what looks like a secret ({f.rule}); remove it and use a setting or an environment variable"))
    return out


def check_code(files: Mapping[str, str], scanner: Scanner | None = None) -> list[CodeFinding]:
    """All automatic findings for a set of files {path: text}, most serious first, then by file and line. Bounded."""
    found: list[CodeFinding] = []
    for path in sorted(files):
        found += check_file(path, files[path])
        if scanner is not None:
            found += check_secrets(path, files[path], scanner)
    found.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.file, f.line, f.rule))
    return found[:MAX_FINDINGS]


def counts(findings: Iterable[CodeFinding]) -> dict[str, int]:
    out = {HIGH: 0, MEDIUM: 0, LOW: 0}
    for f in findings:
        out[f.severity] += 1
    return out
