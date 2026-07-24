// Playground page: runs the editor and shows results from the Python worker (worker.js)
"use strict";

(function () {
  const STORAGE = "jarlang-playground:";
  const BUNDLE_URL = "jarlang/bundle.json";
  const MAX_TOKEN_ROWS = 5000;
  const MAX_GLYPH_CELLS = 200000;
  const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  const INPUT_CALL = /\binput\s*\(/;

  // Helpers
  const $ = (id) => document.getElementById(id);

  function store(key, value) {
    try {
      if (value === null) localStorage.removeItem(STORAGE + key);
      else localStorage.setItem(STORAGE + key, value);
    } catch (e) {
      // Storage may be blocked (private mode)
    }
  }

  function load(key) {
    try {
      return localStorage.getItem(STORAGE + key);
    } catch (e) {
      return null;
    }
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function debounce(fn, ms) {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  function formatMs(ms) {
    if (ms === undefined || ms === null) return "";
    if (ms < 1) return `${ms.toFixed(2)} ms`;
    if (ms < 1000) return `${ms < 10 ? ms.toFixed(1) : Math.round(ms)} ms`;
    return `${(ms / 1000).toFixed(2)} s`;
  }

  function plural(n, word) {
    return `${n} ${word}${n === 1 ? "" : "s"}`;
  }

  let toastTimer = null;
  function toast(message, ms = 2600) {
    const box = $("toast");
    box.textContent = message;
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (box.hidden = true), ms);
  }

  function setStatus(state, text) {
    $("status").dataset.state = state;
    $("status-text").textContent = text;
  }

  // Theme
  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  function applyTheme(theme, persist) {
    document.documentElement.setAttribute("data-theme", theme);
    $("theme-toggle").textContent = theme === "dark" ? "Light theme" : "Dark theme";
    if (persist) store("theme", theme);
  }

  function setupTheme() {
    applyTheme(currentTheme(), false);
    $("theme-toggle").addEventListener("click", () => applyTheme(currentTheme() === "dark" ? "light" : "dark", true));
    const media = window.matchMedia ? matchMedia("(prefers-color-scheme: dark)") : null;
    if (media && media.addEventListener) {
      media.addEventListener("change", (e) => {
        if (!load("theme")) applyTheme(e.matches ? "dark" : "light", false);
      });
    }
  }

  // Syntax highlighting (the word lists come from the bundle)
  const lang = { keywords: new Set(), builtins: new Set(), constants: new Set() };
  const LITERAL_KEYWORDS = new Set(["true", "false", "nil"]);
  const IDENT = "[\\p{L}_][\\p{L}\\p{N}_]*";

  function setLanguage(meta) {
    for (const key of ["keywords", "builtins", "constants"]) {
      if (meta && Array.isArray(meta[key])) lang[key] = new Set(meta[key]);
    }
  }

  function nameToken(name, isCall) {
    if (LITERAL_KEYWORDS.has(name)) return "atom";
    if (lang.keywords.has(name)) return "keyword";
    if (lang.constants.has(name)) return "atom";
    if (isCall) return lang.builtins.has(name) ? "builtin" : "variable-2";
    return "variable";
  }

  function defineMode() {
    const code = [
      { regex: /#.*/, token: "comment" },
      { regex: /"/, token: "string", push: "dstring" },
      { regex: /'/, token: "string", push: "sstring" },
      { regex: /0[xX][\da-fA-F_]+|0[bB][01_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?/, token: "number" },
      { regex: new RegExp(`(fn)(\\s+)(${IDENT})`, "u"), token: ["keyword", null, "def"] },
      { regex: new RegExp(`${IDENT}(?=\\()`, "u"), token: (m) => nameToken(m[0], true) },
      { regex: new RegExp(IDENT, "u"), token: (m) => nameToken(m[0], false) },
      { regex: /∞/, token: "atom" },
      { regex: /\|>|\.\.<|\.\.|=>|->|\*\*=?|\/\/=?|&&|\|\||[-+*/%^!=<>]=?|[×·÷≤≥≠√−]/, token: "operator" },
      { regex: /[{[(]/, token: "bracket", indent: true },
      { regex: /[}\])]/, token: "bracket", dedent: true },
    ];
    CodeMirror.defineSimpleMode("jarlang", {
      start: code,
      dstring: [
        { regex: /\\(?:u\{[\da-fA-F]+\}|x[\da-fA-F]{2}|.)/, token: "string-2" },
        { regex: /\{/, token: "interp", push: "interp" },
        { regex: /"/, token: "string", pop: true },
        { regex: /[^"\\{]+/, token: "string" },
      ],
      sstring: [
        { regex: /\\./, token: "string-2" },
        { regex: /'/, token: "string", pop: true },
        { regex: /[^'\\]+/, token: "string" },
      ],
      interp: [
        { regex: /\}/, token: "interp", pop: true },
        { regex: /:[^{}"'()]*(?=\})/, token: "string-2" }, // a format spec like {x:>8.2f}
        ...code.filter((rule) => !rule.indent && !rule.dedent),
        { regex: /[{[(]/, token: "bracket" },
        { regex: /[\])]/, token: "bracket" },
      ],
      meta: {
        lineComment: "#",
        dontIndentStates: ["dstring", "sstring"],
        electricInput: /^\s*[}\])]$/,
      },
    });
  }

  // Editor
  let editor = null;

  function setupEditor(initialCode) {
    const textarea = $("code");
    textarea.value = initialCode;
    editor = CodeMirror.fromTextArea(textarea, {
      mode: "jarlang",
      theme: "jarlang",
      lineNumbers: true,
      matchBrackets: true,
      autoCloseBrackets: true,
      styleActiveLine: true,
      indentUnit: 2,
      tabSize: 2,
      indentWithTabs: false,
      viewportMargin: 30,
      screenReaderLabel: "Code",
      gutters: ["CodeMirror-linenumbers", "CodeMirror-lint-markers"],
      lint: { getAnnotations: lintAnnotations, async: true, delay: 400, highlightLines: true },
      extraKeys: {
        "Ctrl-Enter": () => runProgram(),
        "Cmd-Enter": () => runProgram(),
        "Shift-Alt-F": () => formatCode(),
        "Ctrl-/": "toggleComment",
        "Cmd-/": "toggleComment",
        "Ctrl-S": () => toast("Code is saved automatically"),
        "Cmd-S": () => toast("Code is saved automatically"),
        Tab: (cm) => cm.execCommand(cm.somethingSelected() ? "indentMore" : "insertSoftTab"),
        "Shift-Tab": "indentLess",
      },
    });
    editor.on("cursorActivity", () => {
      const pos = editor.getCursor();
      $("cursor").textContent = `Ln ${pos.line + 1}, Col ${pos.ch + 1}`;
    });
    editor.on("change", onCodeChange);
  }

  function jumpTo(line, column, endLine, endColumn) {
    if (!editor || !line) return;
    const from = { line: line - 1, ch: Math.max(0, (column || 1) - 1) };
    const to = endLine ? { line: endLine - 1, ch: Math.max(0, (endColumn || 1) - 1) } : from;
    editor.focus();
    editor.setSelection(from, to);
    editor.scrollIntoView({ from, to }, 80);
  }

  // Python worker
  class PythonWorker {
    constructor(bundleText, onState) {
      this.bundleText = bundleText;
      this.onState = onState;
      this.nextId = 0;
      this.pending = new Map();
      this.info = null;
      this.pyodideVersion = null;
      this.started = false;
      this.start();
    }

    start() {
      this.loaded = false;
      this.ready = new Promise((resolve, reject) => {
        this._resolveReady = resolve;
        this._rejectReady = reject;
      });
      this.ready.catch(() => {}); // errors are shown through onState
      this.worker = new Worker("worker.js");
      this.worker.onmessage = (event) => this.onMessage(event.data);
      this.worker.onerror = (event) => {
        event.preventDefault();
        this.fail(event.message || "the worker did not start");
      };
      this.worker.postMessage({ type: "init", bundle: this.bundleText });
      this.onState("loading", this.started ? "Restarting Python..." : "Loading Python...");
      this.started = true;
    }

    // Usually the CDN is blocked or the connection dropped
    fail(detail) {
      const error = new Error("Could not load Python");
      error.stderr = `error: could not load Python\n  = note: ${detail}\n  = help: check the connection and reload the page`;
      this._rejectReady(error);
      this.onState("failed", error.message);
      for (const [id, entry] of this.pending) {
        entry.resolve({ ok: false, stderr: error.stderr, diagnostics: [] });
        this.pending.delete(id);
      }
    }

    onMessage(msg) {
      const entry = msg.id !== undefined ? this.pending.get(msg.id) : null;
      switch (msg.type) {
        case "status":
          this.onState("loading", msg.text);
          break;
        case "ready":
          this.loaded = true;
          this.info = msg.info;
          this.pyodideVersion = msg.pyodide;
          this._resolveReady(msg);
          this.onState("ready", "");
          break;
        case "init-error":
          this.fail(msg.error);
          break;
        case "stdout":
          if (entry && entry.onStdout) entry.onStdout(msg.chunk);
          break;
        case "result":
          if (entry) {
            this.pending.delete(msg.id);
            entry.resolve(msg.result);
          }
          break;
        case "fatal":
          this.crashed(msg.error);
          break;
      }
    }

    call(fn, args = {}, options = {}) {
      const id = ++this.nextId;
      return new Promise((resolve, reject) => {
        this.pending.set(id, { resolve, reject, onStdout: options.onStdout });
        this.worker.postMessage({ type: "call", id, fn, args });
      });
    }

    // Python crashed: answer every pending call, then start over
    crashed(error) {
      const stack = /call stack|recursion/i.test(error || "");
      const message = stack
        ? "error: the program ran out of stack space\n  = help: use a loop instead of deep recursion"
        : `error: Python stopped unexpectedly and was restarted\n  = note: ${error}`;
      const pending = [...this.pending.values()];
      this.pending.clear();
      this.worker.terminate();
      for (const entry of pending) {
        entry.resolve({ ok: false, fatal: true, stderr: message, diagnostics: [], error_kind: "crash" });
      }
      this.start();
    }

    // Stop the running program by starting a new worker
    restart(reason) {
      const pending = [...this.pending.values()];
      this.pending.clear();
      this.worker.terminate();
      if (!this.loaded) this._rejectReady(new Error(reason));
      for (const entry of pending) entry.reject(new Error(reason));
      this.start();
    }
  }

  let py = null;

  function onWorkerState(state, text) {
    if (state === "loading") {
      setStatus("loading", text);
    } else if (state === "ready") {
      const info = py.info || {};
      $("version").title = `JarLang ${info.version}, Python ${info.python}, Pyodide ${py.pyodideVersion}`;
      if (!running) setStatus("ready", "Ready");
      editor.performLint();
      rendered.ast = rendered.tokens = rendered.asm = rendered.types = null;
      refreshActiveTab();
    } else if (state === "failed") {
      setStatus("error", text);
    }
  }

  // Output
  const ANSI_RE = /\x1b\[([0-9;]*)m|\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]/g;
  const GLYPH_RE = /[\u2500-\u259f\u2800-\u28ff]/;
  const GLYPH_RUN_RE = /[\u2500-\u259f\u2800-\u28ff]+/g;

  // Writes program output with ANSI colors and keeps plots lined up
  class OutputWriter {
    constructor(pre) {
      this.pre = pre;
      this.classes = [];
      this.glyphCells = 0;
      this.text = "";
    }

    write(text) {
      if (!text) return;
      this.text += text;
      let last = 0;
      ANSI_RE.lastIndex = 0;
      let m;
      while ((m = ANSI_RE.exec(text))) {
        this.append(text.slice(last, m.index));
        if (m[1] !== undefined) this.sgr(m[1]);
        last = m.index + m[0].length;
      }
      this.append(text.slice(last));
    }

    sgr(params) {
      const codes = params === "" ? [0] : params.split(";").map(Number);
      const isColor = (c) => /^a-(3|9)\d$/.test(c);
      for (const code of codes) {
        if (code === 0) this.classes = [];
        else if (code === 1) this.add("a-b");
        else if (code === 2) this.add("a-d");
        else if (code === 3) this.add("a-i");
        else if (code === 4) this.add("a-u");
        else if (code === 22) this.remove(["a-b", "a-d"]);
        else if (code === 23) this.remove(["a-i"]);
        else if (code === 24) this.remove(["a-u"]);
        else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97)) {
          this.classes = this.classes.filter((c) => !isColor(c));
          this.add(`a-${code}`);
        } else if (code === 39) this.classes = this.classes.filter((c) => !isColor(c));
      }
    }

    add(cls) {
      if (!this.classes.includes(cls)) this.classes.push(cls);
    }

    remove(list) {
      this.classes = this.classes.filter((c) => !list.includes(c));
    }

    append(text) {
      if (!text) return;
      let parent = this.pre;
      if (this.classes.length) {
        parent = el("span", this.classes.join(" "));
        this.pre.appendChild(parent);
      }
      if (!GLYPH_RE.test(text) || this.glyphCells > MAX_GLYPH_CELLS) {
        parent.appendChild(document.createTextNode(text));
        return;
      }
      let last = 0;
      GLYPH_RUN_RE.lastIndex = 0;
      let m;
      while ((m = GLYPH_RUN_RE.exec(text))) {
        if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
        for (const ch of m[0]) parent.appendChild(el("span", "g", ch));
        this.glyphCells += m[0].length;
        last = m.index + m[0].length;
      }
      if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  // Shows error messages with colors and clickable locations
  function renderDiagnostics(text) {
    const pre = el("pre", "diag");
    const lines = text.replace(/\n+$/, "").split("\n");
    lines.forEach((line, i) => {
      if (i) pre.appendChild(document.createTextNode("\n"));
      let m;
      if ((m = /^(error|warning|note|internal error)(:?)(.*)$/.exec(line))) {
        pre.appendChild(el("span", m[1] === "warning" ? "d-warning" : m[1] === "note" ? "d-note" : "d-error", m[1]));
        pre.appendChild(el("span", "d-msg", m[2] + m[3]));
      } else if ((m = /^(\s*-->\s+)(main\.jlang):(\d+):(\d+)$/.exec(line))) {
        pre.appendChild(el("span", "d-gutter", m[1]));
        const link = el("button", "linkish", `${m[2]}:${m[3]}:${m[4]}`);
        link.type = "button";
        link.title = "Go to this line";
        const [ln, col] = [Number(m[3]), Number(m[4])];
        link.addEventListener("click", () => jumpTo(ln, col));
        pre.appendChild(link);
      } else if ((m = /^(\s*\d*\s*\|)(\s*)([\^\-]+)(.*)$/.exec(line))) {
        pre.appendChild(el("span", "d-gutter", m[1]));
        pre.appendChild(document.createTextNode(m[2]));
        pre.appendChild(el("span", "d-mark", m[3] + m[4]));
      } else if ((m = /^(\s*\d*\s*\|)(.*)$/.exec(line))) {
        pre.appendChild(el("span", "d-gutter", m[1]));
        pre.appendChild(document.createTextNode(m[2]));
      } else if ((m = /^(\s*)(= (?:help|note|trace):)(.*)$/.exec(line))) {
        pre.appendChild(document.createTextNode(m[1]));
        pre.appendChild(el("span", m[2].includes("help") ? "d-help" : "d-note", m[2]));
        pre.appendChild(document.createTextNode(m[3]));
      } else {
        pre.appendChild(document.createTextNode(line));
      }
    });
    return pre;
  }

  function note(text) {
    return el("div", "note", text);
  }

  // Running
  let running = false;
  let runMarks = [];
  let runLines = [];

  function clearRunMarks() {
    if (!runMarks.length && !runLines.length) return;
    editor.operation(() => {
      for (const mark of runMarks) mark.clear();
      for (const line of runLines) editor.removeLineClass(line, "background", "run-error-line");
    });
    runMarks = [];
    runLines = [];
  }

  function markRunErrors(diagnostics) {
    clearRunMarks();
    for (const d of diagnostics || []) {
      if (!d.line || (d.file && d.file !== "main.jlang")) continue;
      const from = { line: d.line - 1, ch: d.column - 1 };
      const to = { line: d.end_line - 1, ch: d.end_column - 1 };
      runMarks.push(editor.markText(from, to, { className: "cm-run-error", title: d.message }));
      runLines.push(editor.addLineClass(d.line - 1, "background", "run-error-line"));
    }
  }

  function setRunning(on) {
    running = on;
    $("run").disabled = on;
    $("stop").disabled = !on;
  }

  async function runProgram() {
    if (running || !py) return;
    showTab("output");
    const code = editor.getValue();
    const stdinLines = stdinValue();
    setRunning(true);
    clearRunMarks();

    const out = $("output");
    out.replaceChildren();
    if (!py.loaded) {
      out.appendChild(el("p", "placeholder loading", "Loading Python. The first load can take a few seconds."));
      try {
        await py.ready;
      } catch (e) {
        setRunning(false);
        if (e.message === "stopped") {
          out.replaceChildren(note("Stopped"));
          setStatus("stopped", "Stopped");
        } else {
          out.replaceChildren(renderDiagnostics(e.stderr || `error: ${e.message}`));
        }
        return;
      }
      out.replaceChildren();
    }
    const pre = el("pre", "out-stdout");
    out.appendChild(pre);
    const writer = new OutputWriter(pre);
    let stick = true;
    const onScroll = () => (stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 24);
    out.addEventListener("scroll", onScroll);

    setStatus("running", "Running...");
    const started = performance.now();
    let result;
    try {
      result = await py.call("run", { code, stdin_lines: stdinLines, stream: true }, {
        onStdout: (chunk) => {
          writer.write(chunk);
          // Follow the output like a terminal, but not for quick programs, so their output starts at the top
          if (stick && performance.now() - started > 300) out.scrollTop = out.scrollHeight;
        },
      });
    } catch (e) {
      // The worker was stopped
      out.removeEventListener("scroll", onScroll);
      const seconds = ((performance.now() - started) / 1000).toFixed(1);
      out.appendChild(note(`Stopped after ${seconds} s`));
      setRunning(false);
      setStatus("stopped", `Stopped after ${seconds} s`);
      return;
    }
    out.removeEventListener("scroll", onScroll);
    setRunning(false);
    renderRunResult(result, writer, out);
  }

  function renderRunResult(result, writer, out) {
    // Add any stdout that wasn't streamed
    const stdout = result.stdout === undefined ? writer.text : result.stdout;
    if (stdout.startsWith(writer.text)) writer.write(stdout.slice(writer.text.length));
    else {
      writer.pre.replaceChildren();
      writer.text = "";
      writer.write(stdout);
    }

    const hasValue = result.value !== null && result.value !== undefined;
    if (hasValue) {
      const row = el("div", "out-value");
      row.appendChild(el("span", "arrow", "=>"));
      row.appendChild(el("span", "val", result.value));
      if (result.value_type) row.appendChild(el("span", "type", result.value_type));
      out.appendChild(row);
    }
    if (result.stderr) {
      out.appendChild(renderDiagnostics(result.stderr));
      markRunErrors(result.diagnostics);
    }
    if (result.exit_code) out.appendChild(note(`Exited with code ${result.exit_code}`));
    if (result.stdin_exhausted) {
      out.appendChild(note("input() ran out of lines and returned an empty string. Add more lines under Input."));
    }
    if (!stdout && !result.stderr && !hasValue && !result.exit_code) {
      out.appendChild(el("p", "placeholder", "No output"));
    }
    if (result.stderr) out.scrollTop = out.scrollHeight;

    const time = formatMs(result.time_ms);
    if (result.fatal) setStatus("error", "Python crashed and was restarted");
    else if (!result.ok) setStatus("error", `Failed after ${time}`);
    else if (result.exit_code) setStatus("stopped", `Exited with code ${result.exit_code} after ${time}`);
    else setStatus("ok", `Ran in ${time}`);
  }

  function stopProgram() {
    if (!running || !py) return;
    py.restart("stopped");
  }

  function stdinValue() {
    const text = $("stdin").value;
    if (!text) return [];
    const lines = text.replace(/\r\n/g, "\n").split("\n");
    if (lines[lines.length - 1] === "") lines.pop();
    return lines;
  }

  function updateStdinBadge() {
    const n = stdinValue().length;
    $("stdin-count").textContent = n ? String(n) : "";
    $("stdin-count").title = n ? plural(n, "line") : "";
  }

  // Linting
  let problems = [];

  function lintAnnotations(text, callback) {
    if (!py) return callback([]);
    py.call("check", { code: text })
      .then((result) => {
        problems = result.diagnostics || [];
        updateProblems();
        const annotations = [];
        for (const d of problems) {
          if (!d.line) continue;
          const helps = (d.helps || []).map((h) => `help: ${h}`);
          annotations.push({
            from: CodeMirror.Pos(d.line - 1, d.column - 1),
            to: CodeMirror.Pos(d.end_line - 1, d.end_column - 1),
            message: [d.message + (d.label ? `: ${d.label}` : ""), ...helps].join("\n"),
            severity: d.severity === "error" ? "error" : "warning",
          });
        }
        callback(annotations);
      })
      .catch(() => callback([]));
  }

  function updateProblems() {
    const errors = problems.filter((d) => d.severity === "error").length;
    const warnings = problems.length - errors;
    const btn = $("problems");
    btn.classList.toggle("has-errors", errors > 0);
    btn.classList.toggle("has-warnings", errors === 0 && warnings > 0);
    btn.disabled = !problems.length;
    if (!problems.length) {
      btn.textContent = "No problems";
      return;
    }
    const parts = [];
    if (errors) parts.push(plural(errors, "error"));
    if (warnings) parts.push(plural(warnings, "warning"));
    btn.textContent = parts.join(", ");
  }

  // Tabs
  const TABS = ["output", "ast", "tokens", "asm", "types"];
  let activeTab = "output";
  const rendered = {}; // tab -> key of the code it shows
  let astFormat = "tree";

  function showTab(name) {
    if (!TABS.includes(name)) name = "output";
    activeTab = name;
    for (const tab of TABS) {
      const button = $(`tab-${tab}`);
      const selected = tab === name;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      $(`panel-${tab}`).hidden = !selected;
    }
    store("tab", name);
    refreshActiveTab();
  }

  function setupTabs() {
    const list = document.querySelector(".tabs");
    list.addEventListener("click", (e) => {
      const tab = e.target.closest("[data-tab]");
      if (tab) showTab(tab.dataset.tab);
    });
    list.addEventListener("keydown", (e) => {
      const i = TABS.indexOf(activeTab);
      let next = null;
      if (e.key === "ArrowRight") next = TABS[(i + 1) % TABS.length];
      else if (e.key === "ArrowLeft") next = TABS[(i - 1 + TABS.length) % TABS.length];
      else if (e.key === "Home") next = TABS[0];
      else if (e.key === "End") next = TABS[TABS.length - 1];
      if (next) {
        e.preventDefault();
        showTab(next);
        $(`tab-${next}`).focus();
      }
    });
    document.querySelectorAll("[data-ast]").forEach((btn) =>
      btn.addEventListener("click", () => {
        astFormat = btn.dataset.ast;
        document.querySelectorAll("[data-ast]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
        rendered.ast = null;
        refreshActiveTab();
      }),
    );
    $("asm-target").addEventListener("change", () => {
      store("asm-target", $("asm-target").value);
      refreshActiveTab();
    });
  }

  function viewKey(tab, code) {
    if (tab === "asm") return `${$("asm-target").value}\u0000${code}`;
    if (tab === "ast") return `${astFormat}\u0000${code}`;
    return code;
  }

  let refreshSeq = 0;
  async function refreshActiveTab() {
    const tab = activeTab;
    if (tab === "output" || !editor || !py) return;
    const code = editor.getValue();
    const key = viewKey(tab, code);
    if (rendered[tab] === key) return;
    const box = $(tab);
    if (!box.firstChild || !py.loaded) {
      box.replaceChildren(el("p", "placeholder loading", py.loaded ? "Working..." : "Loading Python..."));
    }
    const seq = ++refreshSeq;
    let result;
    try {
      if (tab === "ast") result = await py.call(astFormat === "json" ? "ast_json" : "ast_tree", { code });
      else if (tab === "tokens") result = await py.call("tokens", { code });
      else if (tab === "asm") result = await py.call("asm", { code, target: $("asm-target").value });
      else if (tab === "types") result = await py.call("explain", { code });
    } catch (e) {
      return; // stopped, the next refresh will redo it
    }
    if (seq !== refreshSeq) return; // a newer request will render
    if (editor.getValue() !== code) {
      rendered[tab] = null;
      return scheduleRefresh();
    }
    rendered[tab] = key;
    const render = { ast: renderAst, tokens: renderTokens, asm: renderAsm, types: renderTypes }[tab];
    box.replaceChildren(...render(result));
  }

  const scheduleRefresh = debounce(refreshActiveTab, 450);

  function failureNodes(result) {
    const nodes = [renderDiagnostics(result.stderr || "error: something went wrong")];
    if (result.note) nodes.push(note(result.note));
    return nodes;
  }

  function renderAst(result) {
    if (!result.ok) return failureNodes(result);
    const pre = el("pre");
    if (result.ast !== undefined) {
      pre.textContent = JSON.stringify(result.ast, null, 2);
      return [pre];
    }
    const re = /^([│├└─ ]*)((?:[\w\[\]\.]+: )?)(\w+)(?: (.*))?$/;
    result.tree.split("\n").forEach((line, i) => {
      if (i) pre.appendChild(document.createTextNode("\n"));
      const m = re.exec(line);
      if (!m) return pre.appendChild(document.createTextNode(line));
      if (m[1]) pre.appendChild(el("span", "t-branch", m[1]));
      if (m[2]) pre.appendChild(el("span", "t-edge", m[2]));
      pre.appendChild(el("span", "t-kind", m[3]));
      if (m[4] !== undefined) {
        pre.appendChild(document.createTextNode(" "));
        pre.appendChild(el("span", "t-label", m[4]));
      }
    });
    return [pre];
  }

  const TOKEN_KINDS = {
    keyword: /^(LET|CONST|FN|RETURN|IF|ELIF|ELSE|WHILE|FOR|IN|BREAK|CONTINUE|AND|OR|NOT|TRY|CATCH|THROW|IMPORT)$/,
    literal: /^(INT|FLOAT|TRUE|FALSE|NIL)$/,
    string: /^STRING$/,
    ident: /^IDENT$/,
    punct: /^(LPAREN|RPAREN|LBRACKET|RBRACKET|LBRACE|RBRACE|COMMA|SEMI|COLON|DOT|NEWLINE|EOF)$/,
  };

  function tokenClass(kind) {
    for (const [name, re] of Object.entries(TOKEN_KINDS)) if (re.test(kind)) return `k-${name}`;
    return "k-op";
  }

  function renderTokens(result) {
    const nodes = [];
    if (!result.ok) nodes.push(renderDiagnostics(result.stderr));
    const rows = result.tokens || [];
    const table = el("table", "tok-table");
    const head = el("thead");
    const hr = el("tr");
    for (const h of ["#", "Position", "Kind", "Text"]) hr.appendChild(el("th", null, h));
    head.appendChild(hr);
    table.appendChild(head);
    const body = el("tbody");
    rows.slice(0, MAX_TOKEN_ROWS).forEach((tok, i) => {
      const tr = el("tr");
      tr.tabIndex = 0;
      tr.appendChild(el("td", "idx", String(i + 1)));
      tr.appendChild(el("td", "pos", `${tok.line}:${tok.column}`));
      tr.appendChild(el("td", tokenClass(tok.kind), tok.kind));
      const text = tok.kind === "NEWLINE" ? "\\n" : tok.kind === "EOF" ? "" : tok.text;
      tr.appendChild(el("td", null, text));
      const select = () => jumpTo(tok.line, tok.column, tok.end_line, tok.end_column);
      tr.addEventListener("click", select);
      tr.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          select();
        }
      });
      body.appendChild(tr);
    });
    table.appendChild(body);
    nodes.push(table);
    if (rows.length > MAX_TOKEN_ROWS) nodes.push(note(`Showing the first ${MAX_TOKEN_ROWS} of ${rows.length} tokens.`));
    return nodes;
  }

  function renderAsm(result) {
    if (!result.ok) return failureNodes(result);
    const pre = el("pre");
    result.asm.replace(/\n$/, "").split("\n").forEach((line, i) => {
      if (i) pre.appendChild(document.createTextNode("\n"));
      const stripped = line.trim();
      let m;
      if (!stripped) return;
      if ((m = /^(\s*)# (\d+): (.*)$/.exec(line))) {
        // Link source line comments back to the editor
        pre.appendChild(document.createTextNode(m[1]));
        const link = el("button", "linkish s-src", `# ${m[2]}:`);
        link.type = "button";
        link.title = `Go to line ${m[2]}`;
        const target = Number(m[2]);
        link.addEventListener("click", () => jumpTo(target, 1));
        pre.appendChild(link);
        pre.appendChild(el("span", "s-src", ` ${m[3]}`));
      } else if (stripped.startsWith("#")) {
        pre.appendChild(el("span", "s-comment", line));
      } else if (/^[\w.$@]+:$/.test(stripped)) {
        pre.appendChild(el("span", "s-label", line));
      } else if (stripped.startsWith(".")) {
        pre.appendChild(el("span", "s-directive", line));
      } else {
        const indent = line.slice(0, line.length - line.trimStart().length);
        const hash = stripped.indexOf(" #");
        const codePart = hash >= 0 ? stripped.slice(0, hash) : stripped;
        const comment = hash >= 0 ? stripped.slice(hash) : "";
        const space = codePart.indexOf(" ");
        const mnemonic = space >= 0 ? codePart.slice(0, space) : codePart;
        const operands = space >= 0 ? codePart.slice(space) : "";
        pre.appendChild(document.createTextNode(indent));
        pre.appendChild(el("span", "s-mnemonic", mnemonic));
        operands.split(/(%[a-z0-9]+|\$-?(?:0x)?[0-9a-f]+)/i).forEach((part) => {
          if (!part) return;
          if (part.startsWith("%")) pre.appendChild(el("span", "s-reg", part));
          else if (part.startsWith("$")) pre.appendChild(el("span", "s-imm", part));
          else pre.appendChild(document.createTextNode(part));
        });
        if (comment) pre.appendChild(el("span", "s-comment", comment));
      }
    });
    return [pre];
  }

  function renderTypes(result) {
    if (!result.ok) return failureNodes(result);
    const pre = el("pre");
    result.text.split("\n").forEach((line, i) => {
      if (i) pre.appendChild(document.createTextNode("\n"));
      let m;
      if (i === 0 || /^\S.*:$/.test(line)) {
        pre.appendChild(el("span", "s-head", line));
      } else if ((m = /^(\s*fn )(\S+?)(\(.*\) -> )(\S+)(\s+)(\[.*\])$/.exec(line))) {
        pre.appendChild(document.createTextNode(m[1]));
        pre.appendChild(el("span", "s-name", m[2]));
        m[3].split(/(: [^,)]+)/).forEach((part) => {
          if (part.startsWith(": ")) {
            pre.appendChild(document.createTextNode(": "));
            pre.appendChild(el("span", "s-type", part.slice(2)));
          } else pre.appendChild(document.createTextNode(part));
        });
        pre.appendChild(el("span", "s-type", m[4]));
        pre.appendChild(document.createTextNode(m[5]));
        pre.appendChild(el("span", "s-comment", m[6]));
      } else if ((m = /^(\s+)([^:]+)(: )(.+)$/.exec(line))) {
        pre.appendChild(document.createTextNode(m[1]));
        pre.appendChild(el("span", "s-name", m[2]));
        pre.appendChild(document.createTextNode(m[3]));
        pre.appendChild(el("span", "s-type", m[4]));
      } else {
        pre.appendChild(document.createTextNode(line));
      }
    });
    return [pre];
  }

  // Examples
  let examples = [];

  function setupExamples(bundle) {
    const files = bundle.files || {};
    examples = (bundle.examples || [])
      .map((ex) => ({ ...ex, code: files[`examples/${ex.name}.jlang`] }))
      .filter((ex) => typeof ex.code === "string");
    const select = $("examples");
    for (const ex of examples) {
      const option = el("option", null, ex.title);
      option.value = ex.name;
      if (ex.description) option.title = ex.description;
      select.appendChild(option);
    }
    // Works like a menu, so the same example can be picked again
    select.addEventListener("change", () => {
      const name = select.value;
      select.value = "";
      select.blur();
      loadExample(name);
    });
  }

  function loadExample(name) {
    const ex = examples.find((e) => e.name === name);
    if (!ex) return;
    const current = editor.getValue();
    const edited = current.trim() && !examples.some((e) => e.code === current);
    if (edited && !confirm(`Replace the code in the editor with the ${ex.title} example?`)) return;
    editor.setValue(ex.code);
    editor.setCursor(0, 0);
    editor.scrollTo(0, 0);
    toast(`Loaded ${ex.title}`);
    if (INPUT_CALL.test(ex.code)) $("stdin-box").open = true;
    if (activeTab === "output") runProgram();
  }

  // Share
  function toBase64Url(bytes) {
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function fromBase64Url(text) {
    const b64 = text.replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(b64 + "===".slice((b64.length + 3) % 4));
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  async function transform(bytes, stream) {
    const piped = new Blob([bytes]).stream().pipeThrough(stream);
    return new Uint8Array(await new Response(piped).arrayBuffer());
  }

  // "z." and the compressed text if supported, otherwise "u." and the plain text (base64url)
  async function encodeText(text) {
    const bytes = new TextEncoder().encode(text);
    if (typeof CompressionStream === "function") {
      try {
        return "z." + toBase64Url(await transform(bytes, new CompressionStream("deflate-raw")));
      } catch (e) {
        // Use plain text
      }
    }
    return "u." + toBase64Url(bytes);
  }

  async function decodeText(value) {
    const [scheme, data] = [value.slice(0, 2), value.slice(2)];
    const bytes = fromBase64Url(data);
    if (scheme === "u.") return new TextDecoder().decode(bytes);
    if (scheme === "z.") {
      if (typeof DecompressionStream !== "function") throw new Error("this browser cannot open compressed links");
      return new TextDecoder().decode(await transform(bytes, new DecompressionStream("deflate-raw")));
    }
    throw new Error("unknown link format");
  }

  async function shareLink() {
    const params = new URLSearchParams();
    params.set("code", await encodeText(editor.getValue()));
    const stdin = $("stdin").value;
    if (stdin) params.set("stdin", await encodeText(stdin));
    const url = `${location.origin}${location.pathname}${location.search}#${params.toString()}`;
    history.replaceState(null, "", url);
    hashIsCurrent = true;
    let copied = false;
    try {
      await navigator.clipboard.writeText(url);
      copied = true;
    } catch (e) {
      // Clipboard may be blocked, the link is in the address bar anyway
    }
    toast(copied ? "Link copied" : "Copy the link from the address bar");
  }

  async function readHash() {
    const hash = location.hash.slice(1);
    if (!hash) return null;
    const params = new URLSearchParams(hash);
    const code = params.get("code");
    if (!code) return null;
    try {
      const result = { code: await decodeText(code) };
      if (params.get("stdin")) result.stdin = await decodeText(params.get("stdin"));
      return result;
    } catch (e) {
      toast("Could not open the shared link", 5000);
      return null;
    }
  }

  // Format
  async function formatCode() {
    if (!py || !py.loaded) return toast("Python is still loading");
    const code = editor.getValue();
    let result;
    try {
      result = await py.call("format", { code });
    } catch (e) {
      return;
    }
    if (!result.ok) {
      const first = (result.stderr || "").split("\n")[0].replace(/^error:\s*/, "");
      return toast(`Can't format: ${first}`, 5000);
    }
    if (editor.getValue() !== code) return; // code changed while formatting
    if (!result.changed) return toast("Already formatted");
    const cursor = editor.getCursor();
    const scroll = editor.getScrollInfo();
    editor.operation(() => {
      editor.replaceRange(result.code, CodeMirror.Pos(0, 0), CodeMirror.Pos(editor.lastLine()));
      editor.setCursor({ line: Math.min(cursor.line, editor.lastLine()), ch: cursor.ch });
      editor.scrollTo(scroll.left, scroll.top);
    });
    toast("Formatted");
  }

  // Copy
  async function copyActive() {
    const box = activeTab === "output" ? $("output") : $(activeTab);
    const text = box.innerText.replace(/\u00a0/g, " ");
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied");
    } catch (e) {
      toast("Could not copy");
    }
  }

  // Splitter
  function setupSplitter() {
    const splitter = $("splitter");
    const workspace = $("workspace");
    const apply = (ratio) => {
      ratio = Math.min(0.8, Math.max(0.2, ratio));
      workspace.style.setProperty("--left", `${ratio}fr`);
      workspace.style.setProperty("--right", `${1 - ratio}fr`);
      splitter.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
      return ratio;
    };
    let ratio = apply(Number(load("split")) || 0.5);
    const save = () => store("split", ratio.toFixed(3));
    splitter.addEventListener("pointerdown", (e) => {
      splitter.setPointerCapture(e.pointerId);
      splitter.classList.add("dragging");
      // Measure the two panes, so padding and the splitter's own width don't throw the ratio off
      const left = workspace.firstElementChild.getBoundingClientRect().left;
      const right = workspace.lastElementChild.getBoundingClientRect().right;
      const width = right - left - splitter.offsetWidth;
      const move = (ev) => {
        ratio = apply((ev.clientX - left - splitter.offsetWidth / 2) / width);
        editor.refresh();
      };
      const up = () => {
        splitter.classList.remove("dragging");
        splitter.removeEventListener("pointermove", move);
        splitter.removeEventListener("pointerup", up);
        save();
      };
      splitter.addEventListener("pointermove", move);
      splitter.addEventListener("pointerup", up);
    });
    splitter.addEventListener("keydown", (e) => {
      const step = e.shiftKey ? 0.1 : 0.03;
      if (e.key === "ArrowLeft") ratio = apply(ratio - step);
      else if (e.key === "ArrowRight") ratio = apply(ratio + step);
      else return;
      e.preventDefault();
      editor.refresh();
      save();
    });
  }

  // Code changes
  let hashIsCurrent = false;
  const saveCode = debounce(() => store("code", editor.getValue()), 300);

  function onCodeChange() {
    saveCode();
    clearRunMarks();
    if (hashIsCurrent) {
      // Remove the old shared link from the URL
      history.replaceState(null, "", location.pathname + location.search);
      hashIsCurrent = false;
    }
    if (activeTab !== "output") scheduleRefresh();
  }

  // Init
  async function fetchBundle() {
    const response = await fetch(BUNDLE_URL, { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const text = await response.text();
    return { text, bundle: JSON.parse(text) };
  }

  async function main() {
    setupTheme();
    if (typeof CodeMirror === "undefined") {
      setStatus("error", "Could not load the editor. Check the connection and reload the page.");
      return;
    }
    $("run").title = `Run (${IS_MAC ? "Cmd" : "Ctrl"}+Enter)`;

    // The editor needs the word lists and the hello example from the bundle, so load it first
    let text = null;
    let bundle = {};
    let bundleError = null;
    try {
      ({ text, bundle } = await fetchBundle());
    } catch (e) {
      bundleError = e;
    }
    $("version").textContent = bundle.version ? `v${bundle.version}` : "";
    setLanguage(bundle.language);
    defineMode();

    const shared = await readHash();
    const hello = (bundle.files || {})["examples/hello.jlang"] || "";
    const initial = shared ? shared.code : load("code") || hello;
    setupEditor(initial);
    hashIsCurrent = !!shared;
    if (shared && shared.stdin !== undefined) $("stdin").value = shared.stdin;
    else $("stdin").value = load("stdin") || "";
    updateStdinBadge();
    if ($("stdin").value || INPUT_CALL.test(initial)) $("stdin-box").open = true;

    setupTabs();
    setupSplitter();
    const savedTarget = load("asm-target");
    if (["linux", "windows", "macos"].includes(savedTarget)) $("asm-target").value = savedTarget;

    $("run").addEventListener("click", () => runProgram());
    $("stop").addEventListener("click", stopProgram);
    $("format").addEventListener("click", formatCode);
    $("share").addEventListener("click", shareLink);
    $("copy").addEventListener("click", copyActive);
    $("problems").addEventListener("click", () => {
      const first = problems.find((d) => d.line);
      if (first) jumpTo(first.line, first.column, first.end_line, first.end_column);
    });
    $("stdin").addEventListener("input", () => {
      updateStdinBadge();
      store("stdin", $("stdin").value);
    });
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !e.defaultPrevented) {
        e.preventDefault();
        runProgram();
      }
    });
    window.addEventListener("hashchange", async () => {
      const next = await readHash();
      if (next) {
        hashIsCurrent = false;
        editor.setValue(next.code);
        if (next.stdin !== undefined) $("stdin").value = next.stdin;
        updateStdinBadge();
        hashIsCurrent = true;
      }
    });

    if (bundleError) {
      setStatus("error", `Could not load ${BUNDLE_URL}`);
      $("output").replaceChildren(renderDiagnostics(`error: could not load ${BUNDLE_URL}\n  = note: ${bundleError.message}\n` +
        "  = help: run `jarlang playground`, or run `python tools/build_playground.py` and serve the playground folder"));
      return;
    }
    setupExamples(bundle);

    py = new PythonWorker(text, onWorkerState);
    const savedTab = load("tab");
    showTab(TABS.includes(savedTab) ? savedTab : "output");
    // Run the program as soon as Python is ready
    if (activeTab === "output") runProgram();
  }

  main();
})();
