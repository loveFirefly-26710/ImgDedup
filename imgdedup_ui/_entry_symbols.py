"""把 `__main__` 的启动逻辑导进一个可被归档收录的模块。

## 为什么需要这个文件

PyInstaller 的入口脚本（`__main__.py`）不会被放进 `PYZ.pyz`：它是 exe
的启动点，被编译进 CArchive 的脚本区，不在模块表里。后果有两个：

1. `dev/check_frozen.py` 只扫 PYZ 的 `co_names`，因此永远看不到
   入口脚本里的任何符号（`icon_path` / `set_app_icon` 直接报"缺失"，
   哪怕它们明明就在包里）。
2. 想 `import imgdedup_ui.__main__` 去验证也做不到：冻结后它不是可导入
   模块，`ModuleNotFoundError`。

第二条会直接挡住验证。本轮就栽了一次：写了个探针想在冻结产物里
跑一遍「自动下载 cv2」的完整链路（Qt 对话框 + 后台线程 + 下载），
结果 `ModuleNotFoundError: No module named 'imgdedup_ui.__main__'` ——
探针根本进不去那段代码。所以凡是"想在冻结产物里验一遍"的启动逻辑，
都必须放在这个模块里。

## 这里的函数和 `__main__.py` 里的是同一份实现

搬动而不是复制：`__main__.py` 现在从这里 import。所以符号表断言
`icon_path` / `set_app_icon` 存在时，检查的就是真正跑的那份代码。

## 这里放什么

启动期需要"单独验一遍"的逻辑：

- 图标（`icon_path` / `set_app_icon`）：漏了会让窗口/任务栏图标不对；
- 外置组件 cv2 的检查与自动下载（`_require_cv2` / `_download_cv2`）：
  漏了或写错了会让用户"下载成功却报失败"，而那条路径平时测试碰不到。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 只导入 `QtCore`（轻量）。`QtWidgets` 留到函数里按需导入。
# 这个模块会被入口脚本在启动最早期导入，不该在这里付 QtWidgets 的开销。
from PySide6.QtCore import QThread, Signal

#: 任务栏分组用的 AppUserModelID。不设的话 Windows 把进程归到
#: `python.exe` 那一组，任务栏会显示 Python 的图标（不是本程序的）。
APP_USER_MODEL_ID = "ImgDedup.ImgDedup.App"


def icon_path() -> str | None:
    """在四种可能的部署形态里找图标，返回第一个存在的路径。

    候选顺序必须与 `imgdedup_core.embed.bundled_root()` 保持一致：

    1. `sys._MEIPASS/resources/`：onefile 解包目录
    2. exe 同级的 `resources/`：手工绿色版（用户自己拷的图标）
    3. exe 的 `_internal/resources/`：onedir（PyInstaller 6+ 的布局）
    4. 源码树的 `resources/`：开发态 `python -m imgdedup_ui`

    全找不到就返回 None（不抛异常）：没图标只是不好看，不该拦着用户启动。
    """
    cands: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / "resources" / "icon.ico")
    try:
        exe_dir = Path(sys.executable).resolve().parent
    except Exception:  # noqa: BLE001
        exe_dir = None
    if exe_dir is not None:
        cands.append(exe_dir / "resources" / "icon.ico")
        cands.append(exe_dir / "_internal" / "resources" / "icon.ico")
    here = Path(__file__).resolve().parent
    cands.append(here.parent / "resources" / "icon.ico")
    for c in cands:
        if c.is_file():
            return str(c)
    return None


def set_app_icon(app) -> bool:
    """给 QApplication 装上窗口图标，并修正任务栏分组。

    这两件事互不替代，必须都做：

    * `setWindowIcon` 管窗口（左上角、Alt+Tab 缩略图）
    * `AppUserModelID` 管任务栏怎么分组：不设的话，即使窗口图标对
      了，任务栏上钉着的仍是 Python 的图标

    返回是否真的设上了图标（用于测试/诊断），失败静默不抛。
    """
    ico = icon_path()
    if not ico:
        return False
    from PySide6.QtGui import QIcon

    app.setWindowIcon(QIcon(ico))
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                APP_USER_MODEL_ID
            )
        except Exception:  # noqa: BLE001
            # 设不上只是任务栏分组不好看，不该拦着启动
            pass
    return True

# ============================================================ 外置组件 cv2
#
# 启动时检查 / 自动下载。放在这里而不是 `__main__.py`。
# 入口脚本冻结后不可导入，探针进不去，这条链路就没法在产物里验证。
# 而它恰恰是最容易出问题的地方（进度对话框 + 线程 + 事件循环）。


class _Cv2DownloadTask(QThread):
    """后台下载外置组件 cv2，把进度回主线程。

    用 `QThread` + `Signal`（不是裸线程 + 回调）：跨线程投递由 Qt 保证，
    不用自己搓 `QMetaObject.invokeMethod`。同 `widgets._AnimDecodeTask` 的套路。
    """

    #: (已下载, 总大小, 说明文字)
    progress = Signal(int, int, str)
    #: (成功?, 说明文字)
    done = Signal(bool, str)

    def __init__(self, extra_dir, parent=None):
        super().__init__(parent)
        self.extra_dir = extra_dir

    def run(self) -> None:
        try:
            from imgdedup_core import cv2_fetch

            n, total = cv2_fetch.install(
                self.extra_dir,
                cb=lambda a, b, m: self.progress.emit(a, b, m))
            self.done.emit(True, f"共 {n} 个文件，{total / 1048576:.1f} MB")
        except Exception as e:  # noqa: BLE001
            self.done.emit(False, f"{type(e).__name__}: {e}")


def _download_cv2(app, extra_dir) -> bool:
    """下载并装好 cv2。成功返回 True。不抛异常（失败用对话框说清楚）。"""
    from PySide6.QtCore import QEventLoop
    from PySide6.QtWidgets import QMessageBox, QProgressDialog

    task = _Cv2DownloadTask(extra_dir)
    box = QProgressDialog("正在从 PyPI 下载 cv2…", "取消", 0, 100)
    box.setWindowTitle("下载 cv2")
    box.setMinimumDuration(0)
    box.setAutoClose(False)
    box.setAutoReset(False)
    box.setValue(0)

    result = {}
    loop = QEventLoop()

    def on_progress(got: int, total: int, msg: str) -> None:
        box.setLabelText(msg)
        if total > 0:
            box.setValue(int(got * 100 / total))
        else:
            # 没有总大小（解压阶段）→ 走"不确定"进度，别让进度条僵在某个百分比
            box.setRange(0, 0)

    def on_done(ok: bool, msg: str) -> None:
        result["ok"] = ok
        result["msg"] = msg
        loop.quit()

    task.progress.connect(on_progress)
    task.done.connect(on_done)
    box.canceled.connect(loop.quit)      # 用户点取消 → 不再等（任务自己跑完）
    task.start()
    loop.exec()                          # 真事件循环，别用忙轮询

    # `wasCanceled()` 必须在 `close()` 之前读！
    # `QProgressDialog.close()` 本身就会发出 `canceled` 信号
    # （Qt 的既定行为：关闭进度对话框 = 取消）。所以
    #   close() → canceled → wasCanceled() 变 True
    # 于是"下载成功"也会被判成"用户取消了"，永远返回失败。
    # 这个 bug 只有真的把这段代码跑一遍才能发现。
    # 光断言"按钮存在"是看不出来的。
    user_canceled = box.wasCanceled()
    box.close()
    task.wait(30000)                     # 别让线程活着离开这个作用域

    if user_canceled:
        return False
    if not result.get("ok"):
        QMessageBox.critical(
            None, "下载失败",
            f"{result.get('msg', '未知错误')}\n\n"
            f"也可以手动下载后放进：\n{extra_dir / 'cv2'}")
        return False
    QMessageBox.information(None, "下载完成",
                            f"cv2 已装好（{result.get('msg', '')}）。")
    return True


def _require_cv2(app) -> bool:
    """启动前确认外置组件 cv2 在位。缺了就把话说清楚再退出。

    为什么要在启动时挡：cv2 是扫描/特征提取的必需件，
    缺了它"开始扫描"必然失败。与其让用户点进去、扫到一半报个看不懂的错，
    不如一开始就告诉他缺什么、放哪儿、怎么拿。

    用对话框而不是 `print`：GUI 版 exe 没有控制台，
    `print` 出去的信息用户永远看不到（这正是"闪退且无提示"的成因）。

    返回 True 表示可以继续启动。
    """
    from imgdedup_core import opencv as OPENCV

    if OPENCV.is_available():
        return True

    from PySide6.QtWidgets import QMessageBox

    target = OPENCV.cv2_dir()
    box = QMessageBox()
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("ImgDedup —— 缺少图像处理组件")
    box.setText("缺少图像处理组件 cv2，程序无法开始扫描。")
    box.setInformativeText(
        f"这个组件有 86 MB，为了不让安装包变得很大，改成了单独下载。\n\n"
        f"可以点「自动下载」让它自己从 PyPI 取（约 42 MB），\n"
        f"也可以自己放好：\n{target}"
    )
    dl_btn = box.addButton("自动下载", QMessageBox.AcceptRole)
    open_btn = box.addButton("打开该文件夹", QMessageBox.ActionRole)
    box.addButton("退出", QMessageBox.RejectRole)
    box.exec()

    clicked = box.clickedButton()
    if clicked is dl_btn:
        # 目录可能还不存在：先建出来，别让下载因为目录缺失失败
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if _download_cv2(app, OPENCV.extra_dir()):
            # 必须 refresh()：require() 把"没有"缓存住了，
            # 不清掉的话刚下好也会继续报缺组件。
            return OPENCV.refresh()
        return False

    if clicked is open_btn:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        _reveal_path(str(target.parent if not target.exists() else target))
    return False


def _reveal_path(path: str) -> None:
    """在资源管理器里打开某个路径（失败就算了，不拦着退出）。

    和 `window._reveal` 同一个坑：`explorer` 不按 MSVCRT 规则解析
    含空格的实参，必须写成一条完整的命令行字符串并自己加引号。
    """
    import subprocess

    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return
    try:
        if p.is_dir():
            subprocess.Popen(f'explorer "{p}"', shell=False)
        else:
            subprocess.Popen(f'explorer /select,"{p}"', shell=False)
    except Exception:  # noqa: BLE001
        pass


# ============================================================ 外置 CLIP 模型
#
# 与 cv2 同一套套路（弹对话框 → 后台线程下载 → 进度对话框），
# 但有两处刻意不同：
#
#   1. 不拦启动。 模型只服务「深度」一档，快速/精确两档不需要它。
#      为一个可选档位拦住整个程序启动，代价太大。所以它是"用到才问"，
#      入口是 `window._guard_deep_mode`，不是 `__main__.main`。
#   2. 下载落到 `<exe 同级>/extra/models/`，不是用户缓存目录。
#      这样落地位置和提示文案里的路径一致，用户不会"下完了却找不到"。
#
# 和 cv2 一样放在这里而不是 `__main__.py`：入口脚本冻结后不可导入，
# 探针进不去，这条链路就没法在产物里验证。


class _ModelDownloadTask(QThread):
    """后台下载 CLIP 模型，把进度回主线程。

    用 `QThread` + `Signal` 而不是裸线程 + 回调：跨线程投递由 Qt 保证。
    同 `_Cv2DownloadTask` 的套路。
    """

    #: (已下载字节, 总字节, 说明文字)
    progress = Signal(int, int, str)
    #: (成功?, 说明文字)
    done = Signal(bool, str)

    def __init__(self, dest, parent=None):
        super().__init__(parent)
        self.dest = dest

    def run(self) -> None:
        try:
            from imgdedup_core import embed as EMB

            EMB.ensure_model(
                dest=self.dest,
                cb=lambda a, b, m: self.progress.emit(a, b, m))
            self.done.emit(True, f"已装到 {self.dest}")
        except Exception as e:  # noqa: BLE001
            self.done.emit(False, f"{type(e).__name__}: {e}")


def _download_model(parent=None) -> bool:
    """下载并装好 CLIP 模型。成功返回 True。不抛异常（失败用对话框说清楚）。"""
    from PySide6.QtCore import QEventLoop
    from PySide6.QtWidgets import QMessageBox, QProgressDialog

    from imgdedup_core import embed as EMB

    dest = EMB.model_extra_dir()
    # 目录可能还不存在：先建出来，别让下载因为目录缺失失败
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    task = _ModelDownloadTask(dest)
    box = QProgressDialog("正在下载 CLIP 模型（约 85 MB）…", "取消",
                          0, 100, parent)
    box.setWindowTitle("下载 CLIP 模型")
    box.setMinimumDuration(0)
    box.setAutoClose(False)
    box.setAutoReset(False)
    box.setValue(0)

    result = {}
    loop = QEventLoop()

    def on_progress(got: int, total: int, msg: str) -> None:
        box.setLabelText(f"正在下载 {msg}")
        if total > 0:
            box.setValue(int(got * 100 / total))
        else:
            # 没有总大小 → 走"不确定"进度，别让进度条僵在某个百分比
            box.setRange(0, 0)

    def on_done(ok: bool, msg: str) -> None:
        result["ok"] = ok
        result["msg"] = msg
        loop.quit()

    task.progress.connect(on_progress)
    task.done.connect(on_done)
    box.canceled.connect(loop.quit)      # 用户点取消 → 不再等（任务自己跑完）
    task.start()
    loop.exec()                          # 真事件循环，别用忙轮询

    # `wasCanceled()` 必须在 `close()` 之前读！
    # `QProgressDialog.close()` 本身就会发出 `canceled` 信号
    # （Qt 的既定行为：关闭进度对话框 = 取消）。顺序反了，"下载成功"
    # 也会被判成"用户取消"，永远返回失败。同 `_download_cv2` 的说明。
    user_canceled = box.wasCanceled()
    box.close()
    task.wait(180000)                    # 85 MB，收尾给足时间

    if user_canceled:
        return False
    if not result.get("ok"):
        QMessageBox.critical(
            parent, "下载失败",
            f"{result.get('msg', '未知错误')}\n\n"
            f"也可以手动下载后放进：\n{dest}")
        return False
    QMessageBox.information(parent, "下载完成",
                            f"CLIP 模型已装到：\n{dest}")
    return True


def ensure_model_ready(parent=None) -> bool:
    """确认 CLIP 模型可用；缺了就弹对话框让用户选。

    返回 True 表示现在可以进「深度」模式。
    不抛异常，任何失败路径都返回 False（由调用方拨回上一档）。
    """
    from imgdedup_core import embed as EMB

    if EMB.is_ready():
        return True

    from PySide6.QtWidgets import QMessageBox

    target = EMB.model_extra_dir()
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("ImgDedup —— 缺少语义模型")
    box.setText("「深度」模式需要 CLIP 语义模型，当前没有安装。")
    box.setInformativeText(
        f"模型约 85 MB，为了不让安装包变得很大，改成了单独下载。\n"
        f"快速 / 精确两档不需要它，随时可以切回去。\n\n"
        f"可以点「自动下载」让它自己取，\n"
        f"也可以自己放好：\n{target}"
    )
    dl_btn = box.addButton("自动下载", QMessageBox.AcceptRole)
    open_btn = box.addButton("打开该文件夹", QMessageBox.ActionRole)
    box.addButton("暂不下载", QMessageBox.RejectRole)
    box.exec()

    clicked = box.clickedButton()
    if clicked is dl_btn:
        if _download_model(parent):
            # 不能直接 return True：下载可能"报成功"但文件不全
            # （镜像给了 404 页面、磁盘满……），必须再查一次实际状态。
            return EMB.is_ready()
        return False

    if clicked is open_btn:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        _reveal_path(str(target))
    return False
