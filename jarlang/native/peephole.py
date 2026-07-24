"""Removes unnecessary instructions from the generated assembly."""

from __future__ import annotations


def _is_instr(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#") and not s.endswith(":") and not s.startswith(".")


def _is_barrier(line: str) -> bool:
    s = line.strip()
    return s.endswith(":") or s.startswith(".")


def _instr(line: str) -> str:
    return line.strip()


def optimize(asm: str) -> str:
    lines = asm.split("\n")
    changed = True
    passes = 0
    while changed and passes < 10:
        passes += 1
        changed = False
        lines, c1 = _remove_unreachable(lines)
        lines, c2 = _pairs(lines)
        changed = c1 or c2
    return "\n".join(lines)


def _next_instr(lines: list[str], i: int) -> int | None:
    """Index of the next instruction after i, or None if a label or directive comes first."""
    j = i + 1
    while j < len(lines):
        s = lines[j].strip()
        if not s or s.startswith("#"):
            j += 1
            continue
        if _is_barrier(lines[j]):
            return None
        return j
    return None


def _pairs(lines: list[str]) -> tuple[list[str], bool]:
    out = list(lines)
    changed = False
    i = 0
    while i < len(out):
        line = out[i]
        if not _is_instr(line):
            i += 1
            continue
        a = _instr(line)
        j = _next_instr(out, i)
        if a.startswith("jmp "):
            # Remove a jump to the next line
            target = a[4:].strip()
            k = i + 1
            while k < len(out) and (not out[k].strip() or out[k].strip().startswith("#")):
                k += 1
            if k < len(out) and out[k].strip() == f"{target}:":
                del out[i]
                changed = True
                continue
        if j is None:
            i += 1
            continue
        b = _instr(out[j])
        replacement = _combine(a, b)
        if replacement is not None:
            new_lines = ["    " + r for r in replacement]
            out[i:j + 1] = new_lines + [ln for ln in out[i + 1:j] if ln.strip().startswith("#")]
            changed = True
            continue
        # Replace a float push and pop with a register move
        if a == "subq $8, %rsp" and b == "movsd %xmm0, (%rsp)":
            k = _next_instr(out, j)
            m = _next_instr(out, k) if k is not None else None
            if k is not None and m is not None:
                c, d = _instr(out[k]), _instr(out[m])
                if c.startswith("movsd (%rsp), %xmm") and d == "addq $8, %rsp":
                    reg = c.split(", ")[1]
                    new = [] if reg == "%xmm0" else [f"    movapd %xmm0, {reg}"]
                    out[i:m + 1] = new
                    changed = True
                    continue
        i += 1
    return out, changed


def _combine(a: str, b: str) -> list[str] | None:
    if a == "pushq %rax" and b.startswith("popq "):
        reg = b[5:].strip()
        return [] if reg == "%rax" else [f"movq %rax, {reg}"]
    if a.startswith("movq %rax, ") and b.startswith("movq ") and b.endswith(", %rax"):
        dst = a[len("movq %rax, "):]
        src = b[len("movq "):-len(", %rax")]
        if dst == src:
            return [a]
    if a.startswith("movsd %xmm0, ") and b.startswith("movsd ") and b.endswith(", %xmm0"):
        dst = a[len("movsd %xmm0, "):]
        src = b[len("movsd "):-len(", %xmm0")]
        if dst == src:
            return [a]
    if a == "movq %rax, %rcx" and b == "movq %rcx, %rax":
        return [a]
    if a == b and a in ("xorl %eax, %eax", "xorpd %xmm1, %xmm1", "xorpd %xmm2, %xmm2"):
        return [a]
    if a.startswith("addq $") and a.endswith(", %rsp") and b == "subq " + a[5:]:
        return []
    return None


def _remove_unreachable(lines: list[str]) -> tuple[list[str], bool]:
    out: list[str] = []
    dead = False
    changed = False
    for line in lines:
        if _is_barrier(line):
            dead = False
            out.append(line)
            continue
        if dead and _is_instr(line):
            changed = True
            continue
        out.append(line)
        s = line.strip()
        if s.startswith("jmp ") or s == "ret":
            dead = True
    return out, changed
