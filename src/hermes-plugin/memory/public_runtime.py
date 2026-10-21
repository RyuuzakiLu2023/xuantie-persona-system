"""================================================================
XTMEM · 公共安装路径导游图

▍这是什么
为源码、wheel 和端接头提供相同的用户目录，不导入引擎、不读取凭据。
依赖仅为 Python 标准库；由公开适配的路径默认值调用。

▍修改铁律
路径解析不得创建库或探测私人部署；环境变量属于使用者自己的配置。

▍更新记录
2026-10-22：公开同步时集中声明路径，避免擦掉地址后留下不可执行占位符。
================================================================"""
import os
from pathlib import Path


def home_file(relative=""):
    """配置和诊断共用用户目录，避免跟随调用者当前工作目录漂移。"""
    home = Path(os.environ.get("XTMEM_HOME", "~/.xtmem")).expanduser()
    return str(home.joinpath(relative).resolve())


def db_default():
    """只返回路径；调用者负责权限、迁移和真正开库。"""
    return str(Path(os.environ.get("XTMEM_DB_PATH", home_file("vector_store.db"))).expanduser().resolve())
