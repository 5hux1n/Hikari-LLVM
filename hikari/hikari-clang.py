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


# Quoted tokens, comments, and IR identifiers can contain the same text as
# optimizer assertions. Match them first and preserve them verbatim.
_IR_TOKENS = re.compile(
    r'(?P<literal>"[^"\n]*"|;[^\n]*|[%@$!][-\w$.]+|[-\w$.]+(?=:))'
    r'|(?<![-\w$.])(?:captures\([^()\n]*\)|nocreateundeforpoison(?![-\w$.])'
    r'|(?P<icmp>icmp[ \t]+)samesign[ \t]+)'
)
_COMPAT_INTRINSICS = re.compile(
    r"(?m)^[ \t]*(?:(?:(?:tail|musttail|notail) )?call|declare) void"
    r"(?: \(\.\.\.\))? @llvm\.(?:objc\.clang\.arc\.(?:noop\.)?use"
    r"|lifetime\.(?:start|end)\.[\w.]+)\([^\n]*(?:\n|$)"
)


def apple_ir(text):
    # ARC use markers have no machine code. LLVM 22's one-operand lifetime
    # calls cannot be read by older Apple LLVM and are optional.
    text = _COMPAT_INTRINSICS.sub("", text)
    return _IR_TOKENS.sub(lambda match: match["literal"] or match["icmp"] or "", text)


def write_json(path, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def toolchain_metadata(reports, apple, clang, plugin):
    tools = [Path(path).resolve(strict=True) for path in (apple, clang, plugin)]
    key = hashlib.sha256(os.fsencode("\0".join(map(str, tools)))).hexdigest()[:16]
    cache = reports / f".toolchain-{key}.cache"

    def stamp():
        stats = (path.stat() for path in tools)
        return [[st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
                for st in stats]

    before = stamp()
    try:
        cached = json.loads(cache.read_text())
        if cached["stamp"] == before:
            return cached["metadata"]
    except (OSError, KeyError, TypeError, ValueError):
        pass

    metadata = dict(
        plugin_sha256=hashlib.sha256(tools[2].read_bytes()).hexdigest(),
        apple_version=subprocess.check_output([apple, "--version"], text=True).strip(),
        llvm_version=subprocess.check_output([clang, "--version"], text=True).strip(),
    )
    if stamp() == before:
        write_json(cache, dict(stamp=before, metadata=metadata))
    return metadata


def lto_enabled(args):
    for arg in reversed(args):
        if arg == "-fno-lto":
            return False
        if arg == "-flto" or arg.startswith("-flto="):
            return True
    return False


def main(args):
    apple = os.environ.get("HIKARI_APPLE_CLANG") or subprocess.check_output(
        ["xcrun", "--find", "clang"], text=True).strip()
    if "-c" not in args or "-o" not in args or any(
            Path(a).suffix in (".s", ".S") for a in args):
        os.execv(apple, [apple, *args])
    if lto_enabled(args):
        raise ValueError("Hikari's Apple pipeline does not support -flto; use -fno-lto")
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
                  commands=[], success=False)

    def run(command):
        report["commands"].append(list(map(str, command)))
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)

    try:
        report.update(toolchain_metadata(reports, apple, str(llvm / "clang"), plugin))
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
            run([apple, *front, "-S", "-emit-llvm", "-o", str(raw)])
            raw_bytes = raw.read_bytes()
            text = raw_bytes.decode()
            triple = re.search(r'^target triple = "([^"]+)"', text, re.M)
            if not triple:
                raise RuntimeError("Apple frontend did not emit LLVM IR with a target triple")
            triple = triple[1]
            # Salt a replay seed with the module so different modules get different keys.
            module_seed = int.from_bytes(hashlib.sha256(
                seed.to_bytes(8, "little") + raw_bytes).digest()[:8], "little")
            report.update(target=triple, module_seed=module_seed,
                          source_ir_sha256=hashlib.sha256(raw_bytes).hexdigest())
            run([str(llvm / "clang"), "-target", triple, "-x", "ir", "-O0",
                 "-Xclang", "-load", "-Xclang", str(plugin), f"-fpass-plugin={plugin}",
                 "-mllvm", f"-aesSeed={module_seed}",
                 *[a for flag in flags for a in ("-mllvm", flag)],
                 "-S", "-emit-llvm", str(raw), "-o", str(obf)])
            run([str(llvm / "opt"), "-passes=verify", "-disable-output", str(obf)])
            transformed_bytes = obf.read_bytes()
            transformed = transformed_bytes.decode()
            if "ptrauth.abi-version" in text and "ptrauth.abi-version" not in transformed:
                raise RuntimeError("Hikari lost Apple pointer-authentication ABI metadata")
            report["obfuscated_ir_sha256"] = hashlib.sha256(transformed_bytes).hexdigest()
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
        write_json(report_path, report)
        if not report["success"]:
            print(f"Hikari failed; report {report_path}", file=sys.stderr)
    print(f"Hikari: {output.name}, Apple -O{level}, report {report_path}", flush=True)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
