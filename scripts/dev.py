"""开发用启动器：一条命令同时起 API 和 worker。

注意：这里起的是**两个子进程**，不是把 worker 塞进 FastAPI 进程。
隔离性因此保留：
    - worker 崩了，API 还能接收上传、查状态
    - API 崩了，正在跑的任务还在 SQLite 队列里（锁过期后会被重新领取）
    - 将来要给 worker 单独扩容/换机器，启动方式不用改

用法：
    D:/conda_envs/ai_resume_screening/python.exe scripts/dev.py          # 8000
    D:/conda_envs/ai_resume_screening/python.exe scripts/dev.py 8010     # 指定端口

Ctrl+C 会一起停掉两个进程。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "8000"

    commands = {
        "api": [PYTHON, "-m", "uvicorn", "app.main:app", "--port", port],
        "worker": [PYTHON, "-m", "app.worker"],
    }
    procs = {
        name: subprocess.Popen(cmd, cwd=ROOT)
        for name, cmd in commands.items()
    }

    print(f"API     : http://127.0.0.1:{port}/")
    print("worker  : 正在消费队列（Ctrl+C 退出两个进程）\n", flush=True)

    try:
        # 任一进程意外退出就整体退出：半个服务比不服务更难排查
        while True:
            for name, proc in procs.items():
                code = proc.poll()
                if code is not None:
                    print(f"\n{name} 已退出（code={code}），正在停止另一个进程…")
                    return code
            # 简单轮询即可，子进程自己会阻塞在这里
            import time

            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in procs.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
