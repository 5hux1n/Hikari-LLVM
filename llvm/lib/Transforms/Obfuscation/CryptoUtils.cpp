// For open-source license, please refer to
// [License](https://github.com/HikariObfuscator/Hikari/wiki/License).
//===----------------------------------------------------------------------===//
#include "llvm/Transforms/Obfuscation/CryptoUtils.h"
#include "llvm/ADT/Twine.h"
#include "llvm/Support/Format.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Support/RandomNumberGenerator.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;
namespace llvm {
ManagedStatic<CryptoUtils> cryptoutils;
}
CryptoUtils::CryptoUtils() {}

uint32_t CryptoUtils::scramble32(
    uint32_t in, std::unordered_map<uint32_t /*IDX*/, uint32_t /*VAL*/> &VMap) {
  if (VMap.find(in) == VMap.end()) {
    uint32_t V = get_uint32_t();
    VMap[in] = V;
    return V;
  } else {
    return VMap[in];
  }
}
CryptoUtils::~CryptoUtils() {
  if (eng != nullptr)
    delete eng;
}
void CryptoUtils::prng_seed() {
  uint64_t Seed;
  if (auto EC = getRandomBytes(&Seed, sizeof(Seed)))
    report_fatal_error(Twine("Hikari: cannot obtain random seed: ") + EC.message());
  prng_seed(Seed);
}
void CryptoUtils::prng_seed(std::uint_fast64_t seed) {
  errs() << format("std::mt19937_64 seeded with: %" PRIu64 "", seed) << "\n";
  delete eng;
  eng = new std::mt19937_64(seed);
}
std::uint_fast64_t CryptoUtils::get_raw() {
  if (eng == nullptr)
    prng_seed();
  return (*eng)();
}
uint32_t CryptoUtils::get_range(uint32_t min, uint32_t max) {
  if (max == 0)
    return 0;
  if (eng == nullptr)
    prng_seed();
  std::uniform_int_distribution<uint32_t> dis(min, max - 1);
  return dis(*eng);
}
