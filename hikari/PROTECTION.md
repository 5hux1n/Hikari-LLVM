# Apple 后端与关键函数保护

## 使用入口

先按 [COMPATIBILITY.md](COMPATIBILITY.md) 构建插件，然后把本目录的 hikari-clang.py 用作编译入口。
此脚本需要 Python 3.10 或更新版本。
它调用 Apple 前端、LLVM 22 Hikari 插件和 Apple 后端，链接仍交给 Apple。
默认后端是 -O0；前端继续接受工程原有优化参数。
现有项目需切换到这个入口才会采用新流程。

默认只处理函数标记，不隐式打开全模块混淆。C/C++/Objective-C 的关键函数可以写成：

```c
#include "Hikari.h"

HIKARI_PROTECT int request_decision(int value) {
  if (value < 0) return 0;
  return value * 3 + 7;
}

// 反 Hook 与反重绑定另行选择，仅用于需要保护的方法。
HIKARI_PROTECT HIKARI_GUARD int guarded_decision(int value) {
  return request_decision(value);
}
```

编译时将仓库的 `hikari` 目录加入头文件搜索路径，例如 `-I"$PWD/hikari"`。

HIKARI_PROTECT 选择 BCF、CFF、间接分支、指令替换、字符串与常量保护，
并在前端和 Hikari 阶段保留 noinline/optnone。
不适合 CFF 的异常处理和特殊终结指令仍会跳过 CFF；不是声称所有函数都已展平。
已有的逐 pass annotate 标记继续可用；hikari_preserve 单独控制后续优化保留。
HIKARI_GUARD 选择现有反 Hook 和反重绑定检查，不在整个模块上默认开启。

## 构建参数和重放

| 环境变量 | 用途 |
| --- | --- |
| HIKARI_APPLE_CLANG | Apple clang 路径；默认 xcrun --find clang |
| HIKARI_LLVM_BIN | LLVM 22 工具目录，默认 /opt/homebrew/opt/llvm/bin |
| HIKARI_PLUGIN | 插件路径，默认 cmake-build-arm64e/libHikari.so |
| HIKARI_APPLE_OPT | 后端 0、1、2，默认 0 |
| HIKARI_FLAGS | 明确需要的 -mllvm 参数，用空格分隔；默认空 |
| HIKARI_REPORT_DIR | 每次构建独立指定报告目录，默认 /tmp/hikari-reports |
| HIKARI_SEED | 故障重放的 64 位种子；正常构建省略以生成新种子 |
| HIKARI_KEEP_IR | 设为 1 时保存输入和兼容后的混淆 IR |

每个目标文件都有日志和 JSON，保存根种子、由输入 IR 派生的模块种子、
编译器版本、插件/IR/目标文件 SHA-256、后端优化等级、命令和成功状态。
重放须使用同一输入、参数、编译器和插件；临时 IR 文件名不是稳定身份。
正式构建不要复用测试中的固定种子；报告目录按构建归档，避免同一目标的旧报告被覆盖。

直接调用插件时，省略 -aesSeed 会从系统随机源取种子，打印实际种子；
显式的 0 和旧默认值 0x1337 现在也可确定性重放。
布局生成器仍是 mt19937_64，系统随机播种不把它变为密码学随机生成器。

## 修复内容

- CFF 在改 CFG 前处理 PHI，用 volatile dispatcher 状态减少后续优化消除。
- 间接跳转统一命名 blockaddress 目标，修复 Apple 读取混合命名/编号块时的目标错配；
  修正块重排，并以 volatile 读取跳转表，保留最终间接跳转。
- 降栈前删除不再符合 LLVM 22 alloca 约束的 lifetime 提示；
  Apple 交接时移除新版单参数 lifetime 提示，避免旧优化器崩溃。
- 常量操作数使用 volatile 密钥读取，避免立即数 XOR 在后端直接折回明文。
- 字符串按构建生成逐元素密钥及每条字符串的加数和旋转量，使用 add/rotate/xor 编码，
  运行时读取密文及加数，避免后端把解密直接折成静态明文。
- 每条字符串使用共享的 0/1/2 原子初始化状态，避免同函数并发及跨函数共享字符串重复写入。
- 字符串处理按插入顺序遍历，避免地址散列顺序破坏固定种子的重放。

字符串在引用它的函数首次运行时解密，明文保留原来的静态存储期。
字符串指针可以被返回、缓存或交给 Objective-C，因此通用 pass 不能在函数退出时直接清零；
需要及时清除的秘密必须由业务层提供明确的使用范围和所有权。
函数级字符串保护仍保留对跨函数共享常量的适用性过滤，未保护的使用者可能使原明文留在二进制中。
这些变换提高分析成本，不提供隐藏运行时密钥或不可静态恢复的保证。

## 后端优化等级对照

从仓库根目录执行，以同一输入和种子比较 Apple 后端的三个优化等级：

```sh
for level in 0 1 2; do
  HIKARI_APPLE_OPT="$level" HIKARI_SEED=12345 \
    HIKARI_REPORT_DIR="$PWD/cmake-build-arm64e/reports-O$level" \
    python3 hikari/hikari-clang.py -O2 -I"$PWD/hikari" \
    -c input.c -o "cmake-build-arm64e/input-O$level.o"
done
```

固定种子仅用于对照和故障重放，正式构建省略 HIKARI_SEED。将各目标文件分别链接成最终
Mach-O 后，比较 `__TEXT,__text` 大小、条件分支、间接跳转、明文字符串和关键函数的反编译结果。
分别检查返回值、异常路径和并发首次调用；iOS 目标还需检查设备上的启动、后台切换和 PAC 执行。
关键函数保护会增加体积和运行开销，应按实际产物选择函数和后端优化等级。

LLVM 22 插件运行在独立的 LLVM 22 管线末尾，Apple 后端仍会执行自己的代码生成流程。
LLVM ABI 不同，因此不能把这个插件直接加载到 Apple Clang 的优化管线中。
