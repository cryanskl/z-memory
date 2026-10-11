#!/bin/bash
# 给界面测试准备数据：先跑一遍全流程测试，再造几条不同情况的待审批笔记。
ROOT=$(cd "$(dirname "$0")/.." && pwd); V=$ROOT/tests; S=$ROOT/scripts; export T=$V/.work
bash $V/run_tests.sh > /dev/null || true
export HOME=$T/home MEMORY_DIR=$T/mem PATH=$V/bin:$PATH
cp $S/gui.py $S/gui.html $T/mem/scripts/
sess(){ python3 - "$1" "$2" "$3" << 'PY'
import json, os, sys, time
T = os.environ["T"]; name, cwd, first = sys.argv[1:4]
d = T + "/home/.claude/projects/" + ("-shared/" if "shared" in cwd else "-repo/"); os.makedirs(d, exist_ok=True)
recs = [{"type": "user", "cwd": cwd, "gitBranch": "main", "timestamp": "2026-10-08T02:00:00Z", "message": {"content": first}},
        {"type": "assistant", "timestamp": "2026-10-08T02:00:30Z", "message": {"content": [{"type": "text", "text": "我先看一下日志。"}, {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -x tests/"}}]}},
        {"type": "user", "timestamp": "2026-10-08T02:01:00Z", "message": {"content": [{"type": "tool_result", "content": "Traceback (most recent call last):\n  File \"app.py\", line 42\nTimeoutError: pool timeout", "is_error": True}]}},
        {"type": "user", "timestamp": "2026-10-08T02:02:00Z", "message": {"content": "继续查"}},
        {"type": "assistant", "timestamp": "2026-10-08T02:05:00Z", "message": {"content": [{"type": "text", "text": "原因找到了：循环里每次都新建连接。"}]}},
        {"type": "user", "timestamp": "2026-10-08T02:06:00Z", "message": {"content": "好，修吧"}}]
p = d + name + ".jsonl"; open(p, "w").write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
t = time.time() - 2 * 86400; os.utime(p, (t, t))
PY
}
scen(){ python3 -c "
import json,sys
b={'action':'create','keywords':['连接池','pool timeout','import_batch.py'],'symptom':'批量导入超过 200 条时接口无响应，日志出现 \`pool timeout\`。\n\n重试时也会触发。','root_cause':'循环内每次迭代都新建连接且未释放，连接池上限 20 很快耗尽。','fix':'改为在循环外获取一次连接：\n\n- 用上下文管理器保证释放\n- 批量大小改成 100','prevention':'凡是在循环里访问数据库的代码，先确认连接在循环外获取。','confidence':'high','evidence':['L1','L3','L5']}
b.update(json.loads(sys.argv[1])); print(json.dumps({'bugs':[b]},ensure_ascii=False))" "$1" > $T/scen; }
run(){ python3 $T/mem/scripts/nightly.py; }
echo true > $T/tri
sess g1 $T/repo "批量导入接口挂了，报错"; scen '{"slug":"gui-new","title":"批量导入时连接池耗尽导致请求挂起"}'; run
sess g2 $T/repo "部署脚本报错了"; scen '{"slug":"gui-secret","title":"部署脚本连不上数据库","symptom":"连接串 postgres://app:hunter2secret@db/x 报错","confidence":"low","evidence":["L3"]}'; run
sess g3 /Users/someone-else/work/shared-app "共用仓库那个错又出现了 error"; scen '{"action":"update","target":"2026-10-05-shared-bug.md","title":"共用仓库的错（界面测试的更新）","evidence":["L1-L3"]}'; run
echo "我在审批期间手工加的一行" >> $T/mem/projects/shared-app/bugs/2026-10-05-shared-bug.md
sess g4 $T/repo "还有一个头部会被改坏的 error"; scen '{"slug":"gui-broken","title":"头部被手工改坏的笔记"}'; run
perl -pi -e 's/^id: .*/id: oops/' $T/mem/inbox/pending-bugs/example-api/2026-10-08-gui-broken.md
sess g5 $T/repo "第五个问题 error"; scen '{"slug":"gui-extra","title":"用来测试重试和拒绝的笔记"}'; run
sess g6 $T/repo "第六个问题 error"; scen '{"slug":"gui-extra-two","title":"用来测试键盘操作的笔记"}'; run
ls $T/mem/inbox/pending-bugs/*/
tail -3 $T/mem/_state/nightly.log
