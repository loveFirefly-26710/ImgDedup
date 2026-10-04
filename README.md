# ImgDedup

Windows 上的本地图片去重工具。它把字节完全相同的副本、缩放或重压缩过的近似图、以及同场景的语义相似图都找出来，按组摆出缩略图，由你逐组裁决。

工具本身不会替你删东西。删除动作只走系统回收站或你指定的目录，每次都会写一份撤销记录。

![主界面](docs/screenshot-main.png)

## 文档导航

| 你想做什么 | 看哪份 |
|---|---|
| 装上用起来 | 就是这份 |
| 自己打包发布 | 这份的「打包与发布」一节 |
| 改这个仓库的代码 | [docs/开发文档.md](docs/开发文档.md) |
| 了解约束与设计取舍 | [PLAN.md](PLAN.md) |
| 改主题配色 | [docs/流萤主题美化设计方案.md](docs/流萤主题美化设计方案.md) |
| 发布二进制、核对许可 | [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) |

## 它解决什么问题

图库攒久了，同一张图往往有好几份：微信存过一遍，截图存过一遍，相机导入又存了一遍。有的是原图，有的是发朋友圈前压过的，有的是裁掉水印的。按文件名找不到它们，按修改时间也找不到。

现有的工具大致分两类，都不太敢用。一类只比哈希，能找出字节完全一样的副本，但压过的同一张图一个都发现不了；另一类号称智能清理，直接替你勾一堆待删项，甚至默认自动清理。图库删错一次就是不可逆的损失，没人敢按那个按钮。

ImgDedup 走第三条路：扫描尽量全，删除由你决定。它把差异摆出来，给一个推荐保留项，你逐组确认；真正动手删除时只走回收站或你指定的目录，并留下撤销记录。

## 三档扫描模式

工具栏里选档位。档位决定花多少算力、能发现什么。

| 档位 | 比什么 | 能发现 | 漏掉 | 代价 |
|---|---|---|---|---|
| 快速 | 文件大小加头尾各 64 KB 指纹（合计 128 KB） | 字节完全相同的副本 | 重压缩、改过尺寸、加过水印的图 | 秒级，CPU 只用来读几千字节 |
| 精确（默认） | 快速档加全量哈希加感知特征（pHash / dHash / 颜色直方图） | 上述全部，再加缩放、轻微调色、重编码后仍相似的图 | 构图相似但画面内容不同的图 | 每张图解码一次到 256 px |
| 深度 | 精确档加 CLIP 语义向量余弦聚类 | 上述全部，再加同场景、同主体的不同画面 | 语义模型理解不了的抽象相似 | 每张图多跑一次神经网络编码 |

档位旁边常驻一行说明，写的就是「能抓到什么、会漏掉什么」，切档时跟着变。

深度档需要 CLIP 模型（见下面的「两个外置组件」）。模型不在位时，切到这一档会弹框问要不要现在下载；选「暂不下载」的话，档位会拨回你上一次选的那一档，不会带着一个跑不了的档位开始扫描。

## 结果分四类

| 类型 | 含义 |
|---|---|
| 精确重复 | 字节级完全一致 |
| 近似重复 | 感知特征接近，通常是同一张图的缩放、调色或重编码版本 |
| 语义相似 | CLIP 余弦相似度超过阈值 |
| 聚类（过合并） | 连通分量规模超过上限，单独隔离，不参与批量删除 |

一个文件只会出现在其中一类里。分类按一组内部最弱的那条关系定：组里只要有一对只是「看着像」，整组就算近似重复，不会因为另一对字节相同而升级成精确重复。

字节相同的图必然也满足感知接近。两类如果各算各的，同一批文件要裁决两遍，可释放空间也会重复计入，所以这里把它们并成一组。并组之后一行里可能含好几个字节重复组，角标会写「含 N 组字节重复」。

看到这个角标就说明这一行把只是看着像的图也连进来了。每行默认只留 1 张，所以删之前先逐张看一眼。

## 安装

### 免安装版

1. 从 Releases 下 `ImgDedup-portable-win64.zip`，解压到任意目录；
2. 再下 `cv2-win64.zip`，解压后把里面的 `cv2` 文件夹放进 `ImgDedup.exe` 同级的 `extra\`（见下一节）；
3. 双击 `ImgDedup.exe`。

想核对下载有没有坏，可以对着 Release 里的 `SHA256SUMS.txt` 验一下校验和。

### 安装版

下 `ImgDedup-*-setup.exe` 双击安装。装到 `%LOCALAPPDATA%\Programs\ImgDedup`，开始菜单与桌面会有快捷方式，能在「设置 → 应用」里正常卸载。装的过程不弹 UAC，本工具不需要任何系统级权限。

### 从源码跑

```bash
pip install -r requirements.txt
python -m dev.fetch_model     # 可选，只有要用「深度」档才需要
python -m imgdedup_ui
```

开发机上 cv2 也可以直接 `pip install opencv-python`，省得往 `extra/` 里放。注意打包产物不带它，它是外置组件。

## 两个外置组件

为了让安装包不至于 300 MB 以上，这两个大件不随包分发，都要放在 `ImgDedup.exe` 同级的 `extra\` 下。PDF 与 AI 不需要外置组件，解码走随包的 pypdfium2，开箱即用。

### 1. cv2（86 MB），扫描必需

解压 `cv2-win64.zip`，把里面的 `cv2` 文件夹整个放进去，让这个文件能被看到：

```
extra\cv2\cv2.pyd
```

### 2. CLIP 模型（85 MB），只有「深度」档需要

解压 `models-*.zip`，把 `models` 文件夹放进去：

```
extra\models\Xenova__clip-vit-base-patch32\vision_model.onnx
```

### 懒得手动放

程序里会弹提示，对话框上有「自动下载」和「打开该文件夹」两个按钮。缺 cv2 在启动时就提示，缺模型只在切到「深度」档时提示。

## 怎么用

1. 选扫描源：工具栏最左边选目录，子目录会一起扫。
2. 选档位：快速 / 精确 / 深度。
3. 调阈值：只有「深度」档用得上，默认 0.97。这个值必须按图库调，别往低调。
4. 开始：底部是多段进度条，每个阶段一段；扫描期间可以暂停。
5. 审阅：中间网格按组展示，点缩略图切换「待删 / 未裁决」，右栏按钮设为保留。右栏上半是预览大图（滚轮缩放，最大 16 倍），下半是这张图的完整信息。
6. 过滤：左栏可以只看某一类分组（单选）；左下角的滑块调缩略图大小（64 到 256 px）。
7. 执行删除：底栏「执行删除…」。
8. 删错了：底栏「撤销上次删除」。
9. 留档：底栏「导出报告…」，HTML 给人看，CSV 给表格工具。

## 三条保护规则

左栏「保护规则」这三条默认开启，界面上不提供关闭入口：

- 每组至少保留 1 张：任何一组都不可能被整组清空；
- 硬链接保护：指向同一个 inode 的多个路径不算重复，不参与删除；
- 超大聚类不参与批量删除：规模超过上限的连通分量被隔离出来，只能逐张人工确认。

另外，待删项必须落在本次扫描根之下，越界的会被拦下并记进 `blocked`；撤销时如果原路径已经有文件，报冲突而不是覆盖。

## 数据放在哪

| 位置 | 内容 |
|---|---|
| `%LOCALAPPDATA%\ImgDedup\cache\` | 指纹复用库与缩略图缓存。删掉不影响正确性，只是下次要重算 |
| `%LOCALAPPDATA%\ImgDedup\catalog.db` | 删除方式与目标目录的记忆（SQLite，WAL 模式）。扫描结果不落库 |
| `%LOCALAPPDATA%\ImgDedup\manifests\` | 每次删除写的撤销记录 |
| `%LOCALAPPDATA%\ImgDedup\theme.json` | 上次选的主题 |
| `<ImgDedup.exe 同级>\extra\` | 两个外置组件 |

## 卸载

用安装包装的走「设置 → 应用」正常卸载。卸载会连 `extra\` 一起删掉，避免留下一个 171 MB 的孤儿目录。你自己放了 cv2 或模型的话，卸载前先拷出来。

免安装版直接删目录就行。

## 常见问题

**启动就提示缺 cv2。** 扫描必需，装上才能用。对话框上有「自动下载」，也可以手动解压 `cv2-win64.zip`，放成 `extra\cv2\cv2.pyd`。

**切到「深度」档提示缺模型。** 只有这一档需要它，快速与精确两档不需要。同样可以自动下载；不想下就把档位换回去。

**删掉的文件去哪了？** 默认在系统回收站；选过「移动到指定目录」的话就在那个目录里，原目录结构保留。

**撤销能还原到什么程度？** 底栏「撤销上次删除」按撤销记录还原，还原前校验哈希；目标位置已有同名文件时报冲突，不覆盖。

**十万张图会不会卡？** 网格用 Qt 原生虚拟化，只渲染可见行，控件数与图库规模无关。启动路径上也不导入核心引擎，所以窗口是秒开的。

**「快速」档找不到我明明重复的图。** 它只比文件大小和头尾各 64 KB 的指纹，重压缩过的图字节不同，它看不见。要抓这类图请用「精确」档。

## 项目结构

```
imgdedup_core/    核心引擎（不依赖 Qt）
imgdedup_ui/      PySide6 界面
dev/              开发脚本：打包、取外置组件、探针、截图、门禁
tests/            单元测试（23 个模块，只用标准库 unittest）
resources/        图标；models/ 是外置模型的开发来源，不进 git
docs/             开发文档与主题设计文档
```

核心引擎按扫描顺序分工，每个模块只做一段：

| 模块 | 负责 |
|---|---|
| `scan.py` | 目录遍历、噪声过滤、文件身份、硬链接识别、按大小分桶 |
| `decode.py` | 元数据探测与缩略图；按格式派发到 pyvips / Pillow / pypdfium2 |
| `feature.py` | 短指纹 / 全量哈希、pHash / dHash / HSV 直方图 |
| `group.py` | 并查集与分层合并、推荐保留项 |
| `embed.py` | CLIP 语义向量与余弦聚类 |
| `cache.py` | 指纹复用库与缩略图缓存 |
| `catalog.py` | SQLite 目录库（界面只用 settings 表） |
| `action.py` | 删除路径 A：永不真删，走回收站 / 指定目录 + 撤销记录 |
| `scratch.py` | 删除路径 B：真删，但被白名单限定在 `.scratch/` |
| `opencv.py` | cv2 的定位与惰性加载（全仓唯一 `import cv2` 的地方） |
| `cv2_fetch.py` | 从 PyPI 取 cv2 轮子并抽出到 `extra/` |
| `infra.py` | 格式化、内存闸门、预算、计时与进度 |

界面层：`window.py` 主窗口（三栏 + 删除汇合点）、`model.py` 分组行与决策、`widgets.py` 自绘控件、`theme.py` 主题、`worker.py` 扫描线程、`dialogs.py` 删除方式对话框、`report.py` 导出、`_entry_symbols.py` 启动期杂事、`__main__.py` 入口。

模块职责与依赖方向的完整说明见 [docs/开发文档.md](docs/开发文档.md)。

## 第三方组件与许可

本仓库自己的代码是 [MIT](LICENSE)。打包产物里含第三方组件，其中 PySide6 与 libvips 是 LGPL-3.0，分发二进制时要附上许可全文，并保证用户可以替换这两个库。完整清单、版本、许可类型与分发义务见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，发布二进制前请先读它。

PDF 与 AI 解码用 pypdfium2（BSD-3-Clause / Apache-2.0），随包分发。产物里没有 AGPL 组件。

## 系统要求

- Windows 10 / 11，64 位
- 不需要装 Python，免安装版与安装版都自带运行时

## 打包与发布

Release 页上挂着五个文件：安装包、免安装版 zip，加上两个外置组件的 zip 和一份校验和。前两个是本工具本体，自己打包也只需要这两个。

前置：

```bash
pip install -r requirements-dev.txt       # 里面有 PyInstaller
winget install --id JRSoftware.InnoSetup  # 只有要编安装包才需要
```

打包前先把正在运行的 `ImgDedup.exe` 关掉。它锁着产物时，PyInstaller 的 COLLECT 可能产出一个新旧文件混在一起的目录。

四种跑法，按你要什么挑：

```bash
python -m dev.make_installer --build      # 两个形态都出：先免安装版，再安装包
python -m dev.make_installer --portable   # 只出免安装版，不编安装包
python -m dev.make_installer              # 只编安装包，要求免安装版已存在
python -m dev.make_installer --check      # 只体检已有产物，不重新编译
```

`--build` 与 `--portable` 互斥。每条路的退出码都是体检结论，0 才算过，所以脚本里可以直接拿它当判据。

`--portable` 之后的体检只看免安装版那一半，不会因为「安装包不存在」判失败。没装 Inno Setup 的机器上就用它验证免安装版。

| 形态 | 产物 | 怎么用 |
|---|---|---|
| 免安装版 | `dist\ImgDedup\` | 整个文件夹拷到哪都能跑，不写注册表 |
| 安装版 | `dist\installer\ImgDedup-<版本>-setup.exe` | 双击安装，能在「应用和功能」里卸载 |

版本号读的是 `imgdedup_core/__init__.py` 里的 `__version__`，改版本只改那一处。

`--check` 只看图标嵌进去没有、体积对不对。要更实一层，拿 `python dev/check_artifacts.py` 跑一遍产物自检：它会启动 exe、核对缺 cv2 与缺模型时的提示、扫包里有没有混进 AGPL 组件。

两个会绊人的地方：

`--build` 内部带 `--clean` 重跑 PyInstaller，`dist\ImgDedup\extra\` 跟着被清掉重建。你之前把 cv2 放进去了的话，得再放一次。

免安装版的 zip 和 `SHA256SUMS.txt` 不是这个脚本产的。它们由 [.github/workflows/release.yml](.github/workflows/release.yml) 在发版时生成；本地想自己压，把 `dist\ImgDedup\` 压成 zip 就行。

发版不用打标签：改掉 `imgdedup_core/__init__.py` 里的 `__version__`、推上 `master` 就行。release.yml 会先跑一遍测试，再打包、下载两个外置组件、算校验和，按版本号建出 `v<版本>` 标签，最后把五个文件挂到 Release 上。本机不需要装 Inno Setup。

版本号没动的话这一步会跳过。一个版本号只发一次，否则同一个 `1.0.0` 会对应上两份不同的二进制，`SHA256SUMS.txt` 也就没意义了。想重发同一个版本，去 Actions 页手动跑一次 Release workflow 即可。

## 参与开发

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -p "test_*.py"   # 全量测试
python dev/check_no_network.py          # 门禁：测试不许依赖网络
python -m imgdedup_ui --selftest        # 核心引擎 / 界面 / 底层库 / 外置组件能否加载
QT_QPA_PLATFORM=offscreen python -m imgdedup_ui --interact-test   # 界面交互冒烟
python -m pyflakes imgdedup_core imgdedup_ui dev tests             # 静态检查
python dev/check_deps.py                # 依赖声明与第三方组件表是否一致
python dev/check_fresh_clone.py         # 按 git 的口径在干净副本里再跑一遍
python -m dev.make_installer --build
```

测试在 `tests/` 下，23 个模块、443 项断言。它不弹窗口、不需要图库、不需要模型、也不联网：离屏跑，用 Pillow 现场生成小图，外置组件缺失时相关用例自动跳过。

提交前过一遍 [.github/pull_request_template.md](.github/pull_request_template.md) 的自查清单。编码约定、质量门禁和常见改动怎么做，都在 [docs/开发文档.md](docs/开发文档.md)，动手之前值得看一遍。
