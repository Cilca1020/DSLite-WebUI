"""DSLite-WebUI 一键重启脚本（跨平台：Windows / macOS / Linux）。

功能：
  1. 自动检测并停止占用服务端口的旧进程（支持流式对话正在跑的进程）。
  2. 可选 --install：先安装 requirements.txt 依赖（幂等，已装的会跳过）。
  3. 前台启动服务，日志直接打印在终端，Ctrl+C 结束。

用法：
    python restart.py                # 重启服务
    python restart.py --install      # 先装依赖再重启
    python restart.py --port 5001    # 自定义端口（默认读 config.py）
"""

import os
import re
import signal
import subprocess
import sys

APP_FILE = "app.py"


def _host_port(port_arg=None):
    """从 config.py 读取 HOST/PORT，支持命令行覆盖 PORT。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import config
        host = config.HOST
        port = int(port_arg or config.PORT)
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 读取 config.py 失败：{e}")
        sys.exit(1)
    return host, port


def _windows_pids_on_port(port):
    """Windows：解析 netstat -ano，返回 LISTENING 状态的 PID 集合。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return set()
    pids = set()
    pat = re.compile(rf":{port}\s+\S+\s+LISTENING\s+(\d+)$", re.IGNORECASE)
    for line in out.splitlines():
        m = pat.search(line.strip())
        if m and m.group(1) != "0":
            pids.add(m.group(1))
    return pids


def _unix_pids_on_port(port):
    """Unix：优先 fuser，回退 lsof，返回 PID 集合。"""
    pids = set()
    for cmd in (["fuser", f"{port}/tcp"], ["lsof", "-ti", f"tcp:{port}"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True).stdout
        except FileNotFoundError:
            continue
        for tok in out.split():
            if tok.isdigit():
                pids.add(tok)
        if pids:
            break
    return pids


def kill_port_processes(port):
    """停止占用指定端口的进程，返回被杀掉的 PID 列表。"""
    if sys.platform.startswith("win"):
        pids = _windows_pids_on_port(port)
    else:
        pids = _unix_pids_on_port(port)

    killed = []
    for pid in pids:
        try:
            if sys.platform.startswith("win"):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, text=True)
            else:
                os.kill(int(pid), signal.SIGTERM)
            killed.append(pid)
        except Exception as e:  # noqa: BLE001
            print(f"  提示：无法停止 PID {pid}（{e}），可手动处理。")
    return killed


def main():
    args = sys.argv[1:]
    install = "--install" in args
    port_arg = None
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args):
            port_arg = args[i + 1]

    host, port = _host_port(port_arg)

    if install:
        print("==> 安装依赖 ...")
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "-r", "requirements.txt"], check=True)

    print(f"==> 检查 {port} 端口占用 ...")
    killed = kill_port_processes(port)
    if killed:
        print(f"   已停止旧进程：{', '.join(killed)}")
    else:
        print("   端口空闲，无需停止任何进程。")

    print(f"==> 启动服务 http://{host}:{port} （Ctrl+C 结束）...")
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, APP_FILE], check=False)


if __name__ == "__main__":
    main()
