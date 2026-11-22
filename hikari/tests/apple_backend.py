#!/usr/bin/env python3
"""Compare final Mach-O code after one frozen Hikari IR at Apple O0/O1/O2."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys


def metrics(binary, disassembly):
    data = binary.read_bytes()
    assert struct.unpack_from("<I", data)[0] == 0xfeedfacf, binary
    offset = 32
    sections = {}
    for _ in range(struct.unpack_from("<I", data, 16)[0]):
        command, size = struct.unpack_from("<II", data, offset)
        if command == 0x19:
            for i in range(struct.unpack_from("<I", data, offset + 64)[0]):
                section = offset + 72 + i * 80
                name = data[section:section + 16].split(b"\0")[0].decode()
                length, location = struct.unpack_from("<QI", data, section + 40)
                sections[name] = data[location:location + length]
        offset += size
    instructions = re.findall(r"^\s*([0-9a-f]+):\s+([a-z][a-z0-9.]*)\s*(.*)$",
                              disassembly, re.M)
    assert instructions and "__text" in sections, binary
    conditional = lambda op: op.startswith("b.") or op in ("cbz", "cbnz", "tbz", "tbnz")
    indirect = lambda op: op in ("br", "braa", "brab", "braaz", "brabz")
    leaders = {int(instructions[0][0], 16)}
    addresses = {int(address, 16) for address, _, _ in instructions}
    for i, (address, op, operands) in enumerate(instructions):
        if conditional(op) or op == "b" or indirect(op) or op.startswith("ret"):
            if i + 1 < len(instructions):
                leaders.add(int(instructions[i + 1][0], 16))
            if conditional(op) or op == "b":
                target = re.search(r"(?:^|,\s*)0x([0-9a-f]+)", operands)
                if target and int(target[1], 16) in addresses:
                    leaders.add(int(target[1], 16))
    strings = sections.get("__cstring", b"")
    return dict(text_bytes=len(sections["__text"]),
                conditional_branches=sum(conditional(op) for _, op, _ in instructions),
                indirect_jumps=sum(indirect(op) for _, op, _ in instructions),
                basic_block_estimate=len(leaders), cstring_entries=sum(bool(x) for x in strings.split(b"\0")),
                fixture_plaintext_present=b"hikari-shared-concurrent-string" in data,
                protected_plaintext_present=b"hikari-only-protected-marker" in data,
                sha256=hashlib.sha256(data).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llvm-bin", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--apple")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.apple is None:
        args.apple = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    args.output.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("wrapper", Path(__file__).parents[1] / "hikari-clang.py")
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    env = dict(os.environ)
    for name in "SPLITOBF SUBOBF ALLOBF FCO STRCRY INDIBRAN FUNCWRA BCFOBF ACDOBF CFFOBF CONSTENC ANTIHOOK ADB".split():
        env.pop(name, None)
    results = []

    def run(command, log, environment=None, expect_failure=False):
        result = subprocess.run(list(map(str, command)), capture_output=True,
                                text=True, timeout=90, env=environment or env)
        with log.open("a") as f:
            f.write(json.dumps(list(map(str, command))) + "\n" + result.stdout + result.stderr)
        if bool(result.returncode) != expect_failure:
            raise RuntimeError(f"exit {result.returncode}: {log}\n{result.stderr[-1800:]}")
        return result.stdout

    fixture = Path(__file__).with_name("protected.cpp")
    for arch in ("native", "arm64", "arm64e"):
        cross = arch != "native"
        developer = Path(args.apple).parents[4]
        sdk = subprocess.check_output(
            ["/usr/bin/xcrun", "--sdk", "iphoneos" if cross else "macosx", "--show-sdk-path"],
            env=dict(env, DEVELOPER_DIR=str(developer)) if developer.name == "Developer" else env,
            text=True)
        if not cross:
            native_sdk = sdk.strip()
        target = ["-target", f"{arch}-apple-ios16.0"] if cross else []
        for variant, flags in (("baseline", []), ("cff", ["-enable-cffobf"]),
                               ("indirect", ["-enable-indibran"]),
                               ("combined", ["-enable-cffobf", "-enable-indibran"]),
                               ("strings", ["-enable-strcry"]),
                               ("strings-partial", ["-enable-strcry", "-strcry_prob=50"]),
                               ("selected", [])):
            prefix = args.output / f"{arch}-{variant}"
            log = prefix.with_suffix(".log")
            log.write_text("")
            raw, obf = prefix.with_suffix(".input.ll"), prefix.with_suffix(".obf.ll")
            define = [] if variant == "selected" else ["-DHIKARI_TEST_PLAIN"]
            run([args.apple, *target, "-isysroot", sdk.strip(), "-O2", *define,
                 "-S", "-emit-llvm", fixture, "-o", raw], log)
            triple = re.search(r'target triple = "([^"]+)"', raw.read_text())[1]
            run([args.llvm_bin / "clang", "-target", triple, "-x", "ir", "-O0",
                 "-Xclang", "-load", "-Xclang", args.plugin, f"-fpass-plugin={args.plugin}",
                 "-mllvm", f"-aesSeed={args.seed}",
                 *[x for flag in flags for x in ("-mllvm", flag)],
                 "-S", "-emit-llvm", raw, "-o", obf], log)
            run([args.llvm_bin / "opt", "-passes=verify", "-disable-output", obf], log)
            text = obf.read_text()
            if variant in ("cff", "combined"):
                assert "switchVar" in text
            if variant in ("indirect", "combined", "selected"):
                assert "indirectbr ptr" in text
            if variant == "selected":
                assert "@ConstantEncryptionKey" in text and "StringCipherSalt" in text
                assert "cmpxchg" in text and "indirectbr ptr" in text
                assert "optnone" in text and "noinline" in text
                assert "Running ControlFlowFlattening On main" not in log.read_text()
            if "ptrauth.abi-version" in raw.read_text():
                assert "ptrauth.abi-version" in text
            obf.write_text(wrapper.apple_ir(text))
            frozen_hash = hashlib.sha256(obf.read_bytes()).hexdigest()
            for level in (0, 1, 2):
                obj = Path(f"{prefix}-O{level}.o")
                binary = Path(f"{prefix}-O{level}" + (".dylib" if cross else ""))
                run([args.apple, "-target", triple, "-isysroot", sdk.strip(), "-x", "ir",
                     f"-O{level}", "-c", obf, "-o", obj], log)
                run([args.apple, "-target", triple, "-isysroot", sdk.strip(), obj,
                     "-lc++", *(["-dynamiclib"] if cross else []), "-o", binary], log)
                assembly = run([args.llvm_bin / "llvm-objdump", "--disassemble", "--section=__text",
                                "--no-show-raw-insn", binary], log)
                Path(f"{binary}.asm").write_text(assembly)
                record = dict(arch=arch, variant=variant, apple_opt=level,
                              seed=args.seed, ir_sha256=frozen_hash,
                              native_executed=False, **metrics(binary, assembly))
                if variant in ("selected", "strings"):
                    assert not record["protected_plaintext_present"], binary
                if not cross:
                    run([binary], log)
                    record["native_executed"] = True
                results.append(record)
                (args.output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
                print(f"{arch} {variant} O{level}: text={record['text_bytes']} branches={record['conditional_branches']} indirect={record['indirect_jumps']}", flush=True)
    assert len(results) == 63
    # Exercise the actual wrapper, including replay and failure reporting.
    wrapper_log = args.output / "wrapper.log"
    obj = args.output / "wrapper.o"
    report_dir = args.output / "wrapper-reports"
    environment = dict(env, HIKARI_APPLE_CLANG=args.apple,
                       HIKARI_LLVM_BIN=str(args.llvm_bin.resolve()),
                       HIKARI_PLUGIN=str(args.plugin.resolve()), HIKARI_FLAGS="",
                       HIKARI_APPLE_OPT="0", HIKARI_KEEP_IR="0",
                       HIKARI_SEED="0x1337", HIKARI_REPORT_DIR=str(report_dir))
    command = [sys.executable, Path(__file__).parents[1] / "hikari-clang.py",
               "-isysroot", native_sdk, "-O2", "-c", fixture, "-o", obj]
    run(command, wrapper_log, environment)
    first = json.loads(next(report_dir.glob("*.json")).read_text())
    metadata_cache = next(report_dir.glob(".toolchain-*.cache"))
    cache_mtime = metadata_cache.stat().st_mtime_ns
    run(command, wrapper_log, environment)
    assert metadata_cache.stat().st_mtime_ns == cache_mtime
    replay = json.loads(next(report_dir.glob("*.json")).read_text())
    assert first["object_sha256"] == replay["object_sha256"], "seed replay changed code"
    assert first["module_seed"] == replay["module_seed"] and first["seed"] == 0x1337
    assert replay["apple_opt"] == 0 and replay["success"]
    environment.pop("HIKARI_SEED")
    run(command, wrapper_log, environment)
    random = json.loads(next(report_dir.glob("*.json")).read_text())
    assert random["seed"] != replay["seed"] and random["object_sha256"] != replay["object_sha256"]
    exe = args.output / "wrapper-run"
    run([args.apple, "-isysroot", native_sdk, obj, "-lc++", "-o", exe], wrapper_log)
    run([exe], wrapper_log)
    environment["HIKARI_FLAGS"] = "-hikari-invalid-option"
    environment["HIKARI_REPORT_DIR"] = str(report_dir / "failure")
    run(command, wrapper_log, environment, expect_failure=True)
    failure = json.loads(next((report_dir / "failure").glob("*.json")).read_text())
    assert not failure["success"]
    # Compatibility edits must leave source string bytes and debug info intact.
    literal = "captures(none) nocreateundeforpoison icmp samesign end"
    source = args.output / "wrapper-literal.c"
    source.write_text(f'#include <stdio.h>\nint main(void) {{ puts("{literal}"); }}\n')
    environment.update(HIKARI_FLAGS="", HIKARI_REPORT_DIR=str(args.output / "literal-reports"))
    literal_command = [*command[:2], "-isysroot", native_sdk, "-g",
                       "-c", source, "-o", obj]
    run(literal_command, wrapper_log, environment)
    run([args.apple, "-isysroot", native_sdk, obj, "-o", exe], wrapper_log)
    assert run([exe], wrapper_log).strip() == literal
    dwarf = run([args.llvm_bin / "llvm-dwarfdump", "--debug-info", obj], wrapper_log)
    assert "DW_TAG_compile_unit" in dwarf and source.name in dwarf
    run(literal_command + ["-flto=thin"], wrapper_log, environment, expect_failure=True)
    assert "does not support -flto" in wrapper_log.read_text()
    run(literal_command + ["-flto=thin", "-fno-lto"], wrapper_log, environment)

    salt = Path(__file__).with_name("string-salt.c")
    baseline = args.output / "salt-baseline"
    run([args.apple, "-isysroot", native_sdk, "-O2", salt, "-o", baseline], wrapper_log)
    expected = run([baseline], wrapper_log)
    environment.update(HIKARI_FLAGS="", HIKARI_APPLE_OPT="0", HIKARI_KEEP_IR="0",
                       HIKARI_REPORT_DIR=str(args.output / "salt-reports"))
    for seed in (42, 1559610475485605823):
        environment["HIKARI_SEED"] = str(seed)
        run([sys.executable, Path(__file__).parents[1] / "hikari-clang.py",
             "-isysroot", native_sdk, "-O2", "-c", salt, "-o", obj], wrapper_log, environment)
        run([args.apple, "-isysroot", native_sdk, obj, "-o", exe], wrapper_log)
        assert run([exe], wrapper_log) == expected, "byte salt changed decrypted strings"
    # ARC liveness markers survive optnone but must not reach instruction selection.
    environment.update(HIKARI_FLAGS="-enable-strcry", HIKARI_KEEP_IR="1")
    arc = Path(__file__).with_name("objc-arc.m")
    for arch in ("native", "arm64", "arm64e"):
        cross = arch != "native"
        target = ["-target", f"{arch}-apple-ios16.0"] if cross else []
        sdk = subprocess.check_output(
            ["/usr/bin/xcrun", "--sdk", "iphoneos" if cross else "macosx", "--show-sdk-path"],
            env=dict(env, DEVELOPER_DIR=str(developer)), text=True).strip()
        for level in (0, 1, 2):
            prefix = args.output / f"arc-{arch}-O{level}"
            environment.update(HIKARI_APPLE_OPT=str(level),
                               HIKARI_REPORT_DIR=str(prefix) + "-reports")
            obj = Path(str(prefix) + ".o")
            run([sys.executable, Path(__file__).parents[1] / "hikari-clang.py",
                 *target, "-isysroot", sdk, "-fobjc-arc", "-O2", "-c", arc, "-o", obj],
                wrapper_log, environment)
            transformed = next(Path(str(prefix) + "-reports").glob("*.obfuscated.ll")).read_text()
            raw = next(Path(str(prefix) + "-reports").glob("*.input.ll")).read_text()
            assert "llvm.objc.clang.arc.noop.use" in raw
            assert "llvm.objc.clang.arc.noop.use" not in transformed
            assert "llvm.objc.clang.arc.use" not in transformed
            binary = Path(str(prefix) + (".dylib" if cross else ""))
            run([args.apple, *target, "-isysroot", sdk, obj, "-framework", "Foundation",
                 *(["-dynamiclib"] if cross else []), "-o", binary], wrapper_log)
            if not cross:
                run([binary], wrapper_log)
    print("PASS ARC selected functions at Apple O0/O1/O2, native and iOS arm64/arm64e", flush=True)
    print("PASS wrapper replay, fresh seed and native semantics", flush=True)


if __name__ == "__main__":
    main()
