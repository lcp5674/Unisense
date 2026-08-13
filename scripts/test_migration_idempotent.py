#!/usr/bin/env python3
"""Alembic 迁移幂等性测试（OPS-04）：upgrade→downgrade→upgrade 无错误。"""
import subprocess, sys

def run(cmd: str) -> bool:
    print(f">>> {cmd}")
    result = subprocess.run(cmd, shell=True, cwd="backend")
    return result.returncode == 0

def main() -> int:
    if not run("alembic upgrade head"):
        print("FAIL: upgrade head failed"); return 1
    if not run("alembic downgrade base"):
        print("FAIL: downgrade base failed"); return 1
    if not run("alembic upgrade head"):
        print("FAIL: second upgrade head failed"); return 1
    print("PASS: migration idempotent test passed")
    return 0

if __name__ == "__main__":
    sys.exit(main())
