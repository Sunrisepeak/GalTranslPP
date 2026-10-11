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
            # Rows that came back without a result (neither a list nor items): what they were, whole.
            "noResult": [r for r in items if r.get("count") is None][:3],
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


def engine_digest(text):
    """What clangd itself took: its reply times, its prerequisite and preamble builds, why it was not answered in time."""
    def seconds_of(pattern, group_file=None):
        rows = []
        for match in re.finditer(pattern, text):
            rows.append(match.groups())
        return rows
    replies = collections.defaultdict(list)
    for method, ms in re.findall(r"--> reply:(textDocument/\w+)\(\d+\) (\d+) ms", text):
        replies[method].append(int(ms) / 1000)
    reply_stats = {m: {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95), "max": round(max(v), 2), "over1s": sum(1 for x in v if x > 1)}
                   for m, v in replies.items()}
    prereq = [(pathlib.PureWindowsPath(f).name, float(t)) for f, t in re.findall(r"Built prerequisite modules for file (\S+) in ([0-9.]+) seconds", text)]
    preamble = [(pathlib.PureWindowsPath(f).name, int(size), float(t)) for size, f, t in
                re.findall(r"Built preamble of size (\d+) for file (\S+) version \S+ in ([0-9.]+) seconds", text)]
    late = collections.Counter(re.sub(r"\d+", "N", m) for m in re.findall(r"did not answer (\S+ in time[^\n]*)", text))
    incompatible = collections.Counter(m[:160] for m in re.findall(r"is not compatible: ([^\n]*)", text))
    producer = [float(t) for t in re.findall(r"producer \S+ finished in (\d+) ms", text)]
    states = re.findall(r"\[info\] (module preparation idle[^\n]*|preparation[^\n]{0,120})", text)[-4:]
    # When things happened, from the start of the log: module builds (one per name), the prerequisite sets, the first
    # completion clangd answered within a second.
    stamp = re.compile(r"^mcppls (\d{4}-\d\d-\d\dT(\d\d):(\d\d):(\d\d\.\d+))Z", re.M)
    def at(line_start):
        m = stamp.match(text, line_start)
        return int(m.group(2)) * 3600 + int(m.group(3)) * 60 + float(m.group(4)) if m else None
    starts = [m.start() for m in re.finditer(r"^mcppls ", text, re.M)]
    t0 = at(starts[0]) if starts else 0
    def line_at(pos):
        i = text.rfind("\n", 0, pos) + 1
        t = at(i)
        return round(t - t0, 1) if t is not None and t0 is not None else None
    started, built = {}, []
    for m in re.finditer(r"Module build task (\S+) started", text):
        started.setdefault(m.group(1), line_at(m.start()))
    for m in re.finditer(r"\] Built module (\S+) to", text):
        built.append((m.group(1), started.get(m.group(1)), line_at(m.start())))
    first_fast = None
    for m in re.finditer(r"--> reply:textDocument/completion\(\d+\) (\d+) ms", text):
        if int(m.group(1)) < 1000:
            first_fast = line_at(m.start())
            break
    workers = re.findall(r"Module build task \S+ started \(active=\d+, limit=(\d+)\)", text)
    timeline = {
        "logSeconds": line_at(len(text) - 1),
        "modulesBuilt": len(built),
        "firstModuleBuiltAt": built[0][2] if built else None, "lastModuleBuiltAt": built[-1][2] if built else None,
        "slowestModules": sorted(((n, round(e - b, 1) if b is not None and e is not None else None) for n, b, e in built),
                                 key=lambda r: -(r[1] or 0))[:8],
        "buildLimit": collections.Counter(workers).most_common(2),
        "firstCompletionUnder1sAt": first_fast,
        "prerequisiteSetsAt": [(pathlib.PureWindowsPath(m.group(1)).name, float(m.group(2)), line_at(m.start()))
                               for m in re.finditer(r"Built prerequisite modules for file (\S+) in ([0-9.]+) seconds", text)][:12],
    }
    return {
        "timeline": timeline,
        "replies": reply_stats,
        "prerequisiteBuilds": {"n": len(prereq), "p50": pct([t for _, t in prereq], 50), "max": max((t for _, t in prereq), default=None),
                               "slowest": sorted(prereq, key=lambda r: -r[1])[:6]},
        "preambles": {"n": len(preamble), "slowest": sorted(preamble, key=lambda r: -r[2])[:6]},
        "lateReasons": late.most_common(8),
        "incompatible": incompatible.most_common(8),
        "producerSeconds": [round(t / 1000, 1) for t in producer][:12],
        "preparationLines": [s[:200] for s in states],
    }


# Each reason is a line holding all the words of one of its alternatives (substring tests, line by line: a regex
# that starts with [^\n]* backtracks quadratically on clangd's multi-kilobyte command lines).
WHY = {
    "restart": [("restarting clangd",)],
    "databaseChanged": [("engine database changed",)],
    "scanFailed": [("scan", "fail")],
    "moduleFailed": [("Failed to build module",), ("could not build module",), ("did not compile",)],
    "producerPartial": [("described the project in part",)],
    "producer": [("mcpp emit build-database",), ("still running after",)],
    "stale": [("stale",), ("older than its inputs",)],
    "stall": [("the event loop took",)],
    "setAside": [("set aside",), ("did not build in clangd",), ("routed to mcppls",), ("back to clangd",)],
    "preamble": [("not compatible:",), ("Rebuilding invalidated preamble",)],
    "moduleCache": [("out of date",), ("is newer than",), ("rebuild", "module")],
}

def normalized(line):
    line = re.sub(r"^\S+ \S+ \[\w+\] ", "", line)            # mcppls <time> [level]
    line = re.sub(r"[IEVW]\[\d\d:\d\d:\d\d\.\d+\] ", "", line)   # clangd's own stamp
    return re.sub(r"\d+(?:\.\d+)?", "N", line)


def why_digest(text):
    found = {key: [] for key in WHY}
    for line in text.splitlines():
        if len(line) > 4000:
            line = line[:4000]
        for key, alternatives in WHY.items():
            if any(all(word in line for word in words) for words in alternatives):
                found[key].append(line)
    out = {}
    for key, lines in found.items():
        seen, samples = set(), []
        for line in lines:
            n = normalized(line[:400])[:200]
            if n in seen:
                continue
            seen.add(n)
            samples.append(line[:320])
        out[key] = {"n": len(lines), "kinds": len(seen), "samples": samples[:8]}
    return out


REPORT_KINDS = ("engine-restart", "engine-restart-scheduled", "engine-plan-diff", "model-source", "model-loaded", "incident",
                "file-set-aside", "file-doomed", "engine-spin", "engine-exit", "producer-slow", "tool-run", "engine-database", "plan")


def report_digest(report):
    if not isinstance(report, dict):
        return None
    roots = report.get("roots") or []
    out = {"server": {k: report.get("server", {}).get(k) for k in ("version", "uptimeSeconds")}, "roots": []}
    for root in roots:
        project = root.get("project", {})
        events = root.get("events", [])
        picked = []
        for e in events:
            if e.get("kind") in REPORT_KINDS:
                detail = e.get("detail", {})
                if e.get("kind") == "tool-run":
                    detail = {k: detail.get(k) for k in ("arguments", "durationMs", "exitCode", "purpose")} | {"stderrTail": str(detail.get("stderrTail", ""))[-400:]}
                picked.append({"at": e.get("at"), "kind": e.get("kind"), "detail": json.dumps(detail)[:700]})
        out["roots"].append({
            "project": {k: project.get(k) for k in ("origin", "firstOrigin", "staleReason", "producerVersion", "level", "sets", "issues", "notices")},
            "eventTotals": root.get("eventTotals"),
            "events": picked[-60:],
            "engines": [{k: e.get(k) for k in ("name", "state", "version", "prepared", "toPrepare")} | {
                "restarts": (e.get("details") or {}).get("restarts"), "lastPlanDiff": json.dumps((e.get("details") or {}).get("lastPlanDiff"))[:1500],
                "buildTimes": (e.get("details") or {}).get("buildTimes", [])[:12]} for e in root.get("engines", [])],
        })
    return out


def server_lines_digest(text):
    """The lines mcppls 0.0.13 writes for exactly this (docs/50-troubleshooting.md, "What the log says"): what each start
    cost, the health summaries, the versions, why each module was rebuilt and what each file's modules cost."""
    out = {"versions": [], "startup": [], "health": [], "rebuildsByWhy": collections.Counter(), "rebuildSamples": [],
           "prerequisites": [], "timersSlow": []}
    for line in text.splitlines():
        if "[info] versions (" in line:
            out["versions"].append(line.split("[info] ", 1)[1][:300])
        elif "[info] startup (" in line:
            out["startup"].append(line.split("[info] ", 1)[1][:600])
        elif "[info] health (" in line:
            out["health"].append(line.split("[info] ", 1)[1][:600])
        elif "the timers of " in line:
            out["timersSlow"].append(line[-300:])
        elif "Rebuilding module " in line:
            why = line.split("Rebuilding module ", 1)[1]
            module, _, reason = why.partition(": ")
            category = ("no-bmi" if reason.startswith("no BMI was built") else "command" if " built with other " in reason
                        else "no-bmi" if reason.startswith("no BMI for this command") else "input" if "modified" in reason or "changed" in reason
                        else "invalid" if "valid" in reason else "other")
            out["rebuildsByWhy"][category] += 1
            if len(out["rebuildSamples"]) < 12:
                out["rebuildSamples"].append(f"{module}: {reason[:200]}")
        elif "Prerequisites of " in line:
            m = re.search(r"Prerequisites of (.*?): (\d+) modules, (\d+) reused, (\d+) built, (\d+) failed, in ([\d.]+) s \(reuse checks ([\d.]+) s, builds ([\d.]+) s", line)
            if m:
                out["prerequisites"].append({"file": m.group(1).replace("\\", "/").rsplit("/", 1)[-1], "modules": int(m.group(2)), "reused": int(m.group(3)),
                                             "built": int(m.group(4)), "failed": int(m.group(5)), "seconds": float(m.group(6)),
                                             "reuseSeconds": float(m.group(7)), "buildSeconds": float(m.group(8))})
    out["prerequisites"] = sorted(out["prerequisites"], key=lambda r: -r["seconds"])[:15]
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
    for name in ("report.json",):
        if (d / name).exists():
            result["report"] = report_digest(load(d / name))
    poll = load(d / "poll.json")
    if isinstance(poll, list) and poll:
        result["serverAliveAtEnd"] = poll[-1].get("alive")
    logs = sorted(d.glob("*-stderr.log"))   # server-stderr.log, or one per start in e-warm (<step>-stderr.log)
    if logs:
        result["logFiles"] = [p.name for p in logs]
        text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in logs)
        result["why"] = why_digest(text)
        result["server"] = server_lines_digest(text)
        result["log"] = {
            "warnings": len(re.findall(r"\[warning\]", text)), "errors": len(re.findall(r"\[error\]", text)),
            "clangdRestarts": len(re.findall(r"restarting clangd", text)),
            "clangdExits": len(re.findall(r"clangd exited", text)),
            "eventLoopStalls": len(re.findall(r"the event loop took", text)),
            "preambleIncompatible": len(re.findall(r"is not compatible:", text)),
            "builtModules": len(re.findall(r"\] Built module ", text)),
            "lastWarnings": [line[:260] for line in text.splitlines() if "[warning]" in line][-6:],
        }
        result["engine"] = engine_digest(text)
    return result


def digest_one(root):
    root = pathlib.Path(root)
    found = {}
    for name in ("build", "emit-release-cold", "emit-release", "emit", "e-check", "e-diag", "e-warm-pure", "e-typing-plain", "e-typing-autosave", "e-warm",
                 "e-bundle", "e-steady", "e-steady-typing", "e-buildmcpp"):
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
    lines = [f"# mcppls on the Windows probe project: {' vs '.join(digests)}", ""]
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
        ("warm, nothing changed: settled after (s)", ("e-warm-pure", "settle", "settledAfter")),
        ("build.mcpp edits: describes", ("e-buildmcpp", "summary", "describes")),
        ("build.mcpp edits: clangd restarts", ("e-buildmcpp", "summary", "restarts")),
        ("build.mcpp edits: notices seen", ("e-buildmcpp", "summary", "noticesSeen")),
        ("dumps", ("dumps",)),
    ]
    for title, path in rows:
        lines.append(f"| {title} | " + " | ".join(str(get(label, *path)) for label in labels) + " |")
    lines.append("")
    for section in ("e-diag", "e-warm-pure", "e-typing-plain", "e-typing-autosave", "e-steady", "e-steady-typing"):
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
    lines.append("")
    lines.append("## why (log reasons: count, distinct kinds, first samples)")
    for l in labels:
        sections = [(name, digests[l].get(name)) for name in ("e-diag", "e-warm-pure", "e-typing-plain", "e-typing-autosave", "e-steady", "e-steady-typing", "e-buildmcpp")]
        sections += [(f"e-warm/{k}", v) for k, v in (digests[l].get("e-warm-parts") or {}).items()]
        for name, node in sections:
            why = (node or {}).get("why") if isinstance(node, dict) else None
            if not why or not any(w["n"] for w in why.values()):
                continue
            lines.append("")
            lines.append(f"### {l} {name}")
            for key, w in why.items():
                if not w["n"]:
                    continue
                lines.append(f"- **{key}** {w['n']} ({w['kinds']} kinds)")
                for sample in w["samples"][:3]:
                    lines.append(f"  - `{sample[:240].replace('`', chr(39))}`")
    return "\n".join(lines) + "\n"


def startup_markdown(digests):
    """What each start cost, by the server's own lines: one section per version, one line per start."""
    lines = ["# Starts, health and module rebuilds", ""]
    for label, digest in digests.items():
        lines.append(f"## {label}")
        for name, node in digest.items():
            server = node.get("server") if isinstance(node, dict) else None
            parts = [(name, node)] if server else []
            if name == "e-warm-parts" and isinstance(node, dict):
                parts = [(f"e-warm/{k}", v) for k, v in node.items() if isinstance(v, dict) and v.get("server")]
            for part, value in parts:
                sv = value["server"]
                if not (sv["startup"] or sv["health"] or sv["rebuildsByWhy"] or sv["prerequisites"]):
                    continue
                lines.append(f"### {part}")
                for entry in sv["versions"][:1] + sv["startup"] + sv["health"][-2:]:
                    lines.append(f"- `{entry}`")
                if sv["rebuildsByWhy"]:
                    lines.append(f"- rebuilds by why: {dict(sv['rebuildsByWhy'])}")
                    for sample in sv["rebuildSamples"][:6]:
                        lines.append(f"  - `{sample}`")
                if sv["prerequisites"]:
                    lines.append("")
                    lines.append("| file | modules | reused | built | failed | s | reuse s | build s |")
                    lines.append("|---|---|---|---|---|---|---|---|")
                    for r in sv["prerequisites"][:8]:
                        lines.append(f"| {r['file']} | {r['modules']} | {r['reused']} | {r['built']} | {r['failed']} | {r['seconds']} | {r['reuseSeconds']} | {r['buildSeconds']} |")
                lines.append("")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--out", help="the whole digest as one JSON file")
    parser.add_argument("--markdown", help="the overview, as markdown")
    parser.add_argument("--out-dir", help="split: summary.md, startups.md, why.md and json/<label>/<experiment>.json")
    args = parser.parse_args()
    digests = {}
    for item in args.inputs:
        label, directory = item.split("=", 1)
        digests[label] = digest_one(directory)
    overview = markdown(digests)
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(digests, indent=1, default=str), encoding="utf-8")
    if args.markdown:
        pathlib.Path(args.markdown).write_text(overview, encoding="utf-8")
    if args.out_dir:
        out = pathlib.Path(args.out_dir)
        (out / "json").mkdir(parents=True, exist_ok=True)
        head, _, why = overview.partition("## why")
        (out / "summary.md").write_text(head, encoding="utf-8")
        (out / "why.md").write_text("## why" + why if why else "no reasons recorded\n", encoding="utf-8")
        (out / "startups.md").write_text(startup_markdown(digests), encoding="utf-8")
        for label, digest in digests.items():
            (out / "json" / label).mkdir(parents=True, exist_ok=True)
            for name, node in digest.items():
                (out / "json" / label / f"{name}.json").write_text(json.dumps(node, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
