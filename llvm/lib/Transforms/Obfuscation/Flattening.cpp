// For open-source license, please refer to
// [License](https://github.com/HikariObfuscator/Hikari/wiki/License).
//===----------------------------------------------------------------------===//
#include "llvm/Transforms/Obfuscation/Flattening.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/Instructions.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Transforms/Obfuscation/CryptoUtils.h"
#include "llvm/Transforms/Obfuscation/Utils.h"
#include "llvm/Transforms/Utils/LowerSwitch.h"

using namespace llvm;

namespace {
struct Flattening : public FunctionPass {
  static char ID; // Pass identification, replacement for typeid
  bool flag;
  Flattening() : FunctionPass(ID) { this->flag = true; }
  Flattening(bool flag) : FunctionPass(ID) { this->flag = flag; }
  bool runOnFunction(Function &F) override;
  void flatten(Function *f);
};
} // namespace

char Flattening::ID = 0;
FunctionPass *llvm::createFlatteningPass(bool flag) {
  return new Flattening(flag);
}
INITIALIZE_PASS(Flattening, "cffobf", "Enable Control Flow Flattening.", false,
                false)
bool Flattening::runOnFunction(Function &F) {
  Function *tmp = &F;
  // Do we obfuscate
  if (toObfuscate(flag, tmp, "fla") && !F.isPresplitCoroutine()) {
    errs() << "Running ControlFlowFlattening On " << F.getName() << "\n";
    flatten(tmp);
  }

  return true;
}

void Flattening::flatten(Function *f) {
  SmallVector<BasicBlock *, 8> origBB;
  BasicBlock *loopEntry, *loopEnd;
  LoadInst *load;
  SwitchInst *switchI;
  AllocaInst *switchVar;
  const DataLayout &DL = f->getParent()->getDataLayout();

  PassBuilder PB;
  FunctionAnalysisManager FAM;
  FunctionPassManager FPM;
  PB.registerFunctionAnalyses(FAM);
  FPM.addPass(LowerSwitchPass());
  FPM.run(*f, FAM);

  for (BasicBlock &BB : *f) {
    if (BB.isEHPad() || BB.isLandingPad()) {
      errs() << f->getName()
             << " Contains Exception Handing Instructions and is unsupported "
                "for flattening in the open-source version of Hikari.\n";
      return;
    }
    if (!isa<BranchInst>(BB.getTerminator()) &&
        !isa<ReturnInst>(BB.getTerminator()))
      return;
    origBB.emplace_back(&BB);
  }

  BasicBlock *insert = &f->getEntryBlock();
  auto *entryBranch = dyn_cast<BranchInst>(insert->getTerminator());
  if (origBB.size() <= 1 || !entryBranch)
    return;

  // Demote PHIs while their original predecessor edges still exist.
  fixStack(f);

  origBB.erase(origBB.begin());
  if (entryBranch->isConditional())
    origBB.insert(origBB.begin(), insert->splitBasicBlock(entryBranch, "first"));
  Instruction *oldTerm = insert->getTerminator();
  BasicBlock *first = oldTerm->getSuccessor(0);

  // Create switch variable and set as it
  switchVar = new AllocaInst(Type::getInt32Ty(f->getContext()),
                             DL.getAllocaAddrSpace(), "switchVar", oldTerm);

  // Remove jump
  oldTerm->eraseFromParent();

  // Create main loop
  loopEntry = BasicBlock::Create(f->getContext(), "loopEntry", f, insert);
  loopEnd = BasicBlock::Create(f->getContext(), "loopEnd", f, insert);

  load = new LoadInst(switchVar->getAllocatedType(), switchVar, "switchVar",
                      loopEntry);
  load->setVolatile(true);

  // Move first BB on top
  insert->moveBefore(loopEntry);
  BranchInst::Create(loopEntry, insert);

  // loopEnd jump to loopEntry
  BranchInst::Create(loopEntry, loopEnd);

  BasicBlock *swDefault =
      BasicBlock::Create(f->getContext(), "switchDefault", f, loopEnd);
  BranchInst::Create(loopEnd, swDefault);

  // Create switch instruction itself and set condition
  switchI = SwitchInst::Create(load, swDefault, origBB.size(), loopEntry);

  // Put BB in the switch
  DenseSet<uint32_t> caseValues;
  for (BasicBlock *i : origBB) {
    // Move the BB inside the switch (only visual, no code logic)
    i->moveBefore(loopEnd);

    uint32_t value;
    do {
      value = cryptoutils->get_uint32_t();
    } while (!caseValues.insert(value).second);
    switchI->addCase(ConstantInt::get(Type::getInt32Ty(f->getContext()), value), i);
  }
  new StoreInst(switchI->findCaseDest(first), switchVar, true,
                insert->getTerminator());

  // Recalculate switchVar
  for (BasicBlock *i : origBB) {
    ConstantInt *numCase = nullptr;

    // If it's a non-conditional jump
    if (i->getTerminator()->getNumSuccessors() == 1) {
      // Get successor and delete terminator
      BasicBlock *succ = i->getTerminator()->getSuccessor(0);
      i->getTerminator()->eraseFromParent();

      // Get next case
      numCase = switchI->findCaseDest(succ);

      // Update switchVar and jump to the end of loop
      new StoreInst(numCase, switchVar, true, i);
      BranchInst::Create(loopEnd, i);
      continue;
    }

    // If it's a conditional jump
    if (i->getTerminator()->getNumSuccessors() == 2) {
      // Get next cases
      ConstantInt *numCaseTrue =
          switchI->findCaseDest(i->getTerminator()->getSuccessor(0));
      ConstantInt *numCaseFalse =
          switchI->findCaseDest(i->getTerminator()->getSuccessor(1));

      // Create a SelectInst
      BranchInst *br = cast<BranchInst>(i->getTerminator());
      SelectInst *sel =
          SelectInst::Create(br->getCondition(), numCaseTrue, numCaseFalse, "",
                             i->getTerminator());

      // Erase terminator
      i->getTerminator()->eraseFromParent();
      // Update switchVar and jump to the end of loop
      new StoreInst(sel, switchVar, true, i);
      BranchInst::Create(loopEnd, i);
      continue;
    }
  }
  errs() << "Fixing Stack\n";
  fixStack(f);
  errs() << "Fixed Stack\n";
}
