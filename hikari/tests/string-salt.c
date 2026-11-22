#include <stdint.h>
#include <stdio.h>

#define ROW(n) "salt-byte-fixture-" #n
__attribute__((noinline, optnone, annotate("strenc")))
int main(void) {
  const char *volatile strings[] = {
      ROW(0), ROW(1), ROW(2), ROW(3), ROW(4), ROW(5), ROW(6), ROW(7),
      ROW(8), ROW(9), ROW(10), ROW(11), ROW(12), ROW(13), ROW(14), ROW(15),
      ROW(16), ROW(17), ROW(18), ROW(19), ROW(20), ROW(21), ROW(22), ROW(23),
      ROW(24), ROW(25), ROW(26), ROW(27), ROW(28), ROW(29), ROW(30), ROW(31),
      ROW(32), ROW(33), ROW(34), ROW(35), ROW(36), ROW(37), ROW(38), ROW(39),
      ROW(40), ROW(41), ROW(42), ROW(43), ROW(44), ROW(45), ROW(46), ROW(47),
      ROW(48), ROW(49), ROW(50), ROW(51), ROW(52), ROW(53), ROW(54), ROW(55),
      ROW(56), ROW(57), ROW(58), ROW(59), ROW(60), ROW(61), ROW(62), ROW(63)};
  uint64_t checksum = 0;
  for (unsigned i = 0; i < sizeof(strings) / sizeof(*strings); ++i)
    for (const char *p = strings[i]; *p; ++p)
      checksum = checksum * 33 + (unsigned char)*p;
  printf("%llu\n", (unsigned long long)checksum);
}
