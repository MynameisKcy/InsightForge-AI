import logging
import os
from logging.handlers import TimedRotatingFileHandler

from utils.path_tool import get_abs_path

# 日志保存根目录
LOG_ROOT = get_abs_path("logs")
if not os.path.exists(LOG_ROOT):
    os.makedirs(LOG_ROOT, exist_ok=True)

# 日志轮转上限：按天落盘，保留最近 N 个历史文件，超出自动删除
# （防 logs/ 无限增长；对应 SECURITY_AND_LIMITATIONS 的日志容量上限补全）
LOG_BACKUP_COUNT = 30

DEFAULT_LOG_FORMAT = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
)


def get_logger(
    name: str = "agent",
    console_level: int = logging.INFO,
    log_file=None,
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # 避免重复添加 Handler
    if logger.handlers:
        return logger

    # 控制台 Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(DEFAULT_LOG_FORMAT)
    logger.addHandler(console_handler)

    # 文件 Handler：按天轮转（midnight），保留 LOG_BACKUP_COUNT 个历史文件
    # 活动文件为 {name}.log，午夜轮转为 {name}.log.YYYY-MM-DD 并新建活动文件
    if not log_file:
        log_file = os.path.join(LOG_ROOT, f"{name}.log")
    file_handler = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(DEFAULT_LOG_FORMAT)
    logger.addHandler(file_handler)
    return logger


# 快捷获取日记管理器
logger = get_logger()


if __name__ == "__main__":
    logger.info("信息日志")
    logger.error("错误日志")
    logger.warning("警告日志")
    logger.debug("调试日志")
