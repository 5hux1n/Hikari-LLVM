struct Base {
  virtual int value(int n) { return n + 1; }
};
struct Derived : Base {
  int value(int n) override { return n + 2; }
};
__attribute__((noinline)) int invoke(Base *p, int n) { return p->value(n); }
__attribute__((noinline)) int throwing(int n) {
  if (n == 7)
    throw n;
  return n;
}
int main() {
  Derived p;
  if (invoke(&p, 40) != 42)
    return 1;
  try {
    throwing(7);
    return 2;
  } catch (int n) {
    return n != 7;
  }
}
