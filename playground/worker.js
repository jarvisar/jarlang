// Runs Python (Pyodide) in a worker so the page doesn't freeze and a program can be stopped
// Page to worker: init, call. Worker to page: status, ready, init-error, stdout, result, fatal
"use strict";

const PYODIDE_VERSION = "0.29.5";
const PYODIDE_URL = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
// Where the bundled jarlang package and examples go in Pyodide's file system
const ROOT = "/home/pyodide/jarlang";

let web = null; // the jarlang.web module
let currentId = null; // id of the running call (for streamed stdout)
let dead = false; // true after a fatal error
let ready = null; // resolves once Python and JarLang are loaded

function post(message) {
  self.postMessage(message);
}

async function init(bundleText) {
  importScripts(PYODIDE_URL + "pyodide.js");
  const bundle = JSON.parse(bundleText);
  const pyodide = await self.loadPyodide({
    indexURL: PYODIDE_URL,
    stdout: (text) => console.log("[python]", text),
    stderr: (text) => console.warn("[python]", text),
  });
  post({ type: "status", text: "Starting JarLang..." });
  const FS = pyodide.FS;
  for (const [rel, text] of Object.entries(bundle.files)) {
    const path = `${ROOT}/${rel}`;
    FS.mkdirTree(path.slice(0, path.lastIndexOf("/")));
    FS.writeFile(path, text);
  }
  pyodide.runPython(`
import os, sys
sys.path.insert(0, ${JSON.stringify(ROOT)})
import jarlang.web
# Programs run "inside" the examples folder, so \`import "lib/geometry"\` works
os.chdir(jarlang.web.examples_dir())
`);
  web = pyodide.pyimport("jarlang.web");
  web.set_listener((chunk) => post({ type: "stdout", id: currentId, chunk }));
  const info = JSON.parse(web.dispatch(JSON.stringify({ fn: "info", args: {} })));
  post({ type: "ready", info, pyodide: pyodide.version });
}

function handleCall({ id, fn, args }) {
  if (dead) {
    post({ type: "fatal", id, error: "Python is no longer usable" });
    return;
  }
  currentId = id;
  let reply;
  try {
    reply = web.dispatch(JSON.stringify({ fn, args }));
  } catch (err) {
    // dispatch catches Python errors, so this is a Pyodide crash (usually a stack overflow)
    dead = true;
    post({ type: "fatal", id, error: String((err && err.message) || err) });
    return;
  } finally {
    currentId = null;
  }
  post({ type: "result", id, result: JSON.parse(reply) });
}

self.onmessage = (event) => {
  const msg = event.data || {};
  if (msg.type === "init") {
    ready = init(msg.bundle).then(
      () => true,
      (err) => {
        dead = true;
        post({ type: "init-error", error: String((err && err.message) || err) });
        return false;
      },
    );
  } else if (msg.type === "call") {
    if (!ready) {
      post({ type: "fatal", id: msg.id, error: "the worker was not initialised" });
      return;
    }
    // Run calls in order once Python is ready
    ready.then((ok) => (ok ? handleCall(msg) : post({ type: "fatal", id: msg.id, error: "Python failed to load" })));
  }
};
