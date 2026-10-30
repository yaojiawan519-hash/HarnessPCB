#!/usr/bin/env python3
"""kicad_sch 的 S 表达式读取原语（纯文本 → 嵌套 list）。"""
import re

TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')


def parse(text):
    toks = TOK.findall(text)
    pos = 0

    def rd():
        nonlocal pos
        t = toks[pos]
        pos += 1
        if t == '(':
            out = []
            while toks[pos] != ')':
                out.append(rd())
            pos += 1
            return out
        if t.startswith('"'):
            return t[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        return t

    return rd()


def H(n):
    return n[0] if isinstance(n, list) and n else None


def find(n, k):
    return [c for c in n if isinstance(c, list) and H(c) == k]


def one(n, k):
    xs = find(n, k)
    return xs[0] if xs else None


def nums(a):
    out = []
    for v in a[1:]:
        if isinstance(v, str):
            try:
                out.append(round(float(v), 3))
            except ValueError:
                pass
    return out


def at(n):
    a = one(n, 'at')
    return nums(a) if a else None
