"""记忆系统的公共部分：项目注册表、笔记头部的读写、索引、指纹、密钥扫描、锁和台账。"""
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

MEMORY = Path(os.environ.get("MEMORY_DIR", "~/memory")).expanduser()
REGISTRY = MEMORY / "projects" / "INDEX.md"
PENDING = MEMORY / "inbox" / "pending-bugs"     # 待审批的 bug 笔记，按项目分目录
GLOBAL_BUGS = MEMORY / "bugs"                   # 全局层
STATE = MEMORY / "_state" / "processed.jsonl"   # 台账：每个会话文件处理到了第几行
LOCK = MEMORY / "_state" / "memory.lock"
FILENAME = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+(-[a-z0-9]+)*\.md$")
NOTE_ID = re.compile(r"^[a-z]+-[0-9a-f]{12}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EVIDENCE = re.compile(r"^(?P<path>.+)#L(?P<a>\d+)(?:-L(?P<b>\d+))?(?:@(?P<hash>[0-9a-f]{8}))?$")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
HOME_DIR = re.compile(r"^/(?:Users|home)/[^/]+")
BASE_LINE = re.compile(r"^base_hash:.*\n", re.M)

LIVE = ("current", "active", "")                # 这些状态的笔记进索引
LIST_KEYS = ("sources", "derived_from", "evidence", "keywords", "supersedes")
ORDER = ["schema_version", "id", "type", "title", "project", "status", "first_seen", "last_seen",
         "sources", "derived_from", "evidence", "session_branch", "distilled_at",
         "found_at", "fixed_at", "keywords", "confidence", "supersedes", "base_hash"]
INDEX_NOTE = "<!-- 本文件由脚本根据各笔记的头部生成，不要手工修改。改了笔记后运行 review.py reindex -->"


def git(repo, *args):
    """在仓库里跑一条 git 命令，失败或不是 git 仓库时返回空字符串。"""
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


# ---------- 锁和原子写入 ----------
@contextlib.contextmanager
def lock(wait):
    """nightly.py 和 review.py 里所有会改动文件的操作都要先拿这把锁。
    wait=False 时拿不到就直接退出，避免审批命令卡在每晚任务后面。"""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("另一个记忆脚本正在运行（多半是每晚任务），稍后再试。")
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def write_atomic(path, text):
    """先写临时文件再改名，避免中途被打断时留下半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------- 项目注册表 ----------
def load_registry():
    """读 projects/INDEX.md 的表格。每个状态为 active 且有仓库的项目返回一条记录：
    name 项目名，repo M4 Pro 上的仓库路径，local 笔记是否只留在本地，
    prefixes 完整的路径前缀，tails 去掉家目录之后的路径（M2 Max 那一列写 ~ 开头时用）。
    笔记默认只留在本地。只有「笔记位置」一列明确写了「仓库」，批准后的笔记才进项目仓库。"""
    rows = []
    for line in REGISTRY.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6 or cells[0] in ("项目", "") or set(cells[0]) <= set("-: "):
            continue
        name, status, m4, m2 = cells[0], cells[3], cells[4], cells[5]
        if status != "active" or not m4.startswith(("~", "/")):
            continue
        repo = Path(m4).expanduser()
        entry = {"name": name, "repo": repo, "prefixes": [str(repo)], "tails": [],
                 "local": not (len(cells) > 6 and cells[6] in ("仓库", "repo"))}
        if m2.startswith("~/"):
            entry["tails"].append(m2[1:].rstrip("/"))    # 两台机器的用户名可以不同，只比家目录之后的部分
        elif m2.startswith("/"):
            entry["prefixes"].append(m2.rstrip("/"))
        rows.append(entry)
    return rows


def projects():
    return {entry["name"]: entry for entry in load_registry()}


def _under(path, base):
    return path == base or path.startswith(base + "/")


def match_project(cwd, registry):
    """按会话的工作目录找项目，路径最长的优先。找不到返回 None。"""
    home = HOME_DIR.match(cwd)
    rest = cwd[home.end():] if home else None
    best, size = None, -1
    for entry in registry:
        sizes = [len(p) for p in entry["prefixes"] if _under(cwd, p)]
        if rest is not None:
            sizes += [home.end() + len(t) for t in entry["tails"] if _under(rest, t)]
        if sizes and max(sizes) > size:
            best, size = entry, max(sizes)
    return best


def bugs_dir(entry):
    """这个项目已生效的 bug 笔记放在哪。默认留在 memory 仓库里；
    注册表里明确标了「仓库」的项目，才放进项目仓库的 docs/bugs/。"""
    if entry["local"]:
        return MEMORY / "projects" / entry["name"] / "bugs"
    return entry["repo"] / "docs" / "bugs"


# ---------- 笔记头部 ----------
def new_id(kind="bug"):
    """给一条新笔记分配编号。编号一经生成就不再变，笔记之间互相引用时写的就是它。"""
    return f"{kind}-{uuid.uuid4().hex[:12]}"


def _q(value):
    return json.dumps(str(value), ensure_ascii=False)


def _scalar(value):
    value = value.strip()
    if value.startswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value.strip("'\"")


def render_note(meta, body):
    """头部的格式由这里统一决定，模型不参与。"""
    out = ["---"]
    for key in [k for k in ORDER if k in meta] + [k for k in meta if k not in ORDER]:
        value = meta[key]
        if key == "keywords":
            out.append(f"{key}: [{', '.join(_q(x) for x in value)}]")
        elif isinstance(value, list):
            out.append(f"{key}: []" if not value else f"{key}:")
            out += [f"  - {_q(x)}" for x in value]
        elif key == "title":
            out.append(f"{key}: {_q(value)}")
        else:
            out.append(f"{key}: {value}".rstrip())
    return "\n".join(out + ["---", "", body.strip(), ""])


def parse_note(text):
    """返回 (头部字典, 正文)。能读 render_note 写出的格式，也容忍手工改过的常见写法。"""
    if not text.startswith("---"):
        raise ValueError("缺少头部")
    head, sep, body = text.split("\n", 1)[1].partition("\n---")
    if not sep:
        raise ValueError("头部没有结束标记")
    meta, key = {}, None
    for line in head.splitlines():
        if line.lstrip().startswith("- ") and key:
            if not isinstance(meta.get(key), list):
                meta[key] = []
            meta[key].append(_scalar(line.split("- ", 1)[1]))
        elif ":" in line and not line.startswith((" ", "#")):
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if value.startswith("[") and value.endswith("]"):
                try:
                    meta[key] = [str(x) for x in json.loads(value)]
                except json.JSONDecodeError:
                    meta[key] = [_scalar(x) for x in value[1:-1].split(",") if x.strip()]
            else:
                meta[key] = _scalar(value)
    for k in LIST_KEYS:
        if not isinstance(meta.get(k), list):
            meta[k] = [meta[k]] if meta.get(k) else []
    return meta, body.strip("\n")


# ---------- 索引 ----------
def _cell(value):
    return CONTROL.sub("", str(value)).replace("|", "/").replace("\n", " ").strip()[:200]


def rebuild_index(folder):
    """INDEX.md 只是各笔记头部的一个视图，每次都从头生成。返回收录的条数。"""
    rows, broken = [], []
    for f in sorted(folder.glob("*.md")):
        if f.name == "INDEX.md":
            continue
        try:
            meta, _ = parse_note(f.read_text(encoding="utf-8"))
        except ValueError:
            broken.append(f.name)
            continue
        if (meta.get("status") or "") not in LIVE:
            continue                                   # 被取代和归档的笔记不进索引
        rows.append((_cell(meta.get("last_seen") or ""), _cell(meta.get("title") or f.stem),
                     _cell(", ".join(meta["keywords"])), f.name))
    rows.sort(key=lambda r: (r[0], r[3]), reverse=True)
    lines = [INDEX_NOTE, "", "| 最近出现 | 症状 | 关键词 | 文件 |", "| --- | --- | --- | --- |"]
    lines += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows]
    lines += [f"| | （头部无法解析，请检查这个文件） | | {name} |" for name in broken]
    write_atomic(folder / "INDEX.md", "\n".join(lines) + "\n")
    return len(rows)


# ---------- 指纹 ----------
def merged(old, new):
    """两个列表去重合并，保持先后顺序。"""
    return list(dict.fromkeys(list(old or []) + list(new or [])))


def text_hash(text):
    """一条笔记全文的指纹。待审批的更新记下它所基于的那一版，批准时核对。"""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def without_base(text):
    """去掉头部里的 base_hash 行。它只在待审批期间有用，不写进正式笔记。"""
    head, sep, body = text.partition("\n---")
    return BASE_LINE.sub("", head + "\n", count=1)[:-1] + sep + body


def digest(lines, n):
    """前 n 行的指纹，用来发现已处理过的内容被改写。"""
    return hashlib.sha1("\n".join(lines[:n]).encode("utf-8", "replace")).hexdigest()[:16]


def span_hash(lines, a, b):
    """第 a 到 b 行（从 1 数起）的指纹，写进证据里，以后核对原文有没有变。"""
    return hashlib.sha1("\n".join(lines[a - 1:b]).encode("utf-8", "replace")).hexdigest()[:8]


def check_evidence(entry):
    """核对一条证据。返回 None 表示原文没变，否则返回问题说明。"""
    m = EVIDENCE.match(entry)
    if not m:
        return "格式无法识别"
    f = MEMORY / m["path"]
    if not f.exists():
        return "原始会话文件不存在"
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    a, b = int(m["a"]), int(m["b"] or m["a"])
    if b > len(lines):
        return "行号超出了文件范围"
    if not m["hash"]:
        return "没有指纹，无法核对"
    if span_hash(lines, a, b) != m["hash"]:
        return "原文已经变了，行号不再可信（旧版本可能在 raw/revisions/ 里）"
    return None


# ---------- 台账 ----------
def load_ledger():
    """台账里的全部记录，按写入顺序。"""
    records = []
    if STATE.exists():
        for line in STATE.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                rec["path"], rec["lines"], rec["size"]
                records.append(rec)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
    return records


def load_done():
    """返回 {会话路径: 最近一条台账记录}。"""
    return {rec["path"]: rec for rec in load_ledger()}


def _append(rec):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    rec["at"] = dt.datetime.now().isoformat(timespec="seconds")
    with STATE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def mark(rel, lines, size, outcome, start=0, project="", titles=None):
    """记一笔：这个会话已经处理到末尾。台账总是在产物落盘之后才写。
    start 是这次处理开始的行，titles 是这次产出的笔记 {文件名: 标题}，重试时要用。"""
    titles = titles or {}
    _append({"path": rel, "lines": len(lines), "size": size, "start": start,
             "hash": digest(lines, len(lines)), "outcome": outcome,
             "project": project, "notes": list(titles), "titles": titles})


def rollback(rel, start, redo, handled):
    """把一个会话退回到第 start 行，下一晚从那里重新提炼。redo 是要重写的问题，
    handled 是同一段会话里已经批准、拒绝或还在待审批的问题，重新提炼时不再提。"""
    rec = {"path": rel, "lines": start, "size": -1, "start": start, "hash": "",
           "outcome": "retry", "project": "", "notes": [], "redo": redo, "handled": handled}
    _append(rec)
    return rec


# ---------- 密钥扫描 ----------
_VAL = r"[A-Za-z0-9+/_.\-]"
SECRET_PATTERNS = [
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("API key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}")),
    ("带密码的连接串", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@")),
    ("密码或密钥赋值", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\b\s*[:=]\s*['\"]?"
        rf"(?={_VAL}*\d)(?={_VAL}*[A-Za-z]){_VAL}{{8,}}")),
]


def scan_secrets(text):
    """返回 [(行号, 类型)]。只找密钥、口令这类模式，不管业务内容。"""
    hits = []
    for no, line in enumerate(text.splitlines(), 1):
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                hits.append((no, label))
    return hits
