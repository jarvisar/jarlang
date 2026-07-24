// Checks how the extension finds JarLang, with a fake vscode module
// Run with: npm test
'use strict';

const assert = require('assert');
const fs = require('fs');
const Module = require('module');
const os = require('os');
const path = require('path');

const settings = {};
const fakeVscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({ get: (key, fallback) => (key in settings ? settings[key] : fallback) }),
  },
};
const load = Module._load;
Module._load = function (request, ...rest) {
  return request === 'vscode' ? fakeVscode : load.call(this, request, ...rest);
};
const { candidates, probe } = require('../extension.js');

// A command that runs this JavaScript and ignores the --version argument added by probe
function node(code) {
  return { command: process.execPath, args: ['-e', code, '--'] };
}

const checks = [];
function check(name, fn) {
  checks.push([name, fn]);
}

check('uses jarlang on the PATH by default', () => {
  assert.deepStrictEqual(candidates(), [{ command: 'jarlang', args: [] }]);
});

check("tries the workspace's .venv first", () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'jarlang-ext-'));
  const python = path.join(folder, '.venv', 'bin', 'python');
  fs.mkdirSync(path.dirname(python), { recursive: true });
  fs.writeFileSync(python, '');
  fakeVscode.workspace.workspaceFolders = [{ uri: { fsPath: folder } }];
  try {
    assert.deepStrictEqual(candidates(), [
      { command: python, args: ['-m', 'jarlang'] },
      { command: 'jarlang', args: [] },
    ]);
    settings.path = '/opt/jarlang/bin/jarlang';
    assert.deepStrictEqual(candidates(), [{ command: '/opt/jarlang/bin/jarlang', args: [] }]);
    settings.pythonPath = '${workspaceFolder}/env/python';
    assert.deepStrictEqual(candidates(), [{ command: `${folder}/env/python`, args: ['-m', 'jarlang'] }]);
  } finally {
    delete settings.path;
    delete settings.pythonPath;
    fakeVscode.workspace.workspaceFolders = [];
    fs.rmSync(folder, { recursive: true, force: true });
  }
});

check('probe accepts JarLang 2', async () => {
  assert.strictEqual(await probe(node('console.log("JarLang 2.0.0")')), null);
});

check('probe rejects other programs', async () => {
  assert.match(await probe(node('console.log("JarLang 1.3")')), /is not JarLang 2/);
  assert.match(await probe(node('console.log("hello")')), /is not JarLang 2/);
  assert.match(await probe(node('process.exit(2)')), /--version` failed/);
  assert.match(await probe({ command: 'jarlang-does-not-exist', args: [] }), /was not found/);
});

check("probe doesn't wait for a program that reads stdin", async () => {
  const start = Date.now();
  const problem = await probe(node('process.stdin.resume(); process.stdin.on("end", () => process.exit(1))'));
  assert.match(problem, /failed/);
  assert.ok(Date.now() - start < 10000, 'probe waited for the timeout');
});

(async () => {
  const failures = [];
  for (const [name, fn] of checks) {
    try {
      await fn();
    } catch (err) {
      failures.push(`${name}: ${err.message}`);
    }
  }
  if (failures.length) {
    console.error(`${failures.length} extension check(s) failed:\n`);
    for (const f of failures) console.error(`FAIL ${f}\n`);
    process.exit(1);
  }
  console.log(`extension: ${checks.length} checks passed`);
})();
