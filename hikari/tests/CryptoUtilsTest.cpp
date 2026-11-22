#include "llvm/Transforms/Obfuscation/CryptoUtils.h"

int main() {
  llvm::CryptoUtils Random;
  if (Random.get_range(0) != 0)
    return 1;
  // get_range must work before the scheduler has seeded the generator.
  for (unsigned Max : {1u, 2u, 17u, 100u})
    for (unsigned I = 0; I != 256; ++I)
      if (Random.get_range(Max) >= Max)
        return 2;
  for (unsigned I = 0; I != 256; ++I) {
    unsigned Value = Random.get_range(7, 13);
    if (Value < 7 || Value >= 13)
      return 3;
  }
  llvm::CryptoUtils Replay;
  for (uint64_t Seed : {uint64_t(0), uint64_t(0x1337), UINT64_MAX}) {
    Random.prng_seed(Seed);
    Replay.prng_seed(Seed);
    for (unsigned I = 0; I != 256; ++I)
      if (Random.get_uint64_t() != Replay.get_uint64_t())
        return 4;
  }
}
