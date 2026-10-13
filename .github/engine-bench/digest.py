#!/usr/bin/env python3
"""The engine bench's evidence, read where it was made and kept small.

  digest.py job --engine E --in D:/bench-out --out D:/bench-out/digest
      E.md (what this engine did), E.json (the numbers the report compares), E-logs.md (what its logs say:
      errors, crashes, rebuilds, the engine's own timings), at most a few hundred lines each
  digest.py report --in <the jobs' digests> --out report
      report.md (side by side), header-vs-modules.md, typing.md, stability.md, logs.md
"""
import argparse
import collections
import json
import pathlib
import re
import statistics

BENCH = ["BenchH", "BenchM", "BenchMG", "BenchHX", "BenchX"]
VARIANT = {"BenchH": "H: headers", "BenchM": "M: modules", "BenchMG": "MG: modules via GPPDefines",
           "BenchHX": "HX: headers + platform headers", "BenchX": "X: mixed (platform headers + modules)"}
ENGINES = ["mcppls", "mcxx", "clangd", "clice"]


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def pct(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def short(rel):
    return rel.rsplit("/", 1)[-1].rsplit(".", 1)[0] if "/bench/" in rel else rel.rsplit("/", 1)[-1]


def fmt(value, unit="s", digits=2):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}{unit}"
    return f"{value}{unit}"


# ---------------------------------------------------------------------------------------------- one run of a server

def run_metrics(record):
    """The numbers of one start (cold or warm) of a server."""
    if not record:
        return None
    m = {"init": record.get("init", {}).get("seconds"), "server": record.get("init", {}).get("server"),
         "settled": record.get("settle", {}).get("settled"), "settledAfter": record.get("settle", {}).get("after"),
         "firstRight": record.get("settle", {}).get("firstRight", {}), "end": record.get("end", {}),
         "methods": record.get("methods", {}), "status": record.get("status", [])[-8:]}
    files = {}
    for rel, d in (record.get("diagnostics") or {}).items():
        files[short(rel)] = {"firstDiagnostics": (d.get("times") or [None])[0], "errors": d.get("errors"),
                             "warnings": d.get("warnings"), "firstErrors": d.get("first", [])[:3], "published": d.get("published")}
    for rel, t in (record.get("semanticTokens") or {}).items():
        files.setdefault(short(rel), {})["semanticTokens"] = t.get("at") if t.get("status") == "ok" else t.get("status")
    rows = record.get("requests") or []
    for rel in {r["file"] for r in rows if "method" in r}:
        key = short(rel)
        f = files.setdefault(key, {})
        idle = [r for r in rows if r["file"] == rel and r.get("phase") == "idle" and "method" in r]
        prep = [r for r in rows if r["file"] == rel and r.get("phase") == "preparing" and "method" in r]
        for method in ("completion", "hover", "definition"):
            of = [r for r in idle if r["method"] == method]
            ok = [r["seconds"] for r in of if r["status"] == "ok"]
            f[method] = {"n": len(of), "ok": len(ok), "right": sum(1 for r in of if r.get("right")),
                         "p50": pct(ok, 50), "p95": pct(ok, 95), "max": max(ok) if ok else None,
                         "failed": dict(collections.Counter(r["status"] for r in of if r["status"] != "ok"))}
        points = {}
        for r in idle:
            p = points.setdefault(r["point"], {"seconds": [], "right": 0, "n": 0, "said": None})
            p["n"] += 1
            if r["status"] == "ok":
                p["seconds"].append(r["seconds"])
                p["right"] += 1 if r.get("right") else 0
                p["said"] = p["said"] or r.get("said")
        f["points"] = {k: {"p50": pct(v["seconds"], 50), "right": f"{v['right']}/{v['n']}", "said": (v["said"] or [])[:5] if isinstance(v["said"], list) else str(v["said"])[:80]}
                       for k, v in points.items()}
        answered = [r for r in prep if r["status"] == "ok"]
        f["preparing"] = {"asked": len(prep), "answered": len(answered), "right": sum(1 for r in prep if r.get("right")),
                          "firstAnswer": min((r["t"] for r in answered), default=None),
                          "firstRight": min((r["t"] for r in prep if r.get("right")), default=None),
                          "timeouts": sum(1 for r in prep if r["status"] == "timeout")}
    m["files"] = files
    typed = record.get("typing") or []
    by_file = {}
    for r in typed:
        f = by_file.setdefault(short(r["file"]), {"completion": [], "member": [], "statuses": collections.Counter(), "diag": [], "diagErrors": []})
        if r.get("kind") == "completion":
            f["statuses"][r["status"]] += 1
            if r["status"] == "ok":
                f["completion"].append(r["seconds"])
                if "right" in r:
                    f["member"].append(bool(r["right"]))
        elif r.get("kind") == "diagnostics-after-line":
            f["diag"].append(r.get("seconds"))
            f["diagErrors"].append(r.get("errors"))
    m["typing"] = {k: {"asked": sum(v["statuses"].values()), "statuses": dict(v["statuses"]), "p50": pct(v["completion"], 50),
                       "p95": pct(v["completion"], 95), "max": max(v["completion"]) if v["completion"] else None,
                       "memberRight": f"{sum(v['member'])}/{len(v['member'])}", "diagnosticsAfterLine": v["diag"],
                       "errorsAfterLine": v["diagErrors"]} for k, v in by_file.items()}
    tree = record.get("tree") or {}
    m["tree"] = {k: tree.get(k) for k in ("measured", "peakRssMB", "peakPrivateMB", "peakAt", "finalRssMB", "maxProcesses",
                                           "cpuCoreSeconds", "started", "exited")}
    return m


# ---------------------------------------------------------------------------------------------- the logs

LOG_PATTERNS = {
    "crash": r"PLEASE submit a bug report|Stack dump|Signalled during|Exception Code|EXCEPTION_|terminate called|Assertion .* failed|panic|crashed",
    "error": r"^E\[|\[error\]|\berror\b:|ERROR",
    "warning": r"^W\[|\[warning\]",
    "moduleBuilt": r"Built module |Built PCM for module|module .* built",
    "moduleRebuilt": r"Rebuilding module",
    "preamble": r"Built preamble|PCH built",
    "restart": r"restart|Restarting|respawn",
}


def scan_log(path, limit=12):
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    counts = {k: 0 for k in LOG_PATTERNS}
    examples = {k: [] for k in ("crash", "error", "warning", "restart")}
    seen = set()
    for line in lines:
        for kind, pattern in LOG_PATTERNS.items():
            if re.search(pattern, line, re.I if kind in ("crash", "restart") else 0):
                counts[kind] += 1
                if kind in examples and len(examples[kind]) < limit:
                    shape = re.sub(r"\d+", "N", re.sub(r"^\S+\s+\S*\s*", "", line))[:160]
                    if shape not in seen:
                        seen.add(shape)
                        examples[kind].append(line.strip()[:300])
    # The engines' own timings.
    cost = [int(m.group(1)) for m in re.finditer(r"Completion in .+?: (\d+) ms \(module input checks", text)]
    checks = [int(m.group(1)) for m in re.finditer(r"module input checks (\d+) ms", text)]
    clice = collections.defaultdict(list)
    for m in re.finditer(r"\[perf:request\] kind=(\w+) .*?total_ms=([\d.]+)", text):
        clice[m.group(1)].append(float(m.group(2)))
    prereq = re.findall(r"Prerequisites of [^\n]*", text)
    return {"lines": len(lines), "bytes": len(text), "counts": counts, "examples": {k: v for k, v in examples.items() if v},
            "clangdCompletion": {"n": len(cost), "p50ms": pct(cost, 50), "p95ms": pct(cost, 95), "checksP50ms": pct(checks, 50)} if cost else None,
            "clicePerf": {k: {"n": len(v), "p50ms": pct(v, 50), "p95ms": pct(v, 95)} for k, v in clice.items()} or None,
            "prerequisites": prereq[:4], "head": [l[:240] for l in lines[:6]]}


# ---------------------------------------------------------------------------------------------- deep

def deep_metrics(base):
    compile_ = load(base / "deep" / "compile" / "compile.json") or {}
    check = load(base / "deep" / "check" / "check.json") or {}
    files = {}
    for rel, r in (compile_.get("files") or {}).items():
        if "syntax" not in r:
            files[short(rel)] = {"error": r.get("error")}
            continue
        read = r.get("read") or {}
        decls = read.get("declarations")
        completion = {}
        for point, c in (r.get("completion") or {}).items():
            d = (c.get("read") or {}).get("declarations")
            completion[point] = {"median": c.get("median"), "results": c.get("results"), "right": c.get("right"),
                                 "declsRead": d[0] if d else None, "declsInBMIs": d[1] if d else None}
        files[short(rel)] = {"imports": r.get("imports"), "exit": r["syntax"].get("exit"), "errors": r["syntax"].get("errors"),
                             "syntaxMedian": r["syntax"].get("median"), "syntaxRuns": r["syntax"].get("seconds"),
                             "stderr": (r["syntax"].get("stderr") or "")[-400:],
                             "declsRead": decls[0] if decls else None, "declsInBMIs": decls[1] if decls else None,
                             "typesRead": (read.get("types") or [None])[0], "timeTrace": r.get("timeTrace"), "completion": completion}
    bmis = {k: {"ok": v.get("ok"), "seconds": v.get("seconds"), "MB": round(v["bytes"] / 2**20, 1) if v.get("bytes") else None,
                "why": (v.get("why") or v.get("stderr") or "")[-300:]} for k, v in (compile_.get("bmis") or {}).items()}
    checks = {}
    for label, runs in check.items():
        rel, mode = label.split("|")
        for run, r in runs.items():
            spans = r.get("spans") or {}
            checks[f"{short(rel)}|{mode}|{run}"] = {
                "seconds": r.get("seconds"), "exit": r.get("exit"), "errors": r.get("errors"), "firstErrors": r.get("firstErrors"),
                "preamble": (spans.get("BuildPreamble") or {}).get("sum"), "ast": (spans.get("BuildAST") or {}).get("sum"),
                "completions": (spans.get("Sema completion") or {}).get("n"),
                "completionMedian": (spans.get("CodeCompleteFlow") or {}).get("median"),
                "semaMedian": (spans.get("Sema completion") or {}).get("median"),
                "validationMedian": (spans.get("Completion module validation") or {}).get("median"),
                "executionMedian": (spans.get("Completion semantic execution") or {}).get("median"),
                "populateMedian": (spans.get("Populate CodeCompleteResult") or {}).get("median"),
                "prerequisites": r.get("prerequisites"), "costLines": r.get("costLines")}
    return {"clang": compile_.get("clang"), "files": files, "bmis": bmis, "check": checks}


def deep_markdown(d):
    out = ["### The compiler alone (the project's LLVM, BMIs built once beforehand)", "",
           "| file | imports | -fsyntax-only (median) | decls read from BMIs | completion `json.` | completion `std::` | completion `statement` | errors |",
           "|---|---|---|---|---|---|---|---|"]
    for name in BENCH + ["NormalJsonTranslator.Run.cpp"]:
        f = d["files"].get(name)
        if not f or "syntaxMedian" not in f:
            continue
        c = f.get("completion") or {}

        def cc(p):
            x = c.get(p) or {}
            if not x:
                return "—"
            reads = f", {x['declsRead']} decls" if x.get("declsRead") else ""
            return f"{fmt(x.get('median'))} ({x.get('results')}{'' if x.get('right') else ', wrong'}{reads})"
        decls = f"{f['declsRead']}/{f['declsInBMIs']}" if f.get("declsRead") else "—"
        out.append(f"| {VARIANT.get(name, name)} | {', '.join(f.get('imports') or []) or '—'} | {fmt(f['syntaxMedian'])} | {decls} | "
                   f"{cc('json.')} | {cc('std::')} | {cc('statement')} | {f.get('errors')} |")
    out += ["", "Time trace (ms, the largest totals) per file:", ""]
    for name, f in d["files"].items():
        if f.get("timeTrace"):
            out.append(f"- **{name}**: " + ", ".join(f"{k} {v}" for k, v in list(f["timeTrace"].items())[:8]))
    failed = {k: v for k, v in d["bmis"].items() if not v.get("ok")}
    out += ["", f"BMIs built: {sum(1 for v in d['bmis'].values() if v.get('ok'))}/{len(d['bmis'])}; "
            f"largest: " + ", ".join(f"{k} {v['MB']} MB ({v['seconds']} s)" for k, v in sorted(d["bmis"].items(), key=lambda kv: -(kv[1].get("MB") or 0))[:6])]
    for k, v in failed.items():
        out.append(f"- BMI **{k}** failed: `{v['why'][-240:]}`")
    out += ["", "### clangd --check (one line, a completion at each token; clangd's own trace, ms)", "",
            "| file | module support | run | total s | preamble | AST | completions | per completion | of which module checks | compile+Sema | results | errors |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for key, c in d["check"].items():
        name, mode, run = key.split("|")
        out.append(f"| {VARIANT.get(name, name)} | {mode} | {run} | {c['seconds']} | {fmt(c['preamble'], '')} | {fmt(c['ast'], '')} | "
                   f"{c['completions']} | {fmt(c['completionMedian'], '')} | {fmt(c['validationMedian'], '')} | {fmt(c['executionMedian'], '')} | "
                   f"{fmt(c['populateMedian'], '')} | {c['errors']} |")
    for key, c in d["check"].items():
        if c.get("firstErrors"):
            out.append(f"- {key}: " + " / ".join(f"`{e[:150]}`" for e in c["firstErrors"][:2]))
    return out


# ---------------------------------------------------------------------------------------------- markdown of one engine

def lsp_markdown(engine, m):
    out = []
    for run in ("cold", "warm"):
        r = m.get(run)
        if not r:
            out += [f"### {run}: no record", ""]
            continue
        end = r.get("end") or {}
        out += [f"### {run} start", "",
                f"server {r.get('server')}; initialize {fmt(r.get('init'))}; "
                f"{'settled' if r.get('settled') else 'did not settle'} after {fmt(r.get('settledAfter'))}; "
                f"{'DIED before the end; ' if end.get('diedEarly') else ''}exit {end.get('exitCode')} ({'graceful' if end.get('graceful') else 'not graceful'}); "
                f"peak memory {fmt((r.get('tree') or {}).get('peakRssMB'), ' MB')} (private {fmt((r.get('tree') or {}).get('peakPrivateMB'), ' MB')}), "
                f"CPU {fmt((r.get('tree') or {}).get('cpuCoreSeconds'), ' core-s')}, processes started {(r.get('tree') or {}).get('started')}", "",
                "| file | first diagnostics | first semantic tokens | errors | completion p50 / p95 (right) | hover p50 (right) | definition p50 (right) | while preparing: first answer / first right |",
                "|---|---|---|---|---|---|---|---|"]
        for name, f in sorted(r.get("files", {}).items(), key=lambda kv: (kv[0] not in BENCH, BENCH.index(kv[0]) if kv[0] in BENCH else 0, kv[0])):
            c, h, d, p = f.get("completion") or {}, f.get("hover") or {}, f.get("definition") or {}, f.get("preparing") or {}
            out.append(f"| {VARIANT.get(name, name)} | {fmt(f.get('firstDiagnostics'))} | {fmt(f.get('semanticTokens')) if not isinstance(f.get('semanticTokens'), str) else f['semanticTokens']} | "
                       f"{f.get('errors')} | {fmt(c.get('p50'))} / {fmt(c.get('p95'))} ({c.get('right', 0)}/{c.get('n', 0)}) | "
                       f"{fmt(h.get('p50'))} ({h.get('right', 0)}/{h.get('n', 0)}) | {fmt(d.get('p50'))} ({d.get('right', 0)}/{d.get('n', 0)}) | "
                       f"{fmt(p.get('firstAnswer'))} / {fmt(p.get('firstRight'))} |")
        errs = [(n, f["firstErrors"]) for n, f in r.get("files", {}).items() if f.get("firstErrors")]
        if errs:
            out += ["", "First errors shown:"] + [f"- **{n}**: " + " / ".join(f"`{e[:180]}`" for e in es[:2]) for n, es in errs]
        if run == "cold" and r.get("typing"):
            out += ["", "Typing (a line typed at about 8 characters a second; completion as VS Code asks it):", "",
                    "| file | asked | answered / cancelled / timed out | p50 | p95 | max | member right | diagnostics after the line |", "|---|---|---|---|---|---|---|---|"]
            for name, t in r["typing"].items():
                st = t.get("statuses") or {}
                out.append(f"| {VARIANT.get(name, name)} | {t['asked']} | {st.get('ok', 0)} / {st.get('cancelled', 0)} / {st.get('timeout', 0)} | "
                           f"{fmt(t['p50'])} | {fmt(t['p95'])} | {fmt(t['max'])} | {t['memberRight']} | "
                           f"{', '.join(fmt(x) for x in t['diagnosticsAfterLine'])} |")
        out.append("")
    return out


def job(args):
    base = pathlib.Path(args.inp)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    engine = args.engine
    result = {"engine": engine, "runner": load(base / "runner.json"), "build": load(base / "build" / "summary.json"),
              "cdb": load(base / "cdb-stats.json"), "dumps": load(base / "dumps.json"), "events": load(base / "application-events.json")}
    md = [f"## {engine}", ""]
    build = result["build"] or {}
    md.append(f"runner {result['runner']}; build exit {build.get('exitCode')} in {build.get('seconds')} s; "
              f"bench files with build errors: {[k for k, v in (build.get('bench') or {}).items() if v.get('errors')]}; "
              f"engine database {(result['cdb'] or {}).get('entries')} entries ({(result['cdb'] or {}).get('moduleUnits')} module units)")
    md.append("")
    logs = {}
    if engine == "deep":
        result["deep"] = deep_metrics(base)
        md += deep_markdown(result["deep"])
        for path in sorted((base / "deep" / "check").glob("check-*.log")):
            logs[path.name] = scan_log(path)
    else:
        lsp = base / engine
        for run in ("cold", "warm"):
            result[run] = run_metrics(load(lsp / f"{run}.json"))
            logs[f"{run}-server.log"] = scan_log(lsp / f"{run}-server.log")
        for path in sorted((lsp / "clice-logs").glob("*/*.log")) if (lsp / "clice-logs").exists() else []:
            scanned = scan_log(path, limit=4)
            if scanned and (scanned["counts"]["error"] or scanned["counts"]["crash"] or path.name == "master.log"):
                logs[f"clice/{path.parent.name[-8:]}/{path.name}"] = scanned
        md += lsp_markdown(engine, result)
    dumps = result["dumps"]
    if dumps:
        md += ["", f"Crash dumps: {dumps if isinstance(dumps, list) else [dumps]}"]
    events = result["events"]
    if events:
        events = events if isinstance(events, list) else [events]
        md += ["", f"Application error events: {len(events)}"] + [f"- {e.get('ProviderName')}: {str(e.get('Message'))[:300]}" for e in events[:6]]
    result["logs"] = {k: {kk: vv for kk, vv in v.items() if kk not in ("examples", "head")} for k, v in logs.items() if v}
    (out / f"{engine}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / f"{engine}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    log_md = [f"## {engine}: logs", ""]
    for name, v in logs.items():
        if not v:
            continue
        log_md += [f"### {name} ({v['lines']} lines, {v['bytes'] // 1024} KB)", "", f"counts: {v['counts']}"]
        if v.get("clangdCompletion"):
            log_md.append(f"clangd's slow completions (≥ 200 ms): {v['clangdCompletion']}")
        if v.get("clicePerf"):
            log_md.append(f"clice's own request times: {v['clicePerf']}")
        if v.get("prerequisites"):
            log_md += ["prerequisites:"] + [f"- `{p[:260]}`" for p in v["prerequisites"]]
        for kind, lines in (v.get("examples") or {}).items():
            log_md += [f"{kind} (first distinct):"] + [f"- `{l[:280]}`" for l in lines]
        log_md.append("")
    (out / f"{engine}-logs.md").write_text("\n".join(log_md) + "\n", encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------------------------- the report

def report(args):
    base = pathlib.Path(args.inp)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data = {}
    for path in base.glob("bench-*-digest/*.json"):
        d = load(path)
        if d and d.get("engine"):
            data[d["engine"]] = d
    engines = [e for e in ENGINES if e in data]
    md = ["# Engine bench on GalTranslPP (Windows)", "",
          "Five ways to reach the same libraries (one function body), five of the project's own heavy files; each server with "
          "the same files open, the same requests at the same points, the same typing. Times are seconds; `(a/b)` is answers "
          "that were right of those asked.", ""]
    for e in engines:
        b = data[e].get("build") or {}
        md.append(f"- **{e}**: runner {(data[e].get('runner') or {}).get('cpu')}, build exit {b.get('exitCode')} ({b.get('seconds')} s)")
    md.append("")
    if engines:
        md += ["## Cold start: when each file first shows diagnostics, and errors shown (the project builds: every error is false)", "",
               "| file | " + " | ".join(engines) + " |", "|---|" + "---|" * len(engines)]
        names = BENCH + ["NormalJsonTranslator.Run.cpp", "NormalJsonTranslator.Core.cpp", "NormalJsonTranslator.TransAgent.cpp",
                         "NormalJsonTranslator.ixx", "Tool.ixx"]
        for name in names:
            cells = []
            for e in engines:
                f = ((data[e].get("cold") or {}).get("files") or {}).get(name) or {}
                cells.append(f"{fmt(f.get('firstDiagnostics'))}, {f.get('errors')} err" if f else "—")
            md.append(f"| {VARIANT.get(name, name)} | " + " | ".join(cells) + " |")
        md += ["", "| | " + " | ".join(engines) + " |", "|---|" + "---|" * len(engines)]
        for label, get in (("settled after (cold)", lambda r: fmt(r.get("settledAfter")) + ("" if r.get("settled") else " (not)")),
                           ("settled after (warm)", None),
                           ("peak memory MB (cold)", lambda r: fmt((r.get("tree") or {}).get("peakRssMB"), "")),
                           ("peak private MB (cold)", lambda r: fmt((r.get("tree") or {}).get("peakPrivateMB"), "")),
                           ("CPU core-seconds (cold)", lambda r: fmt((r.get("tree") or {}).get("cpuCoreSeconds"), "")),
                           ("processes started (cold)", lambda r: str((r.get("tree") or {}).get("started"))),
                           ("died early (cold / warm)", None)):
            cells = []
            for e in engines:
                cold, warm = data[e].get("cold") or {}, data[e].get("warm") or {}
                if label.startswith("settled after (warm)"):
                    cells.append(fmt(warm.get("settledAfter")) + ("" if warm.get("settled") else " (not)") if warm else "—")
                elif label.startswith("died"):
                    cells.append(f"{(cold.get('end') or {}).get('diedEarly')} / {(warm.get('end') or {}).get('diedEarly')}")
                else:
                    cells.append(get(cold) if cold else "—")
            md.append(f"| {label} | " + " | ".join(cells) + " |")
        md += ["", "## Once prepared (idle): completion p50 / p95 (right), per file", "",
               "| file | " + " | ".join(engines) + " |", "|---|" + "---|" * len(engines)]
        for name in BENCH + ["NormalJsonTranslator.Run.cpp"]:
            cells = []
            for e in engines:
                c = ((((data[e].get("cold") or {}).get("files") or {}).get(name) or {}).get("completion")) or {}
                cells.append(f"{fmt(c.get('p50'))} / {fmt(c.get('p95'))} ({c.get('right', 0)}/{c.get('n', 0)})" if c else "—")
            md.append(f"| {VARIANT.get(name, name)} | " + " | ".join(cells) + " |")
        md += ["", "Hover and definition p50 (right):", "", "| file | " + " | ".join(engines) + " |", "|---|" + "---|" * len(engines)]
        for name in BENCH + ["NormalJsonTranslator.Run.cpp"]:
            cells = []
            for e in engines:
                f = (((data[e].get("cold") or {}).get("files") or {}).get(name) or {})
                h, d = f.get("hover") or {}, f.get("definition") or {}
                cells.append(f"{fmt(h.get('p50'))} ({h.get('right', 0)}/{h.get('n', 0)}); {fmt(d.get('p50'))} ({d.get('right', 0)}/{d.get('n', 0)})" if f else "—")
            md.append(f"| {VARIANT.get(name, name)} | " + " | ".join(cells) + " |")
        md += ["", "## Warm start (second start, same cache): first diagnostics and completion p50", "",
               "| file | " + " | ".join(engines) + " |", "|---|" + "---|" * len(engines)]
        for name in BENCH + ["NormalJsonTranslator.Run.cpp"]:
            cells = []
            for e in engines:
                f = (((data[e].get("warm") or {}).get("files") or {}).get(name) or {})
                cells.append(f"{fmt(f.get('firstDiagnostics'))}; {fmt((f.get('completion') or {}).get('p50'))}" if f else "—")
            md.append(f"| {VARIANT.get(name, name)} | " + " | ".join(cells) + " |")
    if "deep" in data:
        md += ["", "## Headers against modules, below the editor", ""] + deep_markdown(data["deep"]["deep"])
    (out / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    hvm = ["# Headers against modules, per engine", "",
           "The same body; H and M reach the same three libraries and the standard library, HX and X add the platform "
           "headers NormalJsonTranslator.Run.cpp includes. Idle completion p50 (right) and the cold first diagnostics.", "",
           "| engine | " + " | ".join(VARIANT[b] for b in BENCH) + " |", "|---|" + "---|" * len(BENCH)]
    for e in engines:
        cells = []
        for b in BENCH:
            f = (((data[e].get("cold") or {}).get("files") or {}).get(b) or {})
            c = f.get("completion") or {}
            cells.append(f"{fmt(c.get('p50'))} ({c.get('right', 0)}/{c.get('n', 0)}); diag {fmt(f.get('firstDiagnostics'))}" if f else "—")
        hvm.append(f"| {e} | " + " | ".join(cells) + " |")
    hvm += ["", "Per point (idle p50, right):", ""]
    for e in engines:
        hvm += [f"### {e}", "", "| point | " + " | ".join(BENCH) + " |", "|---|" + "---|" * len(BENCH)]
        points = []
        for b in BENCH:
            for p in ((((data[e].get("cold") or {}).get("files") or {}).get(b) or {}).get("points") or {}):
                if p not in points:
                    points.append(p)
        for p in points:
            cells = []
            for b in BENCH:
                x = (((((data[e].get("cold") or {}).get("files") or {}).get(b) or {}).get("points") or {}).get(p)) or {}
                cells.append(f"{fmt(x.get('p50'))} ({x.get('right', '—')})" if x else "—")
            hvm.append(f"| {p} | " + " | ".join(cells) + " |")
        hvm.append("")
    (out / "header-vs-modules.md").write_text("\n".join(hvm) + "\n", encoding="utf-8")

    typing = ["# Typing", ""]
    for e in engines:
        typing += [f"## {e}", ""] + [l for l in lsp_markdown(e, {"cold": {**(data[e].get("cold") or {}), "files": {}}})
                                     if l.startswith("|") or l.startswith("Typing")]
        typing.append("")
    (out / "typing.md").write_text("\n".join(typing) + "\n", encoding="utf-8")

    stab = ["# Stability", "", "| engine | died early (cold/warm) | exit codes | processes started / exited | request failures (idle) | crash lines in logs | error lines | dumps | error events |",
            "|---|---|---|---|---|---|---|---|---|"]
    for e in list(engines) + (["deep"] if "deep" in data else []):
        d = data[e]
        cold, warm = d.get("cold") or {}, d.get("warm") or {}
        failures = collections.Counter()
        for r in (cold, warm):
            for f in (r.get("files") or {}).values():
                for method in ("completion", "hover", "definition"):
                    failures.update((f.get(method) or {}).get("failed") or {})
        crash = sum((v.get("counts") or {}).get("crash", 0) for v in (d.get("logs") or {}).values())
        errors = sum((v.get("counts") or {}).get("error", 0) for v in (d.get("logs") or {}).values())
        dumps = d.get("dumps")
        events = d.get("events")
        stab.append(f"| {e} | {(cold.get('end') or {}).get('diedEarly')}/{(warm.get('end') or {}).get('diedEarly')} | "
                    f"{(cold.get('end') or {}).get('exitCode')}/{(warm.get('end') or {}).get('exitCode')} | "
                    f"{(cold.get('tree') or {}).get('started')} / {(cold.get('tree') or {}).get('exited')} | {dict(failures)} | {crash} | {errors} | "
                    f"{len(dumps) if isinstance(dumps, list) else (1 if dumps else 0)} | {len(events) if isinstance(events, list) else (1 if events else 0)} |")
    (out / "stability.md").write_text("\n".join(stab) + "\n", encoding="utf-8")

    logs = []
    for path in sorted(base.glob("bench-*-digest/*-logs.md")):
        logs.append(path.read_text(encoding="utf-8"))
    (out / "logs.md").write_text("\n".join(logs), encoding="utf-8")
    for path in sorted(base.glob("bench-*-digest/*.md")):
        if not path.name.endswith("-logs.md"):
            (out / f"engine-{path.name}").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (out / "data.json").write_text(json.dumps({e: {k: v for k, v in d.items() if k != "logs"} for e, d in data.items()},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("job")
    one.add_argument("--engine", required=True)
    one.add_argument("--in", dest="inp", required=True)
    one.add_argument("--out", required=True)
    two = commands.add_parser("report")
    two.add_argument("--in", dest="inp", required=True)
    two.add_argument("--out", required=True)
    args = parser.parse_args()
    return {"job": job, "report": report}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
