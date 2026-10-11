#!/bin/bash
# 全流程测试：假的 claude / osascript，独立的 HOME 和 MEMORY_DIR。在仓库里任何位置运行都可以。
ROOT=$(cd "$(dirname "$0")/.." && pwd); V=$ROOT/tests; S=$ROOT/scripts; export T=$V/.work
rm -rf $T; mkdir -p $T/home/.claude/projects $T/mem/scripts/prompts $T/mem/projects $T/other
cp $S/memlib.py $S/nightly.py $S/review.py $T/mem/scripts/; cp $S/prompts/*.md $T/mem/scripts/prompts/
export HOME=$T/home MEMORY_DIR=$T/mem PATH=$V/bin:$PATH
git init -q -b main $T/repo && echo a > $T/repo/a.txt && git -C $T/repo add a.txt && git -C $T/repo -c user.name=t -c user.email=t@t commit -q -m init
git init -q -b main $T/shared && git -C $T/shared -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
printf '| 项目 | 说明 | 类别 | 状态 | M4 Pro 路径 | M2 Max 路径 | 笔记位置 |\n| --- | --- | --- | --- | --- | --- | --- |\n| example-api | 测试 | 工作 | active | %s | ~/code/example-api | 仓库 |\n| shared-app | 共用 | 工作 | active | %s | ~/work/shared-app | 本地 |\n' $T/repo $T/shared > $T/mem/projects/INDEX.md
mkdir -p $T/mem/_state; echo 0 > $T/mem/_state/since      # 测试会话的日期是几天前，起始时间设到最早
PASS=0; FAIL=0
ok(){ if eval "$2" >/dev/null 2>&1; then PASS=$((PASS+1)); echo "  ok   $1"; else FAIL=$((FAIL+1)); echo "  FAIL $1   [$2]"; fi; }
run(){ python3 $T/mem/scripts/nightly.py; SUM=$(grep SUMMARY $T/mem/_state/nightly.log | tail -1 | cut -d' ' -f4-); echo "       $SUM"; }
rv(){ python3 $T/mem/scripts/review.py "$@"; }
mk(){ python3 $V/mk.py "$@"; }
has(){ [[ " $SUM " == *" $1 "* ]]; }
last(){ tail -1 $T/calls.log | python3 -c "import sys,json;d=json.loads(sys.stdin.read());print(d['$1'])"; }
P=$T/mem/inbox/pending-bugs/example-api; L=$T/repo/docs/bugs; LED=$T/mem/_state/processed.jsonl
bug(){ python3 -c "
import json,sys
b={'action':'create','slug':'pool-exhausted','title':'批量导入时请求挂起','keywords':['连接池','pool timeout'],'symptom':'接口无响应，日志出现 pool timeout。','root_cause':'循环内新建连接未释放。','fix':'循环外获取连接。','prevention':'循环里访问数据库前确认连接在循环外获取。','confidence':'high','evidence':['L3','L7-L8','L99']}
b.update(json.loads(sys.argv[1])); print(json.dumps({'bugs':[b]},ensure_ascii=False))" "$1" > $T/scen; }

echo "T1 跨天的 bug：第一晚只有症状，第二晚才有结论"
mk day1; echo false > $T/tri; echo '{"bugs":[]}' > $T/scen; run
ok "第一晚小模型判无，记台账" 'has triaged_out=1'
ok "调用带哨兵" '[ "$(last sentinel)" = True ]'
mk day2; echo true > $T/tri; bug '{}'; run
ok "第二晚短短两行也到了强模型" 'has pending=1'
ok "输入里有背景和新增两部分" 'last stdin | grep -q "背景：" && last stdin | grep -q "新增：本次"'
ok "背景里有第一天的症状" 'last stdin | grep -q "pool timeout"'
ok "thinking 被丢弃" '! last stdin | grep -q "secret thoughts"'
N=$P/2026-10-05-pool-exhausted.md
ok "文件名和 first_seen 取症状那天" 'grep -q "^first_seen: 2026-10-05" $N'
ok "last_seen 取结论那天" 'grep -q "^last_seen: 2026-10-07" $N'
ok "证据带指纹，越界的 L99 被丢掉" '[ $(grep -c "jsonl#L.*@[0-9a-f]\{8\}\"" $N) = 2 ]'
ok "状态是 current" 'grep -q "^status: current" $N'
ok "有 id 和提炼时的 commit，会话里没有分支就留空" 'grep -q "^id: bug-[0-9a-f]\{12\}$" $N && grep -q "^distilled_at: [0-9a-f]\{7\}$" $N && grep -q "^session_branch:$" $N'
ok "新建的笔记没有 base_hash" '! grep -q base_hash $N'
ok "verify 通过" 'rv verify | grep -q "核对了 2 条证据和 0 条引用，0 条有问题"'
ok "再跑一次不重复处理" 'run; has seen=0'

echo "T2 未登记的目录不记台账，补登记后自动处理"
mk other; run
ok "计入 unregistered" 'has unregistered=1'
ok "台账里没有它" '! grep -q oth.jsonl $LED'
echo "| other | x | 个人 | active | $T/other | 无 |" >> $T/mem/projects/INDEX.md; echo '{"bugs":[]}' > $T/scen; run
ok "补登记后处理了" 'grep -q oth.jsonl $LED && has unregistered=0'

echo "T3 重试、拒绝"
ok "list 能看到" 'rv list | grep -q "新建，把握：high"'
rv retry example-api 2026-10-05-pool-exhausted.md
ok "retry 删掉候选" '[ ! -e $N ]'
bug '{}'; run
ok "下一晚重新生成" '[ -e $N ] && has pending=1'
rv reject example-api --all; run
ok "reject 之后不再生成" '[ ! -e $N ] && has seen=0'

echo "T4 规则过滤 + 抽查"
mk filt; echo false > $T/tri; run
ok "两段都被规则过滤，都抽查了，没有命中" 'has filtered=2 && has audit=2 && has audit_hit=0'
mk filt3; echo true > $T/tri; bug '{"slug":"audit-found","evidence":["L1"]}'; run
ok "抽查命中后照常提炼" 'has audit_hit=1 && has pending=1 && has filtered=0'
ok "日志里有 AUDIT" 'grep -q "AUDIT 规则过滤掉了" $T/mem/_state/nightly.log'
ok "没有时间戳时退回文件日期" 'ls $P | grep -q "audit-found"'
rv reject example-api --all >/dev/null

echo "T5 会话还在进行：不记台账"
mk recent; run
ok "计入 waiting" 'has waiting=1 && ! grep -q rec.jsonl $LED'
rm $T/home/.claude/projects/-repo/rec.jsonl $T/mem/raw/sessions/m4pro/claude/-repo/rec.jsonl

echo "T6 自己发起的会话、元信息、压缩摘要、被拒绝的工具调用"
mk own1; mk own2; mk meta; mk reject; echo false > $T/tri; run
ok "哨兵在开头和在中间都能识别" '[ $(grep -c "\"outcome\": \"own\"" $LED) = 2 ]'
ok "isMeta 不算发言，摘要不算发言和信号 -> 用户只有 2 轮，被规则过滤" 'grep meta.jsonl $LED | grep -q filtered'
ok "用户拒绝工具调用不算报错信号…但用户说了 error 仍算" 'grep rj.jsonl $LED | grep -q triaged_out'

echo "T7 分支：会话在 feature/x 上，批准时仓库在 main"
mk branch; echo true > $T/tri; bug '{"slug":"on-branch","evidence":["L1-L2"]}'; run
B=$P/2026-10-07-on-branch.md
ok "session_branch 取会话里的分支" 'grep -q "^session_branch: feature/x" $B'
OUT=$(rv approve example-api 2026-10-07-on-branch.md)
ok "批准时提示分支不同" 'echo "$OUT" | grep -q "来自分支 feature/x 上的会话，仓库现在在 main"'
ok "写入 docs/bugs 并建索引" '[ -e $L/2026-10-07-on-branch.md ] && grep -q "on-branch.md" $L/INDEX.md'

echo "T8 更新已生效的笔记 + 只退回新会话"
python3 - << PY
import json,os
T=os.environ["T"]; p=T+"/home/.claude/projects/-repo/upd.jsonl"
recs=[{"type":"user","cwd":T+"/repo","timestamp":"2026-10-09T02:00:00Z","message":{"content":"分支那个错又出现了 error"}},
      {"type":"user","timestamp":"2026-10-09T02:01:00Z","message":{"content":[{"type":"tool_result","content":"failed again","is_error":True}]}},
      {"type":"user","timestamp":"2026-10-09T02:02:00Z","message":{"content":"原来还有第二个触发条件"}},
      {"type":"user","timestamp":"2026-10-09T02:03:00Z","message":{"content":"好"}}]
open(p,"w").write("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in recs)); t=__import__("time").time()-2*86400; os.utime(p,(t,t))
PY
bug '{"action":"update","target":"2026-10-07-on-branch.md","title":"分支上的错（两个触发条件）","evidence":["L2-L3"]}'; run
U=$P/2026-10-07-on-branch.md
ok "更新进待审批区，文件名不变" '[ -e $U ]'
ok "更新保留 id，并记下所基于的版本" '[ "$(grep "^id:" $U)" = "$(grep "^id:" $L/2026-10-07-on-branch.md)" ] && grep -q "^base_hash: [0-9a-f]\{12\}$" $U'
ok "first_seen 保留，last_seen 前进" 'grep -q "^first_seen: 2026-10-07" $U && grep -q "^last_seen: 2026-10-09" $U'
ok "来源和证据累加" '[ $(grep -c "jsonl\"$" $U) = 2 ] && [ $(grep -c "jsonl#L" $U) = 2 ]'
ok "list 标为更新，show 给出差异" 'rv list | grep -q "更新，把握" && rv show example-api 2026-10-07-on-branch.md | grep -q "^+title"'
rv retry example-api 2026-10-07-on-branch.md
ok "retry 只退回新会话" '[ $(grep -c "\"outcome\": \"retry\"" $LED) = 2 ] && tail -1 $LED | grep -q upd.jsonl'
run; ok "下一晚重新提炼这次更新" '[ -e $U ] && has pending=1'

echo "T9 密钥扫描、表格字符、锁"
bug '{"slug":"leaky","title":"带|竖线\u0007和\n换行的标题","symptom":"连接串 postgres://app:hunter2secret@db/x 报错","keywords":["a|b","k2"],"evidence":["L1"]}'
mk nots; run
S=$(ls $P | grep leaky)
ok "list 标出疑似含密钥" 'rv list | grep -q "疑似含密钥"'
ok "approve 拦下含密钥的那条" 'rv approve example-api --all | grep -q "未写入 $S" && [ -e $P/$S ]'
ok "其余的照常写入" '[ ! -e $U ]'
ok "--force 放行" 'rv approve example-api $S --force | grep -q "已写入"'
ok "索引表格没有被竖线和换行撑坏" '[ $(grep -c "leaky" $L/INDEX.md) = 1 ] && grep leaky $L/INDEX.md | awk -F"|" "{exit NF!=6}"'
ok "索引里的更新生效了" 'grep -q "分支上的错（两个触发条件）" $L/INDEX.md'
ok "正式笔记里没有 base_hash" '! grep -rq base_hash $L'
python3 -c "
import fcntl,time,sys
f=open('$T/mem/_state/memory.lock','w'); fcntl.flock(f,fcntl.LOCK_EX); time.sleep(3)" &
sleep 0.5
ok "另一个脚本持锁时，审批命令直接退出并说明" 'rv reindex 2>&1 | grep -q "另一个记忆脚本正在运行"'
wait
ok "锁释放后正常" 'rv reindex | grep -q "example-api: 2 条"'
ok "没有留下临时文件" '[ -z "$(find $T -name "*.tmp")" ]'

echo "T10 会话内容被改写：保留旧版本、证据失效能查出来"
mk rewrite; echo true > $T/tri; echo '{"bugs":[]}' > $T/scen; run
ok "日志里有改写告警" 'grep -q "会话内容被改写了，旧版本保留在 raw/revisions" $T/mem/_state/nightly.log && grep -q "已处理过的部分被改写了" $T/mem/_state/nightly.log'
ok "旧版本只留了被改写的那一个" '[ $(find $T/mem/raw/revisions -type f | wc -l) = 1 ]'
ok "整段重新处理（没有背景）" '! last stdin | grep -q "背景："'
python3 - << PY
import os,re
T=os.environ["T"]; p=T+"/repo/docs/bugs/2026-10-07-on-branch.md"; s=open(p).read()
s=s.replace("evidence:\n", 'evidence:\n  - "raw/sessions/m4pro/claude/-repo/aaa.jsonl#L3@41e17ba3"\n',1); open(p,"w").write(s)
PY
ok "verify 报出失效的证据" 'rv verify | grep -q "原文已经变了" && rv verify | grep -q "1 条有问题"'

echo "T11 模型输出不合格"
python3 - << PY
import json,os
T=os.environ["T"]
good={'action':'create','slug':'fine','title':'t','keywords':['k'],'symptom':'s','root_cause':'r','fix':'f','prevention':'p','evidence':[]}
bad1=dict(good,slug='../../etc/passwd'); bad2=dict(good,slug='no-cause',root_cause=''); bad3=dict(good,action='delete')
extra=dict(good,slug='extra',project='evil',status='archived',first_seen='1999-01-01',sources=['x'])
open(T+"/scen","w").write(json.dumps({'bugs':[bad1,bad2,bad3,extra,{'action':'skip','target':'x.md'},'junk']},ensure_ascii=False))
p=T+"/home/.claude/projects/-repo/bad.jsonl"
recs=[{"type":"user","cwd":T+"/repo","message":{"content":"报错"}},{"type":"user","message":{"content":"二"}},{"type":"user","message":{"content":"三"}}]
open(p,"w").write("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in recs)); t=__import__("time").time()-2*86400; os.utime(p,(t,t))
PY
run
ok "三条不合格被拒收，其余照常" 'has errors=3 && has pending=1'
X=$(ls $P | grep extra)
ok "模型多给的事实字段被忽略" 'grep -q "^project: example-api" $P/$X && grep -q "^status: current" $P/$X && ! grep -q 1999 $P/$X'
ok "没有写到待审批区之外" '[ -z "$(find $T -name "passwd*")" ]'
ok "失败时发了通知" 'tail -1 $T/notify.log | grep -q "处理失败"'
ok "每晚脚本没有碰项目仓库" '[ -z "$(git -C $T/repo status --porcelain -uall | grep -v "docs/bugs/")" ]'

echo "T12 共用仓库：笔记只留在本地；M2 上的路径不依赖用户名"
LP=$T/mem/inbox/pending-bugs/shared-app; LL=$T/mem/projects/shared-app/bugs
mk local; echo true > $T/tri; bug '{"slug":"shared-bug","title":"共用仓库的错","evidence":["L1-L2"],"found_at":"deadbeef","fixed_at":"v2.3.1"}'; run
SB=$LP/2026-10-05-shared-bug.md
ok "别的用户名下的 ~/work/shared-app 也能对上项目" '[ -e $SB ]'
ok "模型能读到本地的已生效目录" 'last args | grep -q "mem/projects/shared-app/bugs" && last stdin | grep -q "^已生效目录: $LL$"'
ok "会话原文里出现过的版本号保留，没出现过的 commit 清空" 'grep -q "^fixed_at: v2.3.1$" $SB && grep -q "^found_at:$" $SB'
ok "supersedes 是数组" 'grep -q "^supersedes: \[\]$" $SB'
OUT=$(rv approve shared-app --all)
ok "批准后写进 memory，不进项目仓库" '[ -e $LL/2026-10-05-shared-bug.md ] && [ -e $LL/INDEX.md ] && [ -z "$(git -C $T/shared status --porcelain)" ]'
ok "说明笔记只留在本地，不提示分支" 'echo "$OUT" | grep -q "只留在本地" && ! echo "$OUT" | grep -q "提示："'
ok "verify 和 reindex 都认本地目录" 'rv reindex | grep -q "shared-app: 1 条" && rv verify | grep -q "核对了"'

echo "T13 待审批的更新碰上现有笔记被改"
mk local2; bug '{"action":"update","target":"2026-10-05-shared-bug.md","title":"共用仓库的错（补充）","evidence":["L1"]}'; run
SU=$LP/2026-10-05-shared-bug.md
ok "更新记下了 base_hash" 'grep -q "^base_hash:" $SU'
echo "我手工加的一行" >> $LL/2026-10-05-shared-bug.md
ok "list 标出现有笔记已变" 'rv list | grep -q "现有笔记已变"'
ok "show 给出警告" 'rv show shared-app 2026-10-05-shared-bug.md | grep -q "直接批准会盖掉那些改动"'
ok "approve 不写入" 'rv approve shared-app --all | grep -q "在提炼之后被改过" && [ -e $SU ] && grep -q "我手工加的一行" $LL/2026-10-05-shared-bug.md'
ok "--overwrite 才写入，且不带 base_hash" 'rv approve shared-app 2026-10-05-shared-bug.md --overwrite | grep -q "已写入" && ! grep -q base_hash $LL/2026-10-05-shared-bug.md && grep -q "补充" $LL/INDEX.md'
mk local3; bug '{"action":"update","target":"2026-10-05-shared-bug.md","title":"共用仓库的错（第三版）","evidence":["L1"]}'; run
python3 - << PY
import os,sys
sys.path.insert(0, os.environ["T"]+"/mem/scripts"); import memlib
T=os.environ["T"]; a=T+"/mem/inbox/pending-bugs/shared-app/2026-10-05-shared-bug.md"
open(T+"/mem/projects/shared-app/bugs/2026-10-05-shared-bug.md","w").write(memlib.without_base(open(a).read()))
PY
rm $LL/INDEX.md
ok "批准写到一半被打断：再跑一遍能补完" 'rv approve shared-app --all | grep -q "之前已经写入" && [ ! -e $SU ] && grep -q "第三版" $LL/INDEX.md'
rm $LL/INDEX.md
ok "没有待审批的笔记时再跑 approve 也会补建索引" 'rv approve shared-app --all | grep -q "索引已重建" && [ -e $LL/INDEX.md ]'

echo "T14 重试不误伤同一段会话的其他笔记"
python3 - << PY
import json,os
T=os.environ["T"]
b=lambda slug,title:{'action':'create','slug':slug,'title':title,'keywords':['k'],'symptom':'s','root_cause':'r','fix':'f','prevention':'p','evidence':['L1']}
open(T+"/scen","w").write(json.dumps({'bugs':[b('sib-a','甲问题'),b('sib-b','乙问题')]},ensure_ascii=False))
PY
mk sib; echo true > $T/tri; run
ok "一段会话出了两条" 'has pending=2'
rv reject example-api 2026-10-05-sib-a.md >/dev/null; rv retry example-api 2026-10-05-sib-b.md >/dev/null
ok "台账记下要重写的和已处理的" 'tail -1 $LED | grep -q "\"redo\": \[\"乙问题\"\], \"handled\": \[\"甲问题\"\]"'
TRI=$(grep -c '"kind": "triage"' $T/calls.log); echo false > $T/tri
python3 - << PY
import json,os
T=os.environ["T"]
open(T+"/scen","w").write(json.dumps({'bugs':[{'action':'create','slug':'sib-b','title':'乙问题（重写）','keywords':['k'],'symptom':'s','root_cause':'r','fix':'f','prevention':'p','evidence':['L1']}]},ensure_ascii=False))
PY
run
ok "重试的会话跳过粗筛，直接提炼" '[ $(grep -c "\"kind\": \"triage\"" $T/calls.log) = $TRI ] && has pending=1'
ok "模型被告知哪些已处理、哪条要重写" 'last stdin | grep -A1 "已经处理过的问题" | grep -q "甲问题" && last stdin | grep -A1 "需要重新提炼的问题" | grep -q "乙问题"'
ok "被拒绝的那条没有回来" '[ ! -e $P/2026-10-05-sib-a.md ] && [ -e $P/2026-10-05-sib-b.md ]'
rv reject example-api --all >/dev/null
python3 - << PY
import json,os
T=os.environ["T"]
b=lambda slug,title:{'action':'create','slug':slug,'title':title,'keywords':['k'],'symptom':'s','root_cause':'r','fix':'f','prevention':'p','evidence':['L1']}
open(T+"/scen","w").write(json.dumps({'bugs':[b('two-a','丙问题'),b('two-b','丁问题')]},ensure_ascii=False))
PY
mk sib2; echo true > $T/tri; run
rv retry example-api 2026-10-05-two-a.md >/dev/null; rv retry example-api 2026-10-05-two-b.md >/dev/null
ok "两条先后重试：都要重写，谁也不算已处理" 'tail -1 $LED | grep -q "\"redo\": \[\"丙问题\", \"丁问题\"\], \"handled\": \[\]"'
run; rv reject example-api --all >/dev/null

echo "T15 背景被截断时留下记录"
mk big; echo false > $T/tri; run; mk big2; run
ok "日志和汇总里都有" 'grep -q "INFO .*big.jsonl 背景超过 40000 字" $T/mem/_state/nightly.log && has bg_cut=1'

echo "T16 笔记默认只留在本地：注册表里没写「笔记位置」的项目"
python3 - << PY
import json,os,time
T=os.environ["T"]; d=T+"/home/.claude/projects/-other/"; os.makedirs(d,exist_ok=True)
recs=[{"type":"user","cwd":T+"/other","timestamp":"2026-10-06T02:00:00Z","message":{"content":"没写笔记位置的项目也报错了"}},
      {"type":"user","timestamp":"2026-10-06T02:01:00Z","message":{"content":[{"type":"tool_result","content":"failed","is_error":True}]}},
      {"type":"user","timestamp":"2026-10-06T02:02:00Z","message":{"content":"二"}},{"type":"user","timestamp":"2026-10-06T02:03:00Z","message":{"content":"三"}}]
p=d+"dflt.jsonl"; open(p,"w").write("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in recs)); t=time.time()-2*86400; os.utime(p,(t,t))
PY
echo true > $T/tri; bug '{"slug":"default-local","title":"默认本地","evidence":["L1-L2"]}'; run
OP=$T/mem/inbox/pending-bugs/other; OL=$T/mem/projects/other/bugs
OUT=$(rv approve other --all)
ok "没写的按本地处理，不进项目目录" '[ -e $OL/2026-10-06-default-local.md ] && [ ! -e $T/other/docs ] && echo "$OUT" | grep -q "只留在本地"'
ok "明确写了仓库的才进项目仓库，并提醒会随仓库提交" '[ -d $L ] && rv approve example-api --all | grep -q "会随仓库一起提交"'
PT=$(rv pointers)
ok "pointers 列出两个本地项目，不列写了仓库的" 'echo "$PT" | grep -q "projects/other/bugs/INDEX.md" && echo "$PT" | grep -q "projects/shared-app/bugs/INDEX.md" && ! echo "$PT" | grep -q example-api'
ok "pointers 里的路径把家目录写成 ~" 'HOME=$T rv pointers | grep -q "在 ~/other 下工作时" && HOME=$T rv pointers | grep -q "先读 ~/mem/projects/other/bugs/INDEX.md"'

echo "T17 提炼时仓库有未提交的改动"
echo b >> $T/repo/a.txt
python3 - << PY
import json,os,time
T=os.environ["T"]; p=T+"/home/.claude/projects/-repo/dirty.jsonl"
recs=[{"type":"user","cwd":T+"/repo","timestamp":"2026-10-06T03:00:00Z","message":{"content":"工作区有改动时出错了"}},
      {"type":"user","timestamp":"2026-10-06T03:01:00Z","message":{"content":[{"type":"tool_result","content":"failed","is_error":True}]}},
      {"type":"user","timestamp":"2026-10-06T03:02:00Z","message":{"content":"二"}},{"type":"user","timestamp":"2026-10-06T03:03:00Z","message":{"content":"三"}}]
open(p,"w").write("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in recs)); t=time.time()-2*86400; os.utime(p,(t,t))
PY
bug '{"slug":"dirty-tree","title":"工作区有改动","evidence":["L1"]}'; run
DN=$P/2026-10-06-dirty-tree.md
ok "distilled_at 带 -dirty" 'grep -q "^distilled_at: [0-9a-f]\{7\}-dirty$" $DN'
git -C $T/repo checkout -q a.txt

echo "T18 批准前检查手工改坏的头部"
cp $DN $T/keep.md
perl -pi -e 's/^id: .*/id: oops/; s/^project: .*/project: wrong/; s/^first_seen: .*/first_seen: 昨天/' $DN
OUT=$(rv approve example-api 2026-10-06-dirty-tree.md)
ok "改坏的不写入，并指出是哪几个字段" 'echo "$OUT" | grep -q "未写入" && echo "$OUT" | grep -q "id 缺失或格式不对" && echo "$OUT" | grep -q "project 应该是 example-api" && echo "$OUT" | grep -q "first_seen" && [ ! -e $L/2026-10-06-dirty-tree.md ]'
cp $T/keep.md $DN; perl -pi -e 's/^title: .*/title: "我手工改过的标题"/' $DN
ok "只改标题和正文的照常批准" 'rv approve example-api 2026-10-06-dirty-tree.md | grep -q "已写入" && grep -q "我手工改过的标题" $L/INDEX.md'
python3 - << PY
import os,re
T=os.environ["T"]; a=T+"/repo/docs/bugs/2026-10-06-dirty-tree.md"; b=T+"/mem/inbox/pending-bugs/example-api/2026-10-06-dirty-tree.md"
s=open(a).read(); os.makedirs(os.path.dirname(b),exist_ok=True)
open(b,"w").write(re.sub(r"^id: .*", "id: bug-aaaaaaaaaaaa", s, flags=re.M).replace("我手工改过的标题","又改了一次"))
PY
ok "更新时 id 和现有笔记不一致的不写入" 'rv approve example-api 2026-10-06-dirty-tree.md | grep -q "id 和现有笔记的不一致"'
rv reject example-api --all >/dev/null

echo "T19 按 id 查找，核对相互引用"
ID=$(grep "^id:" $L/2026-10-06-dirty-tree.md | cut -d" " -f2)
ok "find 给出位置和标题" 'rv find $ID | grep -q "docs/bugs/2026-10-06-dirty-tree.md" && rv find $ID | grep -q "我手工改过的标题"'
ok "找不到时说明并返回非零" '! rv find bug-000000000000 2>/dev/null && rv find bug-000000000000 2>&1 | grep -q "没有找到"'
mkdir -p $T/mem/bugs
sed "s/^id: .*/id: bug-bbbbbbbbbbbb/; s/^derived_from: .*/derived_from: [\"$ID\"]/; s/^project: .*/project: global/" $L/2026-10-06-dirty-tree.md > $T/mem/bugs/2026-10-06-general.md
BEFORE=$(rv verify | tail -1)
ok "引用存在的 id 不报错" 'rv verify | tail -1 | grep -q "和 1 条引用" && ! rv verify | grep -q "引用的笔记不存在"'
perl -pi -e 's/^supersedes: .*/supersedes: ["bug-000000000000", "2026-10-05-old.md"]/' $T/mem/bugs/2026-10-06-general.md
VF=$(rv verify)
ok "引用不存在的 id、写成文件名的都报出来" 'echo "$VF" | grep -q "引用的笔记不存在" && echo "$VF" | grep -q "引用要写笔记的 id" && echo "$VF" | tail -1 | grep -q "和 3 条引用"'
ok "全局层的笔记能按 id 找到" 'rv find bug-bbbbbbbbbbbb | grep -q "全局层"'
ok "newid 给出合格的新编号，每次不同" 'rv newid | grep -q "^bug-[0-9a-f]\{12\}$" && [ "$(rv newid)" != "$(rv newid)" ]'
echo; echo "通过 $PASS，失败 $FAIL"
[ $FAIL = 0 ]
