## 这个 PR 做了什么

<!-- 一两句话说明动机和结果。如果是修 bug，写清"修之前会怎样、修之后会怎样"。 -->

## 改了哪些地方

- [ ] `imgdedup_core/`（核心逻辑，**不依赖 Qt**）
- [ ] `imgdedup_ui/`（界面）
- [ ] `tests/`
- [ ] 打包配置（`imgdedup.spec` / `imgdedup.iss` / `dev/`）
- [ ] 文档（README / PLAN / docs / 主题文档）
- [ ] `.github/`（workflows / issue 模板 / PR 模板）

## 自查清单

- [ ] `python -m unittest discover -s tests -p "test_*.py"` 全过
- [ ] `python dev/check_no_network.py` 全过（测试不许依赖网络）
- [ ] `python dev/check_fresh_clone.py` 全过（**模拟全新克隆**，
      本机有模型 / 有 `dist/` / 有缓存，会把"只在干净环境才暴露"的问题盖住；
      这条抓到过会让 **CI 挂到 job 超时**的事故）
- [ ] 新增/修改的行为**有测试钉住**，并做过**注入验证**（把修复改坏，确认测试真的会红）
- [ ] 没留下没人测的防御性代码
- [ ] 弹对话框的代码**不会**被程序化调用触发（`QMessageBox.exec()` 会阻塞，
      测试 / CI 会挂死，用 `activated` 这类"只在用户操作时发"的信号，
      别用 `currentIndexChanged` 这类"程序化也会发"的）

### 如果动了打包或启动链路

- [ ] 重新打包过：`python dev/make_installer.py --build`
- [ ] 用 `--probe-script` / `--selftest` **探过产物**，确认修复真的进了包里
      （判据是**产物里的符号**，不是源码里的函数，"改完源码 ≠ 用户拿到修复"）
- [ ] 两个外置组件（cv2 / CLIP 模型）**缺了也能通过自检**
      （跳过而不是判失败）
- [ ] `python dev/check_artifacts.py dist/ImgDedup/ImgDedup.exe` 通过
      （跑 exe 自检 + 扫 `_internal` 里的外置组件 / AGPL 红线）
- [ ] `python dev/check_frozen.py dist/ImgDedup/ImgDedup.exe` 通过
      （读 exe 内嵌的 PYZ 逐项核对符号，它才是"包里是**本次**代码"的判据）

### 三个容易踩的坑

- [ ] **没有手工改行尾**：行尾由 `.gitattributes` 钉住
      （`* text=auto eol=lf`，只有 `*.bat` 是 CRLF），检出时与机器的
      `core.autocrlf` 无关。所以不需要"统一行尾"，也不要把某个文件
      手工改成 CRLF —— 那只会让工作区和别人不一样。
- [ ] 如果动了 `启动ImgDedup.bat`：**它必须保持 GBK(cp936) + CRLF**，
      且**不能用编辑器直接保存**（只能写 UTF-8 会把 cmd 解析搞失步）。
      验证方式：把 `start` 和 `pause` 换成 `echo` 做 dry-run 副本，
      用 cmd 跑、按 GBK 解码，断言 0 条解析错误 + 中文原样出现。
- [ ] **断言没有裸 `assertIn` 一个词**：本仓库的注释比代码还多，
      `assertIn("某个词", 源码)` 很容易命中的是**注释** ——
      于是"把配置删掉、只留注释，测试照样绿"。断言要么锚到带缩进 /
      带调用形式的整行，要么先剥掉整行注释再断言
      （`tests/test_release_pipeline.py` 的 `strip_comments()` 就是干这个的）。

## 相关 issue

<!-- 例：Closes #12 -->
