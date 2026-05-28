from agent.utils.config_handler import prompts_conf
from agent.utils.logger_handler import logger
from agent.utils.path_tool import get_abs_path
import os


def load_system_prompts():
    try:
        system_prompt_path = get_abs_path(prompts_conf['main_prompt_path'])
    except KeyError as e:
        logger.error(f"[load system prompts]在yaml配置项中没有main_prompt_path")
        raise e

    try:
        return open(system_prompt_path,"r",encoding="utf-8").read()
    except Exception as e:
        logger.error(f"[load system prompts]解析系统提示词出错,{str(e)}")
        raise e

def _load_prompt_file(conf_key: str, log_prefix: str) -> str:
    try:
        prompt_path = get_abs_path(prompts_conf[conf_key])
    except KeyError as e:
        logger.error(f"[{log_prefix}]在yaml配置项中没有{conf_key}")
        raise e

    if not os.path.exists(prompt_path):
        error = FileNotFoundError(f"提示词文件不存在: {prompt_path}")
        logger.error(f"[{log_prefix}]解析提示词出错,{str(error)}")
        raise error

    try:
        with open(prompt_path, "r", encoding="utf-8") as prompt_file:
            return prompt_file.read()
    except Exception as e:
        logger.error(f"[{log_prefix}]解析提示词出错,{str(e)}")
        raise e

def load_rag_prompts():
    return _load_prompt_file("rag_summarize_prompt_path", "load rag prompts")

def load_report_prompts():
    return _load_prompt_file("report_prompt_path", "load report prompts")


if __name__ == '__main__':
    print(load_system_prompts())
