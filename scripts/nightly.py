#!/usr/bin/env python3
"""每晚任务：同步本地会话 -> 清洗 -> 粗筛 -> 提炼 bug -> 放进待审批区。
这个脚本不往任何项目仓库里写东西。笔记要经过 review.py 批准才会生效。
写入顺序固定：产物先落盘，台账最后写。中途被打断时，下一晚会把那段会话重新处理一遍。"""
import datetime as dt
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memlib import (CONTROL, FILENAME, MEMORY, PENDING, bugs_dir, digest, git, load_done,
                    load_registry, lock, mark, match_project, merged, new_id, parse_note,
                    render_note, span_hash, text_hash, write_atomic)

RAW = MEMORY / "raw" / "sessions"               # 两台机器会话目录的镜像
REVISIONS = MEMORY / "raw" / "revisions"        # 内容被改写过的会话，旧版本留在这里
SINCE = MEMORY / "_state" / "since"             # 起始时间，首次运行时生成
LOG = MEMORY / "_state" / "nightly.log"
PROMPTS = MEMORY / "scripts" / "prompts"

SENTINEL = "[memory-nightly]"        # 脚本自己发起的会话以它开头，扫描时跳过
M2_HOST = "m2max"                    # ~/.ssh/config 里 M2 Max 的主机名
TRIAGE_MODEL = "haiku"               # 粗筛用便宜的小模型
DISTILL_MODEL = "sonnet"             # 提炼用强模型
FIRST_RUN_DAYS = 1                   # 首次运行时往回处理几天的会话
SETTLE_HOURS = 24                    # 会话超过这么久没有改动，才认为已经结束
MIN_USER_TURNS = 3                   # 整段会话里用户发言少于这个数，不值得提炼
MAX_CHARS = 120_000                  # 新增部分送给模型的字数上限
CONTEXT_CHARS = 40_000               # 背景部分（之前处理过的内容）的字数上限
AUDIT_SAMPLE = 2                     # 每晚抽几段被规则过滤掉的会话，交给小模型复核
# 报错信号只认两种：工具输出里的报错，和用户自己说出了问题。助手的话不算。
ERR_OUTPUT = re.compile(r"traceback|exception|error[:\s]|failed|fatal|panic|exit code [1-9]|报错|失败", re.I)
COMPLAINT = re.compile(r"报错|出错|不行|不对|失败|挂了|崩溃|bug|error|doesn'?t work|not working|broken", re.I)
REJECTED = "doesn't want to proceed"   # 用户拒绝工具调用也会带 is_error，不算报错
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
LINE_REF = re.compile(r"^L(\d+)(?:-L(\d+))?$")
COMMITISH = re.compile(r"^[0-9A-Za-z._-]{3,40}$")
SECTIONS = [("symptom", "症状"), ("root_cause", "根因"), ("fix", "修复"), ("prevention", "如何避免")]


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def notify(msg):
    script = f'display notification {json.dumps(msg, ensure_ascii=False)} with title "memory nightly"'
    subprocess.run(["osascript", "-e", script], capture_output=True)


# ---------- 1. 同步 ----------
def mirror(src, machine, ssh=False):
    """把一台机器的会话目录镜像到 raw/sessions/。不带 --delete，源头清理掉的旧会话在镜像里仍然保留。
    被覆盖的旧文件先挪到 raw/revisions/，再由 prune 决定去留。"""
    dst = RAW / machine / "claude"
    dst.mkdir(parents=True, exist_ok=True)
    backup = REVISIONS / machine / time.strftime("%Y%m%d-%H%M%S")
    base = ["rsync", "-a"] + (["-e", "ssh -o ConnectTimeout=10 -o BatchMode=yes"] if ssh else [])
    r = subprocess.run(base + ["--backup", f"--backup-dir={backup}", src, f"{dst}/"],
                       capture_output=True)
    if r.returncode == 1:            # 1 是用法错误：这个 rsync 不认识 --backup-dir
        log("WARN 这台机器的 rsync 不支持 --backup-dir，本次同步不保留被覆盖的旧版本")
        r = subprocess.run(base + [src, f"{dst}/"], capture_output=True)
    prune(backup, dst)
    return r.returncode == 0


def prune(backup, dst):
    """会话正常变长时，旧版本就是新版本的开头，没有保留价值，删掉。内容真被改写的才留下。"""
    if not backup.is_dir():
        return
    for old in sorted(backup.rglob("*")):
        if not old.is_file():
            continue
        cur = dst / old.relative_to(backup)
        if cur.is_file() and cur.read_bytes().startswith(old.read_bytes()):
            old.unlink()
        else:
            log(f"WARN 会话内容被改写了，旧版本保留在 {old.relative_to(MEMORY)}")
    for d in sorted(backup.rglob("*"), reverse=True) + [backup, backup.parent]:
        try:
            d.rmdir()                # 只能删掉空目录，非空的会留下
        except OSError:
            pass


def sync():
    local = Path("~/.claude/projects/").expanduser()
    if not mirror(f"{local}/", "m4pro"):
        log("WARN 本机会话同步失败")
    if not mirror(f"{M2_HOST}:.claude/projects/", "m2max", ssh=True):
        log("WARN m2max 同步失败，本次只处理本机会话；它的会话会在下次同步成功后补上")


# ---------- 2. 清洗 ----------
def clip(text, n):
    text = text.strip()
    return text if len(text) <= n else text[:n] + f"\n…（省略 {len(text) - n} 字）"


def fit(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit // 3] + "\n\n…（中段省略）…\n\n" + text[-(limit * 2 // 3):]


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def record_date(rec):
    """这条记录的本地日期。Claude Code 的 timestamp 是 UTC 时间；没有这个字段时返回空字符串。"""
    m = re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", str(rec.get("timestamp") or ""))
    if not m:
        return ""
    utc = dt.datetime.strptime(m.group(0), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    return str(utc.astimezone().date())


def clean_session(lines, start=0):
    """把 Claude Code 的 jsonl 会话压成可读文本。第 start 行之前是已经处理过的背景，之后是新增部分。
    每一段都标上它在原文件里的行号（L 开头，从 1 数起），供模型引用为证据。
    返回 (cwd, 背景, 新增, 统计, {行号: 日期})。轮数和报错信号按整段会话统计，
    统计里的 branch 是会话记录自带的 git 分支，取最后一条的；bg_cut 表示背景超长被截断了。"""
    parts, day, dates = {"old": [], "new": []}, {"old": "", "new": ""}, {}
    cwd, first_user = "", True
    stats = {"user_turns": 0, "signals": 0, "new_blocks": 0, "own": False, "branch": "",
             "bg_cut": False}
    for i, raw in enumerate(lines):
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant") \
                or rec.get("isSidechain") or rec.get("isMeta"):
            continue                                  # 子代理的记录和 Claude Code 自己插入的说明不算
        cwd = cwd or rec.get("cwd", "")
        if rec.get("gitBranch") not in (None, "", "HEAD"):
            stats["branch"] = str(rec["gitBranch"])
        side, at = ("new" if i >= start else "old"), f"L{i + 1}"
        out, before = parts[side], len(parts[side])
        content = (rec.get("message") or {}).get("content", "")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        for b in blocks or []:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "text" and b.get("text", "").strip():
                text = b["text"]
                if rec["type"] == "user" and rec.get("isCompactSummary"):
                    out.append(f"### 压缩摘要 [{at}]\n{clip(text, 4000)}")
                elif rec["type"] == "user":
                    if first_user and SENTINEL in text:   # 管道输入可能排在提示词前面
                        stats["own"] = True
                        return cwd, "", "", stats, dates
                    first_user = False
                    stats["user_turns"] += 1
                    stats["signals"] += bool(COMPLAINT.search(text))
                    out.append(f"### 用户 [{at}]\n{clip(text, 4000)}")
                else:
                    out.append(f"### 助手 [{at}]\n{clip(text, 4000)}")
            elif kind == "tool_use":
                name, inp = b.get("name", "?"), b.get("input") or {}
                arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                out.append(f"[工具 {name} {at}] {clip(str(arg), 300)}")
            elif kind == "tool_result":
                text = result_text(b.get("content"))
                failed = b.get("is_error") and REJECTED not in text
                if failed or ERR_OUTPUT.search(text[:3000]):
                    stats["signals"] += 1
                    out.append(f"[工具结果·含报错 {at}]\n{clip(text, 1500)}")
            # thinking 和其他类型一律丢弃
        if len(out) == before:
            continue                                  # 这条记录没有留下任何内容
        if side == "new":
            stats["new_blocks"] += len(out) - before
        date = record_date(rec)
        if date:
            dates[i + 1] = date
            if date != day[side]:                     # 日期变了，插一行分隔，方便看出跨天
                day[side] = date
                out.insert(before, f"—— {date} ——")
    background = "\n\n".join(parts["old"])
    stats["bg_cut"] = len(background) > CONTEXT_CHARS
    return (cwd, fit(background, CONTEXT_CHARS),
            fit("\n\n".join(parts["new"]), MAX_CHARS), stats, dates)


def assemble(background, new):
    """一个 bug 经常跨好几天才解决，所以新增部分要带着之前的内容一起给模型看。"""
    if not background:
        return "[新增：本次要处理的内容]\n\n" + new
    return ("[背景：这段会话里之前已经处理过的内容，只用来理解上下文]\n\n" + background
            + "\n\n[新增：本次要处理的内容]\n\n" + new)


# ---------- 3. 调用模型 ----------
def ask_claude(prompt, stdin_text, cwd, model, tools=(), add_dirs=()):
    """无人值守调用 Claude Code。只给只读工具，所有文件都由本脚本自己写。"""
    cmd = ["claude", "-p", f"{SENTINEL}\n{prompt}", "--model", model,
           "--output-format", "json", "--max-turns", "15"]
    for d in add_dirs:
        cmd += ["--add-dir", str(d)]
    if tools:
        cmd += ["--allowedTools", *tools]
    r = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True,
                       cwd=str(cwd), timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"claude 退出码 {r.returncode}: {r.stderr[:300]}")
    try:
        answer = json.loads(r.stdout).get("result", "")
    except json.JSONDecodeError:
        answer = r.stdout
    m = re.search(r"\{.*\}", answer, re.S)       # 取回答里的 JSON 对象
    if not m:
        raise RuntimeError(f"模型没有返回 JSON: {answer[:200]}")
    return json.loads(m.group(0))


# ---------- 4. 校验和组装 ----------
def one_line(value, limit):
    return re.sub(r"\s+", " ", CONTROL.sub("", str(value or ""))).replace("|", "/").strip()[:limit]


def validate(bug, total_lines, raw):
    """模型只负责语义字段。这里逐项校验，不合格就整条拒收。raw 是会话原文。"""
    out = {"action": bug.get("action"), "target": str(bug.get("target") or ""),
           "slug": str(bug.get("slug") or ""), "title": one_line(bug.get("title"), 120)}
    if out["action"] not in ("create", "update"):
        raise ValueError(f"未知的 action: {out['action']!r}")
    if not out["title"]:
        raise ValueError("缺少 title")
    for key, label in SECTIONS:
        out[key] = CONTROL.sub("", str(bug.get(key) or "")).strip()
        if not out[key]:
            raise ValueError(f"缺少「{label}」")
    kws = bug.get("keywords") if isinstance(bug.get("keywords"), list) else []
    out["keywords"] = [k for k in (one_line(k, 60) for k in kws) if k][:12]
    if not out["keywords"]:
        raise ValueError("缺少 keywords")
    out["confidence"] = "high" if bug.get("confidence") == "high" else "low"
    out["spans"] = []
    for ev in bug.get("evidence") if isinstance(bug.get("evidence"), list) else []:
        m = LINE_REF.match(str(ev).strip())
        if m:
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            if 1 <= a <= b <= total_lines:           # 行号超出文件范围的证据直接丢掉
                out["spans"].append((a, b))
    for key in ("found_at", "fixed_at"):
        value = str(bug.get(key) or "").strip()
        # commit 和版本号是事实：必须在会话原文里逐字出现过，否则留空
        out[key] = value if COMMITISH.match(value) and value in raw else ""
    return out


def seen_range(spans, dates, start, fallback):
    """这个 bug 出现的日期范围。优先取证据所在行的消息日期，其次取新增部分的，
    都没有时退回会话文件的修改日期。"""
    hit = sorted(d for no, d in dates.items() if any(a <= no <= b for a, b in spans))
    if not hit:
        hit = sorted(d for no, d in dates.items() if no > start)
    return (hit[0], hit[-1]) if hit else (fallback, fallback)


def resolve(bug, pend, live, day):
    """决定文件名，并找出要更新的旧笔记。返回 (文件名, 旧笔记的头部, 旧笔记全文的指纹)。
    新建时后两项为 None。待审批的版本优先于已生效的；接着待审批的版本改时指纹也为 None，
    因为它的头部里已经记着最初所基于的那一版。"""
    slug = bug["slug"]
    if bug["action"] == "update" and FILENAME.match(bug["target"]):
        for folder in (pend, live):
            if (folder / bug["target"]).exists():
                text = (folder / bug["target"]).read_text(encoding="utf-8")
                base = text_hash(text) if folder == live else None
                return bug["target"], parse_note(text)[0], base
        slug = bug["target"][11:-3]                  # 要更新的笔记不存在，当作新建
    if not SLUG.match(slug) or len(slug) > 50:
        raise ValueError(f"非法的 slug: {slug!r}")
    name, i = f"{day}-{slug}.md", 2
    while (pend / name).exists() or (live / name).exists():
        name, i = f"{day}-{slug}-{i}.md", i + 1
    return name, None, None


def build_note(bug, old, base, item, first, last, head):
    """语义字段来自模型，事实字段全部由代码决定。更新时保留旧笔记的 id、首次日期、来源和证据。"""
    old = old or {}
    rel, lines = item["rel"], item["lines"]
    evidence = [f"{rel}#L{a}" + (f"-L{b}" if b != a else "") + f"@{span_hash(lines, a, b)}"
                for a, b in bug["spans"]]            # @ 后面是所引原文的指纹
    meta = dict(old)
    meta.update({
        "schema_version": 1, "id": old.get("id") or new_id(),
        "type": "bug", "title": bug["title"], "project": item["project"],
        "status": old.get("status") or "current",
        "first_seen": min(str(old.get("first_seen") or first), first),
        "last_seen": max(str(old.get("last_seen") or last), last),
        "sources": merged(old.get("sources"), [rel]),
        "derived_from": old.get("derived_from") or [],
        "evidence": merged(old.get("evidence"), evidence),
        "session_branch": one_line(item["branch"], 100),   # 会话当时所在的分支，会话里没有就留空
        "distilled_at": head,                # 提炼时 M4 Pro 上仓库的 commit，有未提交的改动时带 -dirty
        "found_at": bug["found_at"] or old.get("found_at") or "",
        "fixed_at": bug["fixed_at"] or old.get("fixed_at") or "",
        "keywords": bug["keywords"], "confidence": bug["confidence"],
        "supersedes": old.get("supersedes") or [],
    })
    if base:
        meta["base_hash"] = base                     # 这次更新基于现有笔记的哪一版，批准时核对
    body = "\n\n".join(f"## {label}\n{bug[key]}" for key, label in SECTIONS)
    return render_note(meta, body)


def distill(item, prompt, n):
    """强模型提炼一段会话。合格的笔记放进待审批区，然后记入台账。"""
    project, repo, rel, lines = item["project"], item["repo"], item["rel"], item["lines"]
    pend, live = PENDING / project, item["live"]
    pend.mkdir(parents=True, exist_ok=True)
    header = f"项目: {project}\n已生效目录: {live}\n待审批目录: {pend}\n"
    if item["redo"]:
        header += "需要重新提炼的问题（上一次写得不好）:\n" + "".join(f"- {t}\n" for t in item["redo"])
    if item["handled"]:
        header += "已经处理过的问题（不要再提）:\n" + "".join(f"- {t}\n" for t in item["handled"])
    # 在项目目录里只读运行，另外能读待审批目录，用来查重。留在本地的笔记不在项目目录里，也要放行
    extra = [pend]
    if item["local"]:
        live.mkdir(parents=True, exist_ok=True)
        extra.append(live)
    result = ask_claude(prompt, header + "\n" + item["text"], repo, DISTILL_MODEL,
                        tools=["Read", "Glob", "Grep"], add_dirs=extra)
    head = git(repo, "rev-parse", "--short", "HEAD") or "unknown"
    if head != "unknown" and git(repo, "status", "--porcelain", "--untracked-files=no"):
        head += "-dirty"                         # 模型核实时看到的代码比这个 commit 多了未提交的改动
    raw, titles = "\n".join(lines), {}
    for raw_bug in result.get("bugs", []):
        if not isinstance(raw_bug, dict) or raw_bug.get("action") == "skip":
            continue
        try:
            bug = validate(raw_bug, len(lines), raw)
            first, last = seen_range(bug["spans"], item["dates"], item["start"], item["day"])
            name, old, base = resolve(bug, pend, live, first)
            write_atomic(pend / name, build_note(bug, old, base, item, first, last, head))
            titles[name] = bug["title"]
            log(f"PENDING {project} {'更新' if old else '新建'} {name}")
        except ValueError as e:                   # 一条不合格不影响其他条
            n["errors"] += 1
            log(f"ERROR {rel}: {e}")
    n["pending"] += len(titles)
    mark(rel, lines, item["size"], f"pending={len(titles)}", item["start"], project, titles)


# ---------- 5. 主流程 ----------
def load_since():
    """起始时间只在首次运行时生成一次。之后不管停机多久，这个时间之后的会话都不会漏。"""
    if not SINCE.exists():
        SINCE.parent.mkdir(parents=True, exist_ok=True)
        SINCE.write_text(str(time.time() - FIRST_RUN_DAYS * 86400))
    return float(SINCE.read_text().strip())


def main():
    sync()
    registry = load_registry()
    since = load_since()
    done = load_done()
    triage_prompt = (PROMPTS / "triage.md").read_text(encoding="utf-8")
    distill_prompt = (PROMPTS / "distill-bug.md").read_text(encoding="utf-8")
    n = {"seen": 0, "waiting": 0, "unregistered": 0, "filtered": 0, "triaged_out": 0,
         "pending": 0, "audit": 0, "audit_hit": 0, "bg_cut": 0, "errors": 0}
    filtered, unregistered = [], set()

    for path in sorted(RAW.glob("*/claude/**/*.jsonl")):
        st = path.stat()
        rel = str(path.relative_to(MEMORY))
        rec = done.get(rel, {})
        if st.st_mtime < since or st.st_size == rec.get("size", -1):
            continue
        n["seen"] += 1
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = rec.get("lines", 0)
            if start and rec.get("hash") and digest(lines, start) != rec["hash"]:
                log(f"WARN {rel} 已处理过的部分被改写了，整段重新处理")
                start = 0
            cwd, background, new, stats, dates = clean_session(lines, start)
            if stats["own"]:
                mark(rel, lines, st.st_size, "own")
                continue
            entry = match_project(cwd, registry)
            if not entry:                         # 不记台账：补登记项目之后，下一晚自动处理
                n["unregistered"] += 1
                unregistered.add(cwd or "（会话里没有工作目录）")
                continue
            if not stats["new_blocks"]:
                mark(rel, lines, st.st_size, "empty", start)
                continue
            retry = rec.get("outcome") == "retry"   # 我在审批时要求重新提炼的会话
            item = {"rel": rel, "lines": lines, "size": st.st_size, "start": start,
                    "text": assemble(background, new), "dates": dates,
                    "day": str(dt.date.fromtimestamp(st.st_mtime)), "branch": stats["branch"],
                    "project": entry["name"], "repo": entry["repo"], "live": bugs_dir(entry),
                    "local": entry["local"], "redo": (rec.get("redo") or []) if retry else [],
                    "handled": (rec.get("handled") or []) if retry else []}
            if stats["bg_cut"]:
                n["bg_cut"] += 1
                log(f"INFO {rel} 背景超过 {CONTEXT_CHARS} 字，只保留了开头和结尾")
            if retry:                             # 值不值得记我已经判断过了，跳过两道粗筛
                distill(item, distill_prompt, n)
                continue
            # 规则粗筛：整段会话太短，或者从头到尾没有任何报错信号
            if stats["user_turns"] < MIN_USER_TURNS or stats["signals"] == 0:
                if time.time() - st.st_mtime < SETTLE_HOURS * 3600:
                    n["waiting"] += 1             # 会话可能还没结束，不记台账，明晚再看
                else:
                    filtered.append(item)         # 循环结束后抽查，再记台账
                continue
            # 小模型粗筛
            verdict = ask_claude(triage_prompt, item["text"], MEMORY, TRIAGE_MODEL)
            if not verdict.get("has_bug"):
                n["triaged_out"] += 1
                mark(rel, lines, st.st_size, "triaged_out", start)
                continue
            distill(item, distill_prompt, n)
        except Exception as e:                    # 单个会话失败不影响其他会话，明晚重试
            n["errors"] += 1
            log(f"ERROR {rel}: {e}")

    # 抽查：规则过滤掉的会话里随机挑几段交给小模型，看规则有没有漏掉真 bug
    for item in random.sample(filtered, min(AUDIT_SAMPLE, len(filtered))):
        try:
            verdict = ask_claude(triage_prompt, item["text"], MEMORY, TRIAGE_MODEL)
            n["audit"] += 1
            if verdict.get("has_bug"):
                n["audit_hit"] += 1
                log(f"AUDIT 规则过滤掉了一段小模型认为有 bug 的会话：{item['rel']}")
                distill(item, distill_prompt, n)  # 既然发现了，就照常提炼
                item["done"] = True
        except Exception as e:
            n["errors"] += 1
            log(f"ERROR 抽查 {item['rel']}: {e}")
    for item in filtered:
        if not item.get("done"):
            n["filtered"] += 1
            mark(item["rel"], item["lines"], item["size"], "filtered", item["start"])

    for cwd in sorted(unregistered):
        log(f"SKIP 未登记的目录 {cwd}")
    waiting = len(list(PENDING.glob("*/*.md")))
    log("SUMMARY " + " ".join(f"{k}={v}" for k, v in n.items()) + f" queue={waiting}")
    if n["errors"]:
        notify(f"有 {n['errors']} 处处理失败，见 nightly.log")
    elif waiting:
        notify(f"今晚新增 {n['pending']} 条，共 {waiting} 条 bug 笔记等你审批：review.py list")
    else:
        recent = [ln for ln in LOG.read_text(encoding="utf-8").splitlines() if " SUMMARY " in ln][-3:]
        if len(recent) == 3 and all(" seen=0 " in ln for ln in recent):
            notify("连续 3 次没有扫到任何新会话，检查同步是否正常")


if __name__ == "__main__":
    try:
        with lock(wait=True):                     # 审批命令正在写文件时，等它写完
            main()
    except Exception as e:
        log(f"FATAL {e}")
        notify(f"每晚任务整体失败：{e}")
        sys.exit(1)
