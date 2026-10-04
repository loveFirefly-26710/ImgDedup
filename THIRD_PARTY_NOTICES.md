# 第三方组件声明

本仓库自己的代码是 [MIT](LICENSE)。打包产物里含下面这些组件，
其中 PySide6 与 libvips 是 LGPL-3.0，分发二进制时需附许可全文
并允许用户替换这两个库。没有 AGPL 组件：PDF / AI 解码用宽松许可的
`pypdfium2`（BSD-3-Clause / Apache-2.0，见第三节）。

下面按「随包分发」「外置」两类，列出每个组件的许可类型、
分发时的义务，以及许可全文从哪里取。

## 一、随包分发的组件

版本以 `requirements.txt` 里的 pin 为准。`requirements-dev.txt` 只多出构建与
测试用的 `pyinstaller` / `pyflakes`，它们不进发布产物，所以不在下表里。

改依赖版本时回来同步这张表。`dev/check_deps.py` 会校验它与
`requirements.txt` 是否对得上，`tests/test_packaging.py::TestDependencyNotices`
与 `dev/check_deps.py`
在 CI 里跑这条检查，漂移了会红。

| 组件 | 版本 | 许可 | 全文 |
|---|---|---|---|
| PySide6（Qt for Python） | 6.11.2 | LGPL-3.0（或 GPL / 商业） | https://www.gnu.org/licenses/lgpl-3.0.txt |
| libvips（经 `pyvips-binary` 分发） | 8.18.6 | LGPL-3.0-or-later | https://www.gnu.org/licenses/lgpl-3.0.txt |
| pyvips-binary | 8.18.6 | LGPL-3.0-or-later | https://github.com/libvips/pyvips/blob/master/LICENSE |
| numpy | 2.2.5 | BSD-3-Clause | https://numpy.org/doc/stable/license.html |
| Pillow | 12.3.0 | MIT-CMU | https://github.com/python-pillow/Pillow/blob/main/LICENSE |
| pyvips | 3.2.0 | MIT | https://github.com/libvips/pyvips/blob/master/LICENSE |
| onnxruntime | 1.30.0 | MIT | https://github.com/microsoft/onnxruntime/blob/main/LICENSE |
| psutil | 7.2.2 | BSD-3-Clause | https://github.com/giampaolo/psutil/blob/master/LICENSE |
| xxhash | 4.0.1 | BSD-2-Clause | https://github.com/ifduyue/python-xxhash/blob/master/LICENSE |
| Send2Trash | 2.1.0 | BSD-3-Clause | https://github.com/arsenetar/send2trash/blob/master/LICENSE |
| pypdfium2（PDF / AI 解码） | 5.13.0 | BSD-3-Clause / Apache-2.0 | https://github.com/pypdfium2-team/pypdfium2/blob/main/LICENSES/ |

### LGPL-3.0（PySide6 / libvips / pyvips-binary）的义务

1. 附上 LGPL-3.0 全文（链接见上表），并说明用的是哪个版本；
2. 声明它们来自哪里（Qt for Python / libvips 的官方发布）；
3. 不阻止用户替换这些库，PyInstaller 打的是独立 DLL，
   用户可以自行替换 `_internal/` 下的对应文件，这条天然满足。

### 宽松许可（MIT / BSD / Apache-2.0）的义务

保留版权声明与许可声明即可。上面的表格就是这份声明，
把它随产物一起分发（或放进「关于」对话框）就算履行。

> `pypdfium2` 还会带上它自己那批依赖的许可文件（PDFium、freetype、icu、
> libjpeg-turbo、libpng、libtiff、zlib 等），都在
> `site-packages/pypdfium2-*.dist-info/licenses/` 下。这些许可文件里
> 没有任何 copyleft 义务，只有 ICU 与 LLVM 例外条款里提到 GPL
> 兼容性，那是豁免（允许与 GPL 组合），不是限制。

## 二、不随包分发的组件（外置）

这两个由用户自行获取，放在 `<exe 同级>/extra/` 下。
仓库与 Release 不把它们打进产物（但会把压缩包作为独立附件提供）。

| 组件 | 版本 | 许可 | 说明 |
|---|---|---|---|
| cv2（OpenCV） | 不 pin，见下 | Apache-2.0 | `extra/cv2/`，扫描 / 去重必需 |
| CLIP 模型 `Xenova/clip-vit-base-patch32` | — | MIT（源自 OpenAI CLIP） | `extra/models/`，只有「深度」模式需要 |

这两个都不 pin 版本，因为它们是发布时才取的：

- cv2 由 `dev/fetch_cv2.py` 或程序里的「自动下载」从 PyPI 取
  `opencv-python` 的最新 wheel（逻辑在 `imgdedup_core/cv2_fetch.py`），
  拿到哪个版本取决于发布当天的 latest，所以在上表里写死一个版本号没有意义。
  开发机上 `pip install opencv-python` 装到的版本可能不同（本机是 4.10.0）。
  这不影响许可判断，OpenCV 一直是 Apache-2.0。
- CLIP 模型是权重文件，没有版本号可言，取的是
  `Xenova/clip-vit-base-patch32` 当前的主分支内容。

见 README 的「两个外置组件」一节。

## 三、关于 PDF 解码：为什么选 pypdfium2

PDF / AI 解码用 `pypdfium2`，底层是 PDFium（Chrome 的 PDF 引擎），
许可宽松（BSD-3-Clause / Apache-2.0）。三条理由：

- 许可干净。本项目按 MIT 分发，产物里不能混进强 copyleft（AGPL）的
  组件：那会让整个组合作品都得跟着改许可。
- 随包分发。它只有 8 MB，PDF / AI 开箱可用，用户不用自己放任何
  东西，外置组件保持在两个（cv2 与 CLIP 模型）。
- 真能渲染。缩略图要把 PDF 渲成位图，只能读文本与元数据的库不满足。

> 选型时对比过的其它方案：`pypdf` / `pdfplumber` 只能读文本与元数据、
> 做不出缩略图；`pdf2image` 自己 MIT 但依赖 poppler（GPL-2/3），
> copyleft 比 LGPL 还严；Ghostscript 是 AGPL。能渲染又是
> 宽松许可的，实际上就 `pypdfium2` 一个。

## 四、怎么拿到各家的完整许可文本

已安装的 Python 包里通常只带简短声明，不带全文
（PySide6 只带一份商业许可说明，正文里的 LGPL 全文要另找；
`pypdfium2` 是个例外，它把全套依赖许可都带齐了）。
所以要拿全文，请按上表的链接从各项目官方仓库取，
或到对应的 `site-packages/*.dist-info/licenses/` 下看看有没有附带的副本。

发布前建议把需要的全文整理到一个 `THIRD_PARTY_LICENSES/` 目录里
随产物分发。
