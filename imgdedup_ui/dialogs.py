"""删除方式对话框：回收站 / 移动到指定目录，以及「记住选择」。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QPushButton,
                               QRadioButton, QVBoxLayout)

from imgdedup_core import action as A


def _mb(n: int) -> str:
    return f"{n / 1024**2:.1f} MB" if n >= 1024**2 else f"{n / 1024:.0f} KB"


class DeleteDialog(QDialog):

    def __init__(self, count: int, reclaim: int, samples: list[str], *,
                 default_target: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("确认删除")
        self.setMinimumWidth(470)
        self._default_target = default_target

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        title = QLabel(f"确认删除 {count:,} 个文件？")
        f = title.font()
        f.setPointSize(f.pointSize() + 1)
        f.setBold(True)
        title.setFont(f)
        root.addWidget(title)

        sub = QLabel(f"预计释放 <b>{_mb(reclaim)}</b>。本操作会写入撤销记录，可随时还原。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#888780;")
        root.addWidget(sub)

        if samples:
            box = QListWidget()
            box.setMaximumHeight(96)
            box.setSelectionMode(QListWidget.NoSelection)
            for p in samples[:200]:
                box.addItem(p)
            if count > len(samples[:200]):
                box.addItem(f"… 其余 {count - len(samples[:200]):,} 个")
            root.addWidget(box)

        self.radio_recycle = QRadioButton("移到回收站")
        self.radio_recycle.setChecked(True)
        root.addWidget(self.radio_recycle)
        hint1 = QLabel("推荐 · 可从系统回收站还原")
        hint1.setStyleSheet("color:#888780; margin-left:22px;")
        root.addWidget(hint1)

        self.radio_move = QRadioButton("移动到指定目录")
        root.addWidget(self.radio_move)
        hint2 = QLabel("保留原目录结构，便于归档审阅")
        hint2.setStyleSheet("color:#888780; margin-left:22px;")
        root.addWidget(hint2)

        row = QHBoxLayout()
        row.setContentsMargins(22, 0, 0, 0)
        self.path_edit = QLineEdit(default_target)
        self.path_edit.setEnabled(False)
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.setEnabled(False)
        row.addWidget(self.path_edit, 1)
        row.addWidget(self.btn_browse)
        root.addLayout(row)

        self.chk_remember = QCheckBox("记住我的选择，不再提示")
        root.addWidget(self.chk_remember)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_confirm = QPushButton("确认删除")
        self.btn_confirm.setDefault(True)
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_confirm)
        root.addLayout(btns)

        self.radio_move.toggled.connect(self._sync_mode)
        self.btn_browse.clicked.connect(self._browse)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_confirm.clicked.connect(self.accept)

    def _sync_mode(self, move_on: bool) -> None:
        self.path_edit.setEnabled(move_on)
        self.btn_browse.setEnabled(move_on)
        if move_on and not self.path_edit.text().strip():
            self.path_edit.setText(self._default_target)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择归档目录",
                                             self.path_edit.text() or "")
        if d:
            self.path_edit.setText(d)

    def decision(self) -> A.Decision:
        if self.radio_move.isChecked():
            return A.Decision(mode=A.MODE_MOVE,
                              target_dir=self.path_edit.text().strip(),
                              remember=self.chk_remember.isChecked())
        return A.Decision(mode=A.MODE_RECYCLE, target_dir="",
                          remember=self.chk_remember.isChecked())


def ask_delete(parent, count: int, reclaim: int, samples: list[str], *,
               remembered: Optional[A.Decision], default_target: str) -> Optional[A.Decision]:
    if remembered is not None:
        return A.Decision(mode=remembered.mode, target_dir=remembered.target_dir,
                          remember=True)

    dlg = DeleteDialog(count, reclaim, samples, default_target=default_target,
                       parent=parent)
    if dlg.exec() != QDialog.Accepted:
        return None
    return dlg.decision()
