# GalTranslPP 编译指南

## 1. 环境

- **操作系统**：Windows 10 或 Windows 11。适配其它系统需要自行修改代码和构建脚本。
- **需要手动安装的工具**：[git](https://git-scm.com/)、[xlings](https://github.com/openxlings/xlings)，以及带「使用 C++ 的桌面开发」工作负载的
  [Visual Studio Build Tools](https://visualstudio.microsoft.com/zh-hans/downloads/#build-tools-for-visual-studio-2026)（vcpkg 与 ElaWidgetTools 用它编译依赖）。

其余依赖由构建自动取得，不需要手动安装或配置路径：

| 依赖 | 来源 |
|---|---|
| mcpp、LLVM 工具链 | xlings |
| Qt 6.11.1（MSVC 2022 64-bit） | `mcpp:plugins` 的 `rules-qt-xim`（xim:qt） |
| vcpkg 与 `vcpkg.json` 中的库 | `mcpp:plugins` 的 `deps-vcpkg`（xim:vcpkg），首次构建时安装 |
| ElaWidgetTools | `mcpp:plugins` 的 `deps-cmake`，由构建按子模块源码编译 |
| 7z.dll | xim:7zip |

安装 xlings，重启终端后安装 mcpp：

```powershell
irm https://d2learn.org/xlings-install.ps1.txt | iex
```

```cmd
xlings install mcpp -y
```

## 2. 拉取源码

```cmd
git clone --recursive https://github.com/julixian/GalTranslPP.git
cd GalTranslPP
```

## 3. 编译

```cmd
mcpp build -p GPPCLI
mcpp build -p GPPGUI
```

首次构建会下载 Qt、安装 vcpkg 依赖并编译 ElaWidgetTools，耗时较长；vcpkg 的二进制缓存位于
`%LOCALAPPDATA%\vcpkg\archives`，之后的构建复用它。默认的 release profile 使用最高优化等级；`fast-release`
profile 编译更快：

```cmd
mcpp build -p GPPCLI --profile fast-release
mcpp build -p GPPGUI --profile fast-release
```

## 4. 运行

构建产物位于 `Release\` 目录，Qt 运行库、Qt 插件、ElaWidgetTools、vcpkg 运行库、7z.dll 与翻译文件已随程序放置。
还需要：

1. 将 `Example\BaseConfig` 中的 `Python-3.12.10-embed-amd64.zip` 解压到同一文件夹；
2. 运行项目根目录下的 `Release.py`，复制配置数据与 opencc 数据。
