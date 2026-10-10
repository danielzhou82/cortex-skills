#!/usr/bin/env python3
"""Reference implementation of the Cortex annotation protocol v0.1.

Stdlib only; shells out to git.
"""
from __future__ import annotations

import argparse
import bisect
import difflib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

ANN_DIR = ".cortex/annotations"
CONTEXT_LEN = 32
INACTIONABLE_KINDS = {"note", "highlight"}
KINDS = ["question", "source-needed", "correction", "expand", "compress",
         "decision", "action", "note", "highlight"]
STATUS_BY_EVENT = {
    "create": "open",
    "claim": "in_progress",
    "needs_input": "waiting",
    "resolve": "resolved",
    "reopen": "open",
    "withdraw": "withdrawn",
}
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


# ---------- ids ----------

def ulid(ms: int | None = None) -> str:
    ms = int(time.time() * 1000) if ms is None else ms
    n = (ms << 80) | secrets.randbits(80)
    return "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def short_id(ann_id: str) -> str:
    """Last 8 chars: the random part, unlike the timestamp-heavy prefix."""
    return ann_id[-8:]


# ---------- git ----------

class Git:
    def __init__(self, repo: Path):
        self.repo = repo

    def run(self, *args: str, check: bool = True) -> str:
        r = subprocess.run(["git", "-C", str(self.repo), *args],
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").strip()

    def show(self, commit: str, path: str) -> str | None:
        r = subprocess.run(["git", "-C", str(self.repo), "show", f"{commit}:{path}"],
                           capture_output=True)
        if r.returncode != 0:
            return None
        return normalize(r.stdout.decode("utf-8", errors="replace"))

    def follow_rename(self, commit: str, path: str) -> str | None:
        """Follow `path` from `commit` to HEAD through renames; None if deleted."""
        out = self.run("log", "--reverse", "--format=", "--name-status", "-M",
                       f"{commit}..HEAD", check=False)
        cur = path
        for line in out.splitlines():
            parts = line.split("\t")
            if parts[0].startswith("R") and len(parts) == 3 and parts[1] == cur:
                cur = parts[2]
            elif parts[0] == "D" and len(parts) == 2 and parts[1] == cur:
                return None
        return cur if (self.repo / cur).exists() else None

    def commit_adding(self, relpath: str) -> str | None:
        out = self.run("log", "--diff-filter=A", "--format=%H", "--", relpath, check=False)
        lines = out.split()
        return lines[-1] if lines else None


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ---------- HTML plain text ----------

_HEAD_ONLY = {"html", "head", "title", "meta", "link", "style", "script", "base", "noscript", "template"}
_SKIP = {"script", "style", "noscript", "template"}
_DROP_LEADING_NL = {"pre", "listing", "textarea"}


class _HTMLText(HTMLParser):
    """Plain text of an HTML document as defined by the protocol: text nodes under body in
    document order, minus script/style/noscript/template, character references decoded."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.length = 0
        self.in_body = False
        self.skip = 0
        self.head_el = 0          # inside title/style/... before body
        self.drop_nl = False
        self.heading: tuple[int, int] | None = None   # (level, start offset)
        self.headings: list[tuple[int, int, str]] = []  # (offset, level, text)

    def handle_starttag(self, tag, attrs):
        if not self.in_body:
            if tag == "body" or tag not in _HEAD_ONLY:
                self.in_body = True
            elif tag not in ("html", "head", "meta", "link", "base"):
                self.head_el += 1
        if tag in _SKIP and self.in_body:
            self.skip += 1
        self.drop_nl = tag in _DROP_LEADING_NL
        if len(tag) == 2 and tag[0] == "h" and tag[1] in "123456" and self.in_body:
            self.heading = (int(tag[1]), self.length)

    def handle_startendtag(self, tag, attrs):
        if not self.in_body and (tag == "body" or tag not in _HEAD_ONLY):
            self.in_body = True

    def handle_endtag(self, tag):
        if not self.in_body:
            if tag not in ("html", "head", "meta", "link", "base") and self.head_el:
                self.head_el -= 1
            return
        if tag in _SKIP and self.skip:
            self.skip -= 1
        if self.heading and tag == f"h{self.heading[0]}":
            level, start = self.heading
            self.headings.append((start, level, "".join(self.parts)[start:].strip()))
            self.heading = None

    def handle_data(self, data):
        if self.drop_nl:
            self.drop_nl = False
            if data.startswith("\n"):
                data = data[1:]
        if not self.in_body:
            if self.head_el or not data.strip():
                return
            self.in_body = True
        if self.skip or not data:
            return
        self.parts.append(data)
        self.length += len(data)


def html_text(source: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Returns (plain text, headings as (offset, level, text))."""
    p = _HTMLText()
    p.feed(normalize(source))
    p.close()
    return "".join(p.parts), p.headings


def is_html(path: str) -> bool:
    return path.lower().endswith((".html", ".htm"))


def doc_text(source: str, kind: str) -> str:
    """Text the selector is based on: Markdown source, or HTML plain text."""
    return html_text(source)[0] if kind == "html" else normalize(source)


# ---------- selector ----------

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def headings_at(text: str, offset: int) -> list[str]:
    chain: list[tuple[int, str]] = []
    in_fence = False
    pos = 0
    for line in text.split("\n"):
        if pos > offset:
            break
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            m = HEADING_RE.match(line)
            if m:
                level = len(m.group(1))
                chain = [h for h in chain if h[0] < level] + [(level, m.group(2))]
        pos += len(line) + 1
    return [h[1] for h in chain]


def html_headings_at(headings: list[tuple[int, int, str]], offset: int) -> list[str]:
    chain: list[tuple[int, str]] = []
    for off, level, title in headings:
        if off > offset:
            break
        chain = [h for h in chain if h[0] < level] + [(level, title)]
    return [h[1] for h in chain]


def make_selector(text: str, start: int, end: int, headings: list[str] | None = None) -> dict:
    return {
        "quote": {
            "exact": text[start:end],
            "prefix": text[max(0, start - CONTEXT_LEN):start],
            "suffix": text[end:end + CONTEXT_LEN],
        },
        "position": {"start": start, "end": end},
        "headings": headings_at(text, start) if headings is None else headings,
    }


# ---------- re-anchoring ----------

@dataclass
class Anchor:
    result: str              # anchored | drifted | orphaned
    method: str              # exact | context | diff | bracket | none
    start: int = -1
    end: int = -1
    text: str = ""


def _common_suffix_len(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return n


def similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def map_offsets(old: str, new: str, offsets: list[int]) -> list[tuple[int, bool]]:
    """Map code-point offsets in `old` to `new`. Returns (new_offset, unchanged)."""
    old_lines = old.split("\n")
    new_lines = new.split("\n")
    old_starts = [0]
    for ln in old_lines:
        old_starts.append(old_starts[-1] + len(ln) + 1)
    new_starts = [0]
    for ln in new_lines:
        new_starts.append(new_starts[-1] + len(ln) + 1)
    ops = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
    results = []
    for off in offsets:
        li = bisect.bisect_right(old_starts, off) - 1
        li = min(li, len(old_lines) - 1)
        col = off - old_starts[li]
        for tag, i1, i2, j1, j2 in ops:
            if i1 <= li < i2:
                if tag == "equal":
                    results.append((new_starts[j1 + li - i1] + col, True))
                elif tag == "replace":
                    old_block = "\n".join(old_lines[i1:i2])
                    new_block = "\n".join(new_lines[j1:j2])
                    inner = off - old_starts[i1]
                    cops = difflib.SequenceMatcher(None, old_block, new_block,
                                                   autojunk=False).get_opcodes()
                    mapped = len(new_block)
                    for ctag, a1, a2, b1, b2 in cops:
                        if a1 <= inner < a2 or (inner == a2 == len(old_block)):
                            mapped = b1 + min(inner - a1, b2 - b1) if ctag != "delete" else b1
                            break
                    results.append((new_starts[j1] + mapped, False))
                else:  # delete
                    results.append((new_starts[j1], False))
                break
        else:
            results.append((len(new), False))
    return results


def reanchor(selector: dict, current: str, original: str | None,
             min_similarity: float = 0.6) -> Anchor:
    q = selector["quote"]
    exact, prefix, suffix = q["exact"], q.get("prefix", ""), q.get("suffix", "")
    pos = selector.get("position", {}).get("start", 0)

    # 1-2. exact match, disambiguated by context then by distance
    hits = [m.start() for m in re.finditer(re.escape(exact), current)] if exact else []
    if len(hits) == 1:
        s = hits[0]
        return Anchor("anchored", "exact", s, s + len(exact), exact)
    if hits:
        def score(s: int) -> tuple:
            pre = _common_suffix_len(current[max(0, s - len(prefix)):s], prefix)
            suf = _common_prefix_len(current[s + len(exact):s + len(exact) + len(suffix)], suffix)
            return (pre + suf, -abs(s - pos))
        s = max(hits, key=score)
        return Anchor("anchored", "context", s, s + len(exact), exact)

    # 3. map through the diff from the original version
    if original is not None and original[pos:pos + len(exact)] == exact:
        (ns, _), (ne, _) = map_offsets(original, current, [pos, pos + len(exact)])
        if ne > ns:
            cand = current[ns:ne]
            if similarity(cand, exact) >= min_similarity:
                return Anchor("drifted", "diff", ns, ne, cand)

    # 4. longest common substring, if long and unique in the current text
    if len(exact) >= 12:
        m = difflib.SequenceMatcher(None, exact, current, autojunk=False).find_longest_match(
            0, len(exact), 0, len(current))
        core = exact[m.a:m.a + m.size]
        if m.size >= max(16, len(exact) * 7 // 10) and current.count(core) == 1:
            s = max(0, m.b - m.a)
            e = min(len(current), s + len(exact))
            if similarity(current[s:e], exact) >= min_similarity:
                return Anchor("drifted", "substring", s, e, current[s:e])

    # 5. prefix ... suffix bracket
    if len(prefix) >= 8 and len(suffix) >= 8:
        for m in re.finditer(re.escape(prefix), current):
            s = m.end()
            e = current.find(suffix, s, s + max(len(exact) * 3, 200))
            if e > s and similarity(current[s:e], exact) >= min_similarity:
                return Anchor("drifted", "bracket", s, e, current[s:e])

    return Anchor("orphaned", "none")


# ---------- store ----------

@dataclass
class Annotation:
    id: str
    events: list[dict] = field(default_factory=list)
    status: str = "open"
    kind: str = ""
    body: str = ""
    path: str = ""
    target: dict = field(default_factory=dict)
    last_at: str = ""

    @property
    def actionable(self) -> bool:
        return self.status == "open" and self.kind not in INACTIONABLE_KINDS


def derive(ann_id: str, events: list[dict]) -> Annotation:
    events = sorted(events, key=lambda e: e["id"])
    a = Annotation(ann_id, events)
    for e in events:
        t = e["type"]
        if t == "create":
            a.kind, a.body, a.target = e["kind"], e.get("body", ""), e["target"]
            a.path = e["target"]["path"]
        elif t == "edit":
            a.kind = e.get("kind", a.kind)
            a.body = e.get("body", a.body)
        elif t == "relocate":
            a.path = e["path"]
        elif t == "comment" and a.status == "waiting":
            a.status = "open"
        if t in STATUS_BY_EVENT:
            a.status = STATUS_BY_EVENT[t]
        a.last_at = e["at"]
    return a


class Store:
    def __init__(self, repo: Path, author: str):
        self.repo = repo
        self.git = Git(repo)
        self.author = author
        self.root = repo / ANN_DIR

    def load(self) -> list[Annotation]:
        out = []
        if not self.root.exists():
            return out
        for d in sorted(self.root.iterdir()):
            if d.is_dir():
                evs = [json.loads(f.read_text("utf-8")) for f in sorted(d.glob("*.json"))]
                if evs:
                    out.append(derive(d.name, evs))
        return out

    def find(self, prefix: str) -> Annotation:
        key = prefix.upper()
        hits = [a for a in self.load() if a.id.startswith(key) or a.id.endswith(key)]
        if len(hits) != 1:
            sys.exit(f"{len(hits)} annotations match {prefix!r} (prefix or suffix)")
        return hits[0]

    def write_event(self, ann_id: str, type_: str, **fields) -> Path:
        eid = ann_id if type_ == "create" else ulid()
        ev = {"v": 1, "id": eid, "annotation": ann_id, "type": type_,
              "at": now_iso(), "author": self.author}
        ev.update({k: v for k, v in fields.items() if v is not None})
        path = self.root / ann_id / f"{eid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n", "utf-8")
        return path

    def current_path(self, a: Annotation) -> str | None:
        if (self.repo / a.path).exists():
            return a.path
        return self.git.follow_rename(a.target["commit"], a.path)

    def anchor(self, a: Annotation) -> tuple[str | None, Anchor]:
        path = self.current_path(a)
        if path is None:
            return None, Anchor("orphaned", "none")
        kind = a.target.get("text", "markdown")
        current = doc_text((self.repo / path).read_text("utf-8"), kind)
        original = self.git.show(a.target["commit"], a.target["path"])
        if original is not None:
            original = doc_text(original, kind)
        return path, reanchor(a.target["selector"], current, original)


# ---------- cli ----------

def cmd_create(st: Store, args) -> None:
    source = (st.repo / args.path).read_text("utf-8")
    kind = "html" if is_html(args.path) else "markdown"
    text, headings = html_text(source) if kind == "html" else (normalize(source), None)
    hits = [m.start() for m in re.finditer(re.escape(args.quote), text)]
    if not hits:
        sys.exit("quote not found in document")
    if len(hits) > 1 and args.occurrence is None:
        sys.exit(f"quote occurs {len(hits)} times; pass --occurrence N (0-based)")
    s = hits[args.occurrence or 0]
    ann_id = ulid()
    target = {"path": args.path, "commit": st.git.head(),
              "selector": make_selector(text, s, s + len(args.quote),
                                        html_headings_at(headings, s) if headings is not None else None)}
    if kind == "html":
        target["text"] = "html"
    print(st.write_event(ann_id, "create", kind=args.kind, body=args.body, target=target))


def cmd_event(st: Store, args) -> None:
    a = st.find(args.id)
    print(st.write_event(a.id, args.type, body=args.body, outcome=args.outcome,
                         kind=args.kind, path=args.path))


def cmd_list(st: Store, args) -> None:
    for a in st.load():
        if args.status and a.status != args.status:
            continue
        if args.actionable and not a.actionable:
            continue
        path, anc = st.anchor(a)
        if args.path and args.path not in (path, a.path, a.target["path"]):
            continue
        if args.actionable and anc.result == "orphaned":
            continue
        quote = a.target["selector"]["quote"]["exact"].replace("\n", " ")
        print(f"{short_id(a.id)}  {a.status:<11} {a.kind:<13} {anc.result:<8} {path or a.path}")
        print(f"          「{quote[:60]}」 {a.body[:60]}")


def cmd_show(st: Store, args) -> None:
    a = st.find(args.id)
    path, anc = st.anchor(a)
    print(json.dumps({
        "id": a.id, "status": a.status, "kind": a.kind, "body": a.body,
        "path": path, "anchor": anc.__dict__,
        "events": a.events,
    }, ensure_ascii=False, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=".", type=Path)
    p.add_argument("--author", default=os.environ.get("CORTEX_AUTHOR", "agent@" + os.uname().nodename.split(".")[0]))
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("path")
    c.add_argument("quote")
    c.add_argument("--kind", choices=KINDS, default="note")
    c.add_argument("--body", default="")
    c.add_argument("--occurrence", type=int)

    e = sub.add_parser("event")
    e.add_argument("id")
    e.add_argument("type", choices=["comment", "edit", "claim", "needs_input",
                                    "resolve", "reopen", "withdraw", "relocate"])
    e.add_argument("--body")
    e.add_argument("--outcome", choices=["changed", "answered", "no_change"])
    e.add_argument("--kind", choices=KINDS)
    e.add_argument("--path")

    ls = sub.add_parser("list")
    ls.add_argument("--status", choices=["open", "in_progress", "waiting", "resolved", "withdrawn"])
    ls.add_argument("--actionable", action="store_true")
    ls.add_argument("--path", help="only annotations on this document (current or original path)")

    s = sub.add_parser("show")
    s.add_argument("id")

    args = p.parse_args()
    st = Store(args.repo.resolve(), args.author)
    {"create": cmd_create, "event": cmd_event, "list": cmd_list, "show": cmd_show}[args.cmd](st, args)


if __name__ == "__main__":
    main()
