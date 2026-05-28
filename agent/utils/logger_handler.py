import datetime
import logging
import os

try:
    from agent.utils.path_tool import get_abs_path
except ModuleNotFoundError:
    from utils.path_tool import get_abs_path

#日志保存根目录
LOG_ROOT = get_abs_path("logs")
if not os.path.exists(LOG_ROOT):
    os.makedirs(LOG_ROOT,exist_ok=True)

DEFAULT_LOG_FORMAT = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
)

def get_logger(
        name: str = "agent",
        console_level: int = logging.INFO,
        log_file = None,
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    #避免重复添加Handler
    if logger.handlers:
        return logger

    #控制台Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(DEFAULT_LOG_FORMAT)
    logger.addHandler(console_handler)

    #文件Handler
    if not log_file:
        log_file = os.path.join(LOG_ROOT, f"{name}_{datetime.datetime.now().strftime('%Y%m%d')}.log")
    file_handler = logging.FileHandler(log_file,encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(DEFAULT_LOG_FORMAT)
    logger.addHandler(file_handler)
    return logger
#快捷获取日记管理器
logger = get_logger()


if __name__ == "__main__":
    logger.info("信息日志")
    logger.error("错误日志")
    logger.warning("警告日志")
    logger.debug("调试日志")
