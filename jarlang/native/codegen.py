"""Generates x86-64 assembly code (AT&T syntax) from the type checked program."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

from .. import __version__, ast
from ..ast import GLOBAL
from ..builtins import CONSTANTS
from ..interpreter import MAX_DEPTH
from .typecheck import MAIN_SCOPE, MATH1, FnInstance, TypeChecker
from .types import BOOL, FLOAT, INT, NEVER, NIL, NUMERIC, STR, ListT, Type, descriptor

# Same order as MATH_NAMES in runtime.c
MATH_OPS = {name: i for i, name in enumerate((*MATH1, "log"))}


@dataclass(frozen=True)
class Target:
    os: str  # linux, windows, or macos

    @property
    def win64(self) -> bool:
        return self.os == "windows"

    @property
    def shadow(self) -> int:
        return 32 if self.win64 else 0

    def sym(self, name: str) -> str:
        return "_" + name if self.os == "macos" else name

    def call_sym(self, name: str) -> str:
        """Symbol for calling a C function."""
        return self.sym(name) + ("@PLT" if self.os == "linux" else "")

    @property
    def rodata(self) -> str:
        return {"linux": ".section .rodata", "windows": '.section .rdata,"dr"',
                "macos": ".section __TEXT,__const"}[self.os]

    def arg_regs(self, kinds: list[str]) -> list[str]:
        """Registers for C arguments of the given kinds ("int" or "float")."""
        regs = []
        if self.win64:
            # Windows picks the register by argument position, for ints and floats alike
            ints = ["%rcx", "%rdx", "%r8", "%r9"]
            for i, k in enumerate(kinds):
                regs.append(f"%xmm{i}" if k == "float" else ints[i])
            return regs
        ints = ["%rdi", "%rsi", "%rdx", "%rcx", "%r8", "%r9"]
        ni = nf = 0
        for k in kinds:
            if k == "float":
                regs.append(f"%xmm{nf}")
                nf += 1
            else:
                regs.append(ints[ni])
                ni += 1
        return regs


@dataclass
class Arg:
    """An argument to a C call."""

    kind: str  # "int" or "float"
    gen: Callable[[], None] | None = None  # code that leaves the value in %rax or %xmm0
    imm: int | None = None
    label: str | None = None
    mem: str | None = None  # memory operand holding the value


def I(gen: Callable[[], None]) -> Arg:  # noqa: E743
    return Arg("int", gen=gen)


def F(gen: Callable[[], None]) -> Arg:
    return Arg("float", gen=gen)


def Imm(value: int) -> Arg:
    return Arg("int", imm=value)


def Lbl(label: str) -> Arg:
    return Arg("int", label=label)


def Mem(operand: str, kind: str = "int") -> Arg:
    return Arg(kind, mem=operand)


@dataclass
class Loop:
    break_label: str
    continue_label: str


@dataclass
class FnState:
    inst: FnInstance
    lines: list[str] = field(default_factory=list)
    slots: dict[tuple, int] = field(default_factory=dict)
    frame: int = 0
    depth: int = 0
    loops: list[Loop] = field(default_factory=list)
    stubs: dict[tuple, str] = field(default_factory=dict)
    stub_code: list[str] = field(default_factory=list)
    scopes: list[int] = field(default_factory=list)
    ret_label: str = ""
    zero_slots: list[int] = field(default_factory=list)


# Every expression leaves its value in %rax, or in %xmm0 for floats
class CodeGen:
    def __init__(self, checker: TypeChecker, target: Target, source_name: str, source_text: str = "") -> None:
        self.checker = checker
        self.t = target
        self.source_name = source_name
        self.source_lines = source_text.splitlines()
        self.strings: dict[str, str] = {}
        self.floats: dict[int, str] = {}
        self.label_id = 0
        self.fn: FnState = None  # type: ignore[assignment]
        self.last_line = 0
        used_in_functions: set[str] = set()
        for inst in checker.live_instances:
            used_in_functions |= inst.globals_used
        # Globals not used by any function are stored in jl_main's frame
        self.memory_globals = {name for name in checker.globals if name in used_in_functions}
        self.frame_globals = {name for name in checker.globals if name not in used_in_functions}
        # Reads that might happen before an assignment get checked (see definite.py)
        from .definite import analyze
        self.checked_reads, self.flag_keys = analyze(checker)

    # Helpers
    def new_label(self, hint: str = "L") -> str:
        self.label_id += 1
        return f".L{hint}{self.label_id}"

    def emit(self, instr: str) -> None:
        self.fn.lines.append("    " + instr)

    def label(self, name: str) -> None:
        self.fn.lines.append(f"{name}:")

    def comment(self, text: str) -> None:
        self.fn.lines.append(f"    # {text}")

    def source_comment(self, node: ast.Node) -> None:
        line = node.span.line
        if line != self.last_line and 0 < line <= len(self.source_lines):
            self.last_line = line
            text = self.source_lines[line - 1].strip()
            if text:
                self.fn.lines.append(f"    # {line}: {text[:100]}")

    def string_label(self, text: str) -> str:
        if text not in self.strings:
            self.strings[text] = f".Lstr{len(self.strings)}"
        return self.strings[text]

    def float_label(self, value: float) -> str:
        bits = struct.unpack("<q", struct.pack("<d", value))[0]
        if bits not in self.floats:
            self.floats[bits] = f".Lflt{len(self.floats)}"
        return self.floats[bits]

    def ty(self, node: ast.Node) -> Type:
        return self.fn.inst.types.get(id(node))

    def line_of(self, node: ast.Node) -> int:
        return node.span.line

    # Stack and calls
    def push(self, t: Type) -> None:
        if t == FLOAT:
            self.emit("subq $8, %rsp")
            self.emit("movsd %xmm0, (%rsp)")
        else:
            self.emit("pushq %rax")
        self.fn.depth += 8

    def pop(self, t: Type, reg: str) -> None:
        if t == FLOAT:
            self.emit(f"movsd (%rsp), {reg}")
            self.emit("addq $8, %rsp")
        else:
            self.emit(f"popq {reg}")
        self.fn.depth -= 8

    # Keep the stack 16-byte aligned and add 32 bytes of shadow space on Windows
    def aligned_call(self, target: str) -> None:
        pad = (-self.fn.depth) % 16
        total = pad + self.t.shadow
        if total:
            self.emit(f"subq ${total}, %rsp")
        self.emit(f"call {target}")
        if total:
            self.emit(f"addq ${total}, %rsp")

    def ccall(self, fname: str, args: list[Arg]) -> None:
        """Call a C function with the platform's calling convention."""
        # Computed arguments are pushed, except the last one which stays in %rax or %xmm0
        computed = [a for a in args if a.gen is not None]
        for a in computed[:-1]:
            a.gen()
            self.push(FLOAT if a.kind == "float" else INT)
        if computed:
            computed[-1].gen()
        regs = self.t.arg_regs([a.kind for a in args])
        n = len(computed) - 1 if computed else 0
        # Move the last computed value out of %rax or %xmm0 first
        for a, reg in zip(args, regs):
            if computed and a is computed[-1]:
                if a.kind == "float":
                    if reg != "%xmm0":
                        self.emit(f"movapd %xmm0, {reg}")
                else:
                    self.emit(f"movq %rax, {reg}")
        # Then load the other arguments
        k = 0
        for a, reg in zip(args, regs):
            if a.gen is not None:
                if a is computed[-1]:
                    continue
                offset = 8 * (n - 1 - k)
                k += 1
                src = f"{offset}(%rsp)" if offset else "(%rsp)"
                self.emit(f"movsd {src}, {reg}" if a.kind == "float" else f"movq {src}, {reg}")
            elif a.imm is not None:
                self.load_imm(a.imm, reg)
            elif a.label is not None:
                self.emit(f"leaq {a.label}(%rip), {reg}")
            elif a.mem is not None:
                self.emit(f"movsd {a.mem}, {reg}" if a.kind == "float" else f"movq {a.mem}, {reg}")
        if n:
            self.emit(f"addq ${8 * n}, %rsp")
            self.fn.depth -= 8 * n
        self.aligned_call(self.t.call_sym(fname))

    def load_imm(self, value: int, reg: str = "%rax") -> None:
        if value == 0 and reg == "%rax":
            self.emit("xorl %eax, %eax")
        elif -2 ** 31 <= value < 2 ** 31:
            self.emit(f"movq ${value}, {reg}")
        else:
            self.emit(f"movabsq ${value}, {reg}")

    def panic_stub(self, kind: str, line: int) -> str:
        """Add code that reports a runtime error and return its label."""
        key = (kind, line)
        if key in self.fn.stubs:
            return self.fn.stubs[key]
        lab = self.new_label("panic")
        self.fn.stubs[key] = lab
        code = self.fn.stub_code
        code.append(f"{lab}:")
        code.append("    andq $-16, %rsp")
        if self.t.shadow:
            code.append(f"    subq ${self.t.shadow}, %rsp")
        if kind == "index":  # list in %rcx, original index in %r8
            if self.t.win64:
                code += ["    movq %r8, %rdx", f"    movq ${line}, %r8"]
            else:
                code += ["    movq %rcx, %rdi", "    movq %r8, %rsi", f"    movq ${line}, %rdx"]
            fname = "jl_panic_index"
        elif kind in ("sqrt", "sqrt_int"):  # value in %xmm0
            regs = ["%rdx", "%r8"] if self.t.win64 else ["%rdi", "%rsi"]
            code += [f"    movq ${int(kind == 'sqrt_int')}, {regs[0]}", f"    movq ${line}, {regs[1]}"]
            fname = "jl_panic_sqrt"
        else:
            code.append(f"    movq ${line}, {'%rcx' if self.t.win64 else '%rdi'}")
            fname = {"overflow": "jl_panic_overflow", "divzero": "jl_panic_divzero",
                     "modzero": "jl_panic_modzero"}[kind]
        code.append(f"    call {self.t.call_sym(fname)}")
        return lab

    def check_overflow(self, node: ast.Node) -> None:
        self.emit(f"jo {self.panic_stub('overflow', self.line_of(node))}")

    # Frame slots
    def alloc_slot(self) -> int:
        self.fn.frame += 8
        return -self.fn.frame

    def temp(self) -> str:
        return f"{self.alloc_slot()}(%rbp)"

    def slot_for(self, key: tuple) -> str:
        off = self.fn.slots.get(key)
        if off is None:
            off = self.alloc_slot()
            self.fn.slots[key] = off
            self.fn.zero_slots.append(off)
        return f"{off}(%rbp)"

    def var_operand(self, node: ast.Name) -> str:
        if node.depth == GLOBAL:
            name = node.name
            if self.fn.inst.func is None and name in self.frame_globals:
                return self.slot_for(("global", name))
            return f"{self.global_symbol(name)}(%rip)"
        index = len(self.fn.scopes) - 1 - node.depth
        return self.slot_for((self.fn.scopes[index], node.slot))

    def name_key(self, node: ast.Name) -> tuple | None:
        if node.depth == GLOBAL:
            return ("global", node.name) if node.name in self.checker.globals else None
        index = len(self.fn.scopes) - 1 - node.depth
        return (self.fn.scopes[index], node.slot)

    def flag_operand(self, key: tuple) -> str:
        if key[0] == "global":
            name = key[1]
            if self.fn.inst.func is None and name in self.frame_globals:
                return self.slot_for(("assigned", name))
            return f"{self.global_symbol(name)}__set(%rip)"
        return self.slot_for(("assigned",) + key)

    def store_var(self, node: ast.Name, t: Type) -> None:
        self.store(self.var_operand(node), t)
        self.mark_assigned(node)

    def mark_assigned(self, node: ast.Name) -> None:
        key = self.name_key(node)
        if key is not None and key in self.flag_keys:
            self.emit(f"movq $1, {self.flag_operand(key)}")

    def check_assigned(self, node: ast.Name) -> None:
        """Stop with the interpreter's error if the variable might not be assigned yet."""
        if id(node) not in self.checked_reads:
            return
        key = self.name_key(node)
        if key is None:
            return
        if key[0] == "global":
            message = f"`{node.name}` is used before it is defined"
        else:
            message = f"variable `{node.name}` is used before it is assigned"
        self.emit(f"cmpq $0, {self.flag_operand(key)}")
        self.emit(f"je {self.message_stub(message, self.line_of(node))}")

    def message_stub(self, message: str, line: int) -> str:
        key = ("message", message, line)
        if key in self.fn.stubs:
            return self.fn.stubs[key]
        lab = self.new_label("panic")
        self.fn.stubs[key] = lab
        regs = self.t.arg_regs(["int", "int"])
        code = self.fn.stub_code
        code += [f"{lab}:", "    andq $-16, %rsp"]
        if self.t.shadow:
            code.append(f"    subq ${self.t.shadow}, %rsp")
        code += [f"    leaq {self.string_label(message)}(%rip), {regs[0]}", f"    movq ${line}, {regs[1]}",
                 f"    call {self.t.call_sym('jl_panic')}"]
        return lab

    def global_symbol(self, name: str) -> str:
        from .typecheck import _mangle_name
        return f"jlg_{_mangle_name(name)}"

    def load(self, operand: str, t: Type) -> None:
        if t == FLOAT:
            self.emit(f"movsd {operand}, %xmm0")
        else:
            self.emit(f"movq {operand}, %rax")

    def store(self, operand: str, t: Type) -> None:
        if t == FLOAT:
            self.emit(f"movsd %xmm0, {operand}")
        else:
            self.emit(f"movq %rax, {operand}")

    def coerce(self, src: Type, dst: Type) -> None:
        if dst == FLOAT and src in (INT, BOOL):
            self.emit("cvtsi2sdq %rax, %xmm0")

    def float_bits_to_rax(self, t: Type) -> None:
        if t == FLOAT:
            self.emit("movq %xmm0, %rax")

    def rax_bits_to_value(self, t: Type) -> None:
        if t == FLOAT:
            self.emit("movq %rax, %xmm0")

    # Program
    def generate(self) -> str:
        out = [
            f"# Generated by JarLang {__version__} from {self.source_name}",
            f"# target: x86-64 {self.t.os} ({'Microsoft x64' if self.t.win64 else 'System V'} ABI)",
            "",
            "    .text",
        ]
        functions = [self.function(self.checker.main)]
        for inst in sorted(self.checker.live_instances, key=lambda i: i.asm_name):
            functions.append(self.function(inst))
        for code in functions:
            out.extend(code)
            out.append("")
        out.extend(self.data_section())
        if self.t.os == "linux":
            out.append('    .section .note.GNU-stack,"",@progbits')
        return "\n".join(out) + "\n"

    def data_section(self) -> list[str]:
        out = [f"    {self.t.rodata}", "    .p2align 3"]
        out.append(f"    .globl {self.t.sym('jl_source_name')}")
        out.append(f"{self.t.sym('jl_source_name')}:")
        out.append(f"    .asciz {_asm_string(self.source_name)}")
        for bits, lab in self.floats.items():
            value = struct.unpack("<d", struct.pack("<q", bits))[0]
            out.append("    .p2align 3")
            out.append(f"{lab}:")
            out.append(f"    .quad {bits:#x}    # {value!r}")
        for text, lab in self.strings.items():
            out.append(f"{lab}:")
            out.append(f"    .asciz {_asm_string(text)}")
        out += ["", "    .data", "    .p2align 3", f"{self.t.sym('jl_depth')}:    # how deep the calls are", "    .quad 0"]
        if self.memory_globals:
            for name in sorted(self.memory_globals):
                out.append(f"{self.global_symbol(name)}:    # global `{name}`: "
                           f"{self.checker.globals.get(name)}")
                out.append("    .quad 0")
                if ("global", name) in self.flag_keys:
                    out.append(f"{self.global_symbol(name)}__set:    # has `{name}` been assigned yet?")
                    out.append("    .quad 0")
        return out

    def function(self, inst: FnInstance) -> list[str]:
        self.fn = FnState(inst)
        self.fn.ret_label = self.new_label("ret")
        self.last_line = 0
        func = inst.func
        if func is None:
            self.fn.scopes = [MAIN_SCOPE]
            body_lines = self._gen_main()
        else:
            self.fn.scopes = [id(func)]
            n = len(func.params)
            # Arguments are pushed left to right, so parameter i of n is at 16 + 8*(n-1-i)(%rbp)
            param_slots = []
            for i, (p, t) in enumerate(zip(func.params, inst.argtypes)):
                operand = self.slot_for((id(func), p.slot))
                self.fn.zero_slots.remove(int(operand.split("(")[0]))
                param_slots.append((16 + 8 * (n - 1 - i), operand, t))
            for incoming, operand, t in param_slots:
                self.emit(f"movq {incoming}(%rbp), %rax")
                self.emit(f"movq %rax, {operand}")
            self.gen_block_value(func.body.stmts, inst.ret)
            body_lines = self.fn.lines
        frame = (self.fn.frame + 15) // 16 * 16
        name = inst.asm_name if func is not None else "jl_main"
        sym = self.t.sym(name)
        head = []
        if func is None:
            head.append(f"    .globl {sym}")
        else:
            params = ", ".join(f"{p.name}: {t}" for p, t in zip(func.params, inst.argtypes))
            head.append(f"# fn {inst.name}({params}) -> {inst.ret}")
        head += ["    .p2align 4", f"{sym}:", "    pushq %rbp", "    movq %rsp, %rbp"]
        if frame:
            head.append(f"    subq ${frame}, %rsp")
        for off in self.fn.zero_slots:
            head.append(f"    movq $0, {off}(%rbp)")
        tail = [f"{self.fn.ret_label}:", "    leave", "    ret"]
        if func is not None:
            # Count calls so deep recursion stops with the interpreter's error instead of a crash
            depth = f"{self.t.sym('jl_depth')}(%rip)"
            stub = self.message_stub("maximum recursion depth exceeded", self.line_of(func))
            head += [f"    incq {depth}", f"    cmpq ${MAX_DEPTH}, {depth}", f"    jg {stub}"]
            tail.insert(1, f"    decq {depth}")
        return head + body_lines + tail + self.fn.stub_code

    def _gen_main(self) -> list[str]:
        for stmt in self.checker.program.stmts:
            if isinstance(stmt, ast.FnDecl):
                continue
            self.gen_stmt(stmt)
        self.emit("xorl %eax, %eax")
        return self.fn.lines

    # Statements
    def gen_block_value(self, stmts: list[ast.Stmt], want: Type) -> None:
        """Generate a block and leave the last expression's value if want is a value type."""
        for i, stmt in enumerate(stmts):
            last = i == len(stmts) - 1
            if last and want not in (None, NIL, NEVER) and isinstance(stmt, ast.ExprStmt):
                self.source_comment(stmt)
                if isinstance(stmt.expr, ast.IfExpr):
                    self.gen_if(stmt.expr, want)
                else:
                    self.gen_expr(stmt.expr)
                    self.coerce(self.ty(stmt.expr), want)
            else:
                self.gen_stmt(stmt)

    def gen_stmt(self, node: ast.Stmt) -> None:
        self.source_comment(node)
        method = getattr(self, "s_" + type(node).__name__)
        method(node)

    def s_ExprStmt(self, node: ast.ExprStmt) -> None:
        if isinstance(node.expr, ast.IfExpr):
            self.gen_if(node.expr, None)
        else:
            self.gen_expr(node.expr)

    def s_FnDecl(self, node: ast.FnDecl) -> None:
        pass

    def s_Let(self, node: ast.Let) -> None:
        target_t = self.var_type(node.target)
        if node.value is None:
            self.emit("xorl %eax, %eax")
        else:
            self.gen_expr(node.value)
            self.coerce(self.ty(node.value), target_t)
        self.store_var(node.target, target_t)

    def var_type(self, node: ast.Name) -> Type:
        if node.depth == GLOBAL:
            return self.checker.globals.get(node.name)
        index = len(self.fn.scopes) - 1 - node.depth
        return self.fn.inst.vars.get((self.fn.scopes[index], node.slot))

    def s_Assign(self, node: ast.Assign) -> None:
        target = node.target
        if isinstance(target, ast.ListLit):
            # Evaluate every value first so a, b = b, a works, then assign left to right
            values = node.value.items  # type: ignore[union-attr]
            saved = []
            for v in values:
                self.gen_expr(v)
                slot = self.temp()
                self.store(slot, self.ty(v))
                saved.append((slot, self.ty(v)))
            for t_node, (slot, vt) in zip(target.items, saved):
                if isinstance(t_node, ast.Name):
                    dst_t = self.var_type(t_node)
                    self.load(slot, vt)
                    self.coerce(vt, dst_t)
                    self.store_var(t_node, dst_t)
                    continue
                list_t = self.ty(t_node.obj)  # type: ignore[union-attr]
                elem_t = list_t.elem if isinstance(list_t, ListT) else INT
                if elem_t == FLOAT and vt != FLOAT:
                    self.load(slot, vt)
                    self.coerce(vt, FLOAT)
                    self.store(slot, FLOAT)
                self.gen_expr(t_node.obj)  # type: ignore[union-attr]
                self.push(INT)
                self.gen_expr(t_node.index)  # type: ignore[union-attr]
                self.pop(INT, "%rcx")
                self.list_element_address(t_node)
                self.emit(f"movq {slot}, %r10")
                self.emit("movq %r10, (%rdx,%rax,8)")
            return
        if isinstance(target, ast.Name):
            dst_t = self.var_type(target)
            if node.op is not None:
                operand = self.var_operand(target)
                cur_t = self.ty(target)
                self.check_assigned(target)
                self.gen_binary(node.op, lambda: self.load(operand, cur_t), cur_t, node.value,
                                self.ty(node), node)
                self.coerce(self.ty(node), dst_t)
            else:
                self.gen_expr(node.value)
                self.coerce(self.ty(node.value), dst_t)
            self.store_var(target, dst_t)
            return
        if isinstance(target, ast.Index):
            list_t = self.ty(target.obj)
            elem_t = list_t.elem if isinstance(list_t, ListT) else INT
            if node.op is not None:
                # xs[i] += v: save the list and index, then do xs[i] = xs[i] + v
                tl, ti = self.temp(), self.temp()
                self.gen_expr(target.obj)
                self.emit(f"movq %rax, {tl}")
                self.gen_expr(target.index)
                self.emit(f"movq %rax, {ti}")

                def load_elem() -> None:
                    self.emit(f"movq {tl}, %rcx")
                    self.emit(f"movq {ti}, %rax")
                    self.list_element_address(target)
                    self.emit("movq (%rdx,%rax,8), %rax")
                    self.rax_bits_to_value(elem_t)
                self.gen_binary(node.op, load_elem, elem_t, node.value, self.ty(node), node)
                self.coerce(self.ty(node), elem_t)
                self.float_bits_to_rax(elem_t)
                self.emit("movq %rax, %r10")
                self.emit(f"movq {tl}, %rcx")
                self.emit(f"movq {ti}, %rax")
                self.list_element_address(target)
                self.emit("movq %r10, (%rdx,%rax,8)")
                return
            self.gen_expr(node.value)
            self.coerce(self.ty(node.value), elem_t)
            self.float_bits_to_rax(elem_t)
            self.push(INT)
            self.gen_expr(target.obj)
            self.push(INT)
            self.gen_expr(target.index)
            self.pop(INT, "%rcx")
            self.list_element_address(target)
            self.pop(INT, "%r10")
            self.emit("movq %r10, (%rdx,%rax,8)")
            return
        raise AssertionError("unsupported assignment target")  # pragma: no cover

    def list_element_address(self, node: ast.Node) -> None:
        """List in %rcx, index in %rax. Checks the index and leaves the data pointer in %rdx."""
        stub = self.panic_stub("index", self.line_of(node))
        ok = self.new_label("idx")
        self.emit("movq %rax, %r8")
        self.emit("movq (%rcx), %rdx")
        self.emit("testq %rax, %rax")
        self.emit(f"jns {ok}")
        self.emit("addq %rdx, %rax")
        self.label(ok)
        self.emit("cmpq %rdx, %rax")
        self.emit(f"jae {stub}")
        self.emit("movq 16(%rcx), %rdx")

    def s_While(self, node: ast.While) -> None:
        top, end = self.new_label("while"), self.new_label("endwhile")
        self.label(top)
        self.gen_cond(node.cond, end, False)
        self.fn.loops.append(Loop(end, top))
        for stmt in node.body.stmts:
            self.gen_stmt(stmt)
        self.fn.loops.pop()
        self.emit(f"jmp {top}")
        self.label(end)

    def s_For(self, node: ast.For, var: str | None = None, body_scope: int | None = None) -> None:
        target = node.targets[0]
        it = node.iterable
        top, cont, end = self.new_label("for"), self.new_label("next"), self.new_label("endfor")
        mark = None
        if var is None:
            var = self.var_operand(target)
            key = self.name_key(target)
            if key is not None and key in self.flag_keys:
                mark = self.flag_operand(key)
        if isinstance(it, ast.RangeExpr) or self._is_range_call(it):
            cur, stop = self.temp(), self.temp()
            step_op = None
            inclusive = isinstance(it, ast.RangeExpr) and it.inclusive
            if isinstance(it, ast.RangeExpr):
                self.gen_expr(it.start)
                self.emit(f"movq %rax, {cur}")
                self.gen_expr(it.end)
                self.emit(f"movq %rax, {stop}")
            else:
                args = it.args  # type: ignore[union-attr]
                if len(args) == 1:
                    self.emit(f"movq $0, {cur}")
                    self.gen_expr(args[0])
                    self.emit(f"movq %rax, {stop}")
                else:
                    self.gen_expr(args[0])
                    self.emit(f"movq %rax, {cur}")
                    self.gen_expr(args[1])
                    self.emit(f"movq %rax, {stop}")
                if len(args) == 3:
                    step_op = self.temp()
                    self.gen_expr(args[2])
                    self.emit(f"movq %rax, {step_op}")
                    self.emit("testq %rax, %rax")
                    ok = self.new_label("stepok")
                    self.emit(f"jne {ok}")
                    self.ccall("jl_panic", [Lbl(self.string_label("range() step cannot be zero")),
                                            Imm(self.line_of(it))])
                    self.label(ok)
            self.label(top)
            self.emit(f"movq {cur}, %rax")
            if step_op is None:
                self.emit(f"cmpq {stop}, %rax")
                self.emit(f"{'jg' if inclusive else 'jge'} {end}")
            else:
                neg, body = self.new_label("negstep"), self.new_label("body")
                self.emit(f"cmpq $0, {step_op}")
                self.emit(f"jl {neg}")
                self.emit(f"cmpq {stop}, %rax")
                self.emit(f"jge {end}")
                self.emit(f"jmp {body}")
                self.label(neg)
                self.emit(f"cmpq {stop}, %rax")
                self.emit(f"jle {end}")
                self.label(body)
            self.emit(f"movq %rax, {var}")
            if mark:
                self.emit(f"movq $1, {mark}")
            self.loop_body(node.body.stmts, end, cont, body_scope)
            self.label(cont)
            if step_op is None:
                self.emit(f"incq {cur}")
            else:
                self.emit(f"movq {step_op}, %rax")
                self.emit(f"addq %rax, {cur}")
            if inclusive or step_op is not None:
                # The next value doesn't fit in 64 bits, so it's past the end (e.g. 1..9223372036854775807)
                self.emit(f"jo {end}")
            self.emit(f"jmp {top}")
            self.label(end)
            return
        it_t = self.ty(it)
        if it_t == STR:
            s, idx = self.temp(), self.temp()
            self.gen_expr(it)
            self.emit(f"movq %rax, {s}")
            self.emit(f"movq $0, {idx}")
            self.label(top)
            self.ccall("jl_str_len", [Mem(s)])
            self.emit(f"cmpq %rax, {idx}")
            self.emit(f"jge {end}")
            self.ccall("jl_str_index", [Mem(s), Mem(idx), Imm(self.line_of(it))])
            self.emit(f"movq %rax, {var}")
            if mark:
                self.emit(f"movq $1, {mark}")
            self.loop_body(node.body.stmts, end, cont, body_scope)
            self.label(cont)
            self.emit(f"incq {idx}")
            self.emit(f"jmp {top}")
            self.label(end)
            return
        # Loop over a list
        lst, idx = self.temp(), self.temp()
        self.gen_expr(it)
        self.emit(f"movq %rax, {lst}")
        self.emit(f"movq $0, {idx}")
        self.label(top)
        self.emit(f"movq {lst}, %rcx")
        self.emit(f"movq {idx}, %rax")
        self.emit("cmpq (%rcx), %rax")
        self.emit(f"jge {end}")
        self.emit("movq 16(%rcx), %rdx")
        self.emit("movq (%rdx,%rax,8), %rax")
        self.emit(f"movq %rax, {var}")
        if mark:
            self.emit(f"movq $1, {mark}")
        self.loop_body(node.body.stmts, end, cont, body_scope)
        self.label(cont)
        self.emit(f"incq {idx}")
        self.emit(f"jmp {top}")
        self.label(end)

    def _is_range_call(self, node: ast.Expr) -> bool:
        return isinstance(node, ast.Call) and isinstance(node.callee, ast.Name) and node.callee.name == "range" \
            and self.ty(node) == NIL

    def loop_body(self, stmts: list[ast.Stmt], end: str, cont: str, scope: int | None = None) -> None:
        self.fn.loops.append(Loop(end, cont))
        if scope is not None:
            self.fn.scopes.append(scope)
        for stmt in stmts:
            self.gen_stmt(stmt)
        if scope is not None:
            self.fn.scopes.pop()
        self.fn.loops.pop()

    def s_Break(self, node: ast.Break) -> None:
        self.emit(f"jmp {self.fn.loops[-1].break_label}")

    def s_Continue(self, node: ast.Continue) -> None:
        self.emit(f"jmp {self.fn.loops[-1].continue_label}")

    def s_Return(self, node: ast.Return) -> None:
        ret_t = self.fn.inst.ret
        if node.value is not None:
            self.gen_expr(node.value)
            self.coerce(self.ty(node.value), ret_t)
        else:
            self.emit("xorl %eax, %eax")
        self.emit(f"jmp {self.fn.ret_label}")

    # Conditions
    _INT_JUMP_FALSE = {"<": "jge", "<=": "jg", ">": "jle", ">=": "jl", "==": "jne", "!=": "je"}
    _INT_JUMP_TRUE = {"<": "jl", "<=": "jle", ">": "jg", ">=": "jge", "==": "je", "!=": "jne"}

    def gen_cond(self, node: ast.Expr, target: str, when: bool) -> None:
        """Jump to target if the truth value of node equals when."""
        if isinstance(node, ast.Literal) and isinstance(node.value, bool):
            if node.value == when:
                self.emit(f"jmp {target}")
            return
        if isinstance(node, ast.Unary) and node.op == "not":
            self.gen_cond(node.operand, target, not when)
            return
        if isinstance(node, ast.Logical):
            is_and = node.op == "and"
            if is_and != when:
                # Either side alone is enough to jump
                self.gen_cond(node.left, target, when)
                self.gen_cond(node.right, target, when)
            else:
                skip = self.new_label("sc")
                self.gen_cond(node.left, skip, not when)
                self.gen_cond(node.right, target, when)
                self.label(skip)
            return
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and node.ops[0] in self._INT_JUMP_FALSE:
            lt, rt = self.ty(node.operands[0]), self.ty(node.operands[1])
            if lt in (INT, BOOL) and rt in (INT, BOOL) and lt == rt:
                self.gen_int_compare_operands(node.operands[0], node.operands[1])
                table = self._INT_JUMP_TRUE if when else self._INT_JUMP_FALSE
                self.emit(f"{table[node.ops[0]]} {target}")
                return
            if lt in NUMERIC and rt in NUMERIC:
                self.gen_float_operands(node.operands[0], node.operands[1])
                self.float_compare_jump(node.ops[0], target, when)
                return
        self.gen_expr(node)
        self.truth_test(self.ty(node), target, when)

    def truth_test(self, t: Type, target: str, when: bool) -> None:
        if t == NIL:
            # Don't test %rax, it isn't always 0 (e.g. after a function that ends with an assignment)
            if not when:
                self.emit(f"jmp {target}")
        elif t in (INT, BOOL):
            self.emit("testq %rax, %rax")
            self.emit(f"{'jne' if when else 'je'} {target}")
        elif t == FLOAT:
            self.emit("xorpd %xmm1, %xmm1")
            self.emit("ucomisd %xmm1, %xmm0")
            if when:  # truthy: x != 0 or NaN
                self.emit(f"jp {target}")
                self.emit(f"jne {target}")
            else:
                skip = self.new_label("nz")
                self.emit(f"jp {skip}")
                self.emit(f"je {target}")
                self.label(skip)
        elif t == STR:
            self.emit("cmpb $0, (%rax)")
            self.emit(f"{'jne' if when else 'je'} {target}")
        elif isinstance(t, ListT):
            self.emit("cmpq $0, (%rax)")
            self.emit(f"{'jne' if when else 'je'} {target}")
        else:  # pragma: no cover
            raise AssertionError(f"cannot test {t}")

    def float_compare_jump(self, op: str, target: str, when: bool) -> None:
        """Operands are in %xmm0 (a) and %xmm1 (b)."""
        if op in (">", ">="):
            self.emit("ucomisd %xmm1, %xmm0")  # flags: a vs b
            cc = "a" if op == ">" else "ae"
        elif op in ("<", "<="):
            self.emit("ucomisd %xmm0, %xmm1")  # flags: b vs a
            cc = "a" if op == "<" else "ae"
        else:
            self.emit("ucomisd %xmm1, %xmm0")
            equal = op == "=="
            # Equal means ZF=1 and PF=0 (PF=1 means NaN)
            if equal == when:  # jump when equal
                skip = self.new_label("ne")
                self.emit(f"jp {skip}")
                self.emit(f"je {target}")
                self.label(skip)
            else:  # jump when not equal
                self.emit(f"jp {target}")
                self.emit(f"jne {target}")
            return
        if when:
            self.emit(f"j{cc} {target}")
        else:
            self.emit(f"j{'be' if cc == 'a' else 'b'} {target}")

    # Expressions
    def gen_expr(self, node: ast.Expr) -> None:
        method = getattr(self, "e_" + type(node).__name__)
        method(node)

    def e_Literal(self, node: ast.Literal) -> None:
        v = node.value
        if v is None or v is False:
            self.emit("xorl %eax, %eax")
        elif v is True:
            self.emit("movl $1, %eax")
        elif isinstance(v, int):
            self.load_imm(v)
        else:
            if v == 0.0 and struct.pack("<d", v) == struct.pack("<d", 0.0):
                self.emit("xorpd %xmm0, %xmm0")
            else:
                self.emit(f"movsd {self.float_label(v)}(%rip), %xmm0")

    def e_StringLit(self, node: ast.StringLit) -> None:
        if node.is_plain:
            self.emit(f"leaq {self.string_label(node.plain_value)}(%rip), %rax")
            return
        sb = self.temp()
        self.ccall("jl_sb_new", [])
        self.emit(f"movq %rax, {sb}")
        for part in node.parts:
            if isinstance(part, str):
                if part:
                    self.ccall("jl_sb_str", [Mem(sb), Lbl(self.string_label(part))])
                continue
            expr = part.expr
            t = self.ty(expr)
            gen = (lambda e=expr: self.gen_expr(e))
            if part.spec is not None:
                spec = Lbl(self.string_label(part.spec))
                if t == FLOAT:
                    self.ccall("jl_sb_float_fmt", [Mem(sb), F(gen), spec])
                elif t == INT:
                    self.ccall("jl_sb_int_fmt", [Mem(sb), I(gen), spec, Imm(self.line_of(expr))])
                elif t == BOOL:
                    self.ccall("jl_sb_bool_fmt", [Mem(sb), I(gen), spec])
                else:
                    self.ccall("jl_sb_str_fmt", [Mem(sb), I(gen), spec])
                continue
            if t == FLOAT:
                self.ccall("jl_sb_float", [Mem(sb), F(gen)])
            elif t == INT:
                self.ccall("jl_sb_int", [Mem(sb), I(gen)])
            elif t == BOOL:
                self.ccall("jl_sb_bool", [Mem(sb), I(gen)])
            elif t == STR:
                self.ccall("jl_sb_str", [Mem(sb), I(gen)])
            elif t == NIL:
                gen()
                self.ccall("jl_sb_nil", [Mem(sb)])
            elif isinstance(t, ListT):
                self.ccall("jl_sb_list", [Mem(sb), I(gen), Lbl(self.string_label(descriptor(t.elem or INT)))])
        self.ccall("jl_sb_finish", [Mem(sb)])

    def e_Name(self, node: ast.Name) -> None:
        if node.depth == GLOBAL and node.name not in self.checker.globals and node.name in CONSTANTS:
            self.emit(f"movsd {self.float_label(CONSTANTS[node.name])}(%rip), %xmm0    # {node.name}")
            return
        self.check_assigned(node)
        self.load(self.var_operand(node), self.ty(node))

    def e_ListLit(self, node: ast.ListLit) -> None:
        t = self.ty(node)
        elem = t.elem if isinstance(t, ListT) else INT
        lst = self.temp()
        self.ccall("jl_list_new", [Imm(len(node.items))])
        self.emit(f"movq %rax, {lst}")
        for item in node.items:
            def gen(item: ast.Expr = item) -> None:
                self.gen_expr(item)
                self.coerce(self.ty(item), elem)
                self.float_bits_to_rax(elem)
            self.ccall("jl_list_push", [Mem(lst), I(gen)])
        self.emit(f"movq {lst}, %rax")

    def e_Unary(self, node: ast.Unary) -> None:
        op = node.op
        t = self.ty(node.operand)
        if op == "not":
            true_l, end = self.new_label("t"), self.new_label("e")
            self.gen_cond(node.operand, true_l, True)
            self.emit("movl $1, %eax")
            self.emit(f"jmp {end}")
            self.label(true_l)
            self.emit("xorl %eax, %eax")
            self.label(end)
            return
        self.gen_expr(node.operand)
        if op == "+":
            return
        if op == "-":
            if t == FLOAT:
                self.emit("movq %xmm0, %rax")
                self.emit("btcq $63, %rax")
                self.emit("movq %rax, %xmm0")
            else:
                self.emit("negq %rax")
                self.check_overflow(node)
            return
        if op == "√":
            self.coerce(t, FLOAT)
            self.checked_sqrt(node, t)
            return
        if op == "!":
            fname = "jl_ffact" if t == FLOAT else "jl_ifact"
            self.call_with_value(fname, t, [Imm(self.line_of(node))])
            return
        raise AssertionError(op)  # pragma: no cover

    def call_with_value(self, fname: str, t: Type, extra: list[Arg]) -> None:
        """Call fname with the value in %rax or %xmm0, followed by extra immediates or labels."""
        regs = self.t.arg_regs(["float" if t == FLOAT else "int"] + [a.kind for a in extra])
        if t != FLOAT:
            self.emit(f"movq %rax, {regs[0]}")  # a float is already in %xmm0, its first-arg register
        for a, reg in zip(extra, regs[1:]):
            if a.imm is not None:
                self.load_imm(a.imm, reg)
            elif a.label is not None:
                self.emit(f"leaq {a.label}(%rip), {reg}")
        self.aligned_call(self.t.call_sym(fname))

    def checked_sqrt(self, node: ast.Node, t: Type) -> None:
        stub = self.panic_stub("sqrt_int" if t == INT else "sqrt", self.line_of(node))
        ok = self.new_label("sq")
        self.emit("xorpd %xmm1, %xmm1")
        self.emit("ucomisd %xmm1, %xmm0")
        self.emit(f"jp {ok}")
        self.emit(f"jb {stub}")
        self.label(ok)
        self.emit("sqrtsd %xmm0, %xmm0")

    def e_Binary(self, node: ast.Binary) -> None:
        lt = self.ty(node.left)
        self.gen_binary(node.op, lambda: self.gen_expr(node.left), lt, node.right, self.ty(node), node,
                        left_node=node.left)

    def simple_int_operand(self, node: ast.Expr) -> str | None:
        if isinstance(node, ast.Literal) and type(node.value) is int and -2 ** 31 <= node.value < 2 ** 31:
            return f"${node.value}"
        if isinstance(node, ast.Name) and self.ty(node) == INT and id(node) not in self.checked_reads and not (
                node.depth == GLOBAL and node.name not in self.checker.globals):
            return self.var_operand(node)
        return None

    def simple_float_operand(self, node: ast.Expr) -> str | None:
        if isinstance(node, ast.Literal) and type(node.value) in (float, int):
            return f"{self.float_label(float(node.value))}(%rip)"
        if isinstance(node, ast.Name) and self.ty(node) == FLOAT:
            if node.depth == GLOBAL and node.name not in self.checker.globals:
                return f"{self.float_label(CONSTANTS[node.name])}(%rip)"
            if id(node) in self.checked_reads:
                return None
            return self.var_operand(node)
        return None

    def gen_binary(self, op: str, gen_left: Callable[[], None], lt: Type, right: ast.Expr, result: Type,
                   node: ast.Node, left_node: ast.Expr | None = None) -> None:
        rt = self.ty(right)
        line = self.line_of(node)
        if result == STR or isinstance(result, ListT):
            kind = "str" if result == STR else "list"
            if op == "+":
                self.ccall(f"jl_{kind}_concat", [I(gen_left), I(lambda: self.gen_expr(right))])
            elif rt == INT:
                self.ccall(f"jl_{kind}_repeat", [I(gen_left), I(lambda: self.gen_expr(right)), Imm(line)])
            else:  # 3 * "ab", still evaluated left to right
                tmp = self.temp()
                gen_left()
                self.emit(f"movq %rax, {tmp}")
                self.ccall(f"jl_{kind}_repeat", [I(lambda: self.gen_expr(right)), Mem(tmp), Imm(line)])
            return
        if result == INT:
            self.gen_int_binary(op, gen_left, right, node, line)
            return
        # Float result
        if op == "^":
            if isinstance(right, ast.Literal) and right.value in (2, 3) and type(right.value) is int and lt == FLOAT:
                gen_left()
                self.emit("movapd %xmm0, %xmm1")
                self.emit("mulsd %xmm1, %xmm0")
                if right.value == 3:
                    self.emit("mulsd %xmm1, %xmm0")
                return
            self.ccall("jl_fpow", [F(lambda: (gen_left(), self.coerce(lt, FLOAT))),
                                   F(lambda: (self.gen_expr(right), self.coerce(rt, FLOAT))), Imm(line)])
            return
        if op in ("//", "%"):
            fname = "jl_ffloordiv" if op == "//" else "jl_fmod"
            self.ccall(fname, [F(lambda: (gen_left(), self.coerce(lt, FLOAT))),
                               F(lambda: (self.gen_expr(right), self.coerce(rt, FLOAT))), Imm(line)])
            return
        gen_left()
        self.coerce(lt, FLOAT)
        src = None
        if op != "/" and (rt == FLOAT or isinstance(right, ast.Literal)):
            src = self.simple_float_operand(right)
        if src is None:
            self.push(FLOAT)
            self.gen_expr(right)
            self.coerce(rt, FLOAT)
            self.emit("movapd %xmm0, %xmm1")
            self.pop(FLOAT, "%xmm0")
            src = "%xmm1"
        if op == "/":
            stub = self.panic_stub("divzero", line)
            ok = self.new_label("dz")
            self.emit("xorpd %xmm2, %xmm2")
            self.emit("ucomisd %xmm2, %xmm1")
            self.emit(f"jp {ok}")
            self.emit(f"je {stub}")
            self.label(ok)
        instr = {"+": "addsd", "-": "subsd", "*": "mulsd", "/": "divsd"}[op]
        self.emit(f"{instr} {src}, %xmm0")

    def gen_int_binary(self, op: str, gen_left: Callable[[], None], right: ast.Expr, node: ast.Node,
                       line: int) -> None:
        if op == "^":
            if isinstance(right, ast.Literal) and type(right.value) is int and right.value in (0, 1, 2, 3):
                gen_left()
                k = right.value
                if k == 0:
                    self.emit("movq $1, %rax")
                elif k >= 2:
                    self.emit("movq %rax, %rcx")
                    for _ in range(k - 1):
                        self.emit("imulq %rcx, %rax")
                        self.check_overflow(node)
                return
            self.ccall("jl_ipow", [I(gen_left), I(lambda: self.gen_expr(right)), Imm(line)])
            return
        gen_left()
        src = self.simple_int_operand(right)
        if src is None:
            self.push(INT)
            self.gen_expr(right)
            self.emit("movq %rax, %rcx")
            self.pop(INT, "%rax")
            src = "%rcx"
        if op == "+":
            self.emit(f"addq {src}, %rax")
            self.check_overflow(node)
        elif op == "-":
            self.emit(f"subq {src}, %rax")
            self.check_overflow(node)
        elif op == "*":
            if src.startswith("$"):
                self.emit(f"imulq {src}, %rax, %rax")
            else:
                self.emit(f"imulq {src}, %rax")
            self.check_overflow(node)
        elif op in ("//", "%"):
            if src != "%rcx":
                self.emit(f"movq {src}, %rcx")
            zero = self.panic_stub("divzero" if op == "//" else "modzero", line)
            self.emit("testq %rcx, %rcx")
            self.emit(f"je {zero}")
            normal, done = self.new_label("div"), self.new_label("divdone")
            self.emit("cmpq $-1, %rcx")
            self.emit(f"jne {normal}")
            if op == "//":
                self.emit("negq %rax")  # x // -1 == -x (overflows only for INT64_MIN)
                self.check_overflow(node)
            else:
                self.emit("xorl %eax, %eax")  # x % -1 == 0
            self.emit(f"jmp {done}")
            self.label(normal)
            self.emit("cqto")
            self.emit("idivq %rcx")
            # Round the quotient toward -infinity like Python
            if op == "//":
                self.emit("testq %rdx, %rdx")
                self.emit(f"je {done}")
                self.emit("xorq %rcx, %rdx")
                self.emit(f"jns {done}")
                self.emit("decq %rax")
            else:
                self.emit("movq %rdx, %rax")
                self.emit("testq %rax, %rax")
                self.emit(f"je {done}")
                self.emit("movq %rax, %rdx")
                self.emit("xorq %rcx, %rdx")
                self.emit(f"jns {done}")
                self.emit("addq %rcx, %rax")
            self.label(done)
        else:  # pragma: no cover
            raise AssertionError(op)

    def gen_int_compare_operands(self, left: ast.Expr, right: ast.Expr) -> None:
        """Set the flags for left - right (ints and bools)."""
        self.gen_expr(left)
        src = self.simple_int_operand(right)
        if src is None:
            self.push(INT)
            self.gen_expr(right)
            self.emit("movq %rax, %rcx")
            self.pop(INT, "%rax")
            src = "%rcx"
        self.emit(f"cmpq {src}, %rax")

    def gen_float_operands(self, left: ast.Expr, right: ast.Expr) -> None:
        """Leave left in %xmm0 and right in %xmm1, both as floats."""
        self.gen_expr(left)
        self.coerce(self.ty(left), FLOAT)
        src = self.simple_float_operand(right)
        if src is not None:
            self.emit(f"movsd {src}, %xmm1")
            return
        self.push(FLOAT)
        self.gen_expr(right)
        self.coerce(self.ty(right), FLOAT)
        self.emit("movapd %xmm0, %xmm1")
        self.pop(FLOAT, "%xmm0")

    _SETCC = {"<": "setl", "<=": "setle", ">": "setg", ">=": "setge", "==": "sete", "!=": "setne"}

    def e_Compare(self, node: ast.Compare) -> None:
        if len(node.ops) == 1:
            self.compare_pair(node.operands[0], node.ops[0], node.operands[1], node)
            return
        # Chained comparison: a < b < c is (a < b) and (b < c), evaluating b once
        false_l, end = self.new_label("chainf"), self.new_label("chaine")
        prev = self.temp()
        first = node.operands[0]
        self.gen_expr(first)
        self.store(prev, self.ty(first))
        prev_t = self.ty(first)
        for op, rhs in zip(node.ops, node.operands[1:]):
            rt = self.ty(rhs)
            cur = self.temp()
            self.gen_expr(rhs)
            self.store(cur, rt)
            self.compare_values(prev, prev_t, op, cur, rt, node)
            self.emit("testq %rax, %rax")
            self.emit(f"je {false_l}")
            prev, prev_t = cur, rt
        self.emit("movl $1, %eax")
        self.emit(f"jmp {end}")
        self.label(false_l)
        self.emit("xorl %eax, %eax")
        self.label(end)

    def compare_pair(self, left: ast.Expr, op: str, right: ast.Expr, node: ast.Node) -> None:
        lt, rt = self.ty(left), self.ty(right)
        if op in ("in", "not in") or not _is_simple_pair(lt, rt):
            a, b = self.temp(), self.temp()
            self.gen_expr(left)
            self.store(a, lt)
            self.gen_expr(right)
            self.store(b, rt)
            self.compare_values(a, lt, op, b, rt, node)
            return
        if lt in (INT, BOOL) and rt == lt:
            self.gen_int_compare_operands(left, right)
            self.emit(f"{self._SETCC[op]} %al")
            self.emit("movzbl %al, %eax")
            return
        self.gen_float_operands(left, right)
        self.float_setcc(op)

    def float_setcc(self, op: str) -> None:
        if op == ">":
            self.emit("ucomisd %xmm1, %xmm0")
            self.emit("seta %al")
        elif op == ">=":
            self.emit("ucomisd %xmm1, %xmm0")
            self.emit("setae %al")
        elif op == "<":
            self.emit("ucomisd %xmm0, %xmm1")
            self.emit("seta %al")
        elif op == "<=":
            self.emit("ucomisd %xmm0, %xmm1")
            self.emit("setae %al")
        elif op == "==":
            self.emit("ucomisd %xmm1, %xmm0")
            self.emit("sete %al")
            self.emit("setnp %cl")
            self.emit("andb %cl, %al")
        else:
            self.emit("ucomisd %xmm1, %xmm0")
            self.emit("setne %al")
            self.emit("setp %cl")
            self.emit("orb %cl, %al")
        self.emit("movzbl %al, %eax")

    def compare_values(self, a: str, at: Type, op: str, b: str, bt: Type, node: ast.Node) -> None:
        """Compare two values stored in frame slots, leaving 0 or 1 in %rax."""
        if op in ("in", "not in"):
            if bt == STR:
                self.ccall("jl_str_contains", [Mem(b), Mem(a)])
            else:
                elem = bt.elem if isinstance(bt, ListT) else INT
                if elem == FLOAT:
                    if at == INT:
                        self.emit(f"cvtsi2sdq {a}, %xmm0")
                        self.emit(f"movsd %xmm0, {a}")
                    self.ccall("jl_list_contains_float", [Mem(b), Mem(a, "float")])
                elif elem == STR:
                    self.ccall("jl_list_contains_str", [Mem(b), Mem(a)])
                else:
                    if at == FLOAT:  # float looked up in an int list
                        self.ccall("jl_list_contains_float_in_int", [Mem(b), Mem(a, "float")])
                    else:
                        self.ccall("jl_list_contains_int", [Mem(b), Mem(a)])
            if op == "not in":
                self.emit("xorq $1, %rax")
            return
        if at in NUMERIC and bt in NUMERIC:
            if at == INT and bt == INT:
                self.emit(f"movq {a}, %rax")
                self.emit(f"cmpq {b}, %rax")
                self.emit(f"{self._SETCC[op]} %al")
                self.emit("movzbl %al, %eax")
                return
            for operand, t, reg in ((a, at, "%xmm0"), (b, bt, "%xmm1")):
                if t == INT:
                    self.emit(f"cvtsi2sdq {operand}, {reg}")
                else:
                    self.emit(f"movsd {operand}, {reg}")
            self.float_setcc(op)
            return
        if at == STR and bt == STR:
            if op in ("==", "!="):
                self.ccall("jl_str_eq", [Mem(a), Mem(b)])
                if op == "!=":
                    self.emit("xorq $1, %rax")
                return
            self.ccall("jl_str_cmp", [Mem(a), Mem(b)])
            self.emit("cmpq $0, %rax")
            self.emit(f"{self._SETCC[op]} %al")
            self.emit("movzbl %al, %eax")
            return
        if at == bt == NIL:
            self.emit("movl $1, %eax" if op == "==" else "xorl %eax, %eax")
            return
        if at == bt == BOOL:
            self.emit(f"movq {a}, %rax")
            self.emit(f"cmpq {b}, %rax")
            self.emit(f"{self._SETCC[op]} %al")
            self.emit("movzbl %al, %eax")
            return
        # Different types are never equal
        self.emit("movl $1, %eax" if op == "!=" else "xorl %eax, %eax")

    def e_Logical(self, node: ast.Logical) -> None:
        false_l, end = self.new_label("lf"), self.new_label("le")
        self.gen_cond(node, false_l, False)
        self.emit("movl $1, %eax")
        self.emit(f"jmp {end}")
        self.label(false_l)
        self.emit("xorl %eax, %eax")
        self.label(end)

    def e_IfExpr(self, node: ast.IfExpr) -> None:
        self.gen_if(node, self.ty(node))

    def gen_if(self, node: ast.IfExpr, want: Type) -> None:
        end = self.new_label("endif")
        value = want not in (None, NIL, NEVER)
        for cond, block in node.branches:
            nxt = self.new_label("else")
            self.gen_cond(cond, nxt, False)
            if value:
                self.gen_block_value(block.stmts, want)
            else:
                for stmt in block.stmts:
                    self.gen_stmt(stmt)
            self.emit(f"jmp {end}")
            self.label(nxt)
        if node.else_ is not None:
            if value:
                self.gen_block_value(node.else_.stmts, want)
            else:
                for stmt in node.else_.stmts:
                    self.gen_stmt(stmt)
        self.label(end)

    def e_Index(self, node: ast.Index) -> None:
        ot = self.ty(node.obj)
        if ot == STR:
            self.ccall("jl_str_index", [I(lambda: self.gen_expr(node.obj)), I(lambda: self.gen_expr(node.index)),
                                        Imm(self.line_of(node))])
            return
        elem = ot.elem if isinstance(ot, ListT) else INT
        self.gen_expr(node.obj)
        src = self.simple_int_operand(node.index)
        if src is not None:
            self.emit("movq %rax, %rcx")
            self.emit(f"movq {src}, %rax")
        else:
            self.push(INT)
            self.gen_expr(node.index)
            self.pop(INT, "%rcx")
        self.list_element_address(node)
        if elem == FLOAT:
            self.emit("movsd (%rdx,%rax,8), %xmm0")
        else:
            self.emit("movq (%rdx,%rax,8), %rax")

    def e_Comprehension(self, node: ast.Comprehension) -> None:
        result = self.temp()
        self.ccall("jl_list_new", [Imm(0)])
        self.emit(f"movq %rax, {result}")
        elem_t = self.ty(node.expr)
        # The loop variable is in the comprehension's own scope (s_For handles this with body_scope)
        self.fn.scopes.append(id(node))
        var = self.var_operand(node.targets[0])
        self.fn.scopes.pop()
        body = ast.Block([_ComprehensionBody(node, result, elem_t)], span=node.span)  # type: ignore[list-item]
        loop = ast.For([node.targets[0]], node.iterable, body, span=node.span)
        self.s_For(loop, var=var, body_scope=id(node))
        self.emit(f"movq {result}, %rax")

    def s__ComprehensionBody(self, marker: "_ComprehensionBody") -> None:
        node = marker.node
        skip = self.new_label("compskip")
        if node.cond is not None:
            self.gen_cond(node.cond, skip, False)
        elem_t = marker.elem_t

        def gen() -> None:
            self.gen_expr(node.expr)
            self.float_bits_to_rax(elem_t)
        self.ccall("jl_list_push", [Mem(marker.result), I(gen)])
        self.label(skip)

    # Calls
    def e_Call(self, node: ast.Call) -> None:
        callee = self.fn.inst.calls.get(id(node))
        if callee is not None:
            self.gen_user_call(node, callee)
            return
        name = node.callee.name  # type: ignore[union-attr]
        self.gen_builtin(name, node)

    def gen_user_call(self, node: ast.Call, callee: FnInstance) -> None:
        args = list(node.args) + list(self.fn.inst.defaults.get(id(node), []))
        n = len(args)
        # Pad so the stack is 16-byte aligned after the arguments are pushed
        pad = (-(self.fn.depth + 8 * n)) % 16
        if pad:
            self.emit(f"subq ${pad}, %rsp")
            self.fn.depth += pad
        for arg, ptype in zip(args, callee.argtypes):
            self.gen_expr(arg)
            self.coerce(self.ty(arg), ptype)
            self.push(ptype)
        self.emit(f"call {callee.asm_name}")
        total = 8 * n + pad
        if total:
            self.emit(f"addq ${total}, %rsp")
        self.fn.depth -= total

    def gen_builtin(self, name: str, node: ast.Call) -> None:
        args = node.args
        ts = [self.ty(a) for a in args]
        line = self.line_of(node)

        def g(i: int, to: Type | None = None) -> Callable[[], None]:
            def gen() -> None:
                self.gen_expr(args[i])
                if to is not None:
                    self.coerce(ts[i], to)
            return gen

        if name in ("print", "write"):
            if len(args) > 1:
                # Evaluate all arguments before printing, so print(xs, pop(xs)) matches the interpreter
                slots = []
                for a, t in zip(args, ts):
                    self.gen_expr(a)
                    slot = self.temp()
                    self.store(slot, t)
                    slots.append(slot)
                for i, (slot, t) in enumerate(zip(slots, ts)):
                    if i:
                        self.ccall("jl_print_space", [])
                    self.print_stored(slot, t)
            elif args:
                self.print_value(args[0], ts[0])
            if name == "print":
                self.ccall("jl_print_newline", [])
            self.emit("xorl %eax, %eax")
            return
        if name == "sqrt":
            self.gen_expr(args[0])
            self.coerce(ts[0], FLOAT)
            self.checked_sqrt(node, ts[0])
            return
        # Errors show int arguments without .0, so the runtime is told which ones were ints
        if name in MATH1 or name == "log" and len(args) == 1:
            self.ccall("jl_math1", [Imm(MATH_OPS[name]), F(g(0, FLOAT)), Imm(int(ts[0] == INT)), Imm(line)])
            return
        if name == "log":
            int_args = (ts[0] == INT) | (ts[1] == INT) << 1
            self.ccall("jl_log_base", [F(g(0, FLOAT)), F(g(1, FLOAT)), Imm(int_args), Imm(line)])
            return
        if name in ("atan2", "hypot"):
            self.ccall(f"jl_{name}", [F(g(0, FLOAT)), F(g(1, FLOAT))])
            return
        if name == "abs":
            self.gen_expr(args[0])
            if ts[0] == FLOAT:
                self.emit("movq %xmm0, %rax")
                self.emit("btrq $63, %rax")
                self.emit("movq %rax, %xmm0")
            else:
                done = self.new_label("abs")
                self.emit("testq %rax, %rax")
                self.emit(f"jns {done}")
                self.emit("negq %rax")
                self.check_overflow(node)
                self.label(done)
            return
        if name == "sign":
            self.gen_expr(args[0])
            if ts[0] == FLOAT:
                self.emit("xorpd %xmm1, %xmm1")
                self.emit("ucomisd %xmm1, %xmm0")
                self.emit("seta %al")
                self.emit("movzbl %al, %eax")
                self.emit("ucomisd %xmm0, %xmm1")
                self.emit("seta %cl")
                self.emit("movzbl %cl, %ecx")
                self.emit("subq %rcx, %rax")
            else:
                self.emit("xorl %ecx, %ecx")
                self.emit("testq %rax, %rax")
                self.emit("setg %cl")
                self.emit("setl %al")
                self.emit("movzbl %al, %eax")
                self.emit("subq %rax, %rcx")
                self.emit("movq %rcx, %rax")
            return
        if name in ("floor", "ceil", "trunc", "round") and len(args) == 1:
            if ts[0] == INT:
                self.gen_expr(args[0])
                return
            mode = {"floor": 1, "ceil": 2, "round": 3, "trunc": 4}[name]
            self.ccall("jl_float_to_int", [F(g(0)), Imm(mode), Imm(line)])
            return
        if name == "round":
            if ts[0] == FLOAT:
                self.ccall("jl_round_digits", [F(g(0)), I(g(1))])
            else:
                self.ccall("jl_round_int_digits", [I(g(0)), I(g(1)), Imm(line)])
            return
        if name in ("min", "max"):
            if len(args) == 1:
                lt = ts[0]
                elem = lt.elem if isinstance(lt, ListT) else INT
                fname = "jl_list_minmax_float" if elem == FLOAT else "jl_list_minmax_int"
                self.ccall(fname, [I(g(0)), Imm(1 if name == "max" else 0), Imm(line)])
                return
            self.gen_minmax(name, args, ts)
            return
        if name == "clamp":
            # max(lo, min(hi, x)) with Python's argument order
            x, lo, hi = args
            t = self.ty(node)
            tmp_x, tmp_lo = self.temp(), self.temp()
            self.gen_expr(lo)
            self.store(tmp_lo, t)
            self.gen_expr(x)
            self.store(tmp_x, t)
            self.gen_expr(hi)
            self.minmax_step("min", t, tmp_x)       # min(hi, x)
            self.push(t)
            self.load(tmp_lo, t)
            self.pop(t, "%xmm1" if t == FLOAT else "%rcx")
            self.minmax_regs("max", t)              # max(lo, that)
            return
        if name == "gcd":
            self.ccall("jl_gcd", [I(g(0)), I(g(1)), Imm(line)])
            return
        if name == "lcm":
            self.ccall("jl_lcm", [I(g(0)), I(g(1)), Imm(line)])
            return
        if name == "isqrt":
            self.ccall("jl_isqrt", [I(g(0)), Imm(line)])
            return
        if name == "is_prime":
            self.ccall("jl_is_prime", [I(g(0))])
            return
        if name == "factorial":
            if ts[0] == FLOAT:
                self.ccall("jl_ffact", [F(g(0)), Imm(line)])
            else:
                self.ccall("jl_ifact", [I(g(0)), Imm(line)])
            return
        if name == "pow":
            self.gen_binary("^", g(0), ts[0], args[1], self.ty(node), node)
            return
        if name == "int":
            t = ts[0]
            if t in (INT, BOOL):
                self.gen_expr(args[0])
            elif t == FLOAT:
                self.ccall("jl_float_to_int", [F(g(0)), Imm(0), Imm(line)])
            else:
                self.ccall("jl_str_to_int", [I(g(0)), Imm(line)])
            return
        if name == "float":
            t = ts[0]
            if t == STR:
                self.ccall("jl_str_to_float", [I(g(0)), Imm(line)])
            else:
                self.gen_expr(args[0])
                self.coerce(t, FLOAT)
            return
        if name == "str":
            t = ts[0]
            if t == STR:
                self.gen_expr(args[0])
            elif t == INT:
                self.ccall("jl_str_from_int", [I(g(0))])
            elif t == FLOAT:
                self.ccall("jl_str_from_float", [F(g(0))])
            elif t == BOOL:
                self.ccall("jl_str_from_bool", [I(g(0))])
            elif t == NIL:
                self.gen_expr(args[0])
                self.emit(f"leaq {self.string_label('nil')}(%rip), %rax")
            else:
                elem = t.elem if isinstance(t, ListT) else INT
                self.ccall("jl_str_from_list", [I(g(0)), Lbl(self.string_label(descriptor(elem or INT)))])
            return
        if name == "len":
            if ts[0] == STR:
                self.ccall("jl_str_len", [I(g(0))])
            else:
                self.gen_expr(args[0])
                self.emit("movq (%rax), %rax")
            return
        if name in ("upper", "lower", "trim"):
            self.ccall(f"jl_str_{name}", [I(g(0))])
            return
        if name == "split":
            if len(args) == 2:
                self.ccall("jl_str_split", [I(g(0)), I(g(1))])
            else:
                self.ccall("jl_str_split", [I(g(0)), Imm(0)])
            return
        if name == "chars":
            self.ccall("jl_str_chars", [I(g(0))])
            return
        if name == "join":
            li, si = (0, 1)
            if ts[0] == STR and len(args) == 2 and isinstance(ts[1], ListT):
                li, si = 1, 0
            lt = ts[li]
            elem = lt.elem if isinstance(lt, ListT) and lt.elem is not None else INT
            sep = I(g(si)) if len(args) == 2 else Lbl(self.string_label(""))
            if li == 0:
                self.ccall("jl_list_join", [I(g(li)), Lbl(self.string_label(descriptor(elem))), sep])
            else:  # evaluate in source order: separator first
                tmp = self.temp()
                self.gen_expr(args[si])
                self.emit(f"movq %rax, {tmp}")
                self.ccall("jl_list_join", [I(g(li)), Lbl(self.string_label(descriptor(elem))), Mem(tmp)])
            return
        if name == "replace":
            self.ccall("jl_str_replace", [I(g(0)), I(g(1)), I(g(2))])
            return
        if name in ("starts_with", "ends_with"):
            self.ccall(f"jl_str_{name}", [I(g(0)), I(g(1))])
            return
        if name == "ord":
            self.ccall("jl_str_ord", [I(g(0)), Imm(line)])
            return
        if name == "chr":
            self.ccall("jl_str_chr", [I(g(0)), Imm(line)])
            return
        if name == "input":
            self.ccall("jl_input", [I(g(0)) if args else Lbl(self.string_label(""))])
            return
        if name == "sort":
            lt = ts[0]
            elem = lt.elem if isinstance(lt, ListT) and lt.elem is not None else INT
            self.ccall("jl_list_sorted", [I(g(0)), Lbl(self.string_label(descriptor(elem)))])
            return
        if name == "reverse":
            self.ccall("jl_str_reverse" if ts[0] == STR else "jl_list_reversed", [I(g(0))])
            return
        if name == "index_of":
            coll, item = ts
            if coll == STR:
                self.ccall("jl_str_find", [I(g(0)), I(g(1))])
            else:
                elem = coll.elem if isinstance(coll, ListT) and coll.elem is not None else INT
                if elem == FLOAT:
                    self.ccall("jl_list_index_float", [I(g(0)), F(g(1, FLOAT))])
                elif elem == STR:
                    self.ccall("jl_list_index_str", [I(g(0)), I(g(1))])
                elif item == FLOAT:  # a float looked up in an int list
                    self.ccall("jl_list_index_float_in_int", [I(g(0)), F(g(1))])
                else:
                    self.ccall("jl_list_index_int", [I(g(0)), I(g(1))])
            return
        if name == "push":
            lst = self.temp()
            self.gen_expr(args[0])
            self.emit(f"movq %rax, {lst}")
            list_t = self.ty(node)
            elem = list_t.elem if isinstance(list_t, ListT) else INT
            for i in range(1, len(args)):
                def gen(i: int = i) -> None:
                    self.gen_expr(args[i])
                    self.coerce(ts[i], elem)
                    self.float_bits_to_rax(elem)
                self.ccall("jl_list_push", [Mem(lst), I(gen)])
            self.emit(f"movq {lst}, %rax")
            return
        if name == "pop":
            self.ccall("jl_list_pop", [I(g(0)), Imm(line)])
            self.rax_bits_to_value(self.ty(node))
            return
        if name in ("sum", "product"):
            t = self.ty(node)
            if t == FLOAT:
                self.ccall(f"jl_list_{name}_float", [I(g(0))])
            else:
                self.ccall(f"jl_list_{name}_int", [I(g(0)), Imm(line)])
            return
        if name == "contains":
            a, b = self.temp(), self.temp()
            self.gen_expr(args[0])
            self.store(a, ts[0])
            self.gen_expr(args[1])
            self.store(b, ts[1])
            self.compare_values(b, ts[1], "in", a, ts[0], node)
            return
        if name == "clock":
            self.ccall("jl_clock", [])
            return
        if name == "assert":
            ok = self.new_label("assertok")
            self.gen_cond(args[0], ok, True)
            if len(args) == 2:
                self.ccall("jl_panic_assert", [I(g(1)), Imm(line)])
            else:
                self.ccall("jl_panic_assert", [Lbl(self.string_label("assertion failed")), Imm(line)])
            self.label(ok)
            self.emit("xorl %eax, %eax")
            return
        if name == "exit":
            if args:
                self.ccall("jl_exit", [I(g(0))])
            else:
                self.ccall("jl_exit", [Imm(0)])
            return
        raise AssertionError(f"builtin {name} not implemented in codegen")  # pragma: no cover

    def print_value(self, arg: ast.Expr, t: Type) -> None:
        gen = (lambda: self.gen_expr(arg))
        if t == INT:
            self.ccall("jl_print_int", [I(gen)])
        elif t == FLOAT:
            self.ccall("jl_print_float", [F(gen)])
        elif t == BOOL:
            self.ccall("jl_print_bool", [I(gen)])
        elif t == STR:
            self.ccall("jl_print_str", [I(gen)])
        elif t == NIL:
            gen()
            self.ccall("jl_print_nil", [])
        elif isinstance(t, ListT):
            self.ccall("jl_print_list", [I(gen), Lbl(self.string_label(descriptor(t.elem or INT)))])

    def print_stored(self, slot: str, t: Type) -> None:
        if t == INT:
            self.ccall("jl_print_int", [Mem(slot)])
        elif t == FLOAT:
            self.ccall("jl_print_float", [Mem(slot, "float")])
        elif t == BOOL:
            self.ccall("jl_print_bool", [Mem(slot)])
        elif t == STR:
            self.ccall("jl_print_str", [Mem(slot)])
        elif t == NIL:
            self.ccall("jl_print_nil", [])
        elif isinstance(t, ListT):
            self.ccall("jl_print_list", [Mem(slot), Lbl(self.string_label(descriptor(t.elem or INT)))])

    def gen_minmax(self, name: str, args: list[ast.Expr], ts: list[Type]) -> None:
        t = ts[0]
        acc = self.temp()
        self.gen_expr(args[0])
        self.store(acc, t)
        for a in args[1:]:
            self.gen_expr(a)
            self.minmax_step(name, t, acc, value_is_b=True)
            self.store(acc, t)
        self.load(acc, t)

    def minmax_step(self, name: str, t: Type, stored: str, value_is_b: bool = False) -> None:
        """Combine the register with the value at stored. If value_is_b, the register holds b, otherwise a."""
        if t == FLOAT:
            if value_is_b:
                self.emit("movapd %xmm0, %xmm1")
                self.emit(f"movsd {stored}, %xmm0")
            else:
                self.emit(f"movsd {stored}, %xmm1")
        else:
            if value_is_b:
                self.emit("movq %rax, %rcx")
                self.emit(f"movq {stored}, %rax")
            else:
                self.emit(f"movq {stored}, %rcx")
        self.minmax_regs(name, t)

    def minmax_regs(self, name: str, t: Type) -> None:
        """a in %rax or %xmm0, b in %rcx or %xmm1, result in %rax or %xmm0."""
        if t == FLOAT:
            keep = self.new_label("keep")
            if name == "min":
                self.emit("ucomisd %xmm1, %xmm0")  # a vs b: take b if a > b
            else:
                self.emit("ucomisd %xmm0, %xmm1")  # b vs a: take b if b > a
            self.emit(f"jbe {keep}")
            self.emit("movapd %xmm1, %xmm0")
            self.label(keep)
        else:
            self.emit("cmpq %rcx, %rax")
            self.emit("cmovg %rcx, %rax" if name == "min" else "cmovl %rcx, %rax")


class _ComprehensionBody(ast.Stmt):
    """Marker statement for the body of a comprehension loop."""

    __slots__ = ("node", "result", "elem_t")

    def __init__(self, node: ast.Comprehension, result: str, elem_t: Type) -> None:
        self.node = node
        self.result = result
        self.elem_t = elem_t
        self.span = node.span


def _is_simple_pair(lt: Type, rt: Type) -> bool:
    if lt in (INT, BOOL) and rt == lt:
        return True
    return lt in NUMERIC and rt in NUMERIC


def _asm_string(text: str) -> str:
    out = ['"']
    for byte in text.encode("utf-8"):
        ch = chr(byte)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif 32 <= byte < 127:
            out.append(ch)
        else:
            out.append(f"\\{byte:03o}")
    out.append('"')
    return "".join(out)
