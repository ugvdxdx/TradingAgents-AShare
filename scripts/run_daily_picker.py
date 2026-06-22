#!/usr/bin/env python3
"""每日选股全流程编排 (18点后自动跑)。

串联数据刷新 + 选股, 处理失败隔离:
  [必须成功] K线增量 → 资金流增量
  [容错继续] 研报刷新 → 新晋股扫描 → 选股

设计原则:
  - K线 + 资金流 是选股核心输入 (r5/r20/capital 依赖), 失败则终止不跑选股。
  - 研报 (run_daily_update) cookie 过期会 sys.exit(3), 用容错捕获, 不阻塞选股
    (capital 重算失败被 picker 内部吞掉仍能跑, 只是板块动量略陈旧)。
  - 新晋股扫描可跳过 (14天归因缓存兜底, 漏一天不影响)。
  - 选股是最终目标, 只要核心数据新鲜就跑。

用法:
  uv run python3 scripts/run_daily_picker.py            # 全流程
  uv run python3 scripts/run_daily_picker.py --skip-research   # 跳过研报步骤
  uv run python3 scripts/run_daily_picker.py --dry-run         # 选股环节 dry-run (不联网)

输出: results/daily_logs/YYYY-MM-DD.log (全流程日志)
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime

# 确保工作目录在项目根 (launchd 调用时 cwd 可能不对)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)
except Exception:
    pass

LOG_DIR = os.path.join(PROJECT_ROOT, "results", "daily_logs")
os.makedirs(LOG_DIR, exist_ok=True)


def log(msg: str):
    """带时间戳的日志打印。"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run_step(name: str, cmd: list[str], required: bool, log_file) -> bool:
    """运行一个步骤, 捕获退出码。

    Args:
        name: 步骤名 (用于日志)
        cmd: 命令 (已含 uv run python3 前缀)
        required: True=失败终止全流程, False=失败容错继续
        log_file: 日志文件句柄 (stdout+stderr 同时写文件和终端)

    Returns:
        True=成功, False=失败
    """
    log(f"━━━ 开始: {name} ━━━")
    log(f"  命令: {' '.join(cmd)}")
    t0 = datetime.now()

    # stdout/stderr 同时输出到终端和日志文件
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)

    # 输出到日志文件
    log_file.write(f"\n{'='*60}\n[{name}] {' '.join(cmd)}\n{'='*60}\n")
    log_file.write(result.stdout)
    log_file.write(f"\n[退出码: {result.returncode}]\n")
    log_file.flush()

    elapsed = (datetime.now() - t0).total_seconds()
    ok = result.returncode == 0

    if ok:
        log(f"  ✓ {name} 成功 ({elapsed:.0f}s)")
    else:
        status = "终止" if required else "容错继续"
        log(f"  ✗ {name} 失败 (退出码 {result.returncode}, {elapsed:.0f}s) → {status}")
        # 打印尾部输出帮助诊断
        tail = "\n".join(result.stdout.strip().split("\n")[-5:])
        if tail:
            for line in tail.split("\n"):
                log(f"    │ {line}")

    return ok


def main():
    parser = argparse.ArgumentParser(description="每日选股全流程编排")
    parser.add_argument("--skip-research", action="store_true",
                        help="跳过研报刷新 (cookie 过期或想加速时用)")
    parser.add_argument("--skip-scan", action="store_true",
                        help="跳过新晋股扫描")
    parser.add_argument("--dry-run", action="store_true",
                        help="选股环节用 dry-run (不联网/不调LLM, 仅验证管道)")
    args = parser.parse_args()

    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(LOG_DIR, f"{today}.log")
    log_file = open(log_path, "w", encoding="utf-8")

    print("═" * 60)
    print(f"  每日选股全流程 — {today}")
    print(f"  日志: {log_path}")
    print("═" * 60)
    log(f"流程启动")

    uv = "uv"
    py = "python3"
    base = [uv, "run", py]
    t_start = datetime.now()

    # ━━━ 关键步骤 (必须成功, 失败终止) ━━━
    # 1. K线增量刷新 (r5/r20/capital 基础)
    ok = run_step(
        "K线增量刷新",
        base + ["picker/pipeline/update_klines_daily.py"],
        required=True, log_file=log_file,
    )
    if not ok:
        log("⛔ K线刷新失败, 终止全流程 (r5/r20/capital 会失真)")
        _finish(log_file, t_start, success=False)
        sys.exit(1)

    # 2. 资金流增量 (capital 板块动量 + fund_5d 基础)
    ok = run_step(
        "资金流增量刷新",
        base + ["picker/pipeline/fetch_money_flow_all.py"],
        required=True, log_file=log_file,
    )
    if not ok:
        log("⛔ 资金流刷新失败, 终止全流程 (capital/fund_5d 会失真)")
        _finish(log_file, t_start, success=False)
        sys.exit(1)

    # ━━━ 容错步骤 (失败继续, 不阻塞选股) ━━━
    # 3. 研报刷新 (圈子采集+提取+fundamentals+世界知识)
    #    cookie 过期会 sys.exit(3), 用容错捕获
    if not args.skip_research:
        run_step(
            "研报刷新 (run_daily_update)",
            base + ["picker/pipeline/run_daily_update.py"],
            required=False, log_file=log_file,
        )
    else:
        log("⏭ 跳过研报刷新 (--skip-research)")

    # 4. 新晋股扫描 (量价异动 + 归因)
    if not args.skip_scan:
        run_step(
            "新晋股扫描 (scan_mispriced)",
            base + ["picker/discovery/scan_mispriced.py"],
            required=False, log_file=log_file,
        )
    else:
        log("⏭ 跳过新晋股扫描 (--skip-scan)")

    # ━━━ 选股 (最终目标) ━━━
    picker_cmd = base + ["picker/pipeline/debate_picker_v5.py"]
    if args.dry_run:
        picker_cmd.append("--dry-run")
    ok = run_step(
        "选股 (debate_picker_v5)",
        picker_cmd,
        required=True, log_file=log_file,
    )
    if not ok:
        log("⛔ 选股失败 (退出码非0)")
        _finish(log_file, t_start, success=False)
        sys.exit(1)

    _finish(log_file, t_start, success=True)


def _finish(log_file, t_start: datetime, success: bool):
    """收尾: 关日志, 打印汇总。"""
    elapsed = (datetime.now() - t_start).total_seconds()
    status = "✓ 成功" if success else "✗ 失败"
    log(f"━━━ 全流程结束: {status} | 总耗时 {elapsed/60:.1f}分钟 ━━━")
    log_file.write(f"\n[全流程 {status}, 耗时 {elapsed/60:.1f}分钟]\n")
    log_file.close()


if __name__ == "__main__":
    main()
