#!/usr/bin/env python3
"""Temporary probe for Sunrisepeak/mcpp-language-server#23 on a real Windows checkout of this project.

Drives mcppls (or the clangd it bundles) over LSP the way an editor does and records what the issue
needs: whether clangd exited and why, what it printed, what the modules resolved to.

  probe.py session   --server mcppls|clangd ...   an editor session; summary.json, report.json, stderr.log
  probe.py check     --clangd ... --cdb DIR FILE   clangd --check on one file with a given database
  probe.py cdb-stats --cdb DIR                     the shape of a compile_commands.json
  probe.py add-c     --cdb DIR --out DIR           the same database with -c before each source
  probe.py summarize --out DIR                     a Markdown summary of every result below DIR
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

CRASH_MARKERS = {
    "please_submit": re.compile(r"PLEASE submit a bug report"),
    "stack_dump": re.compile(r"Stack dump:"),
    "exception_code": re.compile(r"Exception Code: *0x[0-9A-Fa-f]+"),
    "signalled_while": re.compile(r"Signalled (while|during)"),
}
SIGNALS = {
    "scan_failed": re.compile(r"Scanning modules dependencies for .* failed"),
    "lto_requires_lld": re.compile(r"LTO requires -fuse-ld=lld"),
    "failed_to_build_module": re.compile(r"Failed to build module"),
    "clangd_exited": re.compile(r"clangd exited unexpectedly"),
    "restarting_clangd": re.compile(r"restarting clangd"),
}


def uri_of(path):
    return pathlib.Path(path).resolve().as_uri()


def write_json(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, ensure_ascii=False), encoding="utf-8")


def crash_files(text):
    """clangd names the file it was working on when it crashed (its crash context)."""
    files = re.findall(r"Signalled (?:during|while)[^\n]*\n[^\n]*Filename: *([^\n]+)", text)
    return sorted({item.strip() for item in files})


def scan_reasons(text):
    """Why module scanning failed, grouped: the first diagnostic line after each failure."""
    reasons = {}
    for match in re.finditer(r"Scanning modules dependencies for (\S+) failed: ([^\n]*)\n?([^\n]*)", text):
        reason = (match.group(2).strip() or match.group(3).strip())
        reason = re.sub(r"^.*?(fatal error|error): ", r"\1: ", reason)[:140]
        reasons[reason] = reasons.get(reason, 0) + 1
    return dict(sorted(reasons.items(), key=lambda item: -item[1])[:8])


def scan_text(text):
    counts = {name: len(pattern.findall(text)) for name, pattern in {**SIGNALS, **CRASH_MARKERS}.items()}
    codes = sorted(set(re.findall(r"Exception Code: *(0x[0-9A-Fa-f]+)", text)))
    # The lines around each crash, where LLVM names the file and the action (clangd's crash context).
    excerpts = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if CRASH_MARKERS["please_submit"].search(line) or CRASH_MARKERS["exception_code"].search(line):
            excerpts.append("\n".join(lines[max(0, i - 15): i + 40]))
            if len(excerpts) >= 5:
                break
    return counts, codes, excerpts


class Lsp:
    def __init__(self, argv, cwd, stderr_path, env=None):
        self.stderr_file = open(stderr_path, "wb")
        self.proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.stderr_file, env=env)
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.next_id = 0
        self.responses = {}
        self.waiters = {}
        self.diagnostics = {}
        self.diagnostic_updates = 0
        self.server_messages = []
        self.closed = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        out = self.proc.stdout
        try:
            while True:
                headers = {}
                while True:
                    line = out.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        break
                    key, _, value = line.decode("ascii", "replace").partition(":")
                    headers[key.strip().lower()] = value.strip()
                body = out.read(int(headers["content-length"]))
                self._dispatch(json.loads(body))
        except Exception as error:  # a torn stream is an exit, recorded as such
            self.server_messages.append({"reader-error": repr(error)})
        finally:
            self.closed.set()
            with self.lock:
                for waiter in self.waiters.values():
                    waiter.set()

    def _dispatch(self, message):
        if "id" in message and "method" in message:
            result = None
            if message["method"] == "workspace/configuration":
                result = [None] * len(message.get("params", {}).get("items", []))
            self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
            return
        if "id" in message:
            with self.lock:
                self.responses[message["id"]] = message
                waiter = self.waiters.get(message["id"])
            if waiter:
                waiter.set()
            return
        method = message.get("method", "")
        if method == "textDocument/publishDiagnostics":
            self.diagnostics[message["params"]["uri"]] = message["params"]["diagnostics"]
            self.diagnostic_updates += 1
        elif len(self.server_messages) < 2000 and method in ("window/logMessage", "window/showMessage", "cxxModules/status", "$/progress"):
            params = message.get("params", {})
            self.server_messages.append({"t": round(time.time(), 1), "method": method,
                                         "params": params if method != "window/logMessage" else str(params.get("message", ""))[:500]})

    def _send(self, message):
        data = json.dumps(message).encode("utf-8")
        try:
            with self.write_lock:
                self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
                self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def notify(self, method, params):
        return self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params, timeout):
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
            waiter = threading.Event()
            self.waiters[request_id] = waiter
        started = time.time()
        if not self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}):
            return {"status": "send-failed", "seconds": 0}
        waiter.wait(timeout)
        with self.lock:
            self.waiters.pop(request_id, None)
            response = self.responses.pop(request_id, None)
        seconds = round(time.time() - started, 2)
        if response is None:
            return {"status": "closed" if self.closed.is_set() else "timeout", "seconds": seconds}
        if "error" in response:
            return {"status": "error", "error": response["error"], "seconds": seconds}
        return {"status": "ok", "result": response.get("result"), "seconds": seconds}


def hover_position(text):
    """A place worth hovering: a name from an imported module (std:: first), else the module's own name."""
    lines = text.splitlines()
    keywords = {"if", "for", "while", "switch", "return", "sizeof", "catch", "main", "module", "import", "export", "decltype", "alignof"}
    for pattern in (r"\bstd::([A-Za-z_]\w*)", r"\b(?:json|fs)::([A-Za-z_]\w*)", r"\b([A-Za-z_]\w*)\s*\("):
        for number, line in enumerate(lines):
            if line.lstrip().startswith(("#", "//", "import", "export module", "module")):
                continue
            for match in re.finditer(pattern, line):
                if match.group(1) not in keywords:
                    return {"line": number, "character": match.start(1)}
    return {"line": 0, "character": 0}


def hover_text(result):
    if not isinstance(result, dict):
        return ""
    contents = result.get("contents")
    if isinstance(contents, dict):
        return str(contents.get("value", ""))
    if isinstance(contents, list):
        return " ".join(str(item.get("value", item)) if isinstance(item, dict) else str(item) for item in contents)
    return str(contents or "")


def find_values(value, key, found=None):
    found = [] if found is None else found
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key:
                found.append(item)
            find_values(item, key, found)
    elif isinstance(value, list):
        for item in value:
            find_values(item, key, found)
    return found


def report_summary(report):
    engines = []
    for root_report in report.get("roots") or []:
        for engine in (root_report.get("engines") or []) if isinstance(root_report, dict) else []:
            details = engine.get("details") or {}
            engines.append({"name": engine.get("name"), "state": engine.get("state"), "accepting": engine.get("accepting"),
                            "recentExits": details.get("recentExits"), "restarts": details.get("restarts"), "modulesThatDidNotCompile": details.get("modulesThatDidNotCompile"),
                            "unresolvedModules": details.get("unresolvedModules"), "engineEvents": [event for event in (root_report.get("events") or []) if isinstance(event, dict) and str(event.get("kind", event.get("name", ""))).startswith("engine-")][-30:],
                            "filesSetAside": details.get("filesSetAside"),
                            "issues": [issue.get("code") for issue in engine.get("issues") or [] if isinstance(issue, dict)]})
    roots = [{"trusted": item.get("trusted"), "state": item.get("state"), "project": json.dumps(item.get("project"), ensure_ascii=False)[:600],
              "plan": json.dumps(item.get("plan"), ensure_ascii=False)[:600]} for item in report.get("roots") or [] if isinstance(item, dict)]
    return {
        "roots": roots,
        "engines": engines,
        "recentExits": [engine["recentExits"] for engine in engines if engine["recentExits"] is not None],
        "issueCodes": sorted({code for engine in engines for code in engine["issues"] if code}),
    }


def session(args):
    root = pathlib.Path(args.root).resolve()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if args.cache_dir:
        env["MCPPLS_CACHE_DIR"] = args.cache_dir
    if args.server == "mcppls":
        argv = [args.mcppls, "serve", "--payload", args.payload, "--log-level", "debug"]
    else:
        argv = [args.clangd, "--experimental-modules-support", "--use-dirty-headers", f"--compile-commands-dir={args.cdb}",
                "--background-index", "--header-insertion=never", "--pretty=false", "--log=verbose", f"-j={args.workers}"]
    files = [root / name for name in args.open]
    started = time.time()
    lsp = Lsp(argv, str(root), out / "stderr.log", env)
    initialize = lsp.request("initialize", {
        "processId": os.getpid(),
        "rootUri": uri_of(root),
        "workspaceFolders": [{"uri": uri_of(root), "name": root.name}],
        "capabilities": {
            "textDocument": {"hover": {"contentFormat": ["markdown", "plaintext"]},
                             "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                             "publishDiagnostics": {"relatedInformation": True}},
            "workspace": {"configuration": True, "workspaceFolders": True},
            "window": {"workDoneProgress": False},
        },
        "clientInfo": {"name": "mcppls-issue23-probe"},
    }, timeout=600)
    lsp.notify("initialized", {})
    texts = {}
    for path in files:
        # VS Code hands the text without the byte order mark.
        texts[path] = path.read_text(encoding="utf-8-sig", errors="replace")
        lsp.notify("textDocument/didOpen", {"textDocument": {"uri": uri_of(path), "languageId": "cpp", "version": 1, "text": texts[path]}})
    timeline = []
    deadline = started + args.duration
    rounds = 0
    while time.time() < deadline and not lsp.closed.is_set():
        time.sleep(args.interval)
        rounds += 1
        for path in files:
            if lsp.closed.is_set():
                break
            position = hover_position(texts[path])
            document = {"uri": uri_of(path)}
            hover = lsp.request("textDocument/hover", {"textDocument": document, "position": position}, timeout=args.request_timeout)
            symbols = lsp.request("textDocument/documentSymbol", {"textDocument": document}, timeout=args.request_timeout)
            timeline.append({
                "t": round(time.time() - started, 1), "file": path.name, "position": position,
                "hover": hover["status"], "hoverSeconds": hover["seconds"], "hoverText": hover_text(hover.get("result"))[:160],
                "symbols": symbols["status"], "symbolCount": len(symbols.get("result") or []) if symbols["status"] == "ok" else None,
                "alive": lsp.proc.poll() is None,
            })
    report = None
    if args.server == "mcppls" and not lsp.closed.is_set():
        answered = lsp.request("cxxModules/report", {}, timeout=180)
        report = answered.get("result") if answered["status"] == "ok" else answered
        write_json(out / "report.json", report)
    exited_during_session = lsp.proc.poll() is not None
    exit_code = lsp.proc.poll()
    if not exited_during_session:
        lsp.request("shutdown", None, timeout=30)
        lsp.notify("exit", None)
        try:
            lsp.proc.wait(20)
        except subprocess.TimeoutExpired:
            lsp.proc.kill()
    lsp.stderr_file.close()
    stderr = (out / "stderr.log").read_text(encoding="utf-8", errors="replace")
    counts, codes, excerpts = scan_text(stderr)
    diagnostics = {pathlib.Path(uri.replace("file:///", "")).name: [d.get("message", "") for d in items] for uri, items in lsp.diagnostics.items()}
    summary = {
        "server": args.server,
        "argv": argv,
        "seconds": round(time.time() - started, 1),
        "initialize": initialize["status"],
        "exitedDuringSession": exited_during_session,
        "exitCode": exit_code,
        "exitCodeHex": f"0x{exit_code & 0xFFFFFFFF:08X}" if isinstance(exit_code, int) else None,
        "rounds": rounds,
        "hoverOk": sum(1 for item in timeline if item["hover"] == "ok" and item["hoverText"]),
        "hoverEmpty": sum(1 for item in timeline if item["hover"] == "ok" and not item["hoverText"]),
        "hoverFailed": sum(1 for item in timeline if item["hover"] != "ok"),
        # clangd says where a name comes from when it is another file's: across modules, only with their BMIs.
        "hoverProvidedBy": sum(1 for item in timeline if "provided by" in item["hoverText"]),
        "stderr": counts,
        "exceptionCodes": codes,
        "crashFiles": crash_files(stderr),
        "scanFailureReasons": scan_reasons(stderr),
        "moduleNotFound": sum(1 for items in diagnostics.values() for message in items if re.search(r"[Mm]odule '[^']+' not found", message)),
        "diagnostics": diagnostics,
    }
    if report is not None and isinstance(report, dict):
        summary["report"] = report_summary(report)
    write_json(out / "summary.json", summary)
    write_json(out / "timeline.json", timeline)
    write_json(out / "server-messages.json", lsp.server_messages)
    (out / "crash-excerpts.txt").write_text("\n\n-----\n\n".join(excerpts), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("server", "exitedDuringSession", "exitCodeHex", "hoverOk", "hoverProvidedBy", "hoverEmpty", "moduleNotFound", "stderr")}, indent=1))
    return 0


def report_command(args):
    """The headless `mcppls report`: its JSON on stdout, the server's log on stderr."""
    out = pathlib.Path(args.out)
    text = pathlib.Path(args.report).read_text(encoding="utf-8", errors="replace")
    stderr = pathlib.Path(args.stderr).read_text(encoding="utf-8", errors="replace") if args.stderr else ""
    counts, codes, excerpts = scan_text(stderr)
    summary = {"exitCode": args.exit_code, "stderr": counts, "exceptionCodes": codes}
    try:
        summary["report"] = report_summary(json.loads(text))
    except ValueError as error:
        summary["reportError"] = f"not JSON: {error}; {text[:300]}"
    write_json(out / "summary.json", summary)
    (out / "crash-excerpts.txt").write_text("\n\n-----\n\n".join(excerpts), encoding="utf-8")
    print(json.dumps(summary, indent=1)[:4000])
    return 0


def emit_command(args):
    """What mcpp said about the project: the producer mcppls runs."""
    text = pathlib.Path(args.json).read_text(encoding="utf-8", errors="replace")
    summary = {"exitCode": args.exit_code, "seconds": args.seconds}
    try:
        database = json.loads(text)
        sets = database.get("sets") or []
        summary.update({"sets": len(sets), "units": sum(len(item.get("units") or []) for item in sets),
                        "diagnostics": [f"{item.get('code')}: {str(item.get('message', ''))[:300]}" for item in database.get("diagnostics") or []]})
    except ValueError as error:
        summary["error"] = f"not JSON: {error}; {text[:300]}"
    write_json(pathlib.Path(args.out) / "summary.json", summary)
    print(json.dumps(summary, indent=1))
    return 0


def log_command(args):
    """A command's combined output, scanned like the others (mcppls check)."""
    out = pathlib.Path(args.out)
    text = pathlib.Path(args.log).read_text(encoding="utf-8", errors="replace")
    counts, codes, excerpts = scan_text(text)
    summary = {"exitCode": args.exit_code, "exitCodeHex": f"0x{args.exit_code & 0xFFFFFFFF:08X}" if args.exit_code is not None else None,
               "stderr": counts, "exceptionCodes": codes, "tail": text.splitlines()[-12:]}
    write_json(out / "summary.json", summary)
    (out / "crash-excerpts.txt").write_text("\n\n-----\n\n".join(excerpts), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


def check(args):
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    argv = [args.clangd, f"--check={args.file}", "--experimental-modules-support", f"--compile-commands-dir={args.cdb}", "--log=verbose"]
    started = time.time()
    try:
        done = subprocess.run(argv, cwd=args.root, capture_output=True, timeout=args.timeout)
        code, output, timed_out = done.returncode, (done.stdout + done.stderr).decode("utf-8", "replace"), False
    except subprocess.TimeoutExpired as expired:
        code, output, timed_out = None, ((expired.stdout or b"") + (expired.stderr or b"")).decode("utf-8", "replace"), True
    (out / "check.log").write_text(output, encoding="utf-8")
    counts, codes, excerpts = scan_text(output)
    summary = {"argv": argv, "seconds": round(time.time() - started, 1), "exitCode": code,
               "exitCodeHex": f"0x{code & 0xFFFFFFFF:08X}" if isinstance(code, int) else None, "timedOut": timed_out,
               "stderr": counts, "exceptionCodes": codes,
               "allChecksLine": next((line for line in output.splitlines() if "All checks completed" in line), None)}
    write_json(out / "summary.json", summary)
    (out / "crash-excerpts.txt").write_text("\n\n-----\n\n".join(excerpts), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


def load_cdb(directory):
    return json.loads((pathlib.Path(directory) / "compile_commands.json").read_text(encoding="utf-8"))


def cdb_stats(args):
    entries = load_cdb(args.cdb)
    arguments = [entry.get("arguments") or entry.get("command", "").split() for entry in entries]
    stats = {
        "entries": len(entries),
        "withFlto": sum(1 for items in arguments if any(item.startswith("-flto") for item in items)),
        "withC": sum(1 for items in arguments if "-c" in items or "/c" in items),
        "withFuseLdLld": sum(1 for items in arguments if any(item.startswith("-fuse-ld=lld") for item in items)),
        "targets": sorted({item for items in arguments for item in items if item.startswith("--target=")}),
        "primeEntries": [entry["file"] for entry in entries if "prime" in entry["file"].replace("\\", "/").split("/")],
        "sample": next((entry for entry in entries if entry["file"].endswith("GPPDefines.ixx")), entries[0] if entries else None),
    }
    write_json(pathlib.Path(args.out) / "cdb-stats.json", stats)
    print(json.dumps({key: value for key, value in stats.items() if key != "sample"}, indent=1))
    return 0


def add_c(args):
    entries = load_cdb(args.cdb)
    changed = 0
    for entry in entries:
        items = entry.get("arguments")
        if items and "-c" not in items:
            items.insert(len(items) - 1, "-c")
            changed += 1
    target = pathlib.Path(args.out)
    target.mkdir(parents=True, exist_ok=True)
    (target / "compile_commands.json").write_text(json.dumps(entries, indent=1), encoding="utf-8")
    print(f"-c added to {changed} of {len(entries)} entries")
    return 0


def summarize(args):
    out = pathlib.Path(args.out)
    lines = ["## mcppls 0.0.4 on GalTranslPP (mcpp-language-server#23)", ""]
    for emit in sorted(out.glob("emit*/summary.json")):
        data = json.loads(emit.read_text(encoding="utf-8"))
        lines += [f"**{emit.parent.name}** (mcpp emit build-database): `{json.dumps(data, ensure_ascii=False)[:1200]}`", ""]
    lines += ["| experiment | process exited | exit code | hover ok (cross-module) / empty / failed | 'not found' diagnostics | scan failed | LTO error | crash markers | report recentExits | issues |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for summary_path in sorted(out.glob("*/summary.json")):
        if summary_path.parent.name.startswith(("emit", "cdb-")):
            continue
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        stderr = data.get("stderr", {})
        crash = sum(stderr.get(name, 0) for name in CRASH_MARKERS)
        report = data.get("report", {})
        hover = f"{data.get('hoverOk', '-')} ({data.get('hoverProvidedBy', '-')}) / {data.get('hoverEmpty', '-')} / {data.get('hoverFailed', '-')}" if "hoverOk" in data else "-"
        exited = data.get("exitedDuringSession", data.get("exitCode") not in (0, None) if "exitCode" in data else "-")
        lines.append(f"| {summary_path.parent.name} | {exited} | {data.get('exitCodeHex')} {' '.join(data.get('exceptionCodes', []))} | {hover} | "
                     f"{data.get('moduleNotFound', '-')} | {stderr.get('scan_failed', 0)} | {stderr.get('lto_requires_lld', 0)} | {crash} | {report.get('recentExits', '-')} | "
                     f"{', '.join(report.get('issueCodes', [])) or '-'} |")
    lines += ["", "**Where clangd crashed, and why scanning failed**", ""]
    for summary_path in sorted(out.glob("*/summary.json")):
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        if data.get("crashFiles") or data.get("scanFailureReasons"):
            lines.append(f"- {summary_path.parent.name}: crashed in {data.get('crashFiles') or '-'}; scan failures {data.get('scanFailureReasons') or '-'}")
    for stats_path in sorted(out.glob("*/cdb-stats.json")):
        data = json.loads(stats_path.read_text(encoding="utf-8"))
        lines += ["", f"**{stats_path.parent.name}**: {data['entries']} entries, {data['withFlto']} with -flto, {data['withC']} with -c, targets {data['targets']}"]
    print("\n".join(lines))
    return 0


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("session")
    one.add_argument("--server", choices=["mcppls", "clangd"], required=True)
    one.add_argument("--root", required=True)
    one.add_argument("--out", required=True)
    one.add_argument("--open", nargs="+", required=True)
    one.add_argument("--duration", type=int, default=600)
    one.add_argument("--interval", type=int, default=20)
    one.add_argument("--request-timeout", type=int, default=60)
    one.add_argument("--mcppls")
    one.add_argument("--payload")
    one.add_argument("--cache-dir")
    one.add_argument("--clangd")
    one.add_argument("--cdb")
    one.add_argument("--workers", type=int, default=2)
    one.set_defaults(run=session)
    two = commands.add_parser("check")
    two.add_argument("--clangd", required=True)
    two.add_argument("--cdb", required=True)
    two.add_argument("--root", required=True)
    two.add_argument("--out", required=True)
    two.add_argument("--timeout", type=int, default=900)
    two.add_argument("file")
    two.set_defaults(run=check)
    three = commands.add_parser("cdb-stats")
    three.add_argument("--cdb", required=True)
    three.add_argument("--out", required=True)
    three.set_defaults(run=cdb_stats)
    four = commands.add_parser("add-c")
    four.add_argument("--cdb", required=True)
    four.add_argument("--out", required=True)
    four.set_defaults(run=add_c)
    six = commands.add_parser("report")
    six.add_argument("--report", required=True)
    six.add_argument("--stderr")
    six.add_argument("--exit-code", type=int)
    six.add_argument("--out", required=True)
    six.set_defaults(run=report_command)
    seven = commands.add_parser("emit")
    seven.add_argument("--json", required=True)
    seven.add_argument("--exit-code", type=int)
    seven.add_argument("--seconds", type=float)
    seven.add_argument("--out", required=True)
    seven.set_defaults(run=emit_command)
    eight = commands.add_parser("log")
    eight.add_argument("--log", required=True)
    eight.add_argument("--exit-code", type=int)
    eight.add_argument("--out", required=True)
    eight.set_defaults(run=log_command)
    five = commands.add_parser("summarize")
    five.add_argument("--out", required=True)
    five.set_defaults(run=summarize)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
