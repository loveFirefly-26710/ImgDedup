"""启动期的杂事：窗口图标，以及 cv2 / 模型缺失时的提示与程序内下载。

单独成一个模块，是为了让打包自检能在不起主窗口的情况下验证图标与下载链路。"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal

APP_USER_MODEL_ID = "ImgDedup.ImgDedup.App"


def icon_path() -> str | None:
    cands: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / "resources" / "icon.ico")
    try:
        exe_dir = Path(sys.executable).resolve().parent
    except Exception:
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
        except Exception:
            pass
    return True



class _Cv2DownloadTask(QThread):

    progress = Signal(int, int, str)
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
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}")


def _download_cv2(extra_dir) -> bool:
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
            box.setRange(0, 0)

    def on_done(ok: bool, msg: str) -> None:
        result["ok"] = ok
        result["msg"] = msg
        loop.quit()

    task.progress.connect(on_progress)
    task.done.connect(on_done)
    box.canceled.connect(loop.quit)
    task.start()
    loop.exec()

    user_canceled = box.wasCanceled()
    box.close()
    task.wait(30000)

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
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if _download_cv2(OPENCV.extra_dir()):
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
    except Exception:
        pass




class _ModelDownloadTask(QThread):

    progress = Signal(int, int, str)
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
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}")


def _download_model(parent=None) -> bool:
    from PySide6.QtCore import QEventLoop
    from PySide6.QtWidgets import QMessageBox, QProgressDialog

    from imgdedup_core import embed as EMB

    dest = EMB.model_extra_dir()
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
            box.setRange(0, 0)

    def on_done(ok: bool, msg: str) -> None:
        result["ok"] = ok
        result["msg"] = msg
        loop.quit()

    task.progress.connect(on_progress)
    task.done.connect(on_done)
    box.canceled.connect(loop.quit)
    task.start()
    loop.exec()

    user_canceled = box.wasCanceled()
    box.close()
    task.wait(180000)

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
            return EMB.is_ready()
        return False

    if clicked is open_btn:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        _reveal_path(str(target))
    return False
