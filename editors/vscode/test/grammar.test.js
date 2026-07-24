// Tokenizes JarLang code with the same TextMate engine as VS Code and checks the scopes
// Run with: npm install && npm test
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vsctm = require('vscode-textmate');
const oniguruma = require('vscode-oniguruma');

const GRAMMAR = path.join(__dirname, '..', 'syntaxes', 'jarlang.tmLanguage.json');

async function loadGrammar() {
  const wasm = fs.readFileSync(require.resolve('vscode-oniguruma/release/onig.wasm'));
  await oniguruma.loadWASM(wasm.buffer.slice(wasm.byteOffset, wasm.byteOffset + wasm.byteLength));
  const registry = new vsctm.Registry({
    onigLib: Promise.resolve({
      createOnigScanner: (patterns) => new oniguruma.OnigScanner(patterns),
      createOnigString: (s) => new oniguruma.OnigString(s),
    }),
    loadGrammar: async (scopeName) => {
      if (scopeName !== 'source.jarlang') return null;
      return vsctm.parseRawGrammar(fs.readFileSync(GRAMMAR, 'utf8'), GRAMMAR);
    },
  });
  return registry.loadGrammar('source.jarlang');
}

// Tokenize text (one or more lines) into [{text, scopes, line}]
function tokenize(grammar, text) {
  let stack = vsctm.INITIAL;
  const out = [];
  text.split('\n').forEach((line, lineNo) => {
    const result = grammar.tokenizeLine(line, stack);
    for (const t of result.tokens) {
      out.push({ text: line.substring(t.startIndex, t.endIndex), scopes: t.scopes, line: lineNo });
    }
    stack = result.ruleStack;
  });
  return out;
}

let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    passed++;
  } catch (err) {
    failures.push(`${name}: ${err.message}`);
  }
}

function describe(tokens) {
  return tokens.map((t) => `  ${JSON.stringify(t.text)} -> ${t.scopes.slice(1).join(' ')}`).join('\n');
}

// Check that the nth token with this text has the scope (or doesn't, with negate)
function expectScope(tokens, text, scope, { nth = 0, negate = false } = {}) {
  // Plain names can include the spaces around them, so compare trimmed text
  const matches = tokens.filter((t) => t.text === text || t.text.trim() === text);
  assert.ok(matches.length > nth, `no token ${JSON.stringify(text)} (#${nth}) in:\n${describe(tokens)}`);
  const has = matches[nth].scopes.some((s) => s === scope || s.startsWith(scope + '.'));
  assert.ok(negate ? !has : has,
    `token ${JSON.stringify(text)} ${negate ? 'should not have' : 'should have'} scope ${scope}; ` +
    `got [${matches[nth].scopes.join(', ')}]\nall tokens:\n${describe(tokens)}`);
}

(async () => {
  const g = await loadGrammar();
  const t = (text) => tokenize(g, text);

  check('comment', () => {
    const tokens = t('x = 1  # the answer');
    expectScope(tokens, '#', 'punctuation.definition.comment.jarlang');
    expectScope(tokens, ' the answer', 'comment.line.number-sign.jarlang');
    expectScope(tokens, '1', 'constant.numeric.decimal.jarlang');
  });

  check('string interpolation with format spec', () => {
    const tokens = t('print("{name} is {age:>4} years")');
    expectScope(tokens, 'print', 'support.function.builtin.jarlang');
    expectScope(tokens, '"', 'punctuation.definition.string.begin.jarlang');
    expectScope(tokens, '{', 'punctuation.section.interpolation.begin.jarlang');
    expectScope(tokens, 'name', 'meta.embedded.line.jarlang');
    expectScope(tokens, 'name', 'source.jarlang');
    expectScope(tokens, ' is ', 'string.quoted.double.jarlang');
    expectScope(tokens, ' is ', 'meta.interpolation.jarlang', { negate: true });
    expectScope(tokens, ':', 'punctuation.separator.format-spec.jarlang');
    expectScope(tokens, '>4', 'constant.other.format-spec.jarlang');
    expectScope(tokens, '}', 'punctuation.section.interpolation.end.jarlang', { nth: 1 });
  });

  check('expressions inside interpolation', () => {
    const tokens = t('s = "total: {sum(xs[1:3]) + 2x}, {join(ys, ", ")}"');
    expectScope(tokens, 'sum', 'support.function.builtin.jarlang');
    expectScope(tokens, ':', 'punctuation.separator.format-spec.jarlang', { nth: 0, negate: true });
    expectScope(tokens, '2', 'constant.numeric.decimal.jarlang');
    expectScope(tokens, 'join', 'support.function.builtin.jarlang');
    expectScope(tokens, ', ', 'string.quoted.double.jarlang', { nth: 1 });
    expectScope(tokens, '"', 'punctuation.definition.string.end.jarlang', { nth: 3 });
  });

  check('escapes and single-quoted strings', () => {
    const tokens = t(String.raw`a = "tab\tbrace\{ \u{1F600} \q"` + '\n' + String.raw`b = 'raw {not interpolated}\n'`);
    expectScope(tokens, '\\t', 'constant.character.escape.jarlang');
    expectScope(tokens, '\\{', 'constant.character.escape.jarlang');
    expectScope(tokens, '\\u{1F600}', 'constant.character.escape.jarlang');
    expectScope(tokens, '\\q', 'invalid.illegal.unknown-escape.jarlang');
    expectScope(tokens, 'raw {not interpolated}', 'string.quoted.single.jarlang');
    expectScope(tokens, '\\n', 'constant.character.escape.jarlang');
  });

  check('numbers', () => {
    const tokens = t('n = 0xFF + 0b1010 + 0o17 + 1_000_000 + 3.14 + 6.02e23 + 2x');
    expectScope(tokens, '0xFF', 'constant.numeric.hex.jarlang');
    expectScope(tokens, '0b1010', 'constant.numeric.binary.jarlang');
    expectScope(tokens, '0o17', 'constant.numeric.octal.jarlang');
    expectScope(tokens, '1_000_000', 'constant.numeric.decimal.jarlang');
    expectScope(tokens, '3.14', 'constant.numeric.decimal.jarlang');
    expectScope(tokens, '6.02e23', 'constant.numeric.decimal.jarlang');
    expectScope(tokens, '2', 'constant.numeric.decimal.jarlang');
    expectScope(tokens, 'x', 'constant.numeric', { negate: true });
    const ident = t('x2 = 1..10');
    expectScope(ident, 'x2', 'constant.numeric', { negate: true });
    expectScope(ident, '..', 'keyword.operator.range.jarlang');
    expectScope(ident, '10', 'constant.numeric.decimal.jarlang');
  });

  check('keywords', () => {
    const tokens = t('for i, v in enumerate(xs) { if v > 1 and not done { break } elif true { continue } else { return nil } }');
    expectScope(tokens, 'for', 'keyword.control.jarlang');
    expectScope(tokens, 'in', 'keyword.control.jarlang');
    expectScope(tokens, 'if', 'keyword.control.jarlang');
    expectScope(tokens, 'elif', 'keyword.control.jarlang');
    expectScope(tokens, 'break', 'keyword.control.jarlang');
    expectScope(tokens, 'and', 'keyword.operator.logical.jarlang');
    expectScope(tokens, 'not', 'keyword.operator.logical.jarlang');
    expectScope(tokens, 'true', 'constant.language.boolean.jarlang');
    expectScope(tokens, 'nil', 'constant.language.nil.jarlang');
    expectScope(tokens, 'enumerate', 'support.function.builtin.jarlang');
    const tc = t('try { risky() } catch err { throw err }');
    expectScope(tc, 'try', 'keyword.control.jarlang');
    expectScope(tc, 'catch', 'keyword.control.exception.jarlang');
    expectScope(tc, 'err', 'variable.other.exception.jarlang');
    expectScope(tc, 'throw', 'keyword.control.jarlang');
    expectScope(tc, 'risky', 'entity.name.function.call.jarlang');
  });

  check('builtin and user calls, methods, pipelines', () => {
    const tokens = t('ys = xs.map(fn(x) => x^2).total(1) |> filter(is_prime) |> sum; p.name');
    expectScope(tokens, 'map', 'support.function.builtin.jarlang');
    expectScope(tokens, 'total', 'entity.name.function.member.jarlang');
    expectScope(tokens, '|>', 'keyword.operator.pipe.jarlang');
    expectScope(tokens, 'filter', 'support.function.builtin.jarlang');
    expectScope(tokens, 'is_prime', 'support.function.builtin.jarlang');
    expectScope(tokens, 'sum', 'support.function.builtin.jarlang');
    expectScope(tokens, 'name', 'variable.other.property.jarlang');
    expectScope(tokens, 'x', 'variable.parameter.jarlang');
    expectScope(tokens, '=>', 'keyword.operator.arrow.jarlang');
    const user = t('my_print(1); print_all');
    expectScope(user, 'my_print', 'entity.name.function.call.jarlang');
    expectScope(user, 'my_print', 'support.function', { negate: true });
    expectScope(user, 'print_all', 'support.function', { negate: true });
  });

  check('fn declarations with types', () => {
    const tokens = t('fn fib(n: int, memo: dict[int, int] = {}) -> int {');
    expectScope(tokens, 'fn', 'storage.type.function.jarlang');
    expectScope(tokens, 'fib', 'entity.name.function.jarlang');
    expectScope(tokens, 'n', 'variable.parameter.jarlang');
    expectScope(tokens, 'int', 'support.type.primitive.jarlang');
    expectScope(tokens, 'memo', 'variable.parameter.jarlang');
    expectScope(tokens, 'dict', 'support.type.primitive.jarlang');
    expectScope(tokens, '->', 'keyword.operator.arrow.jarlang');
    expectScope(tokens, 'int', 'support.type.primitive.jarlang', { nth: 3 });
    const arrow = t('fn sq(x) => x * x');
    expectScope(arrow, 'sq', 'entity.name.function.jarlang');
    expectScope(arrow, '*', 'keyword.operator.arithmetic.jarlang');
  });

  check('math-style function definitions', () => {
    const tokens = t('f(x, y) = x^2 + y');
    expectScope(tokens, 'f', 'entity.name.function.jarlang');
    expectScope(tokens, 'x', 'variable.parameter.jarlang');
    expectScope(tokens, 'y', 'variable.parameter.jarlang');
    expectScope(tokens, '=', 'keyword.operator.assignment.jarlang');
    const cmp = t('f(x) == 3');
    expectScope(cmp, 'f', 'entity.name.function.call.jarlang');
    expectScope(cmp, '==', 'keyword.operator.comparison.jarlang');
  });

  check('declarations', () => {
    const tokens = t('const LIMIT: int = 10\nlet total = 0');
    expectScope(tokens, 'const', 'storage.type.const.jarlang');
    expectScope(tokens, 'LIMIT', 'variable.other.constant.jarlang');
    expectScope(tokens, 'int', 'support.type.primitive.jarlang');
    expectScope(tokens, 'let', 'storage.type.let.jarlang');
    expectScope(tokens, 'total', 'variable.other.declaration.jarlang');
  });

  check('math constants and unicode operators', () => {
    const tokens = t('area = π × r^2 ÷ 2 + √e ≤ ∞ != tau; n! ≠ 0');
    expectScope(tokens, 'π', 'support.constant.math.jarlang');
    expectScope(tokens, '×', 'keyword.operator.arithmetic.jarlang');
    expectScope(tokens, '÷', 'keyword.operator.arithmetic.jarlang');
    expectScope(tokens, '√', 'keyword.operator.sqrt.jarlang');
    expectScope(tokens, 'e', 'support.constant.math.jarlang');
    expectScope(tokens, '≤', 'keyword.operator.comparison.jarlang');
    expectScope(tokens, '∞', 'support.constant.math.jarlang');
    expectScope(tokens, 'tau', 'support.constant.math.jarlang');
    expectScope(tokens, '!', 'keyword.operator.factorial.jarlang');
    expectScope(tokens, '≠', 'keyword.operator.comparison.jarlang');
    const names = t('radius_e = πr');
    expectScope(names, 'radius_e', 'support.constant', { negate: true });
    expectScope(names, 'πr', 'support.constant', { negate: true });
  });

  check('operators', () => {
    const tokens = t('x //= 2; y += 1; z = a // b % c ** d; ok = !done && (p || q); r = 0..<n');
    expectScope(tokens, '//=', 'keyword.operator.assignment.compound.jarlang');
    expectScope(tokens, '+=', 'keyword.operator.assignment.compound.jarlang');
    expectScope(tokens, '//', 'keyword.operator.arithmetic.jarlang');
    expectScope(tokens, '**', 'keyword.operator.arithmetic.jarlang');
    expectScope(tokens, '!', 'keyword.operator.logical.jarlang');
    expectScope(tokens, '&&', 'keyword.operator.logical.jarlang');
    expectScope(tokens, '..<', 'keyword.operator.range.jarlang');
  });

  check('import and dict literals', () => {
    const tokens = t('import "lib/geometry.jlang" as geo\nperson = {name: "Ada", born: 1815}');
    expectScope(tokens, 'import', 'keyword.control.import.jarlang');
    expectScope(tokens, 'lib/geometry.jlang', 'string.quoted.double.jarlang');
    expectScope(tokens, 'as', 'keyword.control.import.jarlang');
    expectScope(tokens, 'geo', 'entity.name.namespace.jarlang');
    expectScope(tokens, 'name', 'variable.other.property.key.jarlang');
    expectScope(tokens, 'born', 'variable.other.property.key.jarlang');
  });

  check('multi-line code keeps state', () => {
    const tokens = t('msg = "a {x +\n1}"\nprint(msg)');
    expectScope(tokens, 'print', 'support.function.builtin.jarlang');
    expectScope(tokens, 'print', 'string', { negate: true });
  });

  if (failures.length) {
    console.error(`${failures.length} grammar check(s) failed, ${passed} passed:\n`);
    for (const f of failures) console.error(`FAIL ${f}\n`);
    process.exit(1);
  }
  console.log(`grammar: ${passed} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
