# Hikari-LLVM

Hikari-LLVM 是面向 C、C++、Objective-C 和 Objective-C++ 的 LLVM 代码混淆工具，提供字符串与常量保护、控制流变换、指令替换，以及 Darwin 平台的反调试和反 Hook 检查。

本仓库保留 Hikari 的 LLVM 源码，并在 `fix/arm64e-llvm22` 分支提供 **LLVM 22.1.8 独立插件与 Apple 编译入口**，用于 macOS / iOS 项目的 arm64、arm64e 代码生成。默认文档使用中文。

## 选择构建方式

| 方式 | 适用场景 | LLVM 版本 |
| --- | --- | --- |
| 独立插件 + Apple 前后端 | 复用 Xcode 的 SDK、Objective-C 与 arm64e / PAC 支持 | 插件固定使用 LLVM 22.1.8 |
| 编译仓库中的 LLVM / Clang | 需要自行维护完整混淆编译器 | 仓库内 LLVM 源码为 18.1.8 |

独立插件运行在构建主机上，不能直接加载到 Apple Clang，也不能与其它版本的 LLVM 混用。构建插件不会自动替换 Xcode 或 Theos 工具链，项目需要配置编译入口。

## 快速开始：Apple 编译流程

需要 macOS、Xcode 命令行工具、Python 3.10 或更新版本、CMake、Ninja 和 LLVM 22.1.8。以下路径使用 Apple Silicon 上的 Homebrew 布局。

```sh
git clone --branch fix/arm64e-llvm22 https://github.com/5hux1n/Hikari-LLVM.git
cd Hikari-LLVM

cmake -S hikari -B cmake-build-arm64e -G Ninja \
  -DLLVM_DIR=/opt/homebrew/opt/llvm/lib/cmake/llvm \
  -DCMAKE_C_COMPILER=/opt/homebrew/opt/llvm/bin/clang \
  -DCMAKE_CXX_COMPILER=/opt/homebrew/opt/llvm/bin/clang++ \
  -DCMAKE_BUILD_TYPE=Release
cmake --build cmake-build-arm64e --parallel 1
```

产物为 `cmake-build-arm64e/libHikari.so`。编译入口依次执行：

```text
Apple Clang 前端 → LLVM 22 Hikari 混淆 → IR 校验与兼容处理 → Apple Clang 后端
```

链接仍交给 Apple 工具链。前端接受工程原有的优化参数，**Apple 后端默认使用 `-O0`**，减少混淆表达式被再次简化；可用 `HIKARI_APPLE_OPT=1` 或 `2` 做对照。

### 只保护关键函数

默认不启用全模块混淆。通过 `HIKARI_PROTECT` 标记需要保护的函数：

```c
#include "Hikari.h"

HIKARI_PROTECT int request_decision(int value) {
  if (value < 0) return 0;
  return value * 3 + 7;
}
```

该标记选择 BCF、CFF、间接分支、指令替换、字符串和常量保护，并保留 `noinline` / `optnone` 属性。不适合 CFF 的异常路径和特殊终结指令会跳过展平。

将示例保存为 `input.c`，从仓库根目录编译：

```sh
HIKARI_REPORT_DIR="$PWD/cmake-build-arm64e/reports" \
  python3 hikari/hikari-clang.py -O2 -I"$PWD/hikari" \
  -c input.c -o cmake-build-arm64e/input.o
```

编译 iOS 项目时继续传入工程自身的 `-target`、`-isysroot` 和 ARC 等参数；Theos 工程将编译入口指向 `hikari/hikari-clang.py`。

需要额外保护整个编译单元的字符串时，可显式设置：

```sh
export HIKARI_FLAGS="-enable-strcry -strcry_prob=100"
```

`HIKARI_GUARD` 可叠加在关键函数上，选择反 Hook 与反符号重绑定检查。检查失败会终止宿主进程，应根据实际方法范围启用。

### 配置与构建重放

| 环境变量 | 作用 / 默认值 |
| --- | --- |
| `HIKARI_APPLE_CLANG` | Apple Clang 路径，默认由 `xcrun --find clang` 解析 |
| `HIKARI_LLVM_BIN` | LLVM 工具目录，默认 `/opt/homebrew/opt/llvm/bin` |
| `HIKARI_PLUGIN` | 插件路径，默认仓库内的 `cmake-build-arm64e/libHikari.so` |
| `HIKARI_APPLE_OPT` | Apple 后端优化等级：`0`、`1`、`2`；默认 `0` |
| `HIKARI_FLAGS` | 以空格分隔的混淆参数；默认空，不写 `-mllvm` 前缀 |
| `HIKARI_REPORT_DIR` | 每个目标文件的 JSON 和日志目录；默认 `/tmp/hikari-reports` |
| `HIKARI_SEED` | 用于重放的 64 位无符号种子；省略时生成新种子 |
| `HIKARI_KEEP_IR` | 设为 `1` 时保留输入和兼容处理后的混淆 IR |

报告记录编译器版本、种子、参数、插件及产物哈希和执行结果。正式构建省略固定种子，并按构建归档报告；故障重放需要相同的输入、参数、编译器和插件。布局生成器仍是 `mt19937_64`，系统随机播种不等于密码学随机生成器。

完整说明见 [Apple 后端与关键函数保护](hikari/PROTECTION.md) 和 [LLVM 22 / arm64e 兼容说明](hikari/COMPATIBILITY.md)。

## 混淆功能

以下是显式启用开关。使用 Apple 包装脚本时放入 `HIKARI_FLAGS`；直接调用支持插件的 LLVM Clang 时，每个参数前加 `-mllvm`。

| 参数 | 功能 |
| --- | --- |
| `-enable-strcry` | 字符串加密 |
| `-enable-constenc` | 常量加密 |
| `-enable-cffobf` | 控制流展平（CFF） |
| `-enable-bcfobf` | 虚假控制流（BCF） |
| `-enable-indibran` | 间接分支 |
| `-enable-splitobf` | 基本块拆分 |
| `-enable-subobf` | 指令替换 |
| `-enable-funcwra` | 函数调用包装 |
| `-enable-fco` | 函数调用混淆，需要符号配置 |
| `-enable-acdobf` | 反类信息导出（iOS / macOS） |
| `-enable-antihook` | 反 Hook（iOS / macOS） |
| `-enable-adb` | 反调试（iOS / macOS） |
| `-enable-allobf` | 启用全部混淆，包括反调试、反 Hook 和常量加密 |

推荐先选择关键函数，再根据最终产物调整参数。`-enable-allobf` 会扩大防护范围；反调试会影响整个宿主进程的调试行为。

### 常用参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-bcf_prob` | `70` | 基本块应用 BCF 的概率（%） |
| `-bcf_loop` | `1` | BCF 迭代次数 |
| `-bcf_cond_compl` | `3` | BCF 条件表达式复杂度 |
| `-bcf_junkasm` | `false` | 在虚假块中插入垃圾汇编 |
| `-bcf_onlyjunkasm` | `false` | 仅插入垃圾汇编 |
| `-bcf_junkasm_minnum` / `-bcf_junkasm_maxnum` | `2` / `4` | 垃圾汇编条数范围 |
| `-bcf_createfunc` | `false` | 为不透明谓词生成包装函数 |
| `-fw_prob` / `-fw_times` | `30` / `2` | 调用包装概率（%）与迭代次数 |
| `-strcry_prob` | `100` | 字符串各元素应用加密的概率（%） |
| `-constenc_times` | `1` | 常量加密迭代次数 |
| `-constenc_togv` / `-constenc_togv_prob` | `false` / `50` | 常量转全局变量及概率（%） |
| `-constenc_subxor` / `-constenc_subxor_prob` | `false` / `40` | XOR 替换及概率（%） |
| `-indibran-use-stack` | `true` | 从栈读取跳转表地址 |
| `-indibran-enc-jump-target` | `false` | 编码跳转表及索引 |
| `-sub_prob` / `-sub_loop` | `50` / `1` | 指令替换概率（%）与迭代次数 |
| `-split_num` | `2` | 基本块拆分数量配置 |
| `-ah_inline` | `true` | 启用反 Hook 时检查内联 Hook |
| `-ah_objcruntime` | `true` | 启用反 Hook 时校验 Objective-C IMP 与同模块消息调用点 |
| `-ah_antirebind` | `false` | 启用反 Hook 时额外检查符号重绑定 |
| `-adb_prob` | `40` | 额外函数入口反调试检查比例（%）；加载时初始化始终存在 |
| `-aesSeed` | 系统随机播种 | 直接调用插件时指定种子；包装脚本使用 `HIKARI_SEED` |

字符串在首次使用时按需解密，明文保持原有静态存储期；通用 pass 不会在函数退出时清零仍可能被其它代码引用的字符串。完整边界和配置要求见上方专题文档。

## 评估最终产物

用相同输入和种子比较 Apple 后端 `-O0`、`-O1`、`-O2`，将各目标文件链接为最终 Mach-O 后检查：

- `__TEXT,__text` 大小、条件分支与间接跳转数量。
- 明文字符串残留，以及关键函数的实际反编译结果。
- 返回值、异常路径和并发首次调用。
- iOS 设备上的启动、后台切换及 arm64e / PAC 执行。

压缩包体积不能代表混淆强度，也不能代替解包后的二进制检查。变换会增加体积和运行开销，应按关键逻辑的实际表现选择配置。

## 完整 LLVM / Clang 构建

需要完整编译器时，从仓库根目录执行：

```sh
cmake -S llvm -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_PROJECTS="clang" \
  -DLLVM_TARGETS_TO_BUILD="X86;AArch64;ARM"
cmake --build build --parallel 1
```

这是仓库内 LLVM 18.1.8 源码的构建方式，独立插件的 LLVM 22 构建入口仍是 `hikari/CMakeLists.txt`。LLVM 的其它组件与贡献方式见 [LLVM 文档](https://llvm.org/docs/)。

## 来源、反馈与许可证

本项目源自 **61bcdefg / NeHyci** 维护的 Hikari-LLVM15，继承 **Naville / Zhang** 的 Hikari 项目，并基于 LLVM 编译器基础设施。保留的历史项目入口：[Hikari-LLVM15](https://github.com/61bcdefg/Hikari-LLVM15)、[Hikari](https://github.com/HikariObfuscator/Hikari)。

使用问题可在[当前仓库 Issues](https://github.com/5hux1n/Hikari-LLVM/issues) 反馈，附上分支、编译器版本、目标架构和最小复现。

许可证与署名要求见 [LICENSE.TXT](LICENSE.TXT) 和 [NOTICE](NOTICE)。
