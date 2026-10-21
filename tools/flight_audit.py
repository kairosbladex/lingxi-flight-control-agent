#!/usr/bin/env python3
"""本机/远端只读有界读取10086飞控服务审计；不访问平台或设备。"""
import argparse
import json
import subprocess
import sys
import time
from lingxi_flight_control.audit import digest

UNIT = 'lingxi-flight-browser-10086.service'
FIELDS = {'event','audit_version','time','code_sha','operation','status','result','error_code',
    'task_ref','drone_ref','session_ref','input_ref','request_id','command_id','trace_id',
    'action','parameters','device_effect','platform_ui_distance_mode','elapsed_ms','phase',
    'request_ref','endpoint','http_status','java_code'}


def select(lines, task_ref=None, after=0):
    events=[]
    for line in lines:
        try:
            row=json.loads(line)
        except ValueError:
            continue
        if not isinstance(row,dict) or row.get('event') not in {'flight_gateway','flight_java'}:
            continue
        if not isinstance(row.get('time'),(int,float)) or row['time'] < after:
            continue
        if task_ref is not None and row.get('task_ref') != task_ref:
            continue
        events.append({key:value for key,value in row.items() if key in FIELDS})
    return events


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--minutes',type=int,default=10)
    parser.add_argument('--limit',type=int,default=500)
    parser.add_argument('--task-id',help='用户本轮手动创建的任务编号；按摘要过滤')
    parser.add_argument('--after',type=float,default=0,help='增量起始Unix时间，包含边界以免丢同秒事件')
    args=parser.parse_args()
    if not 1 <= args.minutes <= 180 or not 1 <= args.limit <= 5000 or args.after < 0:
        parser.error('minutes范围1–180，limit范围1–5000，after非负')
    started=time.time()
    result=subprocess.run(['journalctl','--user','-u',UNIT,'--since',f'{args.minutes} minutes ago',
        '--until',f'@{int(started)}','-n',str(args.limit),'-o','cat','--no-pager'],
        capture_output=True,text=True,timeout=15)
    if result.returncode:
        print(json.dumps({'error':'AUDIT_READ_UNAVAILABLE'}))
        return 1
    lines=result.stdout.splitlines()
    events=select(lines,digest(args.task_id),args.after)
    print(json.dumps({'events':events,'next_after':max([args.after]+[e['time'] for e in events]),
        'possibly_truncated':len(lines)>=args.limit,
        'device_completion':'not_evaluated','read_only':True},ensure_ascii=False))
    return 0


if __name__=='__main__':
    sys.exit(main())
