#!/usr/bin/env python3.12
"""
崩溃自动恢复看门狗 — 系统 crontab 每2分钟运行
不依赖 Hermes，机器活着就能自动重启
"""
import subprocess, sys, os, time
from pathlib import Path

WORKDIR = '/home/admin/new_strategy_live'
PID_FILE = '/home/admin/new_strategy_live/.live_pid'
RESTART_LOG = '/home/admin/new_strategy_live/.restart_log'
STATUS_FILE = '/home/admin/new_strategy_live/.watchdog_status'

def get_pid_from_file():
    if os.path.exists(PID_FILE):
        try:
            return int(open(PID_FILE).read().strip())
        except:
            return None
    return None

def is_process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def find_process_by_grep():
    """grep 备份 — 找 main.py 进程"""
    r = subprocess.run(
        ['pgrep', '-f', 'python3.12 main.py'],
        capture_output=True, text=True, timeout=5,
        cwd=WORKDIR
    )
    if r.returncode == 0 and r.stdout.strip():
        pids = r.stdout.strip().split('\n')
        # 干掉 bash 包装层，只取最里面的 python 进程
        for pid in pids:
            try:
                pid_int = int(pid)
                # 检查是不是真正的 python 进程（不是 bash wrapper）
                status = open(f'/proc/{pid_int}/status').read()
                if 'python3.12' in status.split('\n')[0]:
                    return pid_int
            except:
                continue
    return None

def restart():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(RESTART_LOG, 'a') as f:
        f.write(f'{timestamp} 🚨 进程已死，正在重启...\n')
    
    # 写一个标记文件让 Hermes 定时任务看到
    with open(STATUS_FILE, 'w') as f:
        f.write(f'crashed|{timestamp}')
    
    # 启动新进程（异步，不等待）
    log_path = os.path.join(WORKDIR, 'new_boll.log')
    try:
        os.remove(log_path)
    except:
        pass
    
    proc = subprocess.Popen(
        ['python3.12', 'main.py'],
        cwd=WORKDIR,
        stdout=open(os.path.join(WORKDIR, 'new_boll.log'), 'a'),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    
    # 写 PID 文件
    with open(PID_FILE, 'w') as f:
        f.write(str(proc.pid))
    
    with open(RESTART_LOG, 'a') as f:
        f.write(f'{timestamp} ✅ 已启动 PID={proc.pid}\n')

def is_just_started(pid):
    """避免刚启动就报"已恢复"的噪声"""
    if os.path.exists(PID_FILE):
        try:
            saved = int(open(PID_FILE).read().strip())
            return saved == pid
        except:
            pass
    # 没有 PID 文件，把当前 pid 写进去
    try:
        open(PID_FILE, 'w').write(str(pid))
    except:
        pass
    return False

def main():
    # 先检查 PID 文件
    pid = get_pid_from_file()
    alive = is_process_alive(pid)
    
    if not alive:
        # PID 文件无效或进程死了，用 grep 找
        grep_pid = find_process_by_grep()
        if grep_pid:
            # 进程还在但 PID 文件过期了，更新文件
            with open(PID_FILE, 'w') as f:
                f.write(str(grep_pid))
            # 清除 crash 标记（如果还在）
            if os.path.exists(STATUS_FILE) and 'crashed' in open(STATUS_FILE).read():
                os.remove(STATUS_FILE)
            sys.exit(0)
        else:
            # 真的死了，重启
            restart()
    else:
        # 进程活着，清理 crash 标记
        if os.path.exists(STATUS_FILE):
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
            with open(STATUS_FILE, 'w') as f:
                f.write(f'running|{timestamp}')
        # 第一轮刚刚写 PID，不报"已恢复"噪声
    
    # 如果重启过，写入当前状态供 Hermes cron 读取
    with open(STATUS_FILE, 'w') as f:
        try:
            r = subprocess.run(
                ['tail', '-1', 'new_boll.log'],
                capture_output=True, text=True, timeout=3,
                cwd=WORKDIR,
            )
            last_line = r.stdout.strip()[:200]
            f.write(f'running|{time.strftime("%Y-%m-%d %H:%M:%S")}|{last_line}')
        except:
            f.write(f'running|{time.strftime("%Y-%m-%d %H:%M:%S")}')

if __name__ == '__main__':
    main()
