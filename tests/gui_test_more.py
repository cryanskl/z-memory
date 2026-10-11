"""界面的补充测试（由 gui_run_tests.sh 调用）：越界的请求、别的脚本持锁时、从一个空目录第一次启动。
8791 是有数据的服务，8792 是从下载目录启动、家目录全空的服务。"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

T = Path(os.environ["T"])
SHOTS = Path(os.environ.get("SHOTS", T.parent / ".shots"))
SHOTS.mkdir(parents=True, exist_ok=True)
A, B = "http://127.0.0.1:8791", "http://127.0.0.1:8792"
passed = failed = 0


def ok(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


def token(base):
    page = urllib.request.urlopen(base + "/").read().decode()
    return re.search(r'memory-token" content="([^"]+)"', page).group(1)


def call(base, path, body=None, tok=None, headers=None):
    """返回 (状态码, 回答)。"""
    req = urllib.request.Request(base + path, headers={"X-Memory-Token": tok or "", **(headers or {})})
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


print("越界的请求")
tok = token(A)
q = urllib.request.quote
ok("没有令牌读不到", call(A, "/api/pending")[0] == 403)
ok("令牌不对读不到", call(A, "/api/pending", tok="x" * 43)[0] == 403)
ok("别的网页发来的请求被拒绝", call(A, "/api/pending", tok=tok, headers={"Origin": "http://evil.example"})[0] == 403)
ok("换了域名指过来的请求被拒绝", call(A, "/api/pending", tok=tok, headers={"Host": "evil.example:8791"})[0] == 403)
ok("项目名不能往上跳", call(A, "/api/pending/item?project=..&file=2026-10-08-a.md", tok=tok)[0] == 400)
ok("文件名不能带路径", call(A, "/api/pending/item?project=example-api&file=" + q("../../../etc/passwd"), tok=tok)[0] == 400)
ok("会话路径不能跳出会话目录", call(A, "/api/clean?path=" + q("../../../../etc/passwd"), tok=tok)[0] == 404)
ok("证据路径不能跳出 raw/", call(A, "/api/evidence?entry=" + q("../../../etc/passwd#L1"), tok=tok)[0] == 404)
ok("证据不能指向 raw/ 之外的 memory 文件", call(A, "/api/evidence?entry=" + q("projects/INDEX.md#L1"), tok=tok)[0] == 404)
ok("笔记位置不能带路径", call(A, "/api/note?place=" + q("p:../x") + "&file=2026-10-08-a.md", tok=tok)[0] == 400)
ok("批量操作的项目名不能往上跳", call(A, "/api/pending/act", {"action": "approve", "project": "..", "all": True}, tok)[0] == 400)
ok("笔记位置只认三种写法", call(A, "/api/registry/save", {"rows": [{"name": "a", "status": "active", "location": "随便"}]}, tok)[0] == 400)
req = urllib.request.Request(A + "/api/cmd", data=b"name=reindex", headers={"X-Memory-Token": tok})
try:
    urllib.request.urlopen(req)
    code = 200
except urllib.error.HTTPError as e:
    code = e.code
ok("不是 JSON 的提交被拒绝", code == 415)
before = (T / "mem/projects/INDEX.md").read_text()
status, data = call(A, "/api/registry/save", {"rows": [{"name": "a|b\nc", "status": "active", "m4": "/x", "location": ""}]}, tok)
ok("项目名里的竖线和换行不会撑坏表格，带斜杠的名字被拒绝", status == 400 and (T / "mem/projects/INDEX.md").read_text() == before)

print("别的脚本持锁时")
holder = subprocess.Popen([sys.executable, "-c", f"""
import fcntl, time
f = open('{T}/mem/_state/memory.lock', 'w'); fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(4)"""],
                          stdout=subprocess.PIPE)
holder.stdout.readline()
status, data = call(A, "/api/cmd", {"name": "reindex"}, tok)
ok("重建索引说明稍后再试", status == 200 and not data["ok"] and "另一个记忆脚本正在运行" in data["output"])
status, data = call(A, "/api/prompt/save", {"name": "triage", "text": "不该写进去"}, tok)
ok("保存类的操作直接拒绝，不写文件", status == 409 and "不该写进去" not in (T / "mem/scripts/prompts/triage.md").read_text())
ok("总览标出有别的脚本在写", call(A, "/api/overview", tok=tok)[1]["busy"] is True)
holder.wait()
ok("锁放开后恢复正常", call(A, "/api/cmd", {"name": "reindex"}, tok)[1]["ok"] and call(A, "/api/overview", tok=tok)[1]["busy"] is False)

print("从空目录第一次启动")
mem, dl = T / "fresh/memory", T / "dl"
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1360, "height": 880})
    problems = []
    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    text = lambda sel: page.inner_text(sel)
    page.goto(B + "/")
    page.wait_for_selector("main h1")
    ok("memory 目录还不存在时总览照样打开", "没有待审批的笔记" in text("main h1") and "还没有启用的项目" in text("main"))
    ok("打开界面不会自己建目录", not mem.exists())
    page.click("#rail >> text=审批")
    page.wait_for_selector(".empty")
    page.click("#rail >> text=笔记")
    page.wait_for_selector(".empty")
    page.click("#rail >> text=运行")
    page.wait_for_selector("text=台账还是空的")
    ok("审批、笔记、运行三页都能打开", not mem.exists())
    page.click("#rail >> text=向导")
    page.wait_for_selector("text=补上缺的目录和文件")
    ok("向导停在第一步，说明界面是从哪启动的", "建仓库" in text(".reader h1") and str(dl) in text("main"))
    page.click("text=补上缺的目录和文件")
    page.wait_for_selector("#did .note")
    copied = all((mem / "scripts" / n).is_file() for n in ("memlib.py", "nightly.py", "review.py", "gui.py", "gui.html", "prompts/triage.md", "prompts/distill-bug.md"))
    ok("建了目录，复制了脚本和提示词", copied and (mem / "inbox/pending-bugs").is_dir())
    ok("写了 .gitignore、空注册表，执行了 git init", (mem / ".gitignore").is_file() and (mem / ".git").exists() and "| 项目 |" in (mem / "projects/INDEX.md").read_text())
    ok("六项检查全部变成好了", page.locator("table >> nth=0 >> .chip.bad").count() == 0)
    tracked = subprocess.run(["git", "-C", str(mem), "status", "--porcelain", "-uall"], capture_output=True, text=True).stdout
    ok("git 看不到 raw/ 和 _state/", "raw/" not in tracked and "_state/" not in tracked and "scripts/gui.py" in tracked)
    page.screenshot(path=str(SHOTS / "16-fresh-repo.png"))

    page.click(".steps .item:has-text('登记项目')")
    page.wait_for_selector("text=加一行")
    page.click("text=加一行")
    page.get_by_label("项目名").fill("my-proj")
    page.get_by_label("M4 Pro 路径").fill("~/code/my-proj")
    page.get_by_label("M2 Max 路径").fill("~/code/my-proj")
    page.click("text=保存注册表")
    page.wait_for_selector("td.path:has-text('projects/my-proj/bugs')")
    reg = (mem / "projects/INDEX.md").read_text()
    ok("第一个项目写进注册表，表头只有一份", "| my-proj |  |  | active | ~/code/my-proj | ~/code/my-proj |  |" in reg and reg.count("| 项目 |") == 1)
    ok("提醒这台电脑上找不到这个目录", "找不到这个目录" in text("main"))
    page.screenshot(path=str(SHOTS / "17-fresh-registry.png"))
    page.click(".steps .item:has-text('验证清洗')")
    page.wait_for_selector(".note.warn")
    ok("没有会话文件时说明去哪找", "没有找到会话文件" in text(".note.warn"))
    page.click(".steps .item:has-text('加指针')")
    page.wait_for_selector(".copy")
    ok("指针里是这个项目和 ~ 开头的路径", "在 ~/code/my-proj 下工作时" in text("main") and "~/memory/projects/my-proj/bugs/INDEX.md" in text("main") and "~/memory/bugs/INDEX.md" in text("main"))
    page.click(".steps .item:has-text('定时运行')")
    page.wait_for_selector("text=配置内容")
    ok("定时配置指向 memory 里的脚本，不带 MEMORY_DIR", str(mem / "scripts/nightly.py") in text("main") and "MEMORY_DIR" not in text("main"))
    ok("控制台没有报错", not problems)
    for p in problems[:10]:
        print("       ", p)
    browser.close()

print(f"\n通过 {passed}，失败 {failed}")
sys.exit(1 if failed else 0)
