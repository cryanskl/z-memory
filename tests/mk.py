import json, os, sys, time
T = os.environ["T"]
base = T + "/home/.claude/projects/"
REPO = T + "/repo"
D1, D2 = "2026-10-05T03:00:00.000Z", "2026-10-07T09:30:00.000Z"
def u(t, ts=None, cwd=None, br=None, **kw):
    r = {"type": "user", "cwd": cwd or REPO, "message": {"role": "user", "content": t}, **kw}
    if ts: r["timestamp"] = ts
    if br: r["gitBranch"] = br
    return r
def a(t, ts=None):
    r = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "secret thoughts"}, {"type": "text", "text": t},
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}}]}}
    if ts: r["timestamp"] = ts
    return r
def r(t, err=False, ts=None):
    x = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": t, "is_error": err}]}}
    if ts: x["timestamp"] = ts
    return x
def w(name, recs, age=0, mode="w", d="-repo/"):
    os.makedirs(base + d, exist_ok=True)
    p = base + d + name
    open(p, mode).write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in recs))
    if age:
        t = time.time() - age * 86400; os.utime(p, (t, t))
c = sys.argv[1]
day1 = [u("导入接口挂了", D1), a("看日志", D1), r("Traceback: pool timeout", ts=D1), u("继续", D1), a("还没找到原因", D1), u("明天再看", D1)]
fin = [a("定位到了，原因是循环内新建连接，已修复", D2), u("好", D2)]
if c == "day1": w("aaa.jsonl", day1, age=2)
if c == "day2": w("aaa.jsonl", fin, mode="a")
if c == "other": w("oth.jsonl", [u("挂了 error", D1, cwd=T + "/other"), r("failed", True, D1), u("x", D1), u("y", D1)], age=2, d="-other/")
if c == "filt":
    w("f1.jsonl", [u("hi", D1)], age=3); w("f2.jsonl", [u("做A", D1), a("好", D1), u("做B", D1), u("做C", D1), u("做D", D1)], age=3)
if c == "filt3": w("f3.jsonl", [u("做A"), a("好"), u("做B"), u("做C"), u("做D")], age=3)
if c == "nots": w("nts.jsonl", [u("出错了"), r("Error: z", True), u("a"), u("b"), a("修好了")], age=5)
if c == "rewrite": w("aaa.jsonl", [u("（被改写的开头）", D1)] + day1 + fin + [u("补一句", D2)])
if c == "branch": w("br.jsonl", [u("分支上出错了", D2, br="feature/x"), r("failed", True, D2), u("继续", D2, br="feature/x"), u("好", D2, br="feature/x")], age=2)
if c == "own1": w("own1.jsonl", [u("[memory-nightly]\n你在帮我筛选 error"), r("failed", True), u("a"), u("b")], age=2)
if c == "own2": w("own2.jsonl", [u("项目: x\n### 用户 [L1]\n报错\n[memory-nightly]\n你在帮我"), r("failed", True), u("a"), u("b")], age=2)
if c == "meta": w("meta.jsonl", [u("Caveat: error error 报错", D1, isMeta=True), u("This session is being continued… error", D1, isCompactSummary=True),
                                  u("真的出错了", D1), r("Traceback", True, D1), u("二", D1), a("修了", D1)], age=2)
if c == "recent": w("rec.jsonl", [u("刚开始，报错了")])
if c == "reject": w("rj.jsonl", [u("帮我删库 error", D1), r("The user doesn't want to proceed with this tool use.", True, D1), u("算了", D1), u("做别的", D1)], age=3)
if c == "local": w("loc.jsonl", [u("共用仓库里报错了", D1, cwd="/Users/someone-else/work/shared-app/src", br="dev"), r("failed", True, D1), u("二", D1), u("修在 v2.3.1 了", D1)], age=2, d="-shared/")
if c == "local2": w("loc2.jsonl", [u("共用仓库那个错又来了 error", D2, cwd="/Users/someone-else/work/shared-app", br="dev"), r("failed", True, D2), u("二", D2), u("三", D2)], age=2, d="-shared/")
if c == "local3": w("loc3.jsonl", [u("第三次 error", D2, cwd="/Users/someone-else/work/shared-app", br="dev"), r("failed", True, D2), u("二", D2), u("三", D2)], age=2, d="-shared/")
if c in ("sib", "sib2"): w(c + ".jsonl", [u("一段会话里两个 bug，都报错", D1), r("failed", True, D1), u("二", D1), u("三", D1)], age=2)
if c == "big": w("big.jsonl", [u("出错了 " + "长" * 4200, D1) for _ in range(15)] + [r("Traceback", True, D1)], age=2)
if c == "big2": w("big.jsonl", [u("后来补了一句", D2)], mode="a")
