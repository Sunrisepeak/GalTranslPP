#!/usr/bin/env python3
"""Engine comparison on GalTranslPP (temporary, evidence only).

  bench.py cdb      the engine database (mcppls model --export engine) for the engines that read one: clangd, clice
  bench.py lsp      one language server as an editor drives it: a cold start with files open, requests while it
                    prepares, requests once it is idle, typing, then a second start on the same cache
  bench.py check    clangd --check on each file, with clangd's own trace: where one completion's time goes
  bench.py compile  the compiler alone (the project's LLVM): parse, deserialization and code completion per file,
                    headers against modules, no editor and no cache in between

Every command writes small JSON files below --out; digest.py turns them into the report's tables. Nothing here is
meant to be merged.
"""
import argparse
import collections
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

try:
    import psutil
except ImportError:  # memory and CPU are then not measured
    psutil = None

BENCH = ["GalTranslPP/bench/BenchH.cpp", "GalTranslPP/bench/BenchM.cpp", "GalTranslPP/bench/BenchMG.cpp",
         "GalTranslPP/bench/BenchHX.cpp", "GalTranslPP/bench/BenchX.cpp"]
REAL = ["GalTranslPP/NormalJsonTranslator.Run.cpp", "GalTranslPP/NormalJsonTranslator.Core.cpp",
        "GalTranslPP/NormalJsonTranslator.TransAgent.cpp", "GalTranslPP/NormalJsonTranslator.ixx", "GalTranslPP/Tool.ixx"]

# Names of which a right answer after `std::` or `std::ranges::` has one: clangd and clice return the best 100 of
# thousands, ranked by more than the name.
STD_NAMES = ["vector", "string", "ranges", "views", "map", "move", "size_t", "array", "sort", "begin", "pair", "optional"]
RANGES_NAMES = ["sort", "count_if", "find", "copy", "for_each", "begin", "end", "range", "views", "to", "size", "transform"]
# Where requests are asked: (id, method, needle, offset into the needle, names one of which a right answer has).
BENCH_POINTS = [
    ("std::", "completion", "std::ranges::sort(sizes)", 5, STD_NAMES),
    ("std::ranges::", "completion", "std::ranges::sort(sizes)", 13, RANGES_NAMES),
    ("vector.", "completion", "sizes.push_back", 6, ["push_back", "size", "emplace_back"]),
    ("json.", "completion", "doc.dump()", 4, ["dump", "contains", "is_object"]),
    ("nlohmann::", "completion", "nlohmann::json::object", 10, ["json", "ordered_json", "basic_json"]),
    ("absl::", "completion", "absl::flat_hash_map<", 6, ["flat_hash_map", "flat_hash_set"]),
    ("spdlog::", "completion", "spdlog::info(", 8, ["info", "warn", "error"]),
    ("statement", "completion", "        const std::string text", 8, ["sizes", "doc", "index", "names"]),
    ("hover:dump", "hover", "doc.dump()", 5, ["dump"]),
    ("hover:flat_hash_map", "hover", "absl::flat_hash_map<", 7, ["flat_hash_map"]),
    ("definition:dump", "definition", "doc.dump()", 5, ["json"]),
    ("definition:sort", "definition", "std::ranges::sort(sizes)", 14, ["algorithm", "ranges", "std", "xutility"]),
]
REAL_POINTS = {
    "GalTranslPP/NormalJsonTranslator.Run.cpp": [
        ("std::", "completion", "std::views::keys | std::ranges::to", 5, STD_NAMES),
        ("std::views::", "completion", "std::views::keys | std::ranges::to", 12, ["keys", "transform", "filter"]),
        ("json.", "completion", "fileOverview.push_back(std::move(newItem))", 13, ["push_back", "dump", "contains"]),
        ("logger->", "completion", "m_logger->error(", 10, ["error", "info", "debug"]),
        ("hover:get_ref", "hover", "fileOverview.get_ref<", 14, ["get_ref"]),
        ("definition:gppTr", "definition", "gppTr(\"NormalJsonTranslator.normalJsonBeforeRun\"", 1, ["GPPI18n", "I18n"]),
    ],
}
# Typed as a person types, into the function body: a line, completion asked as VS Code asks it.
BENCH_TYPED = ("        const std::string text = doc.dump();",
               "        const auto probe = doc.size() + sizes.size() + index.size() + text.size();",
               {"doc.": ["size", "dump"], "sizes.": ["size", "push_back"], "index.": ["size", "find"], "text.": ["size", "substr"]})
REAL_TYPED = {
    "GalTranslPP/NormalJsonTranslator.Run.cpp": (
        "    std::vector<fs::path> relFilePaths = savedTranslCacheMap | std::views::keys",
        "    const auto probeCount = relFilePaths.size() + savedTranslCacheMap.size();",
        {"relFilePaths.": ["size", "empty"], "savedTranslCacheMap.": ["size", "find", "contains"]}),
}


def now():
    return time.monotonic()


def write_json(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def uri_of(path):
    return pathlib.Path(path).resolve().as_uri()


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def points_of(rel):
    return REAL_POINTS.get(rel, BENCH_POINTS if rel in BENCH else [])


def locate(text, needle, offset):
    index = text.find(needle)
    if index < 0:
        return None
    index += offset
    line = text.count("\n", 0, index)
    character = index - (text.rfind("\n", 0, index) + 1)
    return line, character


# ---------------------------------------------------------------------------------------------- the client

class Client:
    """An editor's side of LSP over standard input and output: requests from any thread, server requests answered."""

    def __init__(self, argv, cwd, env, log_path):
        self.log = open(log_path, "wb")
        self.t0 = now()
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log)
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.next_id = 0
        self.waiters = {}
        self.responses = {}
        self.diagnostics = {}
        self.published = collections.defaultdict(list)   # uri -> [t]
        self.status = []                                  # cxxModules/status transitions (mcppls)
        self.progress = collections.Counter()             # $/progress kinds
        self.messages = []
        self.methods = collections.Counter()              # what the server sent, by method
        self.closed = threading.Event()
        self.exit_at = None
        threading.Thread(target=self._read, daemon=True).start()

    def t(self):
        return round(now() - self.t0, 2)

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
                self._dispatch(json.loads(out.read(int(headers["content-length"]))))
        except Exception as error:  # a torn stream is an exit, recorded as one
            self.messages.append({"t": self.t(), "reader": repr(error)[:300]})
        finally:
            self.exit_at = self.t()
            self.closed.set()
            with self.lock:
                for waiter in self.waiters.values():
                    waiter.set()

    def _dispatch(self, message):
        if "id" in message and "method" in message:
            result = None
            if message["method"] == "workspace/configuration":
                result = [None] * len((message.get("params") or {}).get("items", []))
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
        params = message.get("params") or {}
        self.methods[method] += 1
        if method == "textDocument/publishDiagnostics":
            self.diagnostics[params.get("uri")] = params.get("diagnostics") or []
            self.published[params.get("uri")].append(self.t())
        elif method == "cxxModules/status":
            state = params.get("state")
            if not self.status or self.status[-1][1] != state:
                self.status.append((self.t(), state))
        elif method == "$/progress":
            value = params.get("value") or {}
            self.progress[value.get("kind", "?")] += 1
        elif method == "window/showMessage" and len(self.messages) < 200:
            self.messages.append({"t": self.t(), "type": params.get("type"), "message": str(params.get("message"))[:300]})

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

    def begin(self, method, params):
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
            self.waiters[request_id] = threading.Event()
        sent = self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return request_id, now(), sent

    def finish(self, request_id, started, sent, timeout, cancel=True):
        waiter = self.waiters.get(request_id)
        if sent and waiter:
            waiter.wait(max(0.0, timeout - (now() - started)))
        with self.lock:
            self.waiters.pop(request_id, None)
            response = self.responses.pop(request_id, None)
        seconds = round(now() - started, 3)
        if response is None:
            if cancel and not self.closed.is_set():
                self.notify("$/cancelRequest", {"id": request_id})
            return {"status": "closed" if self.closed.is_set() else ("send-failed" if not sent else "timeout"), "seconds": seconds}
        if "error" in response:
            error = response["error"] or {}
            status = "cancelled" if error.get("code") in (-32800, -32802) else "error"
            return {"status": status, "seconds": seconds, "error": str(error.get("message"))[:200]}
        return {"status": "ok", "seconds": seconds, "result": response.get("result")}

    def request(self, method, params, timeout):
        return self.finish(*self.begin(method, params), timeout)


# ---------------------------------------------------------------------------------------------- the process tree

class Tree:
    """The server and everything it starts: memory, CPU, which processes came and went."""

    def __init__(self, pid):
        self.pid = pid
        self.samples = []          # (t, rss MB, private MB, cpu cores, processes)
        self.seen = {}             # pid -> name
        self.gone = collections.Counter()
        self.started = collections.Counter()
        self.stop = threading.Event()
        self.handles = {}
        self.t0 = now()
        if psutil:
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop.is_set():
            try:
                root = psutil.Process(self.pid)
                tree = [root] + root.children(recursive=True)
            except psutil.Error:
                tree = []
            rss = private = cpu = 0.0
            alive = set()
            for proc in tree:
                try:
                    handle = self.handles.setdefault(proc.pid, proc)
                    if proc.pid not in self.seen:
                        self.seen[proc.pid] = handle.name()
                        self.started[self.seen[proc.pid]] += 1
                        handle.cpu_percent(None)
                    info = handle.memory_info()
                    rss += info.rss
                    private += getattr(info, "private", getattr(info, "vms", 0))
                    cpu += handle.cpu_percent(None) / 100.0
                    alive.add(proc.pid)
                except psutil.Error:
                    pass
            for pid in list(self.handles):
                if pid not in alive:
                    self.gone[self.seen.get(pid, "?")] += 1
                    del self.handles[pid]
            self.samples.append((round(now() - self.t0, 1), round(rss / 2**20), round(private / 2**20), round(cpu, 2), len(alive)))
            self.stop.wait(1.0)

    def cpu_since(self, seconds):
        recent = [s[3] for s in self.samples if s[0] >= self.samples[-1][0] - seconds] if self.samples else []
        return statistics.mean(recent) if recent else None

    def summary(self):
        if not self.samples:
            return {"measured": False}
        peak = max(self.samples, key=lambda s: s[1])
        step = max(1, len(self.samples) // 120)
        return {"measured": True, "peakRssMB": peak[1], "peakAt": peak[0], "peakPrivateMB": max(s[2] for s in self.samples),
                "finalRssMB": self.samples[-1][1], "maxProcesses": max(s[4] for s in self.samples),
                "cpuCoreSeconds": round(sum(s[3] for s in self.samples), 1),
                "started": dict(self.started), "exited": dict(self.gone), "timeline": self.samples[::step]}


# ---------------------------------------------------------------------------------------------- servers

def server_command(args, cache):
    """argv, environment and initializationOptions for each kind of server, as its users would run it."""
    env = dict(os.environ)
    kind = args.server
    if kind in ("mcppls", "mcxx"):
        env["MCPPLS_CACHE_DIR"] = cache
        argv = [args.exe, "serve", "--payload", args.payload, "--log-level", "info"]
        options = {"compiler": None, "semanticKit": "auto", "engine": "clangd" if kind == "mcppls" else "mcxx", "toolEnvironment": "auto",
                   "conflictArbitration": "client", "semanticTokens": {"modules": True, "moduleType": True},
                   "completion": {"triggerOnSpace": True}, "buildDiscovery": "auto", "index.primeImplementationUnits": "auto"}
        return argv, env, options
    if kind == "clangd":
        # The engine database in a directory of its own, so the cache (.cache/clangd) is per run.
        pathlib.Path(cache).mkdir(parents=True, exist_ok=True)
        shutil.copy(args.cdb, pathlib.Path(cache) / "compile_commands.json")
        return [args.exe, "--experimental-modules-support", f"--compile-commands-dir={cache}", "--log=info",
                "--background-index"], env, None
    if kind == "clice":
        return [args.exe, "serve", "--log-level", "info"], env, {
            "project": {"cache_dir": cache, "logging_dir": str(pathlib.Path(cache) / "logs")},
            "rules": [{"compile_commands": [str(pathlib.Path(args.cdb).resolve())]}]}
    raise SystemExit(f"unknown server {kind}")


def start(args, cache, log_name):
    root = pathlib.Path(args.root).resolve()
    argv, env, options = server_command(args, cache)
    client = Client(argv, str(root), env, pathlib.Path(args.out) / log_name)
    tree = Tree(client.proc.pid)
    params = {
        "processId": os.getpid(), "rootUri": uri_of(root), "rootPath": str(root),
        "workspaceFolders": [{"uri": uri_of(root), "name": root.name}],
        "capabilities": {
            "textDocument": {"hover": {"contentFormat": ["markdown", "plaintext"]},
                             "semanticTokens": {"requests": {"full": True}, "tokenTypes": [], "tokenModifiers": [], "formats": ["relative"]},
                             "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                             "inlayHint": {}, "foldingRange": {},
                             "completion": {"completionItem": {"snippetSupport": True}, "contextSupport": True},
                             "definition": {"linkSupport": True},
                             "synchronization": {"didSave": True},
                             "publishDiagnostics": {"relatedInformation": True, "versionSupport": True}},
            "workspace": {"configuration": True, "workspaceFolders": True},
            "window": {"workDoneProgress": True},
            "experimental": {"cxxModules": {"version": 1, "status": True}},
        },
        "clientInfo": {"name": "Visual Studio Code", "version": "1.105.0"},
    }
    if options is not None:
        params["initializationOptions"] = options
    answer = client.request("initialize", params, timeout=300)
    client.notify("initialized", {})
    init = {"status": answer["status"], "seconds": answer["seconds"],
            "server": ((answer.get("result") or {}).get("serverInfo") if isinstance(answer.get("result"), dict) else None)}
    return client, tree, init


def stop(client, tree):
    graceful = False
    if not client.closed.is_set():
        answer = client.request("shutdown", None, timeout=60)
        client.notify("exit", None)
        try:
            client.proc.wait(30)
            graceful = answer["status"] == "ok"
        except subprocess.TimeoutExpired:
            client.proc.kill()
    tree.stop.set()
    try:
        client.proc.wait(10)
    except subprocess.TimeoutExpired:
        client.proc.kill()
    client.log.close()
    return {"graceful": graceful, "exitCode": client.proc.returncode, "closedAt": client.exit_at}


# ---------------------------------------------------------------------------------------------- requests

def names_of(result):
    items = result.get("items") if isinstance(result, dict) else result if isinstance(result, list) else None
    names = []
    for item in items or []:
        if isinstance(item, dict):
            name = (item.get("filterText") or item.get("label") or "").strip().lstrip("•").strip()
            name = re.split(r"[(<\s:]", name, maxsplit=1)[0]
            if name:
                names.append(name)
    return names


def hover_text(result):
    if not isinstance(result, dict):
        return ""
    contents = result.get("contents")
    if isinstance(contents, dict):
        return str(contents.get("value", ""))
    if isinstance(contents, list):
        return " ".join(c.get("value", "") if isinstance(c, dict) else str(c) for c in contents)
    return str(contents or "")


def definition_targets(result):
    items = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
    targets = []
    for item in items:
        if isinstance(item, dict):
            uri = item.get("targetUri") or item.get("uri") or ""
            rng = item.get("targetSelectionRange") or item.get("range") or {}
            targets.append(f"{uri.rsplit('/', 1)[-1]}:{(rng.get('start') or {}).get('line', -1) + 1}")
    return targets


def judge(method, result, expect):
    """(count, right, what it said, briefly)."""
    if method == "completion":
        names = names_of(result)
        return len(names), any(n in names for n in expect), names[:8], bool(isinstance(result, dict) and result.get("isIncomplete"))
    if method == "hover":
        text = hover_text(result)
        return len(text), bool(text) and any(e.lower() in text.lower() for e in expect), text[:120], None
    targets = definition_targets(result)
    return len(targets), bool(targets) and any(e.lower() in t.lower() for t in targets for e in expect), targets[:3], None


def ask(client, doc, method, line, character, timeout):
    lsp = {"completion": "textDocument/completion", "hover": "textDocument/hover", "definition": "textDocument/definition"}[method]
    params = {"textDocument": {"uri": doc}, "position": {"line": line, "character": character}}
    if method == "completion":
        params["context"] = {"triggerKind": 1}
    return client.request(lsp, params, timeout)


def round_of(client, files, texts, phase, rows, timeout, only=None):
    for rel in [f for f in files if f in texts]:
        doc = uri_of(pathlib.Path(rel).resolve())
        misses = 0
        for point, method, needle, offset, expect in points_of(rel):
            if only and point not in only:
                continue
            where = locate(texts[rel], needle, offset)
            if where is None:
                rows.append({"phase": phase, "file": rel, "point": point, "status": "no-such-point"})
                continue
            answer = ask(client, doc, method, where[0], where[1], timeout)
            row = {"phase": phase, "t": client.t(), "file": rel, "point": point, "method": method,
                   "status": answer["status"], "seconds": answer["seconds"]}
            if answer["status"] == "ok":
                row["count"], row["right"], row["said"], row["incomplete"] = judge(method, answer.get("result"), expect)
            elif "error" in answer:
                row["error"] = answer["error"]
            rows.append(row)
            misses = misses + 1 if answer["status"] in ("timeout", "closed") else 0
            if misses >= 3 or client.closed.is_set():
                break   # this file does not answer now; the next round asks again


def stats(rows):
    out = {}
    for key in sorted({(r["file"], r["point"]) for r in rows if "method" in r}):
        of = [r for r in rows if (r["file"], r["point"]) == key and "method" in r]
        ok = [r["seconds"] for r in of if r["status"] == "ok"]
        out[f"{key[0]}|{key[1]}"] = {"n": len(of), "ok": len(ok), "right": sum(1 for r in of if r.get("right")),
                                      "p50": pct(ok, 50), "max": max(ok) if ok else None,
                                      "statuses": dict(collections.Counter(r["status"] for r in of))}
    return out


def diagnostics_of(client, files):
    result = {}
    for rel in files:
        uri = uri_of(pathlib.Path(rel).resolve())
        items = client.diagnostics.get(uri)
        errors = [d for d in items or [] if d.get("severity", 1) == 1]
        result[rel] = {"published": items is not None, "times": client.published.get(uri, [])[:3] + client.published.get(uri, [])[-2:],
                       "errors": len(errors), "warnings": sum(1 for d in items or [] if d.get("severity") == 2),
                       "first": [f"L{(d.get('range') or {}).get('start', {}).get('line', 0) + 1}: {str(d.get('message'))[:160]}" for d in errors[:4]]}
    return result


def open_files(client, files):
    texts = {}
    for rel in files:
        path = pathlib.Path(rel).resolve()
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        texts[rel] = text
        client.notify("textDocument/didOpen", {"textDocument": {"uri": uri_of(path), "languageId": "cpp", "version": 1, "text": text}})
    return texts


PROBED = ["GalTranslPP/bench/BenchH.cpp", "GalTranslPP/bench/BenchX.cpp", "GalTranslPP/NormalJsonTranslator.Run.cpp"]


def editor_requests(client, files):
    """What VS Code asks of a file it shows, right after opening it: some servers (clice) compile a file only when
    asked about it. The time to the first semantic tokens is kept: it is when the file first looks right."""
    answered = {}

    def one(rel, method, params):
        answer = client.request(method, params, timeout=1800)
        if method == "textDocument/semanticTokens/full":
            answered[rel] = {"status": answer["status"], "at": client.t()}

    for rel in files:
        doc = {"uri": uri_of(pathlib.Path(rel).resolve())}
        for method, params in (("textDocument/semanticTokens/full", {"textDocument": doc}),
                               ("textDocument/documentSymbol", {"textDocument": doc}),
                               ("textDocument/foldingRange", {"textDocument": doc}),
                               ("textDocument/inlayHint", {"textDocument": doc, "range": {"start": {"line": 0, "character": 0},
                                                                                           "end": {"line": 60, "character": 0}}})):
            threading.Thread(target=one, args=(rel, method, params), daemon=True).start()
    return answered


def settle(client, tree, files, texts, rows, max_wait, quiet, probe_gap):
    """Until every file has diagnostics and the server has been quiet (no new diagnostics, little CPU) for `quiet` s.
    While it prepares, a few requests every `probe_gap` s: when do they first answer, and answer right."""
    uris = [uri_of(pathlib.Path(rel).resolve()) for rel in files]
    started = now()
    next_probe = 0.0
    first_right = {}
    while now() - started < max_wait and not client.closed.is_set():
        elapsed = now() - started
        if probe_gap and elapsed >= next_probe:
            before = len(rows)
            round_of(client, PROBED, texts, "preparing", rows, timeout=20, only={"std::", "json."})
            for row in rows[before:]:
                if row.get("right"):
                    first_right.setdefault(f"{row['file']}|{row['point']}", row["t"])
            next_probe = (now() - started) + probe_gap
        time.sleep(1)
        published = all(uri in client.diagnostics for uri in uris)
        last = max([ts[-1] for ts in client.published.values() if ts] or [0])
        cpu = tree.cpu_since(quiet)
        if published and client.t() - last >= quiet and now() - started >= quiet and (cpu is None or cpu < 0.3):
            return {"settled": True, "after": round(now() - started, 1), "firstRight": first_right}
    return {"settled": False, "after": round(now() - started, 1), "firstRight": first_right, "closed": client.closed.is_set()}


def typing(client, rel, text, anchor, line, asks, seconds_per_char, out_rows):
    """Type `line` after the line holding `anchor`, a character at a time; completion as VS Code asks it (at a trigger
    and at a word's first letter, the pending one cancelled); after the line, how long until diagnostics come."""
    path = pathlib.Path(rel).resolve()
    doc = uri_of(path)
    at = text.find(anchor)
    if at < 0:
        out_rows.append({"file": rel, "status": "no-anchor"})
        return text
    insert_at = text.find("\n", at) + 1
    row_of_line = text.count("\n", 0, insert_at)
    version = [100 + int(now()) % 100000]
    pending = []

    def change(new_text):
        version[0] += 1
        client.notify("textDocument/didChange", {"textDocument": {"uri": doc, "version": version[0]},
                                                 "contentChanges": [{"text": new_text}]})

    def complete(column, expect, trigger):
        for request_id, started, sent, *_ in pending:
            if request_id in client.waiters:
                client.notify("$/cancelRequest", {"id": request_id})
        pending.clear()
        params = {"textDocument": {"uri": doc}, "position": {"line": row_of_line, "character": column},
                  "context": {"triggerKind": 2, "triggerCharacter": trigger} if trigger else {"triggerKind": 1}}
        request_id, started, sent = client.begin("textDocument/completion", params)
        holder = {}

        def wait():
            answer = client.finish(request_id, started, sent, 30, cancel=False)
            row = {"file": rel, "kind": "completion", "trigger": trigger or "word", "status": answer["status"], "seconds": answer["seconds"]}
            if answer["status"] == "ok":
                count, right, said, incomplete = judge("completion", answer.get("result"), expect or [])
                row.update({"count": count, "incomplete": incomplete})
                if expect:
                    row["right"] = right
                    row["expect"] = expect[:2]
            out_rows.append(row)
        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        pending.append((request_id, started, sent, thread, holder))

    typed = ""
    previous = ""
    for char in line + "\n":
        typed += char
        new_text = text[:insert_at] + typed + ("" if char == "\n" else "\n") + text[insert_at:]
        change(new_text)
        stripped = typed.rstrip("\n")
        trigger = None
        expect = None
        if stripped.endswith(".") or stripped.endswith("::") or stripped.endswith("->"):
            trigger = stripped[-1]
            for key, names in asks.items():
                if stripped.endswith(key):
                    expect = names
            complete(len(stripped), expect, trigger)
        elif char.isalpha() and not (previous.isalnum() or previous == "_"):
            complete(len(stripped), None, None)
        previous = char
        time.sleep(seconds_per_char)
    typed_at = client.t()
    for _, _, _, thread, _ in pending:
        thread.join(35)
    # Diagnostics for the text as it is now.
    deadline = now() + 90
    while now() < deadline and not client.closed.is_set():
        if any(t >= typed_at for t in client.published.get(doc, [])):
            break
        time.sleep(0.2)
    after = [t for t in client.published.get(doc, []) if t >= typed_at]
    items = client.diagnostics.get(doc) or []
    out_rows.append({"file": rel, "kind": "diagnostics-after-line", "seconds": round(after[0] - typed_at, 2) if after else None,
                     "errors": sum(1 for d in items if d.get("severity", 1) == 1)})
    change(text)   # put the line back
    time.sleep(2)
    return text


def lsp(args):
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.chdir(args.root)
    files = [rel for rel in BENCH + REAL if pathlib.Path(rel).exists()]
    result = {"server": args.server, "exe": args.exe, "files": files}
    for run in ("cold", "warm"):
        if run == "warm" and args.no_warm:
            break
        client, tree, init = start(args, args.cache, f"{run}-server.log")
        texts = open_files(client, files)
        tokens = editor_requests(client, files)
        rows = []
        settled = settle(client, tree, files, texts, rows, args.max_wait if run == "cold" else args.max_wait_warm, args.quiet,
                         args.probe_gap if run == "cold" else 5)
        diagnostics = diagnostics_of(client, files)
        for _ in range(args.idle_rounds if run == "cold" else 1):
            if client.closed.is_set():
                break
            round_of(client, files, texts, "idle", rows, timeout=args.timeout)
        typed = []
        if run == "cold" and not client.closed.is_set():
            for rel in ["GalTranslPP/bench/BenchHX.cpp", "GalTranslPP/bench/BenchX.cpp", "GalTranslPP/bench/BenchH.cpp",
                        "GalTranslPP/bench/BenchM.cpp", "GalTranslPP/NormalJsonTranslator.Run.cpp"]:
                if rel not in texts:
                    continue
                anchor, line, asks = REAL_TYPED.get(rel, BENCH_TYPED)
                for _ in range(args.typing_lines):
                    if client.closed.is_set():
                        break
                    typing(client, rel, texts[rel], anchor, line, asks, args.char_seconds, typed)
        died = client.closed.is_set()   # before we asked it to stop
        ended = stop(client, tree)
        ended["diedEarly"] = died
        record = {"init": init, "settle": settled, "diagnostics": diagnostics, "semanticTokens": dict(tokens), "requests": rows, "requestStats": stats(rows),
                  "typing": typed, "status": client.status[:60], "progress": dict(client.progress), "methods": dict(client.methods), "messages": client.messages[:40],
                  "tree": tree.summary(), "end": ended}
        write_json(out / f"{run}.json", record)
        result[run] = {"settled": settled["settled"], "after": settled["after"], "exit": ended}
    write_json(out / "lsp.json", result)
    return 0


# ---------------------------------------------------------------------------------------------- the engine database

def cdb(args):
    """mcppls model --export engine: the database mcppls gives clangd (every unit, std's too), without module hints."""
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run([args.mcppls, "model", "--root", args.root, "--export", "engine", "--payload", args.payload],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=args.root, timeout=1800)
    if done.returncode != 0:
        print(done.stderr[-4000:])
        return 1
    entries = json.loads(done.stdout)
    for entry in entries:
        arguments = entry.get("arguments") or []
        entry["arguments"] = [a for a in arguments if not a.startswith(("-fmodule-file=", "-fmodule-output="))]
    out.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    files = [e["file"].replace("\\", "/") for e in entries]
    write_json(out.parent / "cdb-stats.json", {"entries": len(entries), "bench": [f for f in files if "/bench/" in f],
                                                "std": [f for f in files if re.search(r"/std(\.compat)?\.(ixx|cppm)$", f)],
                                                "moduleUnits": sum(1 for e in entries if "c++-module" in (e.get("arguments") or [])),
                                                "stderrTail": done.stderr[-1500:]})
    print(json.dumps({"entries": len(entries)}))
    return 0


# ---------------------------------------------------------------------------------------------- the compiler alone

MODULE_DECL = re.compile(r"^\s*(export\s+)?module\s+([\w.:]+)\s*;", re.M)
IMPORT_DECL = re.compile(r"^\s*(export\s+)?import\s+([\w.:]+)\s*;", re.M)


def strip_comments(text):
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)


def module_graph(entries):
    """name -> (entry, imports) for the module interface units; imports of a file."""
    provides = {}
    imports_of = {}
    for entry in entries:
        path = pathlib.Path(entry["file"])
        try:
            text = strip_comments(path.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            continue
        declared = list(MODULE_DECL.finditer(text))
        own = declared[-1].group(2) if declared else None
        exported = bool(declared and declared[-1].group(1))
        resolved = []
        for match in IMPORT_DECL.finditer(text):
            name = match.group(2)
            resolved.append(own.split(":")[0] + name if name.startswith(":") and own else name)
        if own and (exported or ":" in own):
            provides[own] = entry          # an interface, or a partition
        elif own:
            resolved.append(own)           # an implementation unit imports its interface
        imports_of[entry["file"]] = resolved
    return provides, imports_of


def plain_arguments(arguments):
    """The command without output, compile-only, module output or the source: what -fsyntax-only adds to."""
    result = []
    skip = False
    for argument in arguments[1:-1]:
        if skip:
            skip = False
            continue
        if argument in ("-o", "-MF", "-MT", "-MQ"):
            skip = True
            continue
        if argument in ("-c", "--precompile", "-MD", "-MMD") or argument.startswith(("-fmodule-output", "-fmodule-file=", "-o")):
            continue
        result.append(argument)
    return result


def run_timed(command, cwd, timeout=1800, env=None):
    started = time.monotonic()
    try:
        done = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return done.returncode, round(time.monotonic() - started, 2), done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        return "timeout", round(time.monotonic() - started, 2), "", ""


READ_STATS = re.compile(r"^\s*(\d+)/(\d+) (types|declarations|identifiers|statements|macros|lexical declcontexts|visible declcontexts|source location entries) read", re.M)


def read_stats(stderr):
    return {m.group(3): [int(m.group(1)), int(m.group(2))] for m in READ_STATS.finditer(stderr)}


def time_trace_totals(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    totals = {}
    for event in data.get("traceEvents", []):
        name = event.get("name", "")
        if event.get("ph") == "X" and name.startswith("Total "):
            totals[name[6:]] = round(event.get("dur", 0) / 1000)
    return dict(sorted(totals.items(), key=lambda kv: -kv[1])[:14])


def compile_command(args):
    """Each file through the compiler alone: -fsyntax-only (time, time trace, what was read from the BMIs), and code
    completion at the same points the editors are asked at."""
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    entries = json.loads(pathlib.Path(args.cdb).read_text(encoding="utf-8"))
    by_file = {pathlib.Path(e["file"]).resolve(): e for e in entries}
    provides, imports_of = module_graph(entries)
    bmi_dir = pathlib.Path(args.bmi_dir)
    bmi_dir.mkdir(parents=True, exist_ok=True)
    built = {}

    def build(name, stack=()):
        if name in built:
            return built[name]["ok"]
        entry = provides.get(name)
        if entry is None or name in stack:
            built[name] = {"ok": False, "why": "no unit provides it" if entry is None else "cycle"}
            return False
        ok = all(build(dep, stack + (name,)) for dep in imports_of.get(entry["file"], []))
        pcm = bmi_dir / (name.replace(":", "-") + ".pcm")
        command = [args.clang] + plain_arguments(entry["arguments"]) + [f"-fprebuilt-module-path={bmi_dir}", "--precompile", "-o", str(pcm), entry["file"]]
        code, seconds, _, stderr = run_timed(command, entry.get("directory") or args.root)
        built[name] = {"ok": ok and code == 0, "seconds": seconds, "bytes": pcm.stat().st_size if pcm.exists() else None,
                       "stderr": stderr[-600:] if code != 0 else ""}
        return built[name]["ok"]

    results = {}
    for rel in BENCH + ["GalTranslPP/NormalJsonTranslator.Run.cpp"]:
        path = pathlib.Path(args.root, rel).resolve()
        if not path.exists():
            continue
        entry = by_file.get(path)
        if entry is None:
            results[rel] = {"error": "not in the database"}
            continue
        needed = imports_of.get(entry["file"], [])
        for name in needed:
            build(name)
        base = [args.clang] + plain_arguments(entry["arguments"]) + [f"-fprebuilt-module-path={bmi_dir}"]
        cwd = entry.get("directory") or args.root
        record = {"imports": needed}
        trace = out / (path.stem + ".trace.json")
        runs = []
        for attempt in range(args.repeat):
            code, seconds, _, stderr = run_timed(base + ["-fsyntax-only", "-Xclang", f"-ftime-trace={trace}",
                                                         "-Xclang", "-ftime-trace-granularity=1000", str(path)], cwd)
            runs.append(seconds)
            if attempt == 0:
                record["syntax"] = {"exit": code, "errors": stderr.count(" error: "), "stderr": stderr[-800:] if code != 0 else ""}
        record["syntax"]["seconds"] = runs
        record["syntax"]["median"] = statistics.median(runs)
        record["timeTrace"] = time_trace_totals(trace)
        code, seconds, _, stderr = run_timed(base + ["-fsyntax-only", "-Xclang", "-print-stats", str(path)], cwd)
        record["read"] = read_stats(stderr)
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        completions = {}
        for point, method, needle, offset, expect in points_of(rel):
            if method != "completion":
                continue
            where = locate(text, needle, offset)
            if where is None:
                continue
            spot = f"{path}:{where[0] + 1}:{where[1] + 1}"
            times = []
            for attempt in range(args.repeat):
                code, seconds, stdout, stderr = run_timed(base + ["-fsyntax-only", "-Xclang", f"-code-completion-at={spot}",
                                                                  "-Xclang", "-print-stats", str(path)], cwd)
                times.append(seconds)
            names = [m.group(1) for m in re.finditer(r"^COMPLETION: (\w+)", stdout, re.M)]
            completions[point] = {"seconds": times, "median": statistics.median(times), "results": len(names),
                                  "right": any(n in names for n in expect), "read": read_stats(stderr)}
        record["completion"] = completions
        results[rel] = record
    write_json(out / "compile.json", {"clang": args.clang, "files": results, "bmis": built})
    return 0


# ---------------------------------------------------------------------------------------------- clangd --check

def check(args):
    """clangd --check on each file at the request points' lines, with the trace's spans summed: preamble, AST, and per
    completion the module checks, the compile (BMIs loaded, the file parsed, Sema) and the rest."""
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    for modules in (True, False):
        for rel in BENCH + ["GalTranslPP/NormalJsonTranslator.Run.cpp"]:
            if not modules and rel not in ("GalTranslPP/bench/BenchH.cpp", "GalTranslPP/bench/BenchHX.cpp"):
                continue   # without module support only the header files compile
            path = pathlib.Path(args.root, rel).resolve()
            if not path.exists():
                continue
            text = path.read_bytes().decode("utf-8-sig", errors="replace")
            # One line, the json member's (about a dozen tokens, a completion at each).
            lines = [locate(text, needle, offset)[0] + 1 for point, method, needle, offset, _ in points_of(rel)
                     if point == "json." and locate(text, needle, offset)]
            label = f"{rel}|{'modules' if modules else 'no-modules'}"
            for run in ("first", "again"):
                trace = out / f"trace-{path.stem}-{'m' if modules else 'n'}-{run}.json"
                env = dict(os.environ, CLANGD_TRACE=str(trace))
                command = [args.clangd, f"--compile-commands-dir={args.cdb_dir}", f"--check={path}", "--check-completion",
                           f"--check-lines={lines[0] if lines else 1}", "--log=info"]
                if modules:
                    command.insert(1, "--experimental-modules-support")
                code, seconds, stdout, stderr = run_timed(command, args.root, timeout=2400, env=env)
                spans = collections.defaultdict(list)
                try:
                    for event in json.loads(trace.read_text(encoding="utf-8")).get("traceEvents", []):
                        if event.get("ph") == "X":
                            spans[event["name"]].append(event.get("dur", 0) / 1000)
                except (OSError, ValueError):
                    pass
                keep = ("BuildPreamble", "BuildAST", "Sema completion", "Completion module validation", "Completion compiler invocation",
                        "Completion semantic execution", "Populate CodeCompleteResult", "Merge and score results", "CodeCompleteFlow")
                summary = {name: {"n": len(spans[name]), "sum": round(sum(spans[name])), "median": round(statistics.median(spans[name]))}
                           for name in keep if spans.get(name)}
                errors = re.findall(r"^E\[[^\]]*\] (.*)$", stderr, re.M)
                results.setdefault(label, {})[run] = {"exit": code, "seconds": seconds, "spans": summary, "errors": len(errors),
                                                      "firstErrors": [e[:200] for e in errors[:3]],
                                                      "costLines": re.findall(r"Completion in [^\n]*", stderr)[:3],
                                                      "prerequisites": re.findall(r"Prerequisites of [^\n]*", stderr)[:2]}
                (out / f"check-{path.stem}-{'m' if modules else 'n'}-{run}.log").write_text(stderr[-200000:], encoding="utf-8")
                try:
                    trace.unlink()
                except OSError:
                    pass
    write_json(out / "check.json", results)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("lsp")
    one.add_argument("--server", required=True, choices=["mcppls", "mcxx", "clangd", "clice"])
    one.add_argument("--exe", required=True)
    one.add_argument("--payload", default="")
    one.add_argument("--cdb", default="")
    one.add_argument("--root", required=True)
    one.add_argument("--cache", required=True)
    one.add_argument("--out", required=True)
    one.add_argument("--max-wait", type=float, default=2400)
    one.add_argument("--max-wait-warm", type=float, default=900)
    one.add_argument("--quiet", type=float, default=45)
    one.add_argument("--probe-gap", type=float, default=20)
    one.add_argument("--idle-rounds", type=int, default=2)
    one.add_argument("--timeout", type=float, default=30)
    one.add_argument("--typing-lines", type=int, default=2)
    one.add_argument("--char-seconds", type=float, default=0.12)
    one.add_argument("--no-warm", action="store_true")
    two = commands.add_parser("cdb")
    two.add_argument("--mcppls", required=True)
    two.add_argument("--payload", required=True)
    two.add_argument("--root", required=True)
    two.add_argument("--out", required=True)
    three = commands.add_parser("compile")
    three.add_argument("--clang", required=True)
    three.add_argument("--cdb", required=True)
    three.add_argument("--root", required=True)
    three.add_argument("--bmi-dir", required=True)
    three.add_argument("--out", required=True)
    three.add_argument("--repeat", type=int, default=3)
    four = commands.add_parser("check")
    four.add_argument("--clangd", required=True)
    four.add_argument("--cdb-dir", required=True)
    four.add_argument("--root", required=True)
    four.add_argument("--out", required=True)
    args = parser.parse_args()
    for name in ("out", "cache", "cdb", "cdb_dir", "root", "bmi_dir"):
        if getattr(args, name, None):
            setattr(args, name, str(pathlib.Path(getattr(args, name)).resolve()))
    return {"lsp": lsp, "cdb": cdb, "compile": compile_command, "check": check}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
