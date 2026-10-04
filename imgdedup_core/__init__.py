"""ImgDedup 核心引擎。

`__version__` 是整个项目版本号的唯一来源：`imgdedup.spec`、`imgdedup.iss` 和
release workflow 都从它取版本，改版本只改这一处。

`__all__` 只列对外模块，不含 action / cache / cv2_fetch / embed / opencv ——
那几个是内部实现，或按需加载的重件。"""

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "infra",
    "scan",
    "decode",
    "feature",
    "group",
    "catalog",
    "scratch",
]
