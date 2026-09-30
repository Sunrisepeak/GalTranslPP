#!/usr/bin/env python3
"""Editor-like experiments for mcpp-language-server 0.0.6 on GalTranslPP (Windows).

  lab.py diag    diagnostics + latency, cold cache: what clangd says about heavy files, and how long requests take
  lab.py typing  a person typing in NormalJsonTranslator.Run.cpp on a warm cache, with status polled every second
  lab.py warm    restart on the same cache; clangd or the server killed in the middle of preparation
  lab.py hyp     std built as mcppls builds it (with the units' -D), with and without -D_RANGES_
  lab.py cdb     the engine database entries clangd used (std, Run.cpp, ...), and the real build's commands
  lab.py summarize --out DIR

Everything is written as JSON below --out; nothing here is a product, it is evidence.
"""
import argparse
import gzip
import json
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import probe  # noqa: E402

RUN = "GalTranslPP/NormalJsonTranslator.Run.cpp"


def now():
    return time.time()


def write_json(path, value):
    probe.write_json(path, value)


class Lab(probe.Lsp):
    """probe.Lsp that keeps every status, every diagnostics publication and every log line with its time."""

    def __init__(self, argv, cwd, stderr_path, env, t0):
        super().__init__(argv, cwd, stderr_path, env)
        self.t0 = t0
        self.status = None
        self.status_history = []
        self.log_lines = []
        self.diag_times = {}
        self.shown = []

    def _dispatch(self, message):
        method = message.get("method", "")
        params = message.get("params", {}) if isinstance(message.get("params"), dict) else {}
        t = round(now() - self.t0, 2)
        if "id" not in message or "method" in message:
            if method == "cxxModules/status":
                self.status = params
                engines = [(e.get("name"), e.get("state")) for e in params.get("engines") or []]
                self.status_history.append({"t": t, "state": params.get("state"), "progress": params.get("progress"), "engines": engines,
                                            "issues": [i.get("code") for i in params.get("issues") or [] if isinstance(i, dict)]})
            elif method == "textDocument/publishDiagnostics":
                self.diag_times.setdefault(params.get("uri"), []).append({"t": t, "count": len(params.get("diagnostics") or [])})
            elif method == "window/logMessage":
                if len(self.log_lines) < 40000:
                    self.log_lines.append({"t": t, "type": params.get("type"), "message": str(params.get("message", ""))[:800]})
            elif method == "window/showMessage":
                self.shown.append({"t": t, "type": params.get("type"), "message": str(params.get("message", ""))[:800]})
        super()._dispatch(message)

    def state(self):
        return (self.status or {}).get("state")

    def progress(self):
        p = (self.status or {}).get("progress") or {}
        return p.get("done"), p.get("total")


def start(args, cache_dir, out, name="server"):
    root = pathlib.Path(args.root).resolve()
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["MCPPLS_CACHE_DIR"] = cache_dir
    argv = [args.mcppls, "serve", "--payload", args.payload, "--log-level", "debug"]
    t0 = now()
    lab = Lab(argv, str(root), out / f"{name}-stderr.log", env, t0)
    init = lab.request("initialize", {
        "processId": os.getpid(), "rootUri": probe.uri_of(root),
        "workspaceFolders": [{"uri": probe.uri_of(root), "name": root.name}],
        "capabilities": {
            "textDocument": {"hover": {"contentFormat": ["markdown", "plaintext"]},
                             "completion": {"completionItem": {"snippetSupport": True}},
                             "semanticTokens": {"requests": {"full": True}, "tokenTypes": [], "tokenModifiers": [], "formats": ["relative"]},
                             "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                             "publishDiagnostics": {"relatedInformation": True}},
            "workspace": {"configuration": True, "workspaceFolders": True},
            "window": {"workDoneProgress": True},
            "experimental": {"cxxModules": {"version": 1, "status": True, "graph": True, "contexts": True}},
        },
        "clientInfo": {"name": "Visual Studio Code", "version": "1.105.0"},
        # what the VS Code extension sends (editors/vscode/src/extension.ts)
        "initializationOptions": {"compiler": None, "semanticKit": "auto", "engine": "clangd", "toolEnvironment": "auto", "conflictArbitration": "client",
                                  "semanticTokens": {"modules": True, "moduleType": True}, "completion": {"triggerOnSpace": True},
                                  "buildDiscovery": "auto", "buildDiscovery.askBeforeDownload": True, "index.primeImplementationUnits": "auto"},
    }, timeout=600)
    lab.notify("initialized", {})
    lab.init = init
    return lab


def open_file(lab, root, rel):
    path = pathlib.Path(root).resolve() / rel
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    lab.notify("textDocument/didOpen", {"textDocument": {"uri": probe.uri_of(path), "languageId": "cpp", "version": 1, "text": text}})
    return path, text, raw.startswith(b"\xef\xbb\xbf")


def stop(lab, graceful=True):
    if lab.proc.poll() is None and graceful:
        lab.request("shutdown", None, timeout=60)
        lab.notify("exit", None)
        try:
            lab.proc.wait(30)
        except subprocess.TimeoutExpired:
            lab.proc.kill()
    lab.stderr_file.close()


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def stats(rows):
    """p50/p95/max of the seconds of the requests that were answered; failures counted."""
    result = {}
    for kind in sorted({row["kind"] for row in rows}):
        of_kind = [row for row in rows if row["kind"] == kind]
        seconds = [row["seconds"] for row in of_kind if row["status"] == "ok"]
        result[kind] = {"n": len(of_kind), "ok": len(seconds), "failed": len(of_kind) - len(seconds),
                        "p50": pct(seconds, 50), "p95": pct(seconds, 95), "max": max(seconds) if seconds else None,
                        "statuses": {s: sum(1 for row in of_kind if row["status"] == s) for s in sorted({row["status"] for row in of_kind})}}
    return result


def points(text):
    """Where the latency requests ask: after `std::`, after `std::views::`, a member of a json value, a statement start."""
    lines = text.split("\n")
    found = {}
    for number, line in enumerate(lines):
        if "after_std" not in found:
            match = re.search(r"std::views::", line)
            if match:
                found["after_std"] = (number, match.start() + 5)
                found["after_views"] = (number, match.end())
                found["hover_views"] = (number, match.end() + 1)
        if "json_member" not in found:
            match = re.search(r"\b(overview|fileOverview|newItem)\.", line)
            if match:
                found["json_member"] = (number, match.end())
        if "in_body" not in found and re.match(r"\s+for \(const auto& inputPath", line):
            found["in_body"] = (number, len(line) - len(line.lstrip()))
    return found


def timed(lab, kind, method, params, timeout, rows, phase, extra=None):
    started = now()
    answer = lab.request(method, params, timeout=timeout)
    result = answer.get("result")
    count = None
    if isinstance(result, dict) and "items" in result:
        count = len(result["items"])
    elif isinstance(result, list):
        count = len(result)
    elif isinstance(result, dict) and "data" in result:
        count = len(result["data"])
    row = {"t": round(started - lab.t0, 2), "phase": phase, "kind": kind, "status": answer["status"], "seconds": answer["seconds"],
           "count": count, "state": lab.state(), "progress": lab.progress()}
    if answer["status"] == "error":
        row["error"] = str(answer.get("error"))[:300]
    if extra:
        row.update(extra)
    rows.append(row)
    return row


def probe_round(lab, path, text, positions, rows, phase, timeout=60):
    doc = {"uri": probe.uri_of(path)}
    for kind in ("after_std", "after_views", "json_member", "in_body"):
        if kind in positions:
            line, character = positions[kind]
            timed(lab, f"completion:{kind}", "textDocument/completion",
                  {"textDocument": doc, "position": {"line": line, "character": character}, "context": {"triggerKind": 1}}, timeout, rows, phase)
    if "hover_views" in positions:
        line, character = positions["hover_views"]
        timed(lab, "hover", "textDocument/hover", {"textDocument": doc, "position": {"line": line, "character": character}}, timeout, rows, phase)
    timed(lab, "semanticTokens", "textDocument/semanticTokens/full", {"textDocument": doc}, timeout, rows, phase)


def line_of(text, line):
    lines = text.split("\n")
    return lines[line] if 0 <= line < len(lines) else ""


def cache_db_dir(cache_dir):
    found = sorted(pathlib.Path(cache_dir).glob("workspaces/*/contexts/default/cdb"))
    return found[0] if found else None


def modules_dir(cache_dir):
    db = cache_db_dir(cache_dir)
    return db / ".cache" / "clangd" / "modules" if db else None


def pcm_listing(cache_dir):
    base = modules_dir(cache_dir)
    rows = []
    if base and base.exists():
        for path in base.rglob("*"):
            if path.is_file() and (path.suffix in (".pcm", ".lock") or path.name.endswith(".lock")):
                stat = path.stat()
                rows.append({"path": str(path.relative_to(base)).replace("\\", "/"), "bytes": stat.st_size, "mtime": stat.st_mtime})
    return sorted(rows, key=lambda row: row["path"])


# ---------------------------------------------------------------------------------------- diag

def diagnostics_dump(lab, root, files, opened):
    result = {}
    for rel in files:
        path, text, _ = opened[rel]
        uri = probe.uri_of(path)
        items = lab.diagnostics.get(uri)
        lines = text.split("\n")
        entries = []
        for item in items or []:
            start = item.get("range", {}).get("start", {})
            number = start.get("line", 0)
            entries.append({"range": item.get("range"), "severity": item.get("severity"), "code": item.get("code"), "source": item.get("source"),
                            "message": item.get("message"), "line": number + 1, "text": lines[number].strip()[:240] if 0 <= number < len(lines) else "",
                            "related": [r.get("message") for r in item.get("relatedInformation") or []][:4]})
        result[rel] = {"published": items is not None, "count": len(entries), "publications": lab.diag_times.get(uri, [])[-12:], "diagnostics": entries}
    return result


def diag(args):
    out = pathlib.Path(args.out)
    root = pathlib.Path(args.root).resolve()
    lab = start(args, args.cache_dir, out)
    files = args.files
    opened = {rel: open_file(lab, root, rel) for rel in files}
    run_path, run_text, _ = opened[RUN]
    positions = points(run_text)
    write_json(out / "positions.json", {name: {"line": line + 1, "character": ch, "text": line_of(run_text, line).strip()[:200]} for name, (line, ch) in positions.items()})
    rows = []
    started = now()
    last_change = started
    last_signature = None
    round_at = 0
    settled_at = None
    while now() - started < args.max_wait and not lab.closed.is_set():
        time.sleep(1)
        t = now() - started
        signature = (lab.state(), lab.progress(), lab.diagnostic_updates, len(lab.status_history))
        if signature != last_signature:
            last_signature, last_change = signature, now()
        if t >= round_at and lab.state() in ("preparing", "starting", "loading", None, "degraded", "ready") and settled_at is None:
            probe_round(lab, run_path, run_text, positions, rows, "preparing" if lab.state() in ("preparing", "starting", "loading", None) else lab.state() + "-before-settle", timeout=90)
            round_at = (now() - started) + args.round_gap
        all_diag = all(probe.uri_of(opened[rel][0]) in lab.diagnostics for rel in files)
        if lab.state() in ("ready", "degraded", "error") and all_diag and now() - last_change > args.quiet:
            settled_at = t
            break
    write_json(out / "settle.json", {"settledAfter": settled_at, "state": lab.state(), "progress": lab.progress(), "closed": lab.closed.is_set(),
                                    "exit": lab.proc.poll(), "diagnosed": {rel: probe.uri_of(opened[rel][0]) in lab.diagnostics for rel in files}})
    # idle phase
    for _ in range(args.idle_rounds):
        if lab.closed.is_set():
            break
        probe_round(lab, run_path, run_text, positions, rows, "idle")
        time.sleep(3)
    write_json(out / "diagnostics.json", diagnostics_dump(lab, root, files, opened))
    write_json(out / "latency.json", rows)
    by_phase = {phase: stats([row for row in rows if row["phase"] == phase]) for phase in sorted({row["phase"] for row in rows})}
    write_json(out / "latency-stats.json", by_phase)
    if not lab.closed.is_set():
        answer = lab.request("cxxModules/report", {}, timeout=180)
        write_json(out / "report.json", answer.get("result") if answer["status"] == "ok" else answer)
    write_json(out / "status-history.json", lab.status_history)
    write_json(out / "log-lines.json", lab.log_lines)
    write_json(out / "shown.json", lab.shown)
    write_json(out / "pcm-listing.json", pcm_listing(args.cache_dir))
    stop(lab)
    text = (out / "server-stderr.log").read_text(encoding="utf-8", errors="replace")
    counts, codes, excerpts = probe.scan_text(text)
    write_json(out / "summary.json", {"kind": "diag", "settledAfter": settled_at, "state": lab.state(), "seconds": round(now() - started, 1),
                                     "stderr": counts, "latency": by_phase,
                                     "diagnosticCounts": {rel: d["count"] for rel, d in json.loads((out / "diagnostics.json").read_text(encoding="utf-8")).items()}})
    return 0


# ---------------------------------------------------------------------------------------- typing

class Editor:
    def __init__(self, lab, path, text, bom, autosave, sync_kind):
        self.lab, self.path, self.bom, self.autosave = lab, path, bom, autosave
        self.lines = text.split("\n")
        self.version = 1
        self.uri = probe.uri_of(path)
        self.last_edit = 0.0
        self.dirty = False
        self.line = 0
        self.col = 0
        self.sync_kind = sync_kind
        self.saves = 0

    def text(self):
        return "\n".join(self.lines)

    def _send(self, changes):
        self.version += 1
        self.lab.notify("textDocument/didChange", {"textDocument": {"uri": self.uri, "version": self.version}, "contentChanges": changes})
        self.last_edit = now()
        self.dirty = True

    def insert(self, ch):
        if self.sync_kind == 2:
            change = [{"range": {"start": {"line": self.line, "character": self.col}, "end": {"line": self.line, "character": self.col}}, "text": ch}]
        if ch == "\n":
            rest = self.lines[self.line][self.col:]
            self.lines[self.line] = self.lines[self.line][:self.col]
            self.lines.insert(self.line + 1, rest)
            self.line, self.col = self.line + 1, 0
        else:
            row = self.lines[self.line]
            self.lines[self.line] = row[:self.col] + ch + row[self.col:]
            self.col += 1
        self._send(change if self.sync_kind == 2 else [{"text": self.text()}])

    def backspace(self):
        if self.col == 0:
            return
        if self.sync_kind == 2:
            change = [{"range": {"start": {"line": self.line, "character": self.col - 1}, "end": {"line": self.line, "character": self.col}}, "text": ""}]
        row = self.lines[self.line]
        self.lines[self.line] = row[:self.col - 1] + row[self.col:]
        self.col -= 1
        self._send(change if self.sync_kind == 2 else [{"text": self.text()}])

    def goto(self, line, col):
        self.line, self.col = line, col

    def clear(self, first, last):
        """Remove the text of lines first..last, leaving one empty line at first."""
        end_col = len(self.lines[last])
        change = [{"range": {"start": {"line": first, "character": 0}, "end": {"line": last, "character": end_col}}, "text": ""}]
        self.lines[first:last + 1] = [""]
        self.line, self.col = first, 0
        self._send(change if self.sync_kind == 2 else [{"text": self.text()}])

    def delete_line(self, number):
        change = [{"range": {"start": {"line": number, "character": 0}, "end": {"line": number + 1, "character": 0}}, "text": ""}]
        del self.lines[number]
        self.line, self.col = number, 0
        self._send(change if self.sync_kind == 2 else [{"text": self.text()}])

    def save_to_disk(self):
        data = self.text().encode("utf-8")
        self.path.write_bytes((b"\xef\xbb\xbf" if self.bom else b"") + data)
        self.lab.notify("textDocument/didSave", {"textDocument": {"uri": self.uri}})
        self.dirty = False
        self.saves += 1

    def tick_autosave(self):
        if self.autosave and self.dirty and now() - self.last_edit >= 1.0:
            self.save_to_disk()


def typing(args):
    out = pathlib.Path(args.out)
    root = pathlib.Path(args.root).resolve()
    lab = start(args, args.cache_dir, out)
    caps = ((lab.init.get("result") or {}).get("capabilities") or {})
    sync = caps.get("textDocumentSync")
    sync_kind = sync.get("change", 2) if isinstance(sync, dict) else (sync if isinstance(sync, int) else 2)
    opened = {rel: open_file(lab, root, rel) for rel in [RUN, *args.also]}
    path, text, bom = opened[RUN]
    original_bytes = path.read_bytes()
    editor = Editor(lab, path, text, bom, args.autosave, sync_kind)
    lines = text.split("\n")
    anchor = next(i for i, line in enumerate(lines) if "std::vector<fs::path> relFilePaths" in line) + 1
    import_line = next(i for i, line in enumerate(lines) if line.strip() == "import Tool;") + 1
    rows = []
    poll = []
    reports = []
    started = now()
    lab.t0 = started
    stop_flag = threading.Event()
    workers = []

    def poller():
        last = 0
        while not stop_flag.is_set():
            time.sleep(1)
            t = round(now() - started, 1)
            poll.append({"t": t, "state": lab.state(), "progress": lab.progress(), "alive": lab.proc.poll() is None})
            if now() - last >= args.report_every and not lab.closed.is_set():
                last = now()
                answer = lab.request("cxxModules/report", {}, timeout=30)
                result = answer.get("result") if answer["status"] == "ok" else None
                subset = {"t": t, "answer": answer["status"], "seconds": answer["seconds"]}
                if isinstance(result, dict):
                    for root_report in result.get("roots") or []:
                        for engine in (root_report.get("engines") or []):
                            details = engine.get("details") or {}
                            subset.setdefault("engines", []).append({"name": engine.get("name"), "state": engine.get("state"), "recentExits": details.get("recentExits"),
                                                                    "restarts": details.get("restarts"), "filesSetAside": details.get("filesSetAside")})
                reports.append(subset)

    threading.Thread(target=poller, daemon=True).start()

    def complete(kind):
        line, col = editor.line, editor.col
        def work():
            timed(lab, f"completion:{kind}", "textDocument/completion",
                  {"textDocument": {"uri": editor.uri}, "position": {"line": line, "character": col}, "context": {"triggerKind": 1}}, args.request_timeout, rows, "typing",
                  {"version": editor.version, "line": line + 1, "col": col})
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        workers.append(thread)

    def sleep(seconds):
        end = now() + seconds
        while now() < end:
            editor.tick_autosave()
            time.sleep(0.05)

    def typ(s, cadence=0.1):
        for ch in s:
            if ch == "\x01":
                complete("typed")
                continue
            editor.insert(ch)
            editor.tick_autosave()
            time.sleep(cadence)

    def erase(n, cadence=0.05):
        for _ in range(n):
            editor.backspace()
            time.sleep(cadence)

    # A blank line to type in, inside buildProblemOverviewFromCache.
    editor.goto(anchor, 0)
    editor.insert("\n")
    cycle = 0
    while now() - started < args.duration and not lab.closed.is_set():
        cycle += 1
        editor.goto(anchor, 0)
        # 1. ordinary typing with std::views and completion at the dots
        typ("    auto sq = std::\x01views::\x01iota(0, 10) | std::views::transform([](int i) { return i * i; });")
        sleep(1.5)
        erase(len(editor.lines[anchor]))
        # 2. half-typed identifiers
        typ("    relFilePaths.\x01re")
        sleep(1.2)
        erase(len(editor.lines[anchor]))
        typ("    savedTranslCacheMap.\x01fi")
        sleep(1.2)
        erase(len(editor.lines[anchor]))
        # 3. an unclosed brace that stays open for a while
        typ("    if (relFilePaths.empty()) {")
        editor.insert("\n")
        typ("        return {}\x01")
        sleep(8)
        typ(";")
        editor.insert("\n")
        typ("    }")
        sleep(1)
        editor.clear(anchor, anchor + 2)
        # 4. a json brace initializer
        typ('    json j{ {"a", 1}, {"b", json::array({1, 2})} };')
        editor.insert("\n")
        typ("    j.\x01")
        sleep(1.5)
        editor.clear(anchor, anchor + 1)
        # 5. a half-typed import, saved half-typed when autosave is on
        editor.goto(import_line, 0)
        editor.insert("\n")
        editor.goto(import_line, 0)
        typ("import \x01")
        typ("NormalJson\x01")
        sleep(3)
        erase(len(editor.lines[import_line]))
        typ("Tool.")
        sleep(4)
        erase(len(editor.lines[import_line]))
        typ("Nor")
        sleep(2)
        erase(len(editor.lines[import_line]))
        editor.delete_line(import_line)
        sleep(2)
        # hover and a break, as a person reads
        timed(lab, "hover", "textDocument/hover", {"textDocument": {"uri": editor.uri}, "position": {"line": anchor - 1, "character": 20}}, args.request_timeout, rows, "typing")
        sleep(3)
    for thread in workers:
        thread.join(timeout=args.request_timeout + 5)
    stop_flag.set()
    # a last report, then put the file back
    final = None
    if not lab.closed.is_set():
        answer = lab.request("cxxModules/report", {}, timeout=180)
        final = answer.get("result") if answer["status"] == "ok" else answer
    path.write_bytes(original_bytes)
    write_json(out / "report.json", final)
    write_json(out / "latency.json", rows)
    write_json(out / "latency-stats.json", stats(rows))
    write_json(out / "poll.json", poll)
    write_json(out / "reports.json", reports)
    write_json(out / "status-history.json", lab.status_history)
    write_json(out / "log-lines.json", lab.log_lines)
    write_json(out / "shown.json", lab.shown)
    write_json(out / "diagnostics-final.json", {uri: [d.get("message") for d in items] for uri, items in lab.diagnostics.items()})
    alive = lab.proc.poll() is None
    stop(lab)
    text_log = (out / "server-stderr.log").read_text(encoding="utf-8", errors="replace")
    counts, codes, excerpts = probe.scan_text(text_log)
    states = {}
    for row in poll:
        states[row["state"]] = states.get(row["state"], 0) + 1
    stalls = [row for row in rows if row["status"] != "ok"]
    write_json(out / "summary.json", {"kind": "typing", "autosave": args.autosave, "seconds": round(now() - started, 1), "cycles": cycle, "saves": editor.saves,
                                     "serverAlive": alive, "stateSeconds": states, "stderr": counts, "latency": stats(rows),
                                     "failedRequests": len(stalls), "statusChanges": len(lab.status_history)})
    return 0


# ---------------------------------------------------------------------------------------- warm

def robocopy(src, dst):
    if pathlib.Path(dst).exists():
        shutil.rmtree(dst, ignore_errors=True)
    if os.name == "nt":
        subprocess.run(["robocopy", src, dst, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"], capture_output=True)
    else:
        shutil.copytree(src, dst)


def processes(names=("clangd.exe", "mcppls.exe")):
    if os.name != "nt":
        return []
    text = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    rows = []
    for line in text.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].strip('"').lower() in names:
            rows.append({"name": parts[0].strip('"'), "pid": parts[1]})
    return rows


def kill(command):
    if os.name != "nt":
        return
    subprocess.run(command, capture_output=True)


def observe(lab, seconds, label, timeline, stall_seconds=240):
    """Wait for ready with everything prepared; returns how it went. A stall is no progress for stall_seconds."""
    started = now()
    last_progress, last_change = None, now()
    while now() - started < seconds:
        if lab.closed.is_set():
            return {"label": label, "outcome": "server-exited", "seconds": round(now() - started, 1)}
        time.sleep(1)
        progress = (lab.state(), lab.progress())
        if progress != last_progress:
            last_progress, last_change = progress, now()
            timeline.append({"t": round(now() - lab.t0, 1), "label": label, "state": lab.state(), "progress": lab.progress()})
        done, total = lab.progress()
        if lab.state() in ("ready", "degraded") and (not total or done == total):
            return {"label": label, "outcome": "ready", "seconds": round(now() - started, 1), "state": lab.state()}
        if now() - last_change > stall_seconds:
            return {"label": label, "outcome": "stalled", "seconds": round(now() - started, 1), "state": lab.state(), "progress": lab.progress()}
    return {"label": label, "outcome": "timeout", "seconds": round(now() - started, 1), "state": lab.state(), "progress": lab.progress()}


def warm(args):
    out = pathlib.Path(args.out)
    root = pathlib.Path(args.root).resolve()
    work = args.work_dir
    result = {"scenarios": {}}
    timeline = []
    for mode in args.modes:
        name = mode
        cache = f"{work}/w-{name}"
        sdir = out / name
        sdir.mkdir(parents=True, exist_ok=True)
        if mode == "restart-clean":
            robocopy(args.cache_dir, cache)   # a warm cache and nothing removed: how long does the next start take?
        else:
            robocopy(args.cache_dir, cache)
            base = modules_dir(cache)
            if base and base.exists():
                shutil.rmtree(base, ignore_errors=True)   # the next start prepares every module again
        scenario = {"mode": mode, "steps": []}
        lab = start(args, cache, sdir, "server1")
        open_file(lab, root, RUN)
        started = now()
        scenario["initialize"] = lab.init["status"]
        if mode == "restart-clean":
            first = observe(lab, args.wait, "warm-start", timeline)
            scenario["steps"].append(first)
            # diagnostics for Run.cpp arrived?
            scenario["runDiagnosed"] = probe.uri_of(root / RUN) in lab.diagnostics
            stop(lab)
            scenario["poll"] = lab.status_history[-40:]
            result["scenarios"][name] = scenario
            continue
        # let preparation get going
        begun = now()
        while now() - begun < 240 and not (lab.state() == "preparing" and (lab.progress()[0] or 0) >= args.kill_after):
            time.sleep(0.5)
        scenario["killedAt"] = {"t": round(now() - started, 1), "state": lab.state(), "progress": lab.progress(), "processes": processes()}
        scenario["locksBeforeKill"] = [row for row in pcm_listing(cache) if row["path"].endswith(".lock")]
        server_pid = lab.proc.pid
        if mode == "kill-clangd":
            kill(["taskkill", "/F", "/IM", "clangd.exe"])
            time.sleep(2)
            scenario["afterKill"] = processes()
            scenario["locksAfterKill"] = [row for row in pcm_listing(cache) if row["path"].endswith(".lock")]
            scenario["steps"].append(observe(lab, args.wait, "same-session-after-clangd-kill", timeline, args.stall))
            scenario["stateAfter"] = lab.state()
            current = lab
        else:
            if mode == "graceful":
                stop(lab)
            elif mode == "kill-server":
                kill(["taskkill", "/F", "/PID", str(server_pid)])
            else:   # kill-tree
                kill(["taskkill", "/F", "/T", "/PID", str(server_pid)])
            time.sleep(3)
            scenario["afterKill"] = processes()
            scenario["locksAfterKill"] = [row for row in pcm_listing(cache) if row["path"].endswith(".lock")]
            scenario["pcmAfterKill"] = len([row for row in pcm_listing(cache) if row["path"].endswith(".pcm")])
            write_json(sdir / "pcm-after-kill.json", pcm_listing(cache))
            lab.stderr_file.close()
            current = start(args, cache, sdir, "server2")
            open_file(current, root, RUN)
            scenario["steps"].append(observe(current, args.wait, "second-start", timeline, args.stall))
        # the ladder: what unblocks it
        last = scenario["steps"][-1]
        if last["outcome"] != "ready":
            locks = [row for row in pcm_listing(cache) if row["path"].endswith(".lock")]
            scenario["locksAtStall"] = locks
            write_json(sdir / "pcm-at-stall.json", pcm_listing(cache))
            scenario["processesAtStall"] = processes()
            base = modules_dir(cache)
            for row in locks:
                try:
                    (base / row["path"]).unlink()
                except OSError as error:
                    scenario.setdefault("lockDeleteErrors", []).append(f"{row['path']}: {error}")
            scenario["steps"].append(observe(current, args.wait, "after-deleting-lock-files", timeline, args.stall))
            if scenario["steps"][-1]["outcome"] != "ready":
                stop(current)
                kill(["taskkill", "/F", "/IM", "clangd.exe"])
                time.sleep(2)
                shutil.rmtree(modules_dir(cache), ignore_errors=True)
                third = start(args, cache, sdir, "server3")
                open_file(third, root, RUN)
                scenario["steps"].append(observe(third, args.wait, "after-deleting-modules-and-restart", timeline, args.stall))
                stop(third)
            else:
                stop(current)
        else:
            stop(current)
        kill(["taskkill", "/F", "/IM", "clangd.exe"])
        result["scenarios"][name] = scenario
        write_json(sdir / "scenario.json", scenario)
        logs = sorted(pathlib.Path(cache).glob("logs/*"))
        if logs:
            shutil.copy(logs[-1], sdir / "last-server-log.txt")
    write_json(out / "timeline.json", timeline)
    write_json(out / "summary.json", {"kind": "warm", **{k: v for k, v in result.items()}})
    return 0


# ---------------------------------------------------------------------------------------- cdb, hyp

def load_entries(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def cdb_command(args):
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    entries = load_entries(args.cdb / "compile_commands.json" if isinstance(args.cdb, pathlib.Path) else pathlib.Path(args.cdb) / "compile_commands.json")
    def arguments(entry):
        return entry.get("arguments") or entry.get("command", "").split()
    interesting = []
    std_entries = []
    for entry in entries:
        file = entry["file"].replace("\\", "/")
        name = file.rsplit("/", 1)[-1]
        if name in ("std.ixx", "std.compat.ixx", "std.cppm", "std.compat.cppm") or "/std.cpp" in file or re.search(r"/\d+-std(\.compat)?\.cpp$", file):
            std_entries.append(entry)
        if file.endswith(("NormalJsonTranslator.Run.cpp", "NormalJsonTranslator.TransAgent.cpp", "NormalJsonTranslator.ixx")):
            interesting.append(entry)
    def flags(entry):
        items = arguments(entry)
        return {"defines": [a for a in items if a.startswith("-D")], "std": [a for a in items if a.startswith("-std=")],
                "flto": any(a.startswith("-flto") for a in items), "noAligned": "-fno-aligned-allocation" in items,
                "ms": [a for a in items if a.startswith("-fms-") or a.startswith("--target")], "driver": items[0] if items else None,
                "fromKit": any("kit" in a.replace("\\", "/").lower() and "libc++" in a.lower() for a in items),
                "isystem": [items[i + 1] for i, a in enumerate(items[:-1]) if a == "-isystem"][:6]}
    write_json(out / "std-entries.json", [{"file": e["file"], "flags": flags(e), "arguments": arguments(e)} for e in std_entries])
    write_json(out / "unit-entries.json", [{"file": e["file"], "flags": flags(e), "arguments": arguments(e)} for e in interesting])
    print(json.dumps({"stdEntries": len(std_entries), "units": len(interesting), "stdFlags": [flags(e) for e in std_entries][:2]}, indent=1)[:3000])
    return 0


def ninja_extract(args):
    """The real build's own commands for std and Run.cpp, from the build.ninja files mcpp wrote."""
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    found = []
    for path in pathlib.Path(args.target).rglob("build.ninja"):
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        hits = []
        for i, line in enumerate(lines):
            if re.search(r"(std\.ixx|std\.compat\.ixx|NormalJsonTranslator\.Run|NormalJsonTranslator\.ixx|std\.pcm|std\.cppm)", line):
                hits.append("\n".join(lines[i:i + 6]))
        if hits:
            found.append({"file": str(path), "bytes": len(text), "hits": hits[:12]})
        with gzip.open(out / (path.parent.name + "-" + path.name + ".gz"), "wt", encoding="utf-8") as handle:
            handle.write(text)
    write_json(out / "ninja-hits.json", found)
    print(json.dumps([{"file": f["file"], "hits": len(f["hits"])} for f in found]))
    return 0


def hyp(args):
    """std.ixx built as mcppls builds it, with and without -D_RANGES_, by the LLVM the project builds with."""
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    clang = args.clang
    entries = load_entries(pathlib.Path(args.cdb) / "compile_commands.json")
    std = next(e for e in entries if e["file"].replace("\\", "/").endswith("/modules/std.ixx"))
    original = std.get("arguments") or std.get("command", "").split()
    result = {"clang": clang, "stdCommand": original, "stdCommandDefines": [a for a in original if a.startswith("-D")], "cases": {}}
    test = out / "views.cpp"
    test.write_text("""import std;
int main() {
    std::map<int, int> m{ {1, 2} };
    auto keys = m | std::views::keys | std::ranges::to<std::vector>();
    auto z = std::views::zip(keys, keys);
    return static_cast<int>(keys.size());
}
""", encoding="utf-8")
    def run(label, defines, std_flag, keep=False):
        case = {}
        pcm = out / f"std-{label}.pcm".replace("=", "").replace("+", "p")
        base = [a for a in original[1:] if not a.startswith("-fmodule-output=") and a != "-c" and not a.startswith("-std=") and (keep or a != "-D_RANGES_")]
        if keep:
            std_flag = next((a for a in original if a.startswith("-std=")), std_flag)
        source = base[-1]
        base = base[:-1]
        command = [clang] + base + [std_flag] + [f"-D{d}" for d in defines] + ["--precompile", "-o", str(pcm), source]
        started = time.time()
        done = subprocess.run(command, capture_output=True, text=True, cwd=args.root)
        case["std"] = {"exit": done.returncode, "seconds": round(time.time() - started, 1), "stderr": done.stderr[-2500:], "pcmBytes": pcm.stat().st_size if pcm.exists() else None}
        if pcm.exists():
            plain = []
            skip = False
            for a in base:
                if skip:
                    skip = False
                    continue
                if a == "-x":
                    skip = True
                    continue
                plain.append(a)
            test_command = [clang] + plain + [std_flag] + [f"-D{d}" for d in defines] + [f"-fmodule-file=std={pcm}", "-fsyntax-only", str(test)]
            done = subprocess.run(test_command, capture_output=True, text=True, cwd=args.root)
            case["test"] = {"exit": done.returncode, "stderr": done.stderr[-2500:]}
        result["cases"][label] = case
    run("as-is", [], "-std=c++23", keep=True)
    for standard in ("-std=c++23", "-std=c++26"):
        run(f"{standard}-plain", [], standard)
        run(f"{standard}-with-RANGES", ["_RANGES_"], standard)
    write_json(out / "hyp.json", result)
    print(json.dumps(result, indent=1)[:5000])
    return 0


# ---------------------------------------------------------------------------------------- summary

def summarize(args):
    out = pathlib.Path(args.out)
    lines = ["## mcppls 0.0.6 on GalTranslPP: the lab", ""]
    for path in sorted(out.glob("*/summary.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("kind") not in ("diag", "typing", "warm"):
            continue
        lines += [f"### {path.parent.name} ({data['kind']})", "", "```", json.dumps({k: v for k, v in data.items() if k not in ('scenarios',)}, ensure_ascii=False, indent=1)[:3500], "```", ""]
        if data["kind"] == "warm":
            for name, scenario in data["scenarios"].items():
                lines.append(f"- **{name}**: " + "; ".join(f"{s['label']} -> {s['outcome']} ({s['seconds']} s)" for s in scenario.get("steps", [])) + f"; locks after kill {len(scenario.get('locksAfterKill', []))}")
        diag_path = path.parent / "diagnostics.json"
        if diag_path.exists():
            diags = json.loads(diag_path.read_text(encoding="utf-8"))
            for rel, entry in diags.items():
                lines.append(f"**{rel}**: {entry['count']} diagnostics")
                for d in entry["diagnostics"][:25]:
                    lines.append(f"- L{d['line']} [{d.get('source')}/{d.get('code')}] {str(d['message'])[:200]}  `{d['text'][:120]}`")
            lines.append("")
    for name in ("hyp", "cdb-e1"):
        for path in sorted(out.glob(f"{name}/*.json")):
            lines.append(f"**{path.parent.name}/{path.name}**: {path.stat().st_size} bytes")
    hyp_path = out / "hyp" / "hyp.json"
    if hyp_path.exists():
        lines += ["", "### hypothesis: -D_RANGES_", "", "```", hyp_path.read_text(encoding="utf-8")[:4000], "```"]
    print("\n".join(lines))
    return 0


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    def common(p, server=True):
        p.add_argument("--root", required=True)
        p.add_argument("--out", required=True)
        if server:
            p.add_argument("--mcppls")
            p.add_argument("--payload")
            p.add_argument("--cache-dir")
    one = commands.add_parser("diag")
    common(one)
    one.add_argument("--files", nargs="+", required=True)
    one.add_argument("--max-wait", type=int, default=2400)
    one.add_argument("--quiet", type=int, default=45)
    one.add_argument("--round-gap", type=int, default=60)
    one.add_argument("--idle-rounds", type=int, default=6)
    one.set_defaults(run=diag)
    two = commands.add_parser("typing")
    common(two)
    two.add_argument("--duration", type=int, default=180)
    two.add_argument("--autosave", action="store_true")
    two.add_argument("--also", nargs="*", default=[])
    two.add_argument("--request-timeout", type=int, default=60)
    two.add_argument("--report-every", type=int, default=20)
    two.set_defaults(run=typing)
    three = commands.add_parser("warm")
    common(three)
    three.add_argument("--work-dir", required=True)
    three.add_argument("--modes", nargs="+", default=["restart-clean", "kill-clangd", "kill-server", "kill-tree", "graceful"])
    three.add_argument("--wait", type=int, default=600)
    three.add_argument("--stall", type=int, default=240)
    three.add_argument("--kill-after", type=int, default=3)
    three.set_defaults(run=warm)
    four = commands.add_parser("cdb")
    four.add_argument("--cdb", required=True)
    four.add_argument("--out", required=True)
    four.set_defaults(run=cdb_command)
    five = commands.add_parser("ninja")
    five.add_argument("--target", required=True)
    five.add_argument("--out", required=True)
    five.set_defaults(run=ninja_extract)
    six = commands.add_parser("hyp")
    six.add_argument("--cdb", required=True)
    six.add_argument("--clang", required=True)
    six.add_argument("--root", required=True)
    six.add_argument("--out", required=True)
    six.set_defaults(run=hyp)
    eight = commands.add_parser("copy")
    eight.add_argument("--src", required=True)
    eight.add_argument("--dst", required=True)
    eight.set_defaults(run=lambda a: (robocopy(a.src, a.dst), 0)[1])
    seven = commands.add_parser("summarize")
    seven.add_argument("--out", required=True)
    seven.set_defaults(run=summarize)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
