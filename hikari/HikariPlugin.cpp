#include "llvm/Passes/PassBuilder.h"
#include "llvm/Plugins/PassPlugin.h"
#include "llvm/Transforms/Obfuscation/Obfuscation.h"

extern "C" LLVM_ATTRIBUTE_WEAK llvm::PassPluginLibraryInfo
llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "Hikari", LLVM_VERSION_STRING,
          [](llvm::PassBuilder &PB) {
            PB.registerOptimizerLastEPCallback(
                [](llvm::ModulePassManager &MPM, llvm::OptimizationLevel,
                   llvm::ThinOrFullLTOPhase Phase) {
                  if (Phase == llvm::ThinOrFullLTOPhase::None)
                    MPM.addPass(llvm::ObfuscationPass());
                });
          }};
}
