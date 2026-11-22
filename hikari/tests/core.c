extern int strcmp(const char *, const char *);
static const char text[] = "hikari-llvm22-pac";

__attribute__((noinline)) const char *message(void) { return text; }
__attribute__((noinline)) int step(int n) { return n + 1; }
__attribute__((noinline)) int call_unary(int (*f)(int), int n) {
  return f(n) + 1;
}
__attribute__((noinline)) int branch(int n) {
  int out = 0;
  for (int i = 0; i < 8; ++i) {
    switch ((n + i) & 3) {
    case 0: out += n * 3; break;
    case 1: out ^= n + 5; break;
    case 2: out -= i; break;
    default: out += i ^ n; break;
    }
  }
  return out;
}
int main(void) {
  return call_unary(step, 40) != 42 || branch(7) != 34 ||
         strcmp(message(), "hikari-llvm22-pac") != 0;
}
