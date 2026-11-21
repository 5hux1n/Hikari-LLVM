/*
    LLVM Anti Hooking Pass
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
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/STLExtras.h"
#include "llvm/Analysis/ValueTracking.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/Intrinsics.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/InlineAsm.h"
#include "llvm/IR/InstIterator.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/Module.h"
#include "llvm/IRReader/IRReader.h"
#include "llvm/Linker/Linker.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/SourceMgr.h"
#include "llvm/Support/raw_ostream.h"
#if LLVM_VERSION_MAJOR >= 17
#include "llvm/ADT/SmallString.h"
#include "llvm/TargetParser/Triple.h"
#else
#include "llvm/ADT/Triple.h"
#endif
#include "llvm/Transforms/Obfuscation/AntiHook.h"
#include "llvm/Transforms/Obfuscation/CryptoUtils.h"
#include "llvm/Transforms/Obfuscation/Utils.h"
#include "llvm/Transforms/Obfuscation/compat/CallSite.h"
#include "llvm/Transforms/Utils/ModuleUtils.h"
#include <fstream>

// Arm A64 Instruction Set for A-profile architecture 2022-12, Page 56
#define AARCH64_SIGNATURE_B 0b000101
// Arm A64 Instruction Set for A-profile architecture 2022-12, Page 75
#define AARCH64_SIGNATURE_BR 0b1101011000011111000000
// Arm A64 Instruction Set for A-profile architecture 2022-12, Page 79
#define AARCH64_SIGNATURE_BRK 0b11010100001

using namespace llvm;

static cl::opt<std::string>
    PreCompiledIRPath("adhexrirpath",
                      cl::desc("External Path Pointing To Pre-compiled Anti "
                               "Hooking Handler IR"),
                      cl::value_desc("filename"), cl::init(""));

static cl::opt<bool> CheckInlineHook("ah_inline", cl::init(true), cl::NotHidden,
                                     cl::desc("Check Inline Hook for AArch64"));
static bool CheckInlineHookTemp = true;

static cl::opt<bool>
    CheckObjectiveCRuntimeHook("ah_objcruntime", cl::init(true), cl::NotHidden,
                               cl::desc("Check Objective-C Runtime Hook"));
static bool CheckObjectiveCRuntimeHookTemp = true;

static cl::opt<bool> AntiRebindSymbol("ah_antirebind", cl::init(false),
                                      cl::NotHidden,
                                      cl::desc("Make fishhook unavailable"));
static bool AntiRebindSymbolTemp = false;

namespace llvm {
struct AntiHook : public ModulePass {
  struct ObjcMethod {
    std::string ClassName;
    std::string Selector;
    bool ClassMethod;
    SmallVector<Function *, 2> Implementations;
    Function *Guard = nullptr;
  };
  static char ID;
  bool flag;
  bool initialized;
  bool opaquepointers;
  Triple triple;
  AntiHook() : ModulePass(ID) {
    this->flag = true;
    this->initialized = false;
  }
  AntiHook(bool flag) : ModulePass(ID) {
    this->flag = flag;
    this->initialized = false;
  }
  StringRef getPassName() const override { return "AntiHook"; }
  bool initialize(Module &M) {
    this->triple = Triple(M.getTargetTriple());
    if (PreCompiledIRPath == "") {
      SmallString<32> Path;
      if (sys::path::home_directory(Path)) { // Stolen from LineEditor.cpp
        sys::path::append(Path, "Hikari");
        sys::path::append(Path,
                          "PrecompiledAntiHooking-" +
                              Triple::getArchTypeName(triple.getArch()) + "-" +
                              Triple::getOSTypeName(triple.getOS()) + ".bc");
        PreCompiledIRPath = Path.c_str();
      }
    }
    std::ifstream f(PreCompiledIRPath);
    if (f.good()) {
      errs() << "Linking PreCompiled AntiHooking IR From:" << PreCompiledIRPath
             << "\n";
      SMDiagnostic SMD;
      std::unique_ptr<Module> ADBM(
          parseIRFile(StringRef(PreCompiledIRPath), SMD, M.getContext()));
      if (!ADBM) {
        SMD.print("Hikari AntiHooking", errs());
        report_fatal_error("Cannot parse AntiHooking IR");
      }
      for (Function &F : *ADBM)
        if (!F.isDeclaration())
          F.addFnAttr("hikari.runtime");
      if (Linker::linkModules(M, std::move(ADBM)))
        report_fatal_error("Cannot link AntiHooking IR");
      Function *Handler = M.getFunction("AHCallBack");
      if (!Handler || Handler->isDeclaration() ||
          Handler->getFunctionType() !=
              FunctionType::get(Type::getVoidTy(M.getContext()), false))
        report_fatal_error("AntiHooking IR must define void AHCallBack(void)");
      Handler->setLinkage(GlobalValue::InternalLinkage);
    } else {
      if (PreCompiledIRPath.getNumOccurrences())
        report_fatal_error(Twine("AntiHooking IR not found: ") +
                           PreCompiledIRPath);
      errs() << "Using built-in AntiHooking handler\n";
    }
#if LLVM_VERSION_MAJOR >= 17
    opaquepointers = true;
#else
    opaquepointers = !M.getContext().supportsTypedPointers();
#endif

    if (triple.getVendor() == Triple::VendorType::Apple &&
        StructType::getTypeByName(M.getContext(), "struct._objc_method")) {
      Type *Int8PtrTy = Type::getInt8Ty(M.getContext())->getPointerTo();
      M.getOrInsertFunction("objc_getClass",
                            FunctionType::get(Int8PtrTy, {Int8PtrTy}, false));
      M.getOrInsertFunction("sel_registerName",
                            FunctionType::get(Int8PtrTy, {Int8PtrTy}, false));
      FunctionType *IMPType =
          FunctionType::get(Int8PtrTy, {Int8PtrTy, Int8PtrTy}, true);
      PointerType *IMPPointerType = PointerType::getUnqual(IMPType);
      M.getOrInsertFunction(
          "method_getImplementation",
          FunctionType::get(IMPPointerType,
                            {PointerType::getUnqual(StructType::getTypeByName(
                                M.getContext(), "struct._objc_method"))},
                            false));
      M.getOrInsertFunction(
          "class_getInstanceMethod",
          FunctionType::get(PointerType::getUnqual(StructType::getTypeByName(
                                M.getContext(), "struct._objc_method")),
                            {Int8PtrTy, Int8PtrTy}, false));
      M.getOrInsertFunction(
          "class_getClassMethod",
          FunctionType::get(PointerType::getUnqual(StructType::getTypeByName(
                                M.getContext(), "struct._objc_method")),
                            {Int8PtrTy, Int8PtrTy}, false));
    }
    this->initialized = true;
    return true;
  }

  bool runOnModule(Module &M) override {
    SmallVector<Function *, 16> Functions;
    SmallVector<Function *, 16> ObjcCallers;
    SmallVector<ObjcMethod, 16> Methods;
    for (Function &F : M)
      if (toObfuscate(flag, &F, "antihook"))
        Functions.push_back(&F);
    for (Function *FP : Functions) {
      Function &F = *FP;
      errs() << "Running AntiHooking On " << F.getName() << "\n";
      if (!this->initialized)
        initialize(M);
      if (!toObfuscateBoolOption(&F, "ah_inline", &CheckInlineHookTemp))
        CheckInlineHookTemp = CheckInlineHook;
      if (triple.isAArch64() && CheckInlineHookTemp) {
        HandleInlineHookAArch64(&F);
      }
      if (!toObfuscateBoolOption(&F, "ah_objcruntime",
                                 &CheckObjectiveCRuntimeHookTemp))
        CheckObjectiveCRuntimeHookTemp = CheckObjectiveCRuntimeHook;
      if (!CheckObjectiveCRuntimeHookTemp)
        continue;
      ObjcCallers.push_back(&F);
      GlobalVariable *methodListGV = nullptr;
      ConstantStruct *methodStruct = nullptr;
      SmallVector<User *, 16> Worklist(F.user_begin(), F.user_end());
      SmallPtrSet<User *, 32> Visited;
      while (!Worklist.empty()) {
        User *U = Worklist.pop_back_val();
        if (!Visited.insert(U).second || !isa<Constant>(U))
          continue;
        if (auto *CS = dyn_cast<ConstantStruct>(U))
          if (CS->getType()->hasName() &&
              CS->getType()->getName() == "struct._objc_method")
            methodStruct = CS;
        if (auto *GV = dyn_cast<GlobalVariable>(U)) {
          if (GV->getName().starts_with("_OBJC_$_INSTANCE_METHODS") ||
              GV->getName().starts_with("_OBJC_$_CLASS_METHODS") ||
              GV->getName().starts_with("_OBJC_$_CATEGORY_INSTANCE_METHODS_") ||
              GV->getName().starts_with("_OBJC_$_CATEGORY_CLASS_METHODS_"))
            methodListGV = GV;
          // Older Apple IR stores authenticated pointers in wrapper globals.
          if (GV->hasSection() && GV->getSection() == "llvm.ptrauth")
            Worklist.append(GV->user_begin(), GV->user_end());
          continue;
        }
        Worklist.append(U->user_begin(), U->user_end());
      }
      if (methodListGV && methodStruct) {
        GlobalVariable *SELNameGV = cast<GlobalVariable>(
            methodStruct->getOperand(0)->stripPointerCasts());
        ConstantDataSequential *SELNameCDS =
            cast<ConstantDataSequential>(SELNameGV->getInitializer());
        StringRef ListName = methodListGV->getName();
        bool category = ListName.starts_with("_OBJC_$_CATEGORY_");
        bool classmethod =
            ListName.starts_with("_OBJC_$_CLASS_METHODS_") ||
            ListName.starts_with("_OBJC_$_CATEGORY_CLASS_METHODS_");
        std::string classname;
        if (category) {
          Worklist.append(methodListGV->user_begin(), methodListGV->user_end());
          Visited.clear();
          while (!Worklist.empty() && classname.empty()) {
            User *U = Worklist.pop_back_val();
            if (!Visited.insert(U).second || !isa<Constant>(U))
              continue;
            if (auto *GV = dyn_cast<GlobalVariable>(U)) {
              if (GV->hasSection() && GV->getSection() == "llvm.ptrauth")
                Worklist.append(GV->user_begin(), GV->user_end());
              continue;
            }
            if (auto *CS = dyn_cast<ConstantStruct>(U))
              if (CS->getType()->hasName() &&
                  CS->getType()->getName() == "struct._category_t") {
                auto *Class = dyn_cast<GlobalVariable>(
                    CS->getOperand(1)->stripPointerCasts());
                if (Class && Class->getName().starts_with("OBJC_CLASS_$_"))
                  classname = Class->getName()
                                  .drop_front(strlen("OBJC_CLASS_$_"))
                                  .str();
              }
            Worklist.append(U->user_begin(), U->user_end());
          }
          if (classname.empty())
            report_fatal_error("Cannot resolve AntiHooking category class");
        } else {
          classname =
              ListName
                  .drop_front(strlen(classmethod ? "_OBJC_$_CLASS_METHODS_"
                                                 : "_OBJC_$_INSTANCE_METHODS_"))
                  .str();
        }
        std::string selname = SELNameCDS->getAsCString().str();
        auto Method = llvm::find_if(Methods, [&](const ObjcMethod &Method) {
          return Method.ClassName == classname && Method.Selector == selname &&
                 Method.ClassMethod == classmethod;
        });
        if (Method == Methods.end())
          Methods.push_back({classname, selname, classmethod, {&F}});
        else
          Method->Implementations.push_back(&F);
      }
    }
    if (!Methods.empty())
      HandleObjcRuntimeHooks(M, Methods, ObjcCallers);
    for (Function *F : Functions)
      HandleAntiRebind(*F, M);
    return true;
  } // End runOnFunction

  void HandleAntiRebind(Function &F, Module &M) {
    if (!toObfuscateBoolOption(&F, "ah_antirebind", &AntiRebindSymbolTemp))
      AntiRebindSymbolTemp = AntiRebindSymbol;
    SmallVector<CallBase *, 8> ToAuthenticate;
    if (AntiRebindSymbolTemp)
      for (Instruction &I : instructions(F))
        if (isa<CallInst>(&I) || isa<InvokeInst>(&I)) {
          CallSite CS(&I);
          Function *Called = CS.getCalledFunction();
          if (!Called)
            Called =
                dyn_cast<Function>(CS.getCalledValue()->stripPointerCasts());
          if (Called && Called->isDeclaration() &&
              Called->isExternalLinkage(Called->getLinkage()) &&
              !Called->isIntrinsic() &&
              !Called->getName().starts_with("clang.")) {
            GlobalVariable *GV = cast<GlobalVariable>(M.getOrInsertGlobal(
                ("AntiRebindSymbol_" + Called->getName()).str(),
                Called->getType()));
            if (!GV->hasInitializer()) {
              GV->setConstant(true);
              Constant *Pointer = Called;
#if LLVM_VERSION_MAJOR >= 22
              if (triple.getArchName() == "arm64e")
                Pointer = ConstantPtrAuth::get(
                    Called,
                    ConstantInt::get(Type::getInt32Ty(M.getContext()), 0),
                    ConstantInt::get(Type::getInt64Ty(M.getContext()), 0),
                    ConstantPointerNull::get(
                        PointerType::getUnqual(M.getContext())),
                    ConstantPointerNull::get(
                        PointerType::getUnqual(M.getContext())));
#endif
              GV->setInitializer(Pointer);
              GV->setLinkage(GlobalValue::PrivateLinkage);
            }
            appendToCompilerUsed(M, {GV});
            Value *Load =
                new LoadInst(GV->getValueType(), GV, Called->getName(), &I);
            CS.setCalledFunction(BitCastInst::CreateBitOrPointerCast(
                Load, CS.getCalledValue()->getType(), "", &I));
#if LLVM_VERSION_MAJOR >= 22
            auto *CB = cast<CallBase>(&I);
            if (F.hasFnAttribute("ptrauth-calls") &&
                !CB->getOperandBundle(LLVMContext::OB_ptrauth))
              ToAuthenticate.push_back(CB);
#endif
          }
        }
    for (CallBase *CB : ToAuthenticate) {
      OperandBundleDef Auth(
          "ptrauth",
          ArrayRef<Value *>{
              ConstantInt::get(Type::getInt32Ty(M.getContext()), 0),
              ConstantInt::get(Type::getInt64Ty(M.getContext()), 0)});
      CallBase *New = CallBase::addOperandBundle(CB, LLVMContext::OB_ptrauth,
                                                 Auth, CB->getIterator());
      New->takeName(CB);
      CB->replaceAllUsesWith(New);
      CB->eraseFromParent();
    }
  }

  Value *codeAddress(IRBuilder<> &B, Value *Pointer) {
    if (triple.getArchName() != "arm64e")
      return Pointer;
    Value *Raw = B.CreateIntrinsic(
        Intrinsic::ptrauth_strip, {},
        {B.CreatePtrToInt(Pointer, B.getInt64Ty()), B.getInt32(0)});
    return B.CreateIntToPtr(Raw, Pointer->getType());
  }

  void HandleInlineHookAArch64(Function *F) {
    BasicBlock *A = &(F->getEntryBlock());
    BasicBlock *C = A->splitBasicBlock(A->getFirstNonPHIOrDbgOrLifetime());
    BasicBlock *B =
        BasicBlock::Create(F->getContext(), "HookDetectedHandler", F);
    BasicBlock *Detect = BasicBlock::Create(F->getContext(), "", F);
    BasicBlock *Detect2 = BasicBlock::Create(F->getContext(), "", F);
    // Change A's terminator to jump to B
    // We'll add new terminator in B to jump C later
    A->getTerminator()->eraseFromParent();
    BranchInst::Create(Detect, A);

    IRBuilder<> IRBDetect(Detect);
    IRBuilder<> IRBDetect2(Detect2);
    IRBuilder<> IRBB(B);

    Type *Int64Ty = Type::getInt64Ty(F->getContext());
    Type *Int32Ty = Type::getInt32Ty(F->getContext());
    Type *Int32PtrTy = Type::getInt32Ty(F->getContext())->getPointerTo();

    F->addFnAttr("disable-tail-calls", "true");
    Value *Address = codeAddress(IRBDetect, F);
    auto *Load = IRBDetect.CreateLoad(
        Int32Ty, IRBDetect.CreateBitCast(Address, Int32PtrTy));
    Load->setVolatile(true);
    Value *LS2 = IRBDetect.CreateLShr(Load, ConstantInt::get(Int32Ty, 26));
    Value *ICmpEQ2 = IRBDetect.CreateICmpEQ(
        LS2, ConstantInt::get(Int32Ty, AARCH64_SIGNATURE_B));
    Value *LS3 = IRBDetect.CreateLShr(Load, ConstantInt::get(Int32Ty, 21));
    Value *ICmpEQ3 = IRBDetect.CreateICmpEQ(
        LS3, ConstantInt::get(Int32Ty, AARCH64_SIGNATURE_BRK));
    Value *Or = IRBDetect.CreateOr(ICmpEQ2, ICmpEQ3);
    IRBDetect.CreateCondBr(Or, B, Detect2);

    Value *PTI = IRBDetect2.CreatePtrToInt(Address, Int64Ty);
    Value *AddFour = IRBDetect2.CreateAdd(PTI, ConstantInt::get(Int64Ty, 4));
    Value *ITP = IRBDetect2.CreateIntToPtr(AddFour, Int32PtrTy);
    auto *Load2 = IRBDetect2.CreateLoad(Int32Ty, ITP);
    Load2->setVolatile(true);
    Value *LS4 = IRBDetect2.CreateLShr(Load2, ConstantInt::get(Int32Ty, 10));
    Value *ICmpEQ4 = IRBDetect2.CreateICmpEQ(
        LS4, ConstantInt::get(Int32Ty, AARCH64_SIGNATURE_BR));
    Value *AddEight = IRBDetect2.CreateAdd(PTI, ConstantInt::get(Int64Ty, 8));
    Value *ITP2 = IRBDetect2.CreateIntToPtr(AddEight, Int32PtrTy);
    auto *Load3 = IRBDetect2.CreateLoad(Int32Ty, ITP2);
    Load3->setVolatile(true);
    Value *LS5 = IRBDetect2.CreateLShr(Load3, ConstantInt::get(Int32Ty, 10));
    Value *ICmpEQ5 = IRBDetect2.CreateICmpEQ(
        LS5, ConstantInt::get(Int32Ty, AARCH64_SIGNATURE_BR));
    Value *Or2 = IRBDetect2.CreateOr(ICmpEQ4, ICmpEQ5);
    IRBDetect2.CreateCondBr(Or2, B, C);
    CreateCallbackAndJumpBack(&IRBB, C);
  }

  void HandleObjcRuntimeHooks(Module &M, MutableArrayRef<ObjcMethod> Methods,
                              ArrayRef<Function *> Callers) {
    LLVMContext &C = M.getContext();
    for (ObjcMethod &Method : Methods) {
      Function *Guard = Function::Create(
          FunctionType::get(Type::getVoidTy(C), false),
          GlobalValue::PrivateLinkage, "__hikari_ah_method", M);
      Guard->addFnAttr("hikari.runtime");
      Guard->addFnAttr(Attribute::NoInline);
      Method.Guard = Guard;
      BasicBlock *Entry = BasicBlock::Create(C, "entry", Guard);
      BasicBlock *Failure = BasicBlock::Create(C, "detected", Guard);
      BasicBlock *Exit = BasicBlock::Create(C, "exit", Guard);
      IRBuilder<> B(Entry);
      Value *Class = B.CreateCall(M.getFunction("objc_getClass"),
                                  {B.CreateGlobalStringPtr(Method.ClassName)});
      Value *Selector =
          B.CreateCall(M.getFunction("sel_registerName"),
                       {B.CreateGlobalStringPtr(Method.Selector)});
      Value *RuntimeMethod = B.CreateCall(
          M.getFunction(Method.ClassMethod ? "class_getClassMethod"
                                           : "class_getInstanceMethod"),
          {Class, Selector});
      Value *IMP =
          codeAddress(B, B.CreateCall(M.getFunction("method_getImplementation"),
                                      {RuntimeMethod}));
      Value *Matches = B.getFalse();
      // A category can legitimately override a method in the same module.
      for (Function *Implementation : Method.Implementations)
        Matches = B.CreateOr(
            Matches, B.CreateICmpEQ(IMP, codeAddress(B, Implementation)));
      B.CreateCondBr(Matches, Exit, Failure);
      B.SetInsertPoint(Failure);
      CreateCallbackAndJumpBack(&B, Exit);
      B.SetInsertPoint(Exit);
      B.CreateRetVoid();
      for (Function *Implementation : Method.Implementations) {
        IRBuilder<> EntryBuilder(
            &*Implementation->getEntryBlock().getFirstInsertionPt());
        EntryBuilder.CreateCall(Guard);
      }
    }

    Function *DispatchGuard = Function::Create(
        FunctionType::get(Type::getVoidTy(C),
                          {Type::getInt8Ty(C)->getPointerTo()}, false),
        GlobalValue::PrivateLinkage, "__hikari_ah_dispatch", M);
    DispatchGuard->addFnAttr("hikari.runtime");
    DispatchGuard->addFnAttr(Attribute::NoInline);
    IRBuilder<> B(BasicBlock::Create(C, "entry", DispatchGuard));
    for (const ObjcMethod &Method : Methods) {
      BasicBlock *Check = BasicBlock::Create(C, "check", DispatchGuard);
      BasicBlock *Next = BasicBlock::Create(C, "next", DispatchGuard);
      Value *Selector =
          B.CreateCall(M.getFunction("sel_registerName"),
                       {B.CreateGlobalStringPtr(Method.Selector)});
      B.CreateCondBr(B.CreateICmpEQ(DispatchGuard->getArg(0), Selector), Check,
                     Next);
      B.SetInsertPoint(Check);
      B.CreateCall(Method.Guard);
      B.CreateBr(Next);
      B.SetInsertPoint(Next);
    }
    B.CreateRetVoid();
    // shortcut: module-local checks; add a registry for cross-module sends.
    for (Function *Caller : Callers)
      for (Instruction &I : instructions(Caller))
        if (auto *Call = dyn_cast<CallBase>(&I)) {
          auto *Callee =
              dyn_cast<Function>(Call->getCalledOperand()->stripPointerCasts());
          if (!Callee || !Callee->getName().starts_with("objc_msgSend"))
            continue;
          unsigned SelectorIndex = Callee->getName().contains("_stret") ? 2 : 1;
          if (Call->arg_size() <= SelectorIndex)
            continue;
          IRBuilder<> CallBuilder(Call);
          Value *Selector = Call->getArgOperand(SelectorIndex);
          StringRef SelectorName;
          bool KnownSelector = false;
          // Apple selector stubs pass undef in the unused selector argument.
          size_t StubSeparator = Callee->getName().find('$');
          if (StubSeparator != StringRef::npos) {
            SelectorName = Callee->getName().drop_front(StubSeparator + 1);
            KnownSelector = true;
          } else if (auto *Load = dyn_cast<LoadInst>(Selector))
            if (auto *GV = dyn_cast<GlobalVariable>(
                    Load->getPointerOperand()->stripPointerCasts()))
              if (GV->getName().starts_with("OBJC_SELECTOR_REFERENCES_") &&
                  GV->hasInitializer())
                KnownSelector =
                    getConstantStringInfo(GV->getInitializer(), SelectorName);
          if (KnownSelector) {
            for (const ObjcMethod &Method : Methods)
              if (Method.Selector == SelectorName)
                CallBuilder.CreateCall(Method.Guard);
          } else {
            CallBuilder.CreateCall(DispatchGuard, {Selector});
          }
        }
  }
  void CreateCallbackAndJumpBack(IRBuilder<> *IRBB, BasicBlock *C) {
    Module *M = C->getModule();
    Function *AHCallBack = M->getFunction("AHCallBack");
    if (AHCallBack) {
      if (AHCallBack->isDeclaration() ||
          AHCallBack->getFunctionType() !=
              FunctionType::get(IRBB->getVoidTy(), false))
        report_fatal_error("AntiHooking IR must define void AHCallBack(void)");
      IRBB->CreateCall(AHCallBack);
      IRBB->CreateBr(C);
    } else {
      IRBB->CreateIntrinsic(Intrinsic::trap, ArrayRef<Type *>{}, {});
      IRBB->CreateUnreachable();
    }
  }
};
} // namespace llvm

ModulePass *llvm::createAntiHookPass(bool flag) { return new AntiHook(flag); }
char AntiHook::ID = 0;
INITIALIZE_PASS(AntiHook, "antihook", "AntiHook", false, false)
