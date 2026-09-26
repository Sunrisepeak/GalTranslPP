# GalTranslPP 编译指南

## 1. 环境

- **操作系统**：Windows 10 或 Windows 11。适配其它系统需要自行修改代码和构建脚本。
- **需要手动安装的工具**：[git](https://git-scm.com/)、[xlings](https://github.com/openxlings/xlings)，以及带「使用 C++ 的桌面开发」工作负载的
  [Visual Studio Build Tools](https://visualstudio.microsoft.com/zh-hans/downloads/#build-tools-for-visual-studio-2026)（vcpkg 与 ElaWidgetTools 用它编译依赖）。

其余依赖由构建自动取得，不需要手动安装或配置路径：

| 依赖 | 来源 |
|---|---|
| mcpp、LLVM 工具链 | xlings |
| Qt 6.11.1（官方 msvc2022_64 预编译包，MSVC ABI；mcpp 默认的 clang 工具链直接使用） | 各成员 `mcpp.toml` 声明的 `xim:qt-base`（qtbase、qttools 与 qttranslations），由 `mcpp:plugins` 的 `rules-qt` 使用 |
| vcpkg 与 `vcpkg.json` 中的库 | `mcpp:plugins` 的 `deps-vcpkg`（xim:vcpkg），首次构建时安装 |
| ElaWidgetTools | `mcpp:plugins` 的 `deps-cmake`，由构建按子模块源码编译 |
| 7z.dll | xim:7zip |
| 嵌入式 Python 环境、OpenCC 数据、`BaseConfig` 与 `SampleProject` | 构建从 `Example/` 与 vcpkg 前缀取得，放在程序旁 |

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

```cmd
mcpp run -p GPPCLI
mcpp run -p GPPGUI
```

程序旁已放置运行所需的全部文件：Qt 运行库与插件、Qt 自身的中文翻译、ElaWidgetTools、vcpkg 运行库、7z.dll、
项目翻译文件，以及 `BaseConfig`（嵌入式 Python 环境由 `Example/BaseConfig` 中的压缩包在构建时解出，OpenCC 数据
来自 vcpkg 前缀）。release 与 fast-release profile 同时写出 `Release\GPPCLI`、`Release\GPPGUI` 与
`Release\GUICORE` 三个发布目录；`mcpp pack --format dir` 在成员目录下运行，产出可分发的目录。
