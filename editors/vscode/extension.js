// Starts the JarLang language server and adds commands to run files and show their assembly
'use strict';

const vscode = require('vscode');
const cp = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const TERMINAL_NAME = 'JarLang';
const DONT_SHOW_KEY = 'jarlang.hideMissingServerWarning';

/** @type {import('vscode-languageclient/node').LanguageClient | null} */
let client = null;
/** @type {vscode.OutputChannel} */
let output;
/** @type {vscode.ExtensionContext} */
let extensionContext;
let warnedThisSession = false;
let starting = null;
// The first way of running JarLang that worked, cleared on restart
let found = null;

// Configuration

function settings() {
  return vscode.workspace.getConfiguration('jarlang');
}

// Expand ~, ${workspaceFolder} and ${userHome} in a path setting
function expandPath(value) {
  if (!value) return value;
  let out = value.trim();
  const folder = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
  out = out.replace(/\$\{workspaceFolder\}/g, folder ? folder.uri.fsPath : '');
  out = out.replace(/\$\{userHome\}/g, os.homedir());
  if (out === '~' || out.startsWith('~/') || out.startsWith('~\\')) {
    out = path.join(os.homedir(), out.slice(1));
  }
  return out;
}

// Ways to run JarLang, in the order they're tried. The settings win if they're set. Otherwise
// the workspace's .venv (made by scripts/setup) comes before jarlang on the PATH, since the
// PATH one might be an older install
function candidates() {
  const python = expandPath(settings().get('pythonPath', ''));
  if (python) return [{ command: python, args: ['-m', 'jarlang'] }];
  const exe = expandPath(settings().get('path', 'jarlang')) || 'jarlang';
  if (exe !== 'jarlang') return [{ command: exe, args: [] }];
  const out = [];
  for (const folder of vscode.workspace.workspaceFolders || []) {
    for (const python of [path.join('.venv', 'Scripts', 'python.exe'), path.join('.venv', 'bin', 'python')]) {
      const full = path.join(folder.uri.fsPath, python);
      if (fs.existsSync(full)) out.push({ command: full, args: ['-m', 'jarlang'] });
    }
  }
  out.push({ command: 'jarlang', args: [] });
  return out;
}

// Resolves to { command, args } for the first candidate that works, or { problem }
async function findJarlang() {
  if (found) return found;
  const problems = [];
  for (const candidate of candidates()) {
    const problem = await probe(candidate);
    if (!problem) {
      found = candidate;
      output.appendLine(`[jarlang] using ${describe(candidate)}`);
      return found;
    }
    problems.push(problem);
  }
  return { problem: problems.join(', ') };
}

// Command to run JarLang with these arguments. If nothing works, the first candidate is used
// so the error shows up in the terminal or output
async function jarlangCommand(args) {
  const result = await findJarlang();
  const base = result.problem ? candidates()[0] : result;
  return { command: base.command, args: [...base.args, ...args] };
}

function describe({ command, args }) {
  return [command, ...args].join(' ');
}

function childEnv() {
  return Object.assign({}, process.env, { NO_COLOR: '1', PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' });
}

// Resolves to null if `<candidate> --version` prints a JarLang 2 (or newer) version, otherwise to the problem
function probe(candidate) {
  const { command, args } = candidate;
  return new Promise((resolve) => {
    try {
      const child = cp.execFile(
        command,
        [...args, '--version'],
        { env: childEnv(), timeout: 15000, windowsHide: true },
        (err, stdout, stderr) => {
          if (err && err.code === 'ENOENT') return resolve(`\`${command}\` was not found`);
          if (err) {
            const detail = (stderr || stdout || err.message || '').toString().trim().split(/\r?\n/).pop();
            return resolve(`\`${describe(candidate)} --version\` failed${detail ? `: ${detail}` : ''}`);
          }
          const version = stdout.toString().trim().split(/\r?\n/)[0];
          const major = /^JarLang (\d+)\./.exec(version);
          if (!major || Number(major[1]) < 2) {
            return resolve(`\`${describe(candidate)}\` is not JarLang 2 (\`--version\` printed "${version}")`);
          }
          resolve(null);
        }
      );
      // Close stdin so a program that waits for input fails right away instead of at the timeout
      if (child.stdin) child.stdin.end();
    } catch (err) {
      resolve(`\`${command}\` could not be started: ${err.message}`);
    }
  });
}

// Language server

async function warnMissingServer(problem) {
  output.appendLine(`[jarlang] language server not started: ${problem}`);
  if (warnedThisSession || extensionContext.globalState.get(DONT_SHOW_KEY)) return;
  warnedThisSession = true;
  const openSettings = 'Open Settings';
  const dontShow = "Don't Show Again";
  const choice = await vscode.window.showWarningMessage(
    `JarLang: could not start the language server (${problem}). ` +
      'Run scripts/setup in the JarLang folder, install it with `pip install -e .`, or set `jarlang.pythonPath`.',
    openSettings,
    dontShow
  );
  if (choice === openSettings) {
    vscode.commands.executeCommand('workbench.action.openSettings', 'jarlang.');
  } else if (choice === dontShow) {
    extensionContext.globalState.update(DONT_SHOW_KEY, true);
  }
}

async function startClient() {
  if (!settings().get('lsp.enabled', true)) {
    output.appendLine('[jarlang] language server disabled (jarlang.lsp.enabled = false)');
    return;
  }
  let lc;
  try {
    lc = require('vscode-languageclient/node');
  } catch (err) {
    output.appendLine('[jarlang] vscode-languageclient is missing; run `npm install` in editors/vscode. ' + err.message);
    return;
  }
  const result = await findJarlang();
  if (result.problem) {
    await warnMissingServer(result.problem);
    return;
  }
  const command = result.command;
  const args = [...result.args, 'lsp'];
  const executable = { command, args, transport: lc.TransportKind.stdio, options: { env: childEnv() } };
  const serverOptions = { run: executable, debug: executable };
  const clientOptions = {
    documentSelector: [
      { scheme: 'file', language: 'jarlang' },
      { scheme: 'untitled', language: 'jarlang' },
    ],
    outputChannel: output,
    // The extension has its own snippets, so don't list them twice
    initializationOptions: { snippets: false },
  };
  const newClient = new lc.LanguageClient('jarlang', 'JarLang Language Server', serverOptions, clientOptions);
  try {
    await newClient.start();
    client = newClient;
    output.appendLine(`[jarlang] language server started: ${[command, ...args].join(' ')}`);
  } catch (err) {
    client = null;
    try {
      await newClient.stop();
    } catch (_) {
      // It never started
    }
    await warnMissingServer(`\`${[command, ...args].join(' ')}\` failed: ${err && err.message ? err.message : err}`);
  }
}

async function stopClient() {
  const old = client;
  client = null;
  if (old) {
    try {
      await old.stop();
    } catch (err) {
      output.appendLine(`[jarlang] error while stopping the language server: ${err.message}`);
    }
  }
}

function restartClient() {
  // Restart one at a time so quick setting changes can't start two servers
  const previous = starting || Promise.resolve();
  starting = previous.then(async () => {
    await stopClient();
    found = null;
    await startClient();
  });
  return starting;
}

// Commands

// The clicked file or the active editor's document
async function targetDocument(uri) {
  if (uri instanceof vscode.Uri) {
    const open = vscode.workspace.textDocuments.find((d) => d.uri.toString() === uri.toString());
    return open || vscode.workspace.openTextDocument(uri);
  }
  const editor = vscode.window.activeTextEditor;
  return editor ? editor.document : undefined;
}

// Save the document and return its path, or undefined if it can't be run
async function savedFile(uri) {
  const doc = await targetDocument(uri);
  if (!doc || doc.languageId !== 'jarlang') {
    vscode.window.showWarningMessage('JarLang: open a .jlang file first.');
    return undefined;
  }
  if (doc.isUntitled) {
    const saved = await doc.save();
    if (!saved) {
      vscode.window.showWarningMessage('JarLang: save the file before running it.');
      return undefined;
    }
    const editor = vscode.window.activeTextEditor;
    return editor && editor.document.languageId === 'jarlang' && !editor.document.isUntitled
      ? editor.document.fileName
      : undefined;
  }
  if (doc.isDirty && !(await doc.save())) {
    vscode.window.showWarningMessage('JarLang: could not save the file.');
    return undefined;
  }
  return doc.fileName;
}

// The terminal's shell: "powershell", "cmd" or "posix"
function shellKind() {
  const shell = path.basename((vscode.env.shell || '').replace(/\\/g, '/')).toLowerCase();
  if (shell.includes('pwsh') || shell.includes('powershell')) return 'powershell';
  if (/(^|[^a-z])(ba|z|fi|k|da|a)?sh(\.exe)?$/.test(shell) || shell.startsWith('wsl')) return 'posix';
  return process.platform === 'win32' ? 'cmd' : 'posix';
}

// Quote a word for the terminal's shell (always if force is set)
function quote(arg, force = false) {
  if (!force && /^[\w@%+=:,./\\-]+$/.test(arg)) return arg;
  switch (shellKind()) {
    case 'powershell':
      return `'${arg.replace(/'/g, "''")}'`;
    case 'cmd':
      return `"${arg.replace(/"/g, '""')}"`;
    default:
      return `'${arg.replace(/'/g, `'\\''`)}'`;
  }
}

function jarlangTerminal() {
  let terminal = vscode.window.terminals.find((t) => t.name === TERMINAL_NAME);
  if (!terminal) {
    terminal = vscode.window.createTerminal({ name: TERMINAL_NAME });
  }
  return terminal;
}

async function runFile(uri, extraArgs) {
  const file = await savedFile(uri);
  if (!file) return;
  const { command, args } = await jarlangCommand(['run', ...extraArgs, file]);
  const quotedCommand = quote(command);
  // PowerShell needs & to run a quoted program path
  const prefix = shellKind() === 'powershell' && quotedCommand !== command ? '& ' : '';
  // Always quote the file so paths with spaces work
  const quotedArgs = args.map((a) => quote(a, a === file));
  const terminal = jarlangTerminal();
  terminal.show(true);
  terminal.sendText(`${prefix}${quotedCommand} ${quotedArgs.join(' ')}`);
}

// Run jarlang <subcommand> <file> and show the output in a new editor
async function showCommandOutput(uri, subcommand, languageId, title) {
  const file = await savedFile(uri);
  if (!file) return;
  const { command, args } = await jarlangCommand([subcommand, file]);
  const cmdline = [command, ...args].join(' ');
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Window, title: `JarLang: ${title}…` },
    () =>
      new Promise((resolve) => {
        cp.execFile(
          command,
          args,
          { env: childEnv(), cwd: path.dirname(file), maxBuffer: 64 * 1024 * 1024, timeout: 120000, windowsHide: true },
          async (err, stdout, stderr) => {
            try {
              if (err) {
                output.appendLine(`$ ${cmdline}`);
                if (stderr) output.appendLine(stderr.toString().trimEnd());
                if (!stderr && stdout) output.appendLine(stdout.toString().trimEnd());
                if (err.code === 'ENOENT') {
                  output.appendLine(`[jarlang] \`${command}\` was not found; set jarlang.path or jarlang.pythonPath.`);
                }
                output.show(true);
                vscode.window.showErrorMessage(`JarLang: ${title} failed. See the JarLang output for details.`);
                return;
              }
              const languages = await vscode.languages.getLanguages();
              const language = languages.includes(languageId) ? languageId : 'plaintext';
              const doc = await vscode.workspace.openTextDocument({ content: stdout.toString(), language });
              await vscode.window.showTextDocument(doc, { viewColumn: vscode.ViewColumn.Beside, preview: false });
            } finally {
              resolve();
            }
          }
        );
      })
  );
}

// Activation

async function activate(context) {
  extensionContext = context;
  output = vscode.window.createOutputChannel('JarLang');
  context.subscriptions.push(output);

  const register = (id, fn) => context.subscriptions.push(vscode.commands.registerCommand(id, fn));
  register('jarlang.run', (uri) => runFile(uri, []));
  register('jarlang.runNative', (uri) => runFile(uri, ['--native']));
  register('jarlang.showAssembly', (uri) => showCommandOutput(uri, 'asm', 'asm', 'Show Assembly'));
  register('jarlang.showSyntaxTree', (uri) => showCommandOutput(uri, 'ast', 'plaintext', 'Show Syntax Tree'));
  register('jarlang.restartServer', async () => {
    warnedThisSession = false;
    await restartClient();
    if (client) vscode.window.setStatusBarMessage('JarLang language server restarted', 3000);
  });

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (
        event.affectsConfiguration('jarlang.path') ||
        event.affectsConfiguration('jarlang.pythonPath') ||
        event.affectsConfiguration('jarlang.lsp.enabled')
      ) {
        warnedThisSession = false;
        restartClient();
      }
    })
  );

  await restartClient();
}

function deactivate() {
  return stopClient();
}

module.exports = { activate, deactivate, candidates, probe };
