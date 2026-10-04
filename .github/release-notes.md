# ImgDedup 发布说明（Release 模板）

> 这份文件会被 `release.yml` 作为 `gh release create --notes-file` 的内容，
> 改这里就等于改每个 Release 的说明。

## 下载哪个？

| 你想要 | 下这个 |
|---|---|
| 装到系统里（开始菜单 / 桌面快捷方式 / 可卸载） | `ImgDedup-*-setup.exe` |
| 免安装，解压就用 | `ImgDedup-portable-win64.zip` |
| 要能真正去重（必需） | 再加 `cv2-win64.zip` |
| 要用「深度」语义模式 | 再加 `models-*.zip` |
| 核对下载有没有坏 | `SHA256SUMS.txt` |

解压后用 `ImgDedup.exe` 直接启动，不需要装 Python。

> 想验校验和的话：`Get-FileHash .\cv2-win64.zip -Algorithm SHA256`，
> 和 `SHA256SUMS.txt` 里对应那行比一下即可（两个大件加起来 171 MB，
> 网络不好时值得花这十秒）。

## 两个外置组件

为了让安装包不至于 300 MB+，这两个大件不随包分发，
都要放在 `ImgDedup.exe` 同级的 `extra\` 下面：

> PDF / AI 不需要外置组件：解码走随包的 pypdfium2（BSD-3-Clause /
> Apache-2.0，宽松许可），开箱即用。

### 1. cv2（86 MB），扫描 / 去重必需

解压 `cv2-win64.zip`，把里面的 `cv2` 文件夹整个放进去，让这个文件能被看到：

```
extra\cv2\cv2.pyd
```

### 2. CLIP 模型（85 MB），只有「深度」模式需要

解压 `models-*.zip`，把 `models` 文件夹放进去：

```
extra\models\Xenova__clip-vit-base-patch32\vision_model.onnx
```

### 懒得手动放？

程序里会弹提示，对话框上有「自动下载」和「打开该文件夹」两个按钮。

- 缺 cv2 → 启动时就会提示（它没法做任何扫描）；
- 缺模型 → 只在切到「深度」模式时提示（快速 / 精确两档不需要它）。

## 系统要求

- Windows 10 / 11，64 位
- 无需安装 Python

## 卸载

用安装包装的走「设置 → 应用」正常卸载。
卸载会连同 `extra\` 一起删掉（避免留下孤儿文件），
如果你自己放了 cv2 / 模型，卸载前先拷出来。

## 这个版本干了什么

见下方自动生成的提交列表。
