; ============================================================================
; ImgDedup 安装包脚本（Inno Setup 6）
;
; 产出：`dist/installer/ImgDedup-<版本>-setup.exe`
;
; ---- 这个脚本管什么 ----
; 把 `dist/ImgDedup/`（PyInstaller 的 onedir 产物）整个装到用户可写目录，
; 建开始菜单 + 桌面快捷方式（带图标），并注册标准的卸载项。
;
; ---- 为什么装到 LocalAppData 而不是 Program Files ----
; 装到 `Program Files` 需要管理员权限（会弹 UAC），而本软件不需要任何
; 系统级权限：数据都写在用户目录，不注册服务、不写 HKLM。
; 用 `{localappdata}\Programs\ImgDedup` 是 per-user 安装，
; 全程不弹 UAC，装完当前用户就能用。
; （代价：换用户要重装。对本工具这个取舍是划算的。）
;
; ---- 编译 ----
;   ISCC.exe imgdedup.iss
; 或由 `dev/make_installer.py` 封装（会先校验 dist 存在、再自动带版本号）。
;
;  路径里的 `\` 在 Inno 里是普通字符，不用转义；但字符串里的引号要写两遍。
;  `{#Var}` 是编译期常量替换，`{app}` 是运行期目录常量，两者别混。
; ============================================================================

#define AppName "ImgDedup"
#define AppExeName "ImgDedup.exe"

; 版本号由 dev/make_installer.py 通过 /DAppVersion= 传入；单独跑 ISCC 时给默认值。
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

; ---- 路径：一律用绝对路径，由 ISCC 的 /D 传入 ----
;
;  不要依赖 `..\` 这类相对路径。Inno 在编译的不同阶段 cwd 并不一致，
; 实测 `OutputDir=..\dist\installer` 被解析成了 `E:\Develop\items\dist\installer`
; （少了一层 ImgDedup），而 `SetupIconFile=..\resources\icon.ico` 直接
; 报"系统找不到指定的路径"。
; 用 `/DSourceDir=...` 显式传绝对路径，行为就和调用方的工作目录无关了。
#ifndef SourceDir
  #define SourceDir "..\dist\ImgDedup"
#endif
#ifndef IconFile
  #define IconFile "..\resources\icon.ico"
#endif
#ifndef OutDir
  #define OutDir "..\dist\installer"
#endif

[Setup]
AppId={{8F1B0C6E-2A4D-4B7E-9C31-6D5A2E7F4A10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=ImgDedup
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
; 卸载时不要顺手删掉用户可能存在里面的数据 —— 只删自己装进去的
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}
; 程序自己的图标
SetupIconFile={#IconFile}
; 只允许非管理员安装（不弹 UAC）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; 压缩：lzma2/max 体积最小；SolidCompression 让整体再小一截
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 产物命名
OutputDir={#OutDir}
OutputBaseFilename={#AppName}-{#AppVersion}-setup

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
; 整个 onedir 产物一起装。`recursesubdirs` 保证 `_internal\` 全带上。
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 开始菜单（卸载项由 Inno 自动加，不用手写）
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; 装完给个"立即运行"的勾选。`skipifsilent` 让静默安装时不自动弹窗。
Filename: "{app}\{#AppExeName}"; Description: "立即运行 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
;  只清 PyInstaller 运行期可能留下的缓存目录。
; 绝不删 `{app}` 本身之外的用户数据（本程序把数据写在用户自己的目录下，
; 不在安装目录里），所以这里保持最小化。
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
;  `extra\` 也必须清 —— 用户会往里面放自己下载的外置组件
; （cv2 86 MB / CLIP 模型 85 MB，最多 171 MB），
; 那是 Inno 没安装过的文件，卸载器本来不认识它。
; 不写这一条的话，卸载完会剩一个巨大的孤儿目录，用户会以为没卸干净。
; （实测过：卸载后 `extra\cv2\` 里 103 个文件原封不动地留着。）
;
; 取舍：这样"卸载很干净"，代价是重装后要重新下载组件。
; 用户想留着的话，卸载前把 extra\ 拷出来（README 里写了）。
Type: filesandordirs; Name: "{app}\extra"
