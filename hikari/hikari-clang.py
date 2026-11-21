#!/usr/bin/env python3
"""Apple frontend -> Hikari -> Apple backend, with per-object replay reports."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile


def apple_ir(text):
    # These LLVM 22 optimizer assertions are absent from Apple LLVM 19.
    text = re.sub(r"\bcaptures\([^()]*\)", "", text)
    text = re.sub(r"\bnocreateundeforpoison\b", "", text)
    text = text.replace("icmp samesign ", "icmp ")
    # optnone skips ARC contraction; this liveness marker has no machine code.
    text = re.sub(r"(?m)^.*(?:call void \(\.\.\.\)|declare void) @llvm\.objc\.clang\.arc\.(?:noop\.)?use[^\n]*\n", "", text)
    # LLVM 22 removed the size operand; omit these optional lifetime hints.
    return re.sub(r"(?m)^.*(?:call void|declare void) @llvm\.lifetime\.(?:start|end)\.[^\n]*\n", "", text)


def main(args):
    apple = os.environ.get("HIKARI_APPLE_CLANG") or subprocess.check_output(
        ["xcrun", "--find", "clang"], text=True).strip()
    if "-c" not in args or "-o" not in args or any(
            Path(a).suffix in (".s", ".S") for a in args):
        os.execv(apple, [apple, *args])
    llvm = Path(os.environ.get("HIKARI_LLVM_BIN", "/opt/homebrew/opt/llvm/bin"))
    plugin = Path(os.environ.get("HIKARI_PLUGIN", str(
        Path(__file__).resolve().parents[1] / "cmake-build-arm64e/libHikari.so")))
    level = os.environ.get("HIKARI_APPLE_OPT", "0").removeprefix("-O").removeprefix("O")
    if level not in ("0", "1", "2"):
        raise ValueError("HIKARI_APPLE_OPT must be 0, 1 or 2")
    flags = shlex.split(os.environ.get("HIKARI_FLAGS", ""))
    if any(f.startswith("-aesSeed") for f in flags):
        raise ValueError("Use HIKARI_SEED so the seed is recorded")
    seed = int(os.environ["HIKARI_SEED"], 0) if "HIKARI_SEED" in os.environ else secrets.randbits(64)
    if not 0 <= seed < 2**64:
        raise ValueError("HIKARI_SEED must be an unsigned 64-bit integer")
    output = Path(args[args.index("-o") + 1]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    reports = Path(os.environ.get("HIKARI_REPORT_DIR", "/tmp/hikari-reports"))
    reports.mkdir(parents=True, exist_ok=True)
    key = output.name + "." + hashlib.sha256(str(output).encode()).hexdigest()[:12]
    report_path = reports / (key + ".json")
    report = dict(object=str(output), flags=flags, seed=seed, apple_opt=int(level),
                  plugin_sha256=hashlib.sha256(plugin.read_bytes()).hexdigest(),
                  apple_version=subprocess.check_output([apple, "--version"], text=True).strip(),
                  llvm_version=subprocess.check_output([str(llvm / "clang"), "--version"], text=True).strip(),
                  commands=[], success=False)

    def run(command):
        report["commands"].append(list(map(str, command)))
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)

    try:
        with tempfile.TemporaryDirectory(prefix="hikari-apple-") as td, (
                reports / (key + ".log")).open("w") as log:
            raw, obf = Path(td) / "apple.ll", Path(td) / "hikari.ll"
            front = []
            i = 0
            while i < len(args):
                if args[i] == "-o":
                    i += 2
                    continue
                if args[i] != "-c":
                    front.append(args[i])
                i += 1
            run([apple, *front, "-g0", "-S", "-emit-llvm", "-o", str(raw)])
            text = raw.read_text()
            triple = re.search(r'target triple = "([^"]+)"', text)[1]
            # Salt a replay seed with the module so different modules get different keys.
            module_seed = int.from_bytes(hashlib.sha256(
                seed.to_bytes(8, "little") + raw.read_bytes()).digest()[:8], "little")
            report.update(target=triple, module_seed=module_seed,
                          source_ir_sha256=hashlib.sha256(raw.read_bytes()).hexdigest())
            run([str(llvm / "clang"), "-target", triple, "-x", "ir", "-O0",
                 "-Xclang", "-load", "-Xclang", str(plugin), f"-fpass-plugin={plugin}",
                 "-mllvm", f"-aesSeed={module_seed}",
                 *[a for flag in flags for a in ("-mllvm", flag)],
                 "-S", "-emit-llvm", str(raw), "-o", str(obf)])
            run([str(llvm / "opt"), "-passes=verify", "-disable-output", str(obf)])
            transformed = obf.read_text()
            if "ptrauth.abi-version" in text and "ptrauth.abi-version" not in transformed:
                raise RuntimeError("Hikari lost Apple pointer-authentication ABI metadata")
            report["obfuscated_ir_sha256"] = hashlib.sha256(obf.read_bytes()).hexdigest()
            obf.write_text(apple_ir(transformed))
            if os.environ.get("HIKARI_KEEP_IR") == "1":
                shutil.copy2(raw, reports / (key + ".input.ll"))
                shutil.copy2(obf, reports / (key + ".obfuscated.ll"))
            backend = [apple, "-target", triple, "-x", "ir", f"-O{level}",
                       "-c", str(obf), "-o", str(output)]
            if "-isysroot" in args:
                backend += ["-isysroot", args[args.index("-isysroot") + 1]]
            if "-fobjc-arc" in args:
                backend.append("-fobjc-arc")
            run(backend)
            report["object_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
            report["success"] = True
    finally:
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        if not report["success"]:
            print(f"Hikari failed; report {report_path}", file=sys.stderr)
    print(f"Hikari: {output.name}, Apple -O{level}, report {report_path}", flush=True)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
