"""ImgDedup 图形界面（PySide6）。

启动路径上不导入任何核心引擎模块：
`imgdedup_core.feature` 会拉起 cv2 + numpy，导入是秒级的，
放在启动路径上会直接毁掉「首屏 < 1s」的目标。
所以界面层对 core 的引用一律写在方法内部或后台线程里。

这里刻意没有 `__version__`：版本号只有 `imgdedup_core/__init__.py` 一个来源
（`dev/make_installer.py` 从那里读出来，再以 /DAppVersion 传给 ISCC）。
多一份没人读的副本，唯一的作用就是某天和真版本对不上。
也不能靠 `from imgdedup_core import __version__` 派生 ——
那会让界面包在导入期拉进核心引擎，正好违反上面这条。
"""
