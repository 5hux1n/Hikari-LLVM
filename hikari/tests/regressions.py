#!/usr/bin/env python3
"""Check runtime guards, wrapper ABI and per-function jump-table options."""
import argparse
from pathlib import Path
import re
import signal
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llvm-bin", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    clang = args.llvm_bin / "clang"
    checks = 0

    def run(command, name, expected=0, contains=None):
        nonlocal checks
        result = subprocess.run(list(map(str, command)), capture_output=True,
                                text=True, timeout=90)
        (args.output / f"{name}.log").write_text(
            " ".join(map(str, command)) + "\n" + result.stdout + result.stderr)
        assert (result.returncode == expected if expected is not None
                else result.returncode != 0), (name, result.returncode, result.stderr[-2000:])
        if contains:
            assert contains in result.stderr, (name, result.stderr)
        checks += 1
        return result

    def write(name, source):
        path = args.output / name
        path.write_text(source)
        return path

    def command(flag, *options):
        return [clang, "-O0", "-Xclang", "-load", "-Xclang", args.plugin,
                f"-fpass-plugin={args.plugin}", "-mllvm", f"-enable-{flag}",
                "-mllvm", "-aesSeed=42", *sum((["-mllvm", x] for x in options), [])]

    # Nonescaping storage can be rewritten. Addresses passed to other code
    # must continue to expose the original value.
    source = write("constant-storage.c", '''
static int escaped = 42;
static int direct = 12;
static volatile int observed = 7;
__attribute__((noinline)) static int read_pointer(int *p) { return *p; }
int main(void) {
  direct += 3;
  return read_pointer(&escaped) != 42 || direct != 15 || observed != 7;
}
''')
    binary = args.output / "constant-storage"
    run(command("constenc") + [source, "-o", binary], "constant-storage-build")
    run([binary], "constant-storage-run")
    ir = args.output / "constant-storage.ll"
    run(command("constenc") + ["-S", "-emit-llvm", source, "-o", ir],
        "constant-storage-ir")
    text = ir.read_text()
    assert "@escaped = internal global i32 42" in text
    assert "@observed = internal global i32 7" in text
    assert "@direct = internal global i32 12" not in text

    source = write("marker-caller.c", '''
extern int hikari_application(int);
extern void hikari_bcf(void);
extern void hikari_sub_prob(unsigned);
extern int hikari_bcf_extra(int);
int main(void) {
  hikari_bcf();
  hikari_sub_prob(100);
  return hikari_application(41) != 42 || hikari_bcf_extra(42) != 42;
}
''')
    definition = write("marker-definition.c", '''
int hikari_application(int n) { return n + 1; }
int hikari_bcf_extra(int n) { return n; }
''')
    binary = args.output / "marker-caller"
    run(command("subobf") + [source, definition, "-o", binary], "markers-build")
    run([binary], "markers-run")

    # Link and execute InitADB without requiring an unused ADBCallBack.
    helper = write("adb-helper.c", "int adb_hits; void InitADB(void) { ++adb_hits; }\n")
    bitcode = args.output / "adb-helper.bc"
    run([clang, "-O0", "-emit-llvm", "-c", helper, "-o", bitcode], "adb-helper")
    source = write("adb-entry.c", '''
extern int adb_hits;
__attribute__((noinline)) int guarded(int n) { return n + 1; }
__attribute__((annotate("noadb"))) int main(void) {
  if (adb_hits != 1) return 1;
  if (guarded(41) != 42) return 2;
  return adb_hits != 2;
}
''')
    binary = args.output / "adb-entry"
    run(command("adb", "-adb_prob=100", f"-adbextirpath={bitcode}") +
        [source, "-o", binary], "adb-entry-build")
    run([binary], "adb-entry-run")
    # Trace only a child fixture created by this test, before its load-time guard.
    driver = write("trace-driver.c", '''
#include <sys/types.h>
#include <sys/ptrace.h>
#include <sys/wait.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
static pid_t child;
static void timeout_handler(int sig) { kill(child, SIGKILL); _exit(76); }
int main(int argc, char **argv) {
  child = fork();
  if (child == -1) return 77;
  if (child == 0) {
    if (ptrace(PT_TRACE_ME, 0, 0, 0) == -1) _exit(77);
    execl(argv[1], argv[1], (char *)0);
    _exit(78);
  }
  signal(SIGALRM, timeout_handler);
  alarm(10);
  int status;
  while (waitpid(child, &status, 0) == child) {
    if (WIFSTOPPED(status)) {
      if (ptrace(PT_CONTINUE, child, (caddr_t)1, 0) == -1) {
        kill(child, SIGKILL); waitpid(child, &status, 0); return 77;
      }
    } else {
      if (WIFEXITED(status) && WEXITSTATUS(status) == 77) return 77;
      return !(WIFEXITED(status) && WEXITSTATUS(status) == ENOTSUP);
    }
  }
  return 77;
}
''')
    trace_driver = args.output / "trace-driver"
    run([clang, driver, "-o", trace_driver], "trace-driver-build")
    builtin = args.output / "adb-builtin"
    minimal = write("adb-builtin.c", "int main(void) { return 0; }\n")
    run(command("adb") + [minimal, "-o", builtin], "adb-builtin-build")
    run([builtin], "adb-builtin-healthy")
    builtin_ir = args.output / "adb-builtin.ll"
    run(command("adb") + ["-S", "-emit-llvm", minimal, "-o", builtin_ir], "adb-builtin-ir")
    # Fault injection tests the failure branch, without claiming OS tracing worked.
    failed, replacements = re.subn(
        r'(?m)^(\s*)(%[-\w.]+) = call i64 asm sideeffect .*$',
        r'\1\2 = add i64 0, -1', builtin_ir.read_text())
    assert replacements == 1
    failed_ir = write("adb-failed.ll", failed)
    failed_binary = args.output / "adb-failed"
    run([clang, failed_ir, "-o", failed_binary], "adb-failed-build")
    failure = run([failed_binary], "adb-failed-run", expected=None)
    assert failure.returncode in (-signal.SIGTRAP, -signal.SIGILL)
    traced = subprocess.run([str(trace_driver), str(builtin)], capture_output=True,
                            text=True, timeout=15)
    (args.output / "adb-builtin-traced.log").write_text(
        f"returncode={traced.returncode}\n" + traced.stdout + traced.stderr)
    assert traced.returncode in (0, 77), ("adb-builtin-traced", traced.returncode)
    if traced.returncode == 77:
        print("SKIP native traced-child check: host denied tracing", flush=True)
    else:
        checks += 1
        print("PASS native traced child rejected with ENOTSUP", flush=True)
    for flag, option in (("adb", "-adbextirpath"), ("antihook", "-adhexrirpath")):
        run(command(flag, f"{option}={args.output / 'missing.bc'}") +
            [source, "-c", "-o", args.output / "invalid.o"],
            f"{flag}-missing", expected=None, contains="IR not found")
        invalid = write(f"{flag}-invalid.bc", "not LLVM bitcode\n")
        run(command(flag, f"{option}={invalid}") +
            [source, "-c", "-o", args.output / "invalid.o"],
            f"{flag}-invalid", expected=None,
            contains=f"Cannot parse Anti{'Debugging' if flag == 'adb' else 'Hooking'} IR")
    for probability in (0, 101):
        run(command("adb", f"-adb_prob={probability}") +
            [source, "-c", "-o", args.output / "invalid.o"],
            f"adb-prob-{probability}", expected=None, contains="between 1 and 100")
    for name, config, diagnostic in (
            ("missing", None, "configuration not found"),
            ("syntax", "{broken", "must be a JSON object"),
            ("type", '{"strcmp": 42}', "must be non-empty symbol strings")):
        path = args.output / f"fco-{name}.json"
        if config is not None:
            write(path.name, config)
        run(command("fco", f"-fcoconfig={path}") +
            [source, "-c", "-o", args.output / "invalid.o"],
            f"fco-{name}", expected=None, contains=diagnostic)

    # Check both fastcc and byval/sret through two layers of wrappers.
    abi = write("wrapper-abi.ll", '''
%Pair = type { i64, i64 }
define fastcc i32 @increment(i32 %n) {
  %r = add i32 %n, 1
  ret i32 %r
}
define void @copy_pair(ptr sret(%Pair) %out, ptr byval(%Pair) align 8 %in) {
  %value = load %Pair, ptr %in
  store %Pair %value, ptr %out
  ret void
}
define i32 @main() {
  %input = alloca %Pair, align 8
  %output = alloca %Pair, align 8
  store %Pair { i64 12, i64 34 }, ptr %input
  call void @copy_pair(ptr sret(%Pair) %output, ptr byval(%Pair) align 8 %input)
  %value = load %Pair, ptr %output
  %first = extractvalue %Pair %value, 0
  %ok = icmp eq i64 %first, 12
  %n = call fastcc i32 @increment(i32 41)
  %ok2 = icmp eq i32 %n, 42
  %both = and i1 %ok, %ok2
  %bad = xor i1 %both, true
  %result = zext i1 %bad to i32
  ret i32 %result
}
''')
    wrapped = args.output / "wrapper-abi-out.ll"
    run([args.llvm_bin / "opt", f"-load={args.plugin}",
         f"-load-pass-plugin={args.plugin}", "-passes=default<O0>",
         "-enable-funcwra", "-fw_prob=100", "-fw_times=2", "-S", abi, "-o", wrapped],
        "wrapper-abi-ir")
    text = wrapped.read_text()
    assert re.search(r"call fastcc i32 @increment", text), "wrapper lost fastcc"
    assert re.search(r"define internal void @HikariFunctionWrapper[^\n]+sret\(%Pair\)[^\n]+byval\(%Pair\)", text), "wrapper lost ABI attributes"
    run([args.llvm_bin / "opt", "-passes=verify", "-disable-output", wrapped], "wrapper-abi-verify")
    binary = args.output / "wrapper-abi"
    run([clang, wrapped, "-o", binary], "wrapper-abi-link")
    run([binary], "wrapper-abi-run")

    jump = write("jump-options.c", '''
__attribute__((noinline, annotate("noindibran_use_stack"),
               annotate("indibran_enc_jump_target")))
int encrypted(int n) { if (n > 4) return n + 3; return n - 2; }
__attribute__((noinline, annotate("indibran_use_stack"),
               annotate("noindibran_enc_jump_target")))
int plain(int n) { if (n > 4) return n + 1; return n - 4; }
int main(void) { return encrypted(5) != 8 || encrypted(2) != 0 || plain(5) != 6 || plain(2) != -2; }
''')
    ir = args.output / "jump-options.ll"
    run(command("indibran") + ["-S", "-emit-llvm", jump, "-o", ir], "jump-options-ir")
    text = ir.read_text()
    functions = dict(re.findall(r"define[^\n]*@([\w]+)\([^\n]*\)[^{]*\{\n(.*?)\n\}", text, re.S))
    assert "IndirectBranchingAddressEncryptKey" in functions["encrypted"], "encrypted option lost"
    assert "IndirectBranchingAddressEncryptKey" not in functions["plain"], "encryption leaked to plain"
    binary = args.output / "jump-options"
    run(command("indibran") + [jump, "-o", binary], "jump-options-build")
    run([binary], "jump-options-run")

    flow = Path(__file__).with_name("control-flow.ll")
    for name, options in (("cffobf", ()), ("indibran", ()),
                          ("combined", ("-enable-indibran",))):
        binary = args.output / f"control-flow-{name}"
        run(command("cffobf" if options else name, *options) +
            [flow, "-o", binary], f"control-flow-{name}-build")
        run([binary], f"control-flow-{name}-run")

    # Cover full replacement as well as hooks that call the saved original IMP.
    objc = write("hook-runtime.m", '''
#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#import <objc/message.h>
#include <string.h>
@interface GuardedProbe : NSObject
- (int)value:(int)n;
+ (int)classValue:(int)n;
@end
@implementation GuardedProbe
- (int)value:(int)n { return n + 5; }
+ (int)classValue:(int)n { return n + 6; }
@end
@interface GuardedProbe (Extra)
- (int)categoryValue:(int)n;
+ (int)categoryClassValue:(int)n;
@end
@implementation GuardedProbe (Extra)
- (int)categoryValue:(int)n { return n + 7; }
+ (int)categoryClassValue:(int)n { return n + 8; }
@end
@interface GuardedChild : GuardedProbe
@end
@implementation GuardedChild
- (int)value:(int)n { return [super value:n] + 1; }
@end
static int replacement(id obj, SEL sel, int n) { return 99; }
int main(int argc, char **argv) {
  @autoreleasepool {
    if (argc > 1 && strcmp(argv[1], "first-class-send") == 0)
      return [GuardedProbe classValue:37] != 43;
    GuardedProbe *obj = [GuardedProbe new];
    if (argc == 1) {
      GuardedChild *child = [GuardedChild new];
      return [obj value:37] != 42 || [GuardedProbe classValue:37] != 43 ||
             [obj categoryValue:37] != 44 ||
             [GuardedProbe categoryClassValue:37] != 45 ||
             [child value:37] != 43 || [(GuardedProbe *)nil value:37] != 0;
    }
    int kind = argv[1][0] - '0';
    BOOL classMethod = kind == 1 || kind == 3;
    SEL selectors[] = {@selector(value:), @selector(classValue:),
                       @selector(categoryValue:), @selector(categoryClassValue:)};
    SEL sel = selectors[kind];
    Method method = classMethod ? class_getClassMethod([GuardedProbe class], sel)
                               : class_getInstanceMethod([GuardedProbe class], sel);
    IMP original = method_getImplementation(method);
    method_setImplementation(method, (IMP)replacement);
    if (argc > 2 && strcmp(argv[2], "original") == 0)
      return ((int (*)(id, SEL, int))original)(classMethod ? (id)[GuardedProbe class] : obj, sel, 37) != 42 + kind;
    if (argc > 2)
      return ((int (*)(id, SEL, int))objc_msgSend)(classMethod ? (id)[GuardedProbe class] : obj, sel, 37) != 42 + kind;
    switch (kind) {
      case 0: return [obj value:37] != 42;
      case 1: return [GuardedProbe classValue:37] != 43;
      case 2: return [obj categoryValue:37] != 44;
      default: return [GuardedProbe categoryClassValue:37] != 45;
    }
  }
}
''')
    handler = write("hook-handler.c", "extern void _Exit(int); void AHCallBack(void) { _Exit(73); }\n")
    handler_bc = args.output / "hook-handler.bc"
    run([clang, "-emit-llvm", "-c", handler, "-o", handler_bc], "hook-handler")
    for level in ("O0", "O2"):
        binary = args.output / f"hook-runtime-{level}"
        options = command("antihook", "-ah_inline=false", f"-adhexrirpath={handler_bc}")
        options[1] = f"-{level}"
        run(options + [objc, "-framework", "Foundation", "-o", binary], f"hook-runtime-{level}-build")
        run([binary], f"hook-runtime-{level}-healthy")
        run([binary, "first-class-send"], f"hook-runtime-{level}-first-class")
        for kind in range(4):
            run([binary, kind], f"hook-runtime-{level}-{kind}-replaced", expected=73)
            run([binary, kind, "original"], f"hook-runtime-{level}-{kind}-original", expected=73)
            run([binary, kind, "dynamic"], f"hook-runtime-{level}-{kind}-dynamic", expected=73)
        for mode, extra in (("disabled", ["-ah_objcruntime=false"]),
                            ("rebind", ["-ah_antirebind=true"]),
                            ("allobf", []), ("builtin", [])):
            variant = args.output / f"hook-{mode}-{level}"
            config = [] if mode == "builtin" else [f"-adhexrirpath={handler_bc}"]
            opts = command("allobf" if mode == "allobf" else "antihook",
                           "-ah_inline=false", *config, *extra)
            opts[1] = f"-{level}"
            run(opts + [objc, "-framework", "Foundation", "-o", variant], f"hook-{mode}-{level}-build")
            run([variant], f"hook-{mode}-{level}-healthy")
            run([variant, "first-class-send"], f"hook-{mode}-{level}-first-class")
            for kind in range(4):
                expected = 1 if mode == "disabled" else -signal.SIGTRAP if mode == "builtin" else 73
                run([variant, kind], f"hook-{mode}-{level}-{kind}-replaced", expected=expected)

    overrides = write("hook-category-override.m", '''
#import <Foundation/Foundation.h>
@interface CategoryProbe : NSObject
- (int)value;
+ (int)classValue;
@end
@implementation CategoryProbe
- (int)value { return 1; }
+ (int)classValue { return 2; }
@end
@interface CategoryProbe (Override)
- (int)value;
+ (int)classValue;
@end
@implementation CategoryProbe (Override)
- (int)value { return 3; }
+ (int)classValue { return 4; }
@end
@interface NSObject (ExternalCategory)
- (int)externalCategoryValue;
@end
@implementation NSObject (ExternalCategory)
- (int)externalCategoryValue { return 5; }
@end
int main(void) {
  @autoreleasepool {
    return [[CategoryProbe new] value] != 3 || [CategoryProbe classValue] != 4 ||
           [[NSObject new] externalCategoryValue] != 5;
  }
}
''')
    for level in ("O0", "O2"):
        binary = args.output / f"hook-category-override-{level}"
        options = command("antihook")
        options[1] = f"-{level}"
        run(options + [overrides, "-framework", "Foundation", "-o", binary], f"hook-category-override-{level}-build")
        run([binary], f"hook-category-override-{level}-run")
    # Check the opt-in anti-rebinding path on arm64e, including its signed slot.
    sdk = subprocess.check_output(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"], text=True).strip()
    for arch, level in (("arm64", "O2"), ("arm64e", "O0"), ("arm64e", "O2")):
        for mode in ("antihook", "allobf", "rebind"):
            name = f"hook-{arch}-{level}-{mode}"
            options = command("allobf" if mode == "allobf" else "antihook",
                              *(["-ah_antirebind=true"] if mode == "rebind" else []))
            options[1] = f"-{level}"
            options += ["-fobjc-arc", "-target", f"{arch}-apple-ios16.0", "-isysroot", sdk]
            ir = args.output / f"{name}.ll"
            run(options + ["-S", "-emit-llvm", objc, "-o", ir], f"{name}-ir")
            text = ir.read_text()
            assert "@__hikari_ah_method" in text and "@__hikari_ah_dispatch" in text, name
            if mode == "antihook":
                bodies = re.findall(r'define[^\n]*@"([^\"]+)"[^\n]*\{\n(.*?)\n\}', text, re.S)
                categories = [body for method, body in bodies if "[GuardedProbe(Extra) " in method]
                assert len(categories) == 2 and all("call void @__hikari_ah_method" in body for body in categories), name
            if arch == "arm64e":
                assert "call i64 @llvm.ptrauth.strip" in text, name
            run([args.llvm_bin / "opt", "-passes=verify", "-disable-output", ir], f"{name}-verify")
            run(options + ["-c", objc, "-o", args.output / f"{name}.o"], f"{name}-object")
    # Apple clang 17 uses llvm.ptrauth globals, unlike upstream constant ptrauth.
    legacy = write("hook-legacy-ptrauth.ll", '''
target triple = "arm64e-apple-ios16.0.0"
%struct._objc_method = type { ptr, ptr, ptr }
%struct._category_t = type { ptr, ptr, ptr, ptr, ptr, ptr, i32 }
@selector = private constant [6 x i8] c"value\\00"
@types = private constant [8 x i8] c"q16@0:8\\00"
@OBJC_CLASS_$_LegacyProbe = external global i8
@imp.auth = private constant {ptr, i32, i64, i64} {ptr @imp, i32 0, i64 0, i64 0}, section "llvm.ptrauth"
@"_OBJC_$_CATEGORY_INSTANCE_METHODS_LegacyProbe_Extra" = internal global {i32, i32, [1 x %struct._objc_method]} {i32 24, i32 1, [1 x %struct._objc_method] [%struct._objc_method {ptr @selector, ptr @types, ptr @imp.auth}]}
@list.auth = private constant {ptr, i32, i64, i64} {ptr @"_OBJC_$_CATEGORY_INSTANCE_METHODS_LegacyProbe_Extra", i32 2, i64 0, i64 49936}, section "llvm.ptrauth"
@category = internal global %struct._category_t {ptr @selector, ptr @OBJC_CLASS_$_LegacyProbe, ptr @list.auth, ptr null, ptr null, ptr null, i32 56}
@llvm.compiler.used = appending global [1 x ptr] [ptr @category], section "llvm.metadata"
define internal i64 @imp(ptr %self, ptr %sel) { ret i64 17 }
declare i64 @"objc_msgSend$value"(ptr, ptr)
define i64 @stub_caller(ptr %self) {
  %value = call i64 @"objc_msgSend$value"(ptr %self, ptr undef)
  ret i64 %value
}
''')
    for level in ("O0", "O2"):
        options = command("antihook")
        options[1] = f"-{level}"
        ir = args.output / f"hook-legacy-ptrauth-{level}.ll"
        run(options + ["-S", "-emit-llvm", legacy, "-o", ir], f"hook-legacy-ptrauth-{level}-ir")
        assert "define private void @__hikari_ah_method" in ir.read_text()
        caller = re.search(r'define i64 @stub_caller\b[^\n]*\{\n(.*?)\n\}', ir.read_text(), re.S).group(1)
        assert caller.index('call void @__hikari_ah_method') < caller.index('@"objc_msgSend$value"')
        run([args.llvm_bin / "opt", "-passes=verify", "-disable-output", ir], f"hook-legacy-ptrauth-{level}-verify")
    core = Path(__file__).parent / "core.c"
    for level in ("O0", "O2"):
        options = command("antihook", "-ah_inline=false", "-ah_objcruntime=false", "-ah_antirebind=true")
        options[1] = f"-{level}"
        options += ["-fno-builtin", "-target", "arm64e-apple-ios16.0", "-isysroot", sdk]
        ir = args.output / f"rebind-{level}.ll"
        run(options + ["-S", "-emit-llvm", core, "-o", ir], f"rebind-{level}-ir")
        text = ir.read_text()
        assert re.search(r"@AntiRebindSymbol_strcmp = private constant ptr ptrauth", text)
        assert re.search(r'call i32 %[^\n]+\[ "ptrauth"\(i32 0, i64 0\) \]', text)
        run([args.llvm_bin / "opt", "-passes=verify", "-disable-output", ir], f"rebind-{level}-verify")
        run(options + ["-c", core, "-o", args.output / f"rebind-{level}.o"], f"rebind-{level}-object")
    print(f"Passed {checks} focused regression commands and structural assertions.")


if __name__ == "__main__":
    main()
