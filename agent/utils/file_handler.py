import hashlib
import os.path
from langchain_core.documents import Document
from langchain_community.document_loaders import PyPDFLoader, TextLoader

try:
    from agent.utils.logger_handler import logger
except ModuleNotFoundError:
    from utils.logger_handler import logger



def get_file_md5_hex(filepath:str):
    if not os.path.exists(filepath):
        logger.error('File %s not exists!', filepath)
        return None
    if not os.path.isfile(filepath):
        logger.error('File %s is not a file!', filepath)
        return None
    md5_obj = hashlib.md5()
    chunk_size = 4096
    try:
        with open(filepath, 'rb') as f:
            while chunk := f.read(chunk_size):
                md5_obj.update(chunk)
        return md5_obj.hexdigest()
    except Exception as e:
        logger.error(e)
        return None

def listdir_with_allowed_type(path:str, allowed_types:tuple[str]):
    files = []
    if not os.path.isdir(path):
        logger.error('%s is not a directory!', path)
        return tuple()

    for f in os.listdir(path):
        if f.endswith(allowed_types):
            files.append(os.path.join(path, f))
    return tuple(files)

def pdf_loader(filepath:str,passwd:str=None) -> list[Document]:
    return PyPDFLoader(filepath,passwd).load()

def text_loader(filepath:str,passwd:str=None) -> list[Document]:
    return TextLoader(filepath, encoding=passwd or "utf-8").load()
