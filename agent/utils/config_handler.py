"""
yaml
k:v
"""
import yaml

try:
    from agent.utils.path_tool import get_abs_path
except ModuleNotFoundError:
    from utils.path_tool import get_abs_path

def load_rag_config(config_path: str = get_abs_path("config/rag.yml"),encoding="utf-8"):
    with open(config_path,"r",encoding=encoding) as f:
        return yaml.load(f,Loader=yaml.FullLoader)

def load_chroma_config(config_path: str = get_abs_path("config/chroma.yml"),encoding="utf-8"):
    with open(config_path,"r",encoding=encoding) as f:
        return yaml.load(f,Loader=yaml.FullLoader)

def load_prompts_config(config_path: str = get_abs_path("config/prompts.yml"),encoding="utf-8"):
    with open(config_path,"r",encoding=encoding) as f:
        return yaml.load(f,Loader=yaml.FullLoader)

def load_agent_config(config_path: str = get_abs_path("config/agent.yml"),encoding="utf-8"):
    with open(config_path,"r",encoding=encoding) as f:
        return yaml.load(f,Loader=yaml.FullLoader)

rag_conf = load_rag_config()
chroma_conf = load_chroma_config()
prompts_conf = load_prompts_config()
agent_conf = load_agent_config()
