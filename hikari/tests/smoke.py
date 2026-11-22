#!/usr/bin/env python3
"""Run native semantics and iOS arm64/arm64e IR/code-generation checks."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

PASSES = ("splitobf", "subobf", "bcfobf", "cffobf", "constenc", "strcry",
          "indibran", "funcwra", "fco", "antihook", "acdobf", "adb", "allobf")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llvm-bin", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    sources = Path(__file__).parent
    config = args.output / "symbols.json"
    config.write_text(json.dumps({"strcmp": "strcmp"}))
    env = dict(os.environ)
    for key in ("SPLITOBF", "SUBOBF", "ALLOBF", "FCO", "STRCRY", "INDIBRAN",
                "FUNCWRA", "BCFOBF", "ACDOBF", "CFFOBF", "CONSTENC", "ANTIHOOK", "ADB"):
        env.pop(key, None)

    def run(command, log):
        result = subprocess.run([str(x) for x in command], capture_output=True,
                                text=True, timeout=90, env=env)
        with log.open("a") as stream:
            stream.write(" ".join(str(x) for x in command) + "\n")
            stream.write(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"exit {result.returncode}: {log}\n{result.stderr[-3000:]}")

    sdk = subprocess.check_output(
        ["xcrun", "--sdk", "iphoneos", "--show-sdk-path"], text=True).strip()
    cases = 0
    for target in ("native-O0", "native-O2", "arm64-O2", "arm64e-O0", "arm64e-O2"):
        cross = not target.startswith("native")
        arch, level = target.split("-")
        for source in ("core.c", "objc.m", "cxx.cpp"):
            for flag in (None, *PASSES):
                name = f"{target}-{source}-{flag or 'baseline'}"
                output = args.output / name
                log = args.output / f"{name}.log"
                log.write_text("")
                compiler = "clang++" if source.endswith(".cpp") else "clang"
                command = [args.llvm_bin / compiler, f"-{level}", "-fno-builtin"]
                if cross:
                    command += ["-target", f"{arch}-apple-ios16.0", "-isysroot", sdk]
                if source.endswith(".m"):
                    command += ["-fobjc-arc"]
                if flag:
                    command += ["-Xclang", "-load", "-Xclang", args.plugin,
                                f"-fpass-plugin={args.plugin}", "-mllvm", f"-enable-{flag}",
                                "-mllvm", "-aesSeed=42", "-mllvm", "-fw_prob=100",
                                "-mllvm", "-bcf_prob=100", "-mllvm", "-sub_prob=100",
                                "-mllvm", "-adb_prob=100", "-mllvm", f"-fcoconfig={config}"]
                if cross:
                    ir = Path(str(output) + ".ll")
                    run(command + ["-S", "-emit-llvm", sources / source, "-o", ir], log)
                    run([args.llvm_bin / "opt", "-passes=verify", "-disable-output", ir], log)
                    # Compile from source with the same ARC/optimization pipeline.
                    run(command + ["-c", sources / source, "-o", str(output) + ".o"], log)
                    text = ir.read_text()
                    if flag in ("adb", "allobf"):
                        assert "@llvm.global_ctors" in text and "@__hikari_adb_init" in text, name
                        assert "svc #0x80" in text and "~{memory}" in text, name
                    if source == "core.c" and flag in ("adb", "antihook", "allobf"):
                        number = re.search(r"define[^\n]*@step\([^\n]*\) [^\n]*#(\d+)", text)[1]
                        attrs = re.search(r"attributes #" + number + r" = \{([^\n]+)\}", text)[1]
                        assert "memory(none)" not in attrs and "nosync" not in attrs, name
                    if flag in ("antihook", "allobf"):
                        assert "load volatile i32" in text, name
                        if source == "objc.m":
                            assert re.search(r"call ptr @method_getImplementation\(", text), name
                            if arch == "arm64e":
                                assert "call i64 @llvm.ptrauth.strip" in text, name
                    if source == "core.c" and flag:
                        baseline = (args.output / f"{target}-{source}-baseline.ll").read_text()
                        if flag in ("constenc", "allobf"):
                            assert "@ConstantEncryptionKey" in text and "load volatile" in text, name
                        if flag in ("strcry", "allobf"):
                            assert "@EncryptedString" in text, name
                        if flag in ("funcwra", "allobf"):
                            assert "define internal" in text and "@HikariFunctionWrapper" in text, name
                        if flag in ("indibran", "allobf"):
                            assert "indirectbr ptr" in text, name
                        if flag in ("splitobf", "bcfobf", "cffobf"):
                            blocks = lambda s: len(re.findall(r"^[-\w.]+:.*$", s, re.M))
                            assert blocks(text) > blocks(baseline), name
                        if flag == "subobf":
                            assert text.count(" = " ) > baseline.count(" = "), name
                    if source == "objc.m" and flag in ("acdobf", "allobf"):
                        assert "ACDNew" in text and "@class_replaceMethod(" in text, name
                    if arch == "arm64e" and source == "core.c":
                        assert '"ptrauth"(i32 0, i64 0)' in text, name
                        if flag == "fco":
                            pointers = re.findall(r"(%[\w.]+) = (?:tail )?call ptr @dlsym\(", text)
                            assert pointers, name
                            for pointer in pointers:
                                calls = re.findall(r"(?:call|invoke)[^\n]* " + re.escape(pointer) + r"\([^\n]*", text)
                                assert calls and all('"ptrauth"' in call for call in calls), name
                    if arch == "arm64e" and source == "objc.m" and flag in ("strcry", "allobf"):
                        strings = [line for line in text.splitlines()
                                   if line.startswith("@DecryptSpaceObjC")]
                        assert strings, name
                        for line in strings:
                            symbol = line.split(" = ")[0]
                            assert f"ptr {symbol})" in line, (name, line)
                else:
                    libraries = ["-framework", "Foundation"] if source.endswith(".m") else []
                    run(command + [sources / source, *libraries, "-o", output], log)
                    run([output], log)
                cases += 1
                print(f"PASS {name}", flush=True)
    print(f"Passed {cases} compiler cases (native execution and iOS IR/object checks).")


if __name__ == "__main__":
    main()
