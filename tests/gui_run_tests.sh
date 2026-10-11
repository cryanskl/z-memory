#!/bin/bash
# 界面测试：造数据 -> 起两个服务 -> 浏览器里逐项检查。需要 Python 的 playwright 包和 Chromium。
#   8791 有数据，从 memory/scripts 启动；8792 家目录全空，从一个「下载目录」启动
ROOT=$(cd "$(dirname "$0")/.." && pwd); V=$ROOT/tests; S=$ROOT/scripts; export T=$V/.work
PIDS=$V/.gui.pids
stop(){ [ -f $PIDS ] && kill $(cat $PIDS) 2>/dev/null; rm -f $PIDS; }
stop; sleep 0.3
bash $V/gui_fixture.sh > /dev/null 2>&1
HOME=$T/home MEMORY_DIR=$T/mem PATH=$V/bin:$PATH python3 $T/mem/scripts/gui.py --port 8791 --no-open > $T/gui.out 2>&1 &
echo $! >> $PIDS
mkdir -p $T/fresh $T/dl/prompts
cp $S/memlib.py $S/nightly.py $S/review.py $S/gui.py $S/gui.html $T/dl/; cp $S/prompts/*.md $T/dl/prompts/
env -u MEMORY_DIR HOME=$T/fresh PATH=$V/bin:$PATH python3 $T/dl/gui.py --port 8792 --no-open > $T/gui2.out 2>&1 &
echo $! >> $PIDS
sleep 1.5
python3 $V/gui_test.py; A=$?
python3 $V/gui_test_more.py; B=$?
[ "$KEEP" = 1 ] || stop
exit $((A + B))
