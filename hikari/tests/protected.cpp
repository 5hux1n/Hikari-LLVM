#include <pthread.h>
#include <stdint.h>
#include <string.h>
#include "../Hikari.h"

#ifdef HIKARI_TEST_PLAIN
#undef HIKARI_PROTECT
#define HIKARI_PROTECT __attribute__((noinline))
#endif

static const char shared[] = "hikari-shared-concurrent-string";
HIKARI_PROTECT const char *first_string() { return shared; }
HIKARI_PROTECT const char *second_string() { return shared; }
HIKARI_PROTECT const char *private_string() { return "hikari-only-protected-marker"; }
HIKARI_PROTECT int decision(int n) {
  int result = 0;
  for (int i = 0; i != 12; ++i) {
    switch ((n + i) & 3) {
    case 0: result += n * 3; break;
    case 1: result ^= n + 5; break;
    case 2: result -= i; break;
    default: result += i ^ n; break;
    }
  }
  return result;
}
HIKARI_PROTECT int wide_strings() {
  static const uint8_t byte[] = {0x9a};
  static const uint16_t a[] = {0x1234, 0xabcd, 7, 0};
  static const uint32_t b[] = {0x12345678, 0xabcdef01, 7, 0};
  static const uint64_t c[] = {UINT64_C(0x123456789abcdef0), 7, 0};
  const volatile uint16_t *pa = a;
  const volatile uint8_t *pbyte = byte;
  const volatile uint32_t *pb = b;
  const volatile uint64_t *pc = c;
  return pbyte[0] != 0x9a || pa[0] != 0x1234 || pa[1] != 0xabcd || pa[3] ||
         pb[0] != 0x12345678 || pb[1] != 0xabcdef01 || pb[3] ||
         pc[0] != UINT64_C(0x123456789abcdef0) || pc[2];
}
HIKARI_PROTECT int exception_path(int n) {
  try {
    if (n < 0) throw n;
    return n + 3;
  } catch (int value) { return value - 5; }
}
static int start;
static void *worker(void *) {
  while (!__atomic_load_n(&start, __ATOMIC_ACQUIRE)) {}
  for (int i = 0; i != 200; ++i)
    if (strcmp(first_string(), "hikari-shared-concurrent-string") ||
        strcmp(second_string(), first_string()) || private_string()[0] != 'h' ||
        private_string()[28] != 0 || wide_strings() ||
        exception_path(-2) != -7 || exception_path(3) != 6)
      return (void *)1;
  return nullptr;
}
int main() {
  if (decision(7) != 63 || decision(0) != 2) return 1;
  pthread_t threads[8];
  for (auto &thread : threads)
    if (pthread_create(&thread, nullptr, worker, nullptr)) return 2;
  __atomic_store_n(&start, 1, __ATOMIC_RELEASE);
  for (auto &thread : threads) {
    void *result;
    if (pthread_join(thread, &result) || result) return 3;
  }
}
