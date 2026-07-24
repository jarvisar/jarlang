"""Generates the grammar JSON from the jarlang package (run again after adding builtins)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # repository root

from jarlang.builtins import BUILTINS, CONSTANTS  # noqa: E402
from jarlang.tokens import KEYWORDS  # noqa: E402
from jarlang.values import Builtin  # noqa: E402

OUT = HERE.parent / "syntaxes" / "jarlang.tmLanguage.json"

ID = r"[\p{L}_][\p{L}\p{N}_]*"
NOT_ID_BEFORE = r"(?<![\p{L}\p{N}_])"
NOT_ID_AFTER = r"(?![\p{L}\p{N}_])"
TYPE = rf"(?:fn|nil|{ID})(?:\[[^\]]*\])?"

CONTROL = ["if", "elif", "else", "while", "for", "in", "break", "continue", "return", "try", "catch", "throw",
           "import"]
DECLARATION = ["fn", "let", "const"]
OPERATOR_WORDS = ["and", "or", "not"]
LANGUAGE_CONSTANTS = ["true", "false", "nil"]
PRIMITIVE_TYPES = ["int", "float", "num", "bool", "str", "list", "dict", "fn", "any", "nil"]


def words(names: list[str]) -> str:
    return "|".join(sorted(names, key=lambda n: (-len(n), n)))


def build() -> dict:
    missing = set(KEYWORDS) - set(CONTROL + DECLARATION + OPERATOR_WORDS + LANGUAGE_CONSTANTS)
    if missing:
        raise SystemExit(f"gen-grammar: classify the new keyword(s) {sorted(missing)} in {__file__}")
    functions = sorted(name for name, value in BUILTINS.items() if isinstance(value, Builtin))
    ascii_constants = sorted(name for name in CONSTANTS if name.isascii())
    unicode_constants = sorted(name for name in CONSTANTS if not name.isascii())
    builtin_fns = words(functions)

    type_capture = {"patterns": [{"include": "#type-names"}]}

    repository = {
        "comments": {"patterns": [{
            "name": "comment.line.number-sign.jarlang",
            "match": r"(#).*$",
            "captures": {"1": {"name": "punctuation.definition.comment.jarlang"}},
        }]},
        "import": {
            "begin": r"\b(import)\b",
            "beginCaptures": {"1": {"name": "keyword.control.import.jarlang"}},
            "end": r"(?=[;#])|$",
            "patterns": [
                {"include": "#strings"},
                {"match": rf"\b(as)\s+({ID})",
                 "captures": {"1": {"name": "keyword.control.import.jarlang"},
                              "2": {"name": "entity.name.namespace.jarlang"}}},
                {"match": r"\bas\b", "name": "keyword.control.import.jarlang"},
            ],
        },
        "strings": {"patterns": [{"include": "#string-double"}, {"include": "#string-single"}]},
        "string-double": {
            "name": "string.quoted.double.jarlang",
            "begin": '"',
            "beginCaptures": {"0": {"name": "punctuation.definition.string.begin.jarlang"}},
            "end": '"',
            "endCaptures": {"0": {"name": "punctuation.definition.string.end.jarlang"}},
            "patterns": [{"include": "#escapes"}, {"include": "#interpolation"}],
        },
        "string-single": {
            "name": "string.quoted.single.jarlang",
            "begin": "'",
            "beginCaptures": {"0": {"name": "punctuation.definition.string.begin.jarlang"}},
            "end": "'",
            "endCaptures": {"0": {"name": "punctuation.definition.string.end.jarlang"}},
            "patterns": [{"include": "#escapes"}],
        },
        "escapes": {"patterns": [
            {"name": "constant.character.escape.jarlang",
             "match": r"\\(?:[ntr0\\\"'{}eab]|u\{[0-9A-Fa-f]{1,6}\}|u[0-9A-Fa-f]{4}|$)"},
            {"name": "invalid.illegal.unknown-escape.jarlang", "match": r"\\."},
        ]},
        "interpolation": {
            "name": "meta.interpolation.jarlang",
            "begin": r"\{",
            "beginCaptures": {"0": {"name": "punctuation.section.interpolation.begin.jarlang"}},
            "end": r"\}",
            "endCaptures": {"0": {"name": "punctuation.section.interpolation.end.jarlang"}},
            "contentName": "meta.embedded.line.jarlang source.jarlang",
            "patterns": [
                {"match": r"(:)([^{}\"'\])]*)(?=\})",
                 "captures": {"1": {"name": "punctuation.separator.format-spec.jarlang"},
                              "2": {"name": "constant.other.format-spec.jarlang"}}},
                {"include": "#groups"},
                {"include": "$self"},
            ],
        },
        "groups": {"patterns": [
            {"begin": r"\(", "end": r"\)",
             "beginCaptures": {"0": {"name": "punctuation.parenthesis.begin.jarlang"}},
             "endCaptures": {"0": {"name": "punctuation.parenthesis.end.jarlang"}},
             "patterns": [{"include": "#groups"}, {"include": "$self"}]},
            {"begin": r"\[", "end": r"\]",
             "beginCaptures": {"0": {"name": "punctuation.definition.list.begin.jarlang"}},
             "endCaptures": {"0": {"name": "punctuation.definition.list.end.jarlang"}},
             "patterns": [{"include": "#groups"}, {"include": "$self"}]},
            {"begin": r"\{", "end": r"\}",
             "beginCaptures": {"0": {"name": "punctuation.definition.dict.begin.jarlang"}},
             "endCaptures": {"0": {"name": "punctuation.definition.dict.end.jarlang"}},
             "patterns": [{"include": "#groups"}, {"include": "$self"}]},
        ]},
        "function-declaration": {
            "name": "meta.function.jarlang",
            "begin": rf"\b(fn)\s+({ID})\s*(\()",
            "beginCaptures": {"1": {"name": "storage.type.function.jarlang"},
                              "2": {"name": "entity.name.function.jarlang"},
                              "3": {"name": "punctuation.definition.parameters.begin.jarlang"}},
            "end": r"\)",
            "endCaptures": {"0": {"name": "punctuation.definition.parameters.end.jarlang"}},
            "patterns": [{"include": "#parameters"}],
        },
        "lambda": {
            "name": "meta.function.lambda.jarlang",
            "begin": r"\b(fn)\s*(\()",
            "beginCaptures": {"1": {"name": "storage.type.function.jarlang"},
                              "2": {"name": "punctuation.definition.parameters.begin.jarlang"}},
            "end": r"\)",
            "endCaptures": {"0": {"name": "punctuation.definition.parameters.end.jarlang"}},
            "patterns": [{"include": "#parameters"}],
        },
        "parameters": {"patterns": [
            {"include": "#comments"},
            {"match": rf"(?:(?<=\()|(?<=,))\s*({ID})",
             "captures": {"1": {"name": "variable.parameter.jarlang"}}},
            {"match": rf"(:)\s*({TYPE})",
             "captures": {"1": {"name": "punctuation.separator.annotation.jarlang"}, "2": type_capture}},
            {"match": ",", "name": "punctuation.separator.parameters.jarlang"},
            {"match": r"=(?![=>])", "name": "keyword.operator.assignment.jarlang"},
            {"include": "#groups"},
            {"include": "$self"},
        ]},
        "math-function": {
            "comment": "Math-style definition at the start of a line: f(x, y) = ...",
            "match": rf"^\s*({ID})(\()([^()]*)(\))\s*(=)(?![=>])",
            "captures": {
                "1": {"name": "entity.name.function.jarlang"},
                "2": {"name": "punctuation.definition.parameters.begin.jarlang"},
                "3": {"patterns": [
                    {"match": ID, "name": "variable.parameter.jarlang"},
                    {"match": ",", "name": "punctuation.separator.parameters.jarlang"},
                ]},
                "4": {"name": "punctuation.definition.parameters.end.jarlang"},
                "5": {"name": "keyword.operator.assignment.jarlang"},
            },
        },
        "return-type": {
            "match": rf"(->)\s*({TYPE})",
            "captures": {"1": {"name": "keyword.operator.arrow.jarlang"}, "2": type_capture},
        },
        "declarations": {"patterns": [
            {"match": rf"\b(const)\s+({ID})(?:\s*(:)\s*({TYPE}))?",
             "captures": {"1": {"name": "storage.type.const.jarlang"},
                          "2": {"name": "variable.other.constant.jarlang"},
                          "3": {"name": "punctuation.separator.annotation.jarlang"},
                          "4": type_capture}},
            {"match": rf"\b(let)\s+({ID})(?:\s*(:)\s*({TYPE}))?",
             "captures": {"1": {"name": "storage.type.let.jarlang"},
                          "2": {"name": "variable.other.declaration.jarlang"},
                          "3": {"name": "punctuation.separator.annotation.jarlang"},
                          "4": type_capture}},
            {"match": rf"\b(catch)\b(?:\s+({ID}))?",
             "captures": {"1": {"name": "keyword.control.exception.jarlang"},
                          "2": {"name": "variable.other.exception.jarlang"}}},
        ]},
        "type-names": {"patterns": [
            {"match": rf"\b(?:{words(PRIMITIVE_TYPES)})\b", "name": "support.type.primitive.jarlang"},
            {"match": ID, "name": "entity.name.type.jarlang"},
            {"match": r"[\[\],]", "name": "punctuation.definition.type.jarlang"},
        ]},
        "keywords": {"patterns": [
            {"match": rf"\b(?:{words(CONTROL)})\b", "name": "keyword.control.jarlang"},
            {"match": r"\bfn\b", "name": "storage.type.function.jarlang"},
            {"match": r"\b(?:let|const)\b", "name": "storage.type.jarlang"},
            {"match": rf"\b(?:{words(OPERATOR_WORDS)})\b", "name": "keyword.operator.logical.jarlang"},
            {"match": r"\b(?:true|false)\b", "name": "constant.language.boolean.jarlang"},
            {"match": r"\bnil\b", "name": "constant.language.nil.jarlang"},
        ]},
        "constants": {"patterns": [
            {"match": rf"{NOT_ID_BEFORE}(?:{words(ascii_constants)}){NOT_ID_AFTER}",
             "name": "support.constant.math.jarlang"},
            {"match": rf"{NOT_ID_BEFORE}(?:{words(unicode_constants)}){NOT_ID_AFTER}",
             "name": "support.constant.math.jarlang"},
            {"match": "∞", "name": "support.constant.math.jarlang"},
        ]},
        "numbers": {"patterns": [
            {"match": r"(?<![\p{L}\p{N}_])0[xX][0-9A-Fa-f](?:_?[0-9A-Fa-f])*", "name": "constant.numeric.hex.jarlang"},
            {"match": r"(?<![\p{L}\p{N}_])0[bB][01](?:_?[01])*", "name": "constant.numeric.binary.jarlang"},
            {"match": r"(?<![\p{L}\p{N}_])0[oO][0-7](?:_?[0-7])*", "name": "constant.numeric.octal.jarlang"},
            {"match": r"(?<![\p{L}\p{N}_])\d(?:_?\d)*(?:\.\d(?:_?\d)*)?(?:[eE][+-]?\d(?:_?\d)*)?",
             "name": "constant.numeric.decimal.jarlang"},
        ]},
        "calls": {"patterns": [
            {"comment": "builtins used as methods: xs.map(f)",
             "match": rf"(\.)\s*(?:({builtin_fns}))(?=\()",
             "captures": {"1": {"name": "punctuation.accessor.jarlang"},
                          "2": {"name": "support.function.builtin.jarlang"}}},
            {"match": rf"(\.)\s*({ID})(?=\()",
             "captures": {"1": {"name": "punctuation.accessor.jarlang"},
                          "2": {"name": "entity.name.function.member.jarlang"}}},
            {"match": rf"(?<!\.){NOT_ID_BEFORE}(?:{builtin_fns}){NOT_ID_AFTER}",
             "name": "support.function.builtin.jarlang"},
            {"match": rf"{ID}(?=\()", "name": "entity.name.function.call.jarlang"},
            {"match": rf"(\.)\s*({ID})",
             "captures": {"1": {"name": "punctuation.accessor.jarlang"},
                          "2": {"name": "variable.other.property.jarlang"}}},
        ]},
        "dict-keys": {"patterns": [
            {"match": rf"({ID})\s*(:)(?=\s)",
             "captures": {"1": {"name": "variable.other.property.key.jarlang"},
                          "2": {"name": "punctuation.separator.key-value.jarlang"}}},
        ]},
        "operators": {"patterns": [
            {"match": r"\|>", "name": "keyword.operator.pipe.jarlang"},
            {"match": r"\.\.<?", "name": "keyword.operator.range.jarlang"},
            {"match": r"=>|->", "name": "keyword.operator.arrow.jarlang"},
            {"match": r"//=|\*\*=|\+=|-=|\*=|/=|%=|\^=", "name": "keyword.operator.assignment.compound.jarlang"},
            {"match": r"==|!=|<=|>=|<|>|≤|≥|≠", "name": "keyword.operator.comparison.jarlang"},
            {"match": r"=", "name": "keyword.operator.assignment.jarlang"},
            {"match": r"&&|\|\|", "name": "keyword.operator.logical.jarlang"},
            {"match": r"(?<=[\p{L}\p{N}_)\]])!", "name": "keyword.operator.factorial.jarlang"},
            {"match": r"!", "name": "keyword.operator.logical.jarlang"},
            {"match": r"√", "name": "keyword.operator.sqrt.jarlang"},
            {"match": r"\*\*|//|[-+*/%^×÷·−]", "name": "keyword.operator.arithmetic.jarlang"},
        ]},
        "punctuation": {"patterns": [
            {"match": r"\\$", "name": "punctuation.separator.continuation.line.jarlang"},
            {"match": ",", "name": "punctuation.separator.comma.jarlang"},
            {"match": ";", "name": "punctuation.terminator.statement.jarlang"},
            {"match": ":", "name": "punctuation.separator.colon.jarlang"},
            {"match": r"\.", "name": "punctuation.accessor.jarlang"},
            {"match": r"\{", "name": "punctuation.section.block.begin.jarlang"},
            {"match": r"\}", "name": "punctuation.section.block.end.jarlang"},
            {"match": r"[()]", "name": "punctuation.parenthesis.jarlang"},
            {"match": r"[\[\]]", "name": "punctuation.definition.list.jarlang"},
        ]},
    }

    return {
        "$schema": "https://raw.githubusercontent.com/martinring/tmlanguage/master/tmlanguage.json",
        "name": "JarLang",
        "scopeName": "source.jarlang",
        "fileTypes": ["jlang"],
        "comment": "Generated by editors/vscode/scripts/gen-grammar.py - edit that script, not this file.",
        "patterns": [
            {"include": "#comments"},
            {"include": "#import"},
            {"include": "#strings"},
            {"include": "#function-declaration"},
            {"include": "#lambda"},
            {"include": "#math-function"},
            {"include": "#declarations"},
            {"include": "#return-type"},
            {"include": "#keywords"},
            {"include": "#constants"},
            {"include": "#numbers"},
            {"include": "#calls"},
            {"include": "#dict-keys"},
            {"include": "#operators"},
            {"include": "#punctuation"},
        ],
        "repository": repository,
    }


def main() -> None:
    grammar = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(grammar, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
