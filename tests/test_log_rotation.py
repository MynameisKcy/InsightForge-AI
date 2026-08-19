"""日志轮转测试（#10）：TimedRotatingFileHandler + backupCount 上限。

不创建临时文件（避免 Windows 文件锁 + 沙箱清理递归）；直接检视模块级
logger 已挂载的文件 Handler 配置。
"""
import unittest
from logging.handlers import TimedRotatingFileHandler

from utils.logger_handler import LOG_BACKUP_COUNT, logger


class LogRotationTests(unittest.TestCase):
    def test_module_logger_uses_timed_rotating_handler(self):
        file_handlers = [
            h for h in logger.handlers
            if isinstance(h, TimedRotatingFileHandler)
        ]
        self.assertEqual(len(file_handlers), 1)
        self.assertEqual(file_handlers[0].backupCount, LOG_BACKUP_COUNT)
        self.assertEqual(file_handlers[0].when, "MIDNIGHT")

    def test_backup_count_is_positive_int(self):
        self.assertIsInstance(LOG_BACKUP_COUNT, int)
        self.assertGreater(LOG_BACKUP_COUNT, 0)


if __name__ == "__main__":
    unittest.main()
