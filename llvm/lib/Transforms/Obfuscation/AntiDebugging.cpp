/*
 *  LLVM AntiDebugging Pass
    Copyright (C) 2017 Zhang(https://github.com/Naville/)

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */
#include "llvm/Transforms/Obfuscation/AntiDebugging.h"
#include "llvm/ADT/SmallString.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/InlineAsm.h"
#include "llvm/IR/Intrinsics.h"
#include "llvm/IRReader/IRReader.h"
#include "llvm/Linker/Linker.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/SourceMgr.h"
#include "llvm/TargetParser/Triple.h"
#include "llvm/Transforms/Obfuscation/CryptoUtils.h"
#include "llvm/Transforms/Obfuscation/Utils.h"
#include "llvm/Transforms/Utils/ModuleUtils.h"

using namespace llvm;

static cl::opt<std::string>
    PreCompiledIRPath("adbextirpath",
                      cl::desc("External pre-compiled AntiDebugging IR"),
                      cl::value_desc("filename"), cl::init(""));
static cl::opt<uint32_t>
    ProbRate("adb_prob",
             cl::desc("Percentage of functions receiving extra entry guards"),
             cl::value_desc("Probability Rate"), cl::init(40));

namespace llvm {
namespace {
struct AntiDebugging : public ModulePass {
  static char ID;
  bool flag;
  AntiDebugging() : ModulePass(ID), flag(true) {}
  AntiDebugging(bool Flag) : ModulePass(ID), flag(Flag) {}
  StringRef getPassName() const override { return "AntiDebugging"; }

  Function *createBuiltin(Module &M, const Triple &Target) {
    LLVMContext &C = M.getContext();
    Function *Init =
        Function::Create(FunctionType::get(Type::getVoidTy(C), false),
                         GlobalValue::PrivateLinkage, "__hikari_adb_init", M);
    Init->addFnAttr("hikari.runtime");
    Init->addFnAttr(Attribute::NoInline);
    Init->addFnAttr(Attribute::NoUnwind);
    auto *Done = new GlobalVariable(
        M, Type::getInt8Ty(C), false, GlobalValue::PrivateLinkage,
        ConstantInt::get(Type::getInt8Ty(C), 0), "__hikari_adb_done");
    Done->setAlignment(Align(1));
    BasicBlock *Entry = BasicBlock::Create(C, "entry", Init);
    BasicBlock *Check = BasicBlock::Create(C, "check", Init);
    BasicBlock *Success = BasicBlock::Create(C, "success", Init);
    BasicBlock *Failure = BasicBlock::Create(C, "failure", Init);
    BasicBlock *Exit = BasicBlock::Create(C, "exit", Init);
    IRBuilder<> B(Entry);
    auto *Loaded = B.CreateLoad(B.getInt8Ty(), Done);
    Loaded->setAtomic(AtomicOrdering::Acquire);
    B.CreateCondBr(B.CreateICmpNE(Loaded, B.getInt8(0)), Exit, Check);
    B.SetInsertPoint(Check);
    Value *Result;
    if (Target.isAArch64()) {
      // Darwin reports syscall errors in carry; declare every overwritten
      // register.
      InlineAsm *IA = InlineAsm::get(
          FunctionType::get(B.getInt64Ty(), false),
          "mov x0, #31\nmov x1, #0\nmov x2, #0\nmov x3, #0\n"
          "mov x16, #26\nsvc #0x80\ncneg x0, x0, cs",
          "={x0},~{x1},~{x2},~{x3},~{x16},~{x17},~{cc},~{memory}", true);
      Result = B.CreateCall(IA->getFunctionType(), IA);
    } else {
      auto Ptrace = M.getOrInsertFunction(
          "ptrace", FunctionType::get(B.getInt32Ty(),
                                      {B.getInt32Ty(), B.getInt32Ty(),
                                       B.getPtrTy(), B.getInt32Ty()},
                                      false));
      Result = B.CreateCall(Ptrace, {B.getInt32(31), B.getInt32(0),
                                     ConstantPointerNull::get(B.getPtrTy()),
                                     B.getInt32(0)});
    }
    B.CreateCondBr(
        B.CreateICmpEQ(Result, ConstantInt::get(Result->getType(), 0)), Success,
        Failure);
    B.SetInsertPoint(Success);
    auto *Store = B.CreateStore(B.getInt8(1), Done);
    Store->setAtomic(AtomicOrdering::Release);
    B.CreateBr(Exit);
    B.SetInsertPoint(Failure);
    B.CreateIntrinsic(Intrinsic::trap, ArrayRef<Type *>{}, {});
    B.CreateUnreachable();
    B.SetInsertPoint(Exit);
    B.CreateRetVoid();
    return Init;
  }

  Function *initialize(Module &M, const Triple &Target) {
    SmallString<128> Path(PreCompiledIRPath.getValue());
    if (Path.empty() && sys::path::home_directory(Path)) {
      sys::path::append(Path, "Hikari",
                        "PrecompiledAntiDebugging-" +
                            Triple::getArchTypeName(Target.getArch()) + "-" +
                            Triple::getOSTypeName(Target.getOS()) + ".bc");
    }
    if (!Path.empty() && sys::fs::exists(Path)) {
      SMDiagnostic Diagnostic;
      auto IR = parseIRFile(Path, Diagnostic, M.getContext());
      if (!IR) {
        Diagnostic.print("Hikari AntiDebugging", errs());
        report_fatal_error("Cannot parse AntiDebugging IR");
      }
      for (Function &F : *IR)
        if (!F.isDeclaration())
          F.addFnAttr("hikari.runtime");
      if (Linker::linkModules(M, std::move(IR)))
        report_fatal_error("Cannot link AntiDebugging IR");
      Function *Init = M.getFunction("InitADB");
      if (!Init || Init->isDeclaration() ||
          Init->getFunctionType() !=
              FunctionType::get(Type::getVoidTy(M.getContext()), false))
        report_fatal_error("AntiDebugging IR must define void InitADB(void)");
      for (StringRef Name : {"InitADB", "ADBCallBack"})
        if (Function *F = M.getFunction(Name)) {
          F->addFnAttr("hikari.runtime");
          F->setLinkage(GlobalValue::InternalLinkage);
        }
      return Init;
    }
    if (!PreCompiledIRPath.empty())
      report_fatal_error(Twine("AntiDebugging IR not found: ") +
                         PreCompiledIRPath);
    errs() << "Using built-in AntiDebugging for " << Target.str() << "\n";
    return createBuiltin(M, Target);
  }

  bool runOnModule(Module &M) override {
    SmallVector<Function *, 16> Functions;
    for (Function &F : M)
      if (F.getName() != "InitADB" && F.getName() != "ADBCallBack" &&
          toObfuscate(flag, &F, "adb"))
        Functions.push_back(&F);
    if (Functions.empty())
      return false;
    if (ProbRate == 0 || ProbRate > 100)
      report_fatal_error("-adb_prob must be between 1 and 100");
    Triple Target(M.getTargetTriple());
    if (!Target.isOSDarwin())
      report_fatal_error("AntiDebugging requires an Apple Darwin target");
    Function *Init = initialize(M, Target);
    // A load-time guard cannot depend on a random choice or a function being
    // called.
    appendToGlobalCtors(M, Init, 0);
    for (Function *F : Functions)
      if (cryptoutils->get_range(100) < ProbRate) {
        IRBuilder<> B(&*F->getEntryBlock().getFirstInsertionPt());
        B.CreateCall(Init);
      }
    return true;
  }
};
} // namespace
ModulePass *createAntiDebuggingPass(bool Flag) {
  return new AntiDebugging(Flag);
}
char AntiDebugging::ID = 0;
INITIALIZE_PASS(AntiDebugging, "adb", "Enable AntiDebugging.", false, false)
} // namespace llvm
