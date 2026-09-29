#!/usr/bin/env python3
"""Syntax-check every PromQL query in the shipped Grafana dashboard.

The dashboard's queries have the same problem the alerting rules had: nothing
ever parsed them. A typo in a panel expression produces an empty panel, and an
empty panel is indistinguishable from a quiet system.

Grafana template variables are not PromQL, so each `$var` is replaced before
parsing. That checks the shape of the query, which is what breaks; it cannot
check what the variable will expand to.

Two kinds of substitution, because one does not fit both. Grafana's duration
macros - $__range and friends - sit inside a range selector, where a label
matcher is a syntax error; they become a literal duration. Everything else is a
label value and becomes a permissive matcher.

The rules file it builds names every query after its panel and its refId -
`dashboard:p<panel id>_<refId>` - and is kept when a third argument names where
to write it, so that promtool unit tests can evaluate the dashboard's own
queries against fixture series (tests/promtool/dashboard_test.yaml).

Usage: check-dashboard-queries.py <dashboard.json> <promtool> [<rules-out.yaml>]
"""
import json
import re
import subprocess
import sys
import tempfile


# Grafana duration macros. Any valid duration will do: this checks syntax, not
# what the dashboard's time range happens to be when someone opens it.
DURATION_MACROS = ("$__range_s", "$__range_ms", "$__range", "$__rate_interval",
                   "$__interval_ms", "$__interval")


def normalise(expr, constants):
    for macro in DURATION_MACROS:
        expr = expr.replace(macro, "5m")
    # Constant variables stand for a number, not a label value; a matcher in
    # their place would be a syntax error. Longest name first, so that one
    # name being a prefix of another cannot split it.
    for name in sorted(constants, key=len, reverse=True):
        expr = expr.replace("$" + name, constants[name])
    return re.sub(r"\$\w+", ".+", expr)


def main(dashboard, promtool, rules_out=None):
    doc = json.load(open(dashboard))
    constants = {
        v["name"]: str(v["query"])
        for v in doc.get("templating", {}).get("list", [])
        if v.get("type") == "constant"
    }
    queries = []
    for panel in doc.get("panels", []):
        for target in panel.get("targets", []):
            expr = target.get("expr")
            if expr:
                queries.append((panel["title"], f"p{panel['id']}_{target['refId']}", expr))

    if not queries:
        print("    BROKEN no panel queries found - this check verified nothing")
        return 1

    # One throwaway rule per query: promtool parses rule expressions, and that
    # is the only PromQL parser available without a running server.
    rules = {"groups": [{"name": "dashboard", "rules": []}]}
    for title, name, expr in queries:
        rules["groups"][0]["rules"].append(
            {"record": f"dashboard:{name}", "expr": normalise(expr, constants)}
        )

    if rules_out:
        path = rules_out
        with open(path, "w") as fh:
            json.dump(rules, fh)
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            json.dump(rules, fh)
            path = fh.name

    result = subprocess.run(
        [promtool, "check", "rules", path], capture_output=True, text=True
    )
    print(f"    {len(queries)} dashboard queries parsed")
    if result.returncode != 0:
        print(result.stdout.strip())
        print(result.stderr.strip())
        for title, _, expr in queries:
            print(f"    panel {title!r}: {expr[:100]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
