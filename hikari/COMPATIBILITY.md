# LLVM 22 / arm64e 兼容修复

本目录把仓库中的 Hikari 混淆 passes 构建为独立插件，使用本机已有的
LLVM 22.1.8，不重新编译整个 LLVM，也不替换 Theos 或 Xcode 工具链。
插件运行在 Mac 上；arm64 / arm64e 是生成代码的目标架构。

## 构建和使用

从仓库根目录执行：

```sh
cmake -S hikari -B cmake-build-arm64e -G Ninja \
  -DLLVM_DIR=/opt/homebrew/opt/llvm/lib/cmake/llvm \
  -DCMAKE_C_COMPILER=/opt/homebrew/opt/llvm/bin/clang \
  -DCMAKE_CXX_COMPILER=/opt/homebrew/opt/llvm/bin/clang++ \
  -DCMAKE_BUILD_TYPE=Release
cmake --build cmake-build-arm64e --parallel 4
```

插件产物是 `cmake-build-arm64e/libHikari.so`。必须搭配相同版本的 LLVM
Clang 使用，不能加载到 Apple Clang 或其它版本的 LLVM Clang 中。

Apple 前后端交接、关键函数标记、构建种子和最终 Mach-O 对照见
[PROTECTION.md](PROTECTION.md)。`hikari-clang.py` 默认使用 Apple -O0 后端。

```sh
PLUGIN="$PWD/cmake-build-arm64e/libHikari.so"
/opt/homebrew/opt/llvm/bin/clang \
  -target arm64e-apple-ios16.0 -isysroot "$(xcrun --sdk iphoneos --show-sdk-path)" \
  -O2 -fobjc-arc \
  -Xclang -load -Xclang "$PLUGIN" -fpass-plugin="$PLUGIN" \
  -mllvm -enable-splitobf -c input.m -o output.o
```

`-Xclang -load` 使插件的 `-mllvm` 参数在解析时可用，
`-fpass-plugin` 注册新 pass manager 的调度入口。
也可以只用 `-fpass-plugin` 配合现有的 `SPLITOBF=1`、`STRCRY=1` 等环境变量。

## LLVM 22 与 arm64e 兼容实现

- 迁移 LLVM 22 的 capture 查询、Triple、迭代器、CallInst 和 IRBuilder API。
- 修复随机数范围接口在尚未显式播种时访问空引擎的问题。
- 使用 LLVM 原生接口重建 FCO 清理后的 used 列表，保持数组类型和长度一致。
- 为 FCO 动态符号调用补充 arm64e 的 ptrauth operand bundle。
- 让 AntiClassDump 读取新的 PAC 常量，保留类元数据的认证信息，并为新方法表和传给运行时的 IMP 使用对应签名。
- 每个 Objective-C 类只转换一次，收集完整的继承关系后再安排转换顺序。
- 字符串加密生成新的 Objective-C 常量字符串时，绑定到新对象的存储地址。
- 修复全部混淆开关遗漏反调试、反 Hook 和常量加密的问题。
- 反调试内置 Darwin 实现，增加加载时初始化及任意返回类型函数的入口检查；
  为系统调用声明寄存器和内存影响，检查失败进入 trap，成功后缓存初始化状态。
- 修复反 Hook 重复初始化、重复链接处理器的问题；识别 PAC 包装后的 Objective-C 方法，
  在读取代码或比较 IMP 前去除 PAC；防符号重绑定的指针槽及间接调用使用相应认证信息。
- 函数包装保留内外层调用约定及返回值/参数 ABI 属性，包含 byval 和 sret；
  对 musttail、returns_twice、携带 operand bundle 等上下文敏感调用保留原调用。
- 间接跳转按函数保存栈模式和加密配置，避免后面的函数覆盖前面的选项。
- 清除插桩前推导的函数及直接调用点副作用属性，避免新增检查和全局访问被错误优化。
- 显式指定的反调试/反 Hook bitcode 及 FCO 配置无效时报告编译错误。

## 防护开关的行为

所有功能仍需通过对应开关、环境变量或函数标记启用。`-enable-allobf` 现在包含
反调试、反 Hook 和常量加密；启用反调试会对整个宿主进程执行 `PT_DENY_ATTACH`，
自建测试程序及实际插件应分别验证。内置检查失败会终止进程。

反调试不再依赖外部 bitcode。`-adb_prob`（1–100，默认 40）控制额外函数入口检查的
覆盖比例；已启用模块的加载时初始化始终存在，不受随机抽样影响。默认实现成功后
不会在每次函数调用中重复系统调用。可通过 `-adbextirpath` 传入定义 `void InitADB(void)`
的外部模块，原有 `ADBCallBack` 可选；外部初始化函数必须支持重复调用。

反 Hook 默认使用 trap 处理检测结果；`-adhexrirpath` 的外部模块须定义
`void AHCallBack(void)`。现有内联 Hook 检查仍是指令模式启发式检查，不覆盖所有改写方式。

Objective-C 运行时检查同时覆盖普通类和 Category 的实例／类方法，并保留 arm64e
方法表的 PAC 解析和 IMP 地址去签名，兼容 Apple 旧版 IR 中的
`llvm.ptrauth` 认证全局包装，以及
`objc_msgSend$selector` 专用派发桩（其 selector 参数可能是 `undef`）。
除原方法入口检查外，启用运行时检查的函数
会在 Objective-C 消息发送前校验同一 LLVM 模块内受保护的同名 selector，
因此替换实现不调用原方法时也能在这些调用点被发现。静态 selector 只调用对应校验；
动态 selector 使用共享分派检查。相同类、selector 和方法类型在同一模块内的合法
Category 覆盖作为原始实现候选共同核验。

`-ah_objcruntime=false` 关闭这两处运行时校验；函数的 `noantihook` 和
`noah_objcruntime` 标记也继续生效。调用前校验依赖当前模块的方法元数据，
不覆盖方法定义与调用点分属不同编译单元、且替换后从不进入原方法的情况。
防符号重绑定仍需显式启用 `-ah_antirebind=true`，默认值不变。

FCO 只改写符号配置中列出的外部调用，配置示例为 `{"strcmp":"strcmp"}`，通过
`-fcoconfig` 指定 JSON 文件；未提供配置且默认 `SymbolConfig.json` 不存在时会输出提示。
这类需要配置或适用输入的功能，不能仅用开关出现在命令行判断已经生效。

## 使用边界

插件固定使用 LLVM 22.1.8 ABI，不能直接加载到 Apple Clang 或其它 LLVM 版本。
插件入口跳过 LTO 阶段。Theos 等工程需要配置编译入口；仅构建插件不会自动替换工程工具链。
