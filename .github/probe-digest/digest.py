#!/usr/bin/env python3
"""The core of two mcppls probe artifacts, side by side: what a report needs, a few kilobytes instead of 120 MB each.

digest.py <label>=<dir> ... --out digest.json --markdown digest.md
"""
import argparse
import collections
import json
import pathlib
import re


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def pct(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = max(0, min(len(values) - 1, round(p / 100 * (len(values) - 1))))
    return round(values[k], 3)


def latency_digest(rows):
    """Per request kind: count, p50/p95/max, failures, empty answers, and how many completions offered the expected names."""
    out = {}
    by_kind = collections.defaultdict(list)
    for row in rows or []:
        by_kind[(row.get("phase", ""), row.get("kind", ""))].append(row)
    for (phase, kind), items in sorted(by_kind.items()):
        ok = [r["seconds"] for r in items if r.get("status") == "ok" and r.get("seconds") is not None]
        checked = [r for r in items if "hit" in r]
        misses = [{"prefix": r.get("prefix"), "expect": r.get("expect"), "top": (r.get("top") or [])[:8], "count": r.get("count")}
                  for r in checked if not r["hit"]][:4]
        out[f"{phase}/{kind}"] = {
            "n": len(items), "ok": len(ok), "p50": pct(ok, 50), "p95": pct(ok, 95), "max": round(max(ok), 3) if ok else None,
            "notOk": collections.Counter(r.get("status") for r in items if r.get("status") != "ok"),
            "empty": sum(1 for r in items if r.get("count") == 0),
            "checked": len(checked), "right": sum(1 for r in checked if r["hit"]), "misses": misses,
        }
    return out


def diagnostics_digest(diags):
    """Per file: how many diagnostics, and the messages (deduplicated) -- a build that succeeds has no errors to show."""
    out = {}
    for rel, entry in (diags or {}).items():
        items = entry.get("diagnostics") or []
        errors = [d for d in items if d.get("severity") == 1]
        messages = collections.Counter(re.sub(r"\s+", " ", d.get("message") or "")[:160] for d in errors)
        out[rel] = {"published": entry.get("published"), "count": len(items), "errors": len(errors),
                    "topErrors": messages.most_common(6)}
    return out


def experiment(directory):
    d = pathlib.Path(directory)
    result = {"exists": d.exists()}
    if not d.exists():
        return result
    summary = load(d / "summary.json")
    if isinstance(summary, dict):
        keep = {k: v for k, v in summary.items() if k not in ("latency",)}
        result["summary"] = keep
    if (d / "latency.json").exists():
        result["latency"] = latency_digest(load(d / "latency.json"))
    if (d / "settle.json").exists():
        result["settle"] = load(d / "settle.json")
    if (d / "diagnostics.json").exists():
        result["diagnostics"] = diagnostics_digest(load(d / "diagnostics.json"))
    history = load(d / "status-history.json")
    if isinstance(history, list):
        result["statusChanges"] = len(history)
        result["statesSeen"] = collections.Counter(str(h.get("state") if isinstance(h, dict) else h) for h in history)
    reports = load(d / "reports.json")
    if isinstance(reports, list) and reports:
        engines = [e for r in reports for e in r.get("engines", [])]
        def number(value):
            return len(value) if isinstance(value, (list, dict)) else (value or 0) if isinstance(value, (int, float)) else 0
        for key, name in (("restarts", "engineRestartsMax"), ("recentExits", "engineExitsMax"), ("filesSetAside", "filesSetAsideMax")):
            result[name] = max((number(e.get(key)) for e in engines), default=None)
    poll = load(d / "poll.json")
    if isinstance(poll, list) and poll:
        result["serverAliveAtEnd"] = poll[-1].get("alive")
    stderr = d / "server-stderr.log"
    if stderr.exists():
        text = stderr.read_text(encoding="utf-8", errors="replace")
        result["log"] = {
            "warnings": len(re.findall(r"\[warning\]", text)), "errors": len(re.findall(r"\[error\]", text)),
            "clangdRestarts": len(re.findall(r"restarting clangd", text)),
            "clangdExits": len(re.findall(r"clangd exited", text)),
            "eventLoopStalls": len(re.findall(r"the event loop took", text)),
            "preambleIncompatible": len(re.findall(r"is not compatible:", text)),
            "builtModules": len(re.findall(r"\] Built module ", text)),
            "lastWarnings": [line[:260] for line in re.findall(r".*\[warning\].*", text)[-6:]],
        }
    return result


def digest_one(root):
    root = pathlib.Path(root)
    found = {}
    for name in ("build", "emit-release-cold", "emit-release", "emit", "e-check", "e-diag", "e-typing-plain", "e-typing-autosave", "e-warm", "e-bundle"):
        try:
            found[name] = experiment(root / name)
        except Exception as error:   # one unreadable experiment does not lose the others
            found[name] = {"error": repr(error)}
    # e-warm writes one directory per start
    warm = root / "e-warm"
    if warm.exists():
        parts = {}
        for p in sorted(warm.iterdir()):
            if p.is_dir():
                try:
                    parts[p.name] = experiment(p)
                except Exception as error:
                    parts[p.name] = {"error": repr(error)}
        found["e-warm-parts"] = parts
    dumps = root / "dumps"
    found["dumps"] = sorted(p.name for p in dumps.iterdir()) if dumps.exists() else []
    events = root / "application-events.txt"
    found["applicationEvents"] = events.read_text(encoding="utf-8", errors="replace")[:3000] if events.exists() else ""
    run = load(root / "mcppls-run.json")
    if run:
        found["mcpplsRun"] = run
    return found


def markdown(digests):
    lines = ["# mcppls on GalTranslPP (Windows): 0.0.12 vs 0.0.11", ""]
    labels = list(digests)
    def get(label, *path):
        node = digests[label]
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node
    lines.append("| | " + " | ".join(labels) + " |")
    lines.append("|---|" + "---|" * len(labels))
    rows = [
        ("cold: settled after (s)", ("e-diag", "settle", "settledAfter")),
        ("cold: state", ("e-diag", "settle", "state")),
        ("typing plain: server alive", ("e-typing-plain", "summary", "serverAlive")),
        ("typing plain: failed requests", ("e-typing-plain", "summary", "failedRequests")),
        ("typing autosave: server alive", ("e-typing-autosave", "summary", "serverAlive")),
        ("typing autosave: failed requests", ("e-typing-autosave", "summary", "failedRequests")),
        ("typing plain: clangd restarts (log)", ("e-typing-plain", "log", "clangdRestarts")),
        ("typing autosave: clangd restarts (log)", ("e-typing-autosave", "log", "clangdRestarts")),
        ("dumps", ("dumps",)),
    ]
    for title, path in rows:
        lines.append(f"| {title} | " + " | ".join(str(get(label, *path)) for label in labels) + " |")
    lines.append("")
    for section in ("e-diag", "e-typing-plain", "e-typing-autosave"):
        lines.append(f"## {section} latency and accuracy")
        lines.append("")
        lines.append("| kind | " + " | ".join(f"{l} p50/p95/max (n, right/checked, empty)" for l in labels) + " |")
        lines.append("|---|" + "---|" * len(labels))
        kinds = sorted({k for l in labels for k in (get(l, section, "latency") or {})})
        for kind in kinds:
            cells = []
            for l in labels:
                s = (get(l, section, "latency") or {}).get(kind)
                cells.append("-" if not s else f"{s['p50']}/{s['p95']}/{s['max']} ({s['n']}, {s['right']}/{s['checked']}, {s['empty']})")
            lines.append(f"| {kind} | " + " | ".join(cells) + " |")
        lines.append("")
    lines.append("## cold diagnostics (errors per file)")
    lines.append("")
    files = sorted({f for l in labels for f in (get(l, "e-diag", "diagnostics") or {})})
    lines.append("| file | " + " | ".join(labels) + " |")
    lines.append("|---|" + "---|" * len(labels))
    for f in files:
        lines.append(f"| {f} | " + " | ".join(str(((get(l, "e-diag", "diagnostics") or {}).get(f) or {}).get("errors")) for l in labels) + " |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--out", required=True)
    parser.add_argument("--markdown", required=True)
    args = parser.parse_args()
    digests = {}
    for item in args.inputs:
        label, directory = item.split("=", 1)
        digests[label] = digest_one(directory)
    pathlib.Path(args.out).write_text(json.dumps(digests, indent=1, default=str), encoding="utf-8")
    pathlib.Path(args.markdown).write_text(markdown(digests), encoding="utf-8")


if __name__ == "__main__":
    main()
