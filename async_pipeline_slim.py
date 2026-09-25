"""精简版：并发处理 + 异步接口 + 自主循环（内部循环取最大值）
本版本不依赖外网：接口用本地 mock，完全离线可运行，保证能看到数字。
"""
import asyncio
import logging
import random

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pipeline")

CONCURRENCY = 5      # 最大并发
TIMEOUT = 10         # 单请求超时(秒，本版本用于模拟)
ROUNDS = 4           # 内部循环轮数（>=4）
MAX_TASKS = 5        # 每轮任务数的最大值（内部循环上限）
INTERVAL = 1         # 轮间隔(秒)

sem = asyncio.Semaphore(CONCURRENCY)


async def call(tid: int) -> dict:
    """本地 mock 异步接口（不依赖外网）：模拟延迟后返回带数字的结果"""
    async with sem:
        await asyncio.sleep(random.uniform(0.05, 0.2))  # 模拟网络耗时（本地，很快）
        return {"task_id": tid, "ok": True, "status": 200, "value": tid * 2}


async def reply(r: dict) -> None:
    """处理接口回复（替换成你的业务逻辑）"""
    log.info("回复: %s", r)


async def one_round(n: int) -> None:
    log.info("==== 第 %s/%s 轮，本轮任务数 = %s ====", n, ROUNDS, MAX_TASKS)
    # 用最大值作为内部循环上限
    results = await asyncio.gather(
        *(call(n * 100 + i) for i in range(1, MAX_TASKS + 1)),
        return_exceptions=True,
    )
    for r in results:
        await reply(r if isinstance(r, dict) else {"error": repr(r)})


async def main() -> None:
    for n in range(1, ROUNDS + 1):
        await one_round(n)
        if n < ROUNDS:
            await asyncio.sleep(INTERVAL)
    log.info("✅ 完成，共循环 %s 轮，每轮最多 %s 个任务，合计 %s 个任务",
             ROUNDS, MAX_TASKS, ROUNDS * MAX_TASKS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("已中断退出")
