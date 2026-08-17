"""SSE 流式管道工具：句子拆分 + 同步生成器的线程/心跳桥。"""
import asyncio
import re as re_module
import threading
import traceback

from utils.logger_handler import logger


def _split_sentences(text: str) -> list[str]:
    """将文本按句子分割，保持分隔符在句尾。仅按中文标点 + 换行拆分。

    刻意不拆分英文句号（.）、感叹号（!）、问号（?）——中文输出中这些符号
    常出现在数字（3.26%）、Markdown 标记（**粗体**）、URL 等非句末语境，
    按它们拆分会导致换行断裂。
    """
    parts = re_module.split(r'(?<=[。！？\n])\s*', text)
    return [p for p in parts if p.strip()]


async def _stream_with_heartbeat(sync_gen_factory, heartbeat: str, interval: float = 15,
                                 progress_emitter=None, cancel_token=None):
    """把同步生成器放进后台线程执行，主协程带心跳消费。

    问题：ReactAgent.execute_stream 在 run_full_analysis 等长工具执行期间，
    同步迭代器会阻塞，async generate() 数分钟不 yield 任何字节，
    前端 idle 超时 abort。此包装用线程跑同步迭代，把每个 chunk 经
    loop.call_soon_threadsafe 推入 asyncio.Queue；主协程 wait_for 队列，
    interval 秒内无新数据则 yield 一个心跳保活。

    yield (kind, value)：kind 为 "heartbeat"（已格式化 SSE 行，直接 yield）、
    "chunk"（原始 chunk 文本，交调用方处理）、"progress"（步骤事件 dict，转 [STEP] 下发）。
    cancel_token 置位后停止消费（生产者线程经协作式检查自行退出）。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    if progress_emitter is not None:
        progress_emitter.bind(loop, queue)   # 让 PlannerAgent 的步骤事件直注同一 queue
    # 线程异常载体
    error_box: list = []

    def _producer():
        try:
            for chunk in sync_gen_factory():
                loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
        except Exception as e:  # 线程内异常推回主协程
            error_box.append(e)
            logger.error(f"_stream_with_heartbeat producer error: {e}\n{traceback.format_exc()}")
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

    t = threading.Thread(target=_producer, daemon=True)
    t.start()
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                if cancel_token is not None and cancel_token.cancelled:
                    break
                yield ("heartbeat", heartbeat)  # 心跳保活
                continue
            if kind == "done":
                break
            if cancel_token is not None and cancel_token.cancelled:
                break
            yield (kind, value)   # "chunk" 或 "progress"
    finally:
        # 线程异常在主协程抛出，触发上层 except → [ERROR]
        if progress_emitter is not None:
            progress_emitter.close()
        if error_box and not (cancel_token is not None and cancel_token.cancelled):
            raise error_box[0]
