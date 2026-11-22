@IndirectBranchingGlobalTable = global [1 x ptr] zeroinitializer

; The first executable successor need not be the next block in the function.
define i32 @choose(i1 %alternate) noinline {
entry:
  br label %start
other:
  ret i32 99
start:
  br i1 %alternate, label %other, label %done
done:
  br label %merge
merge:
  %result = phi i32 [ 42, %done ]
  ret i32 %result
}

define i32 @main() {
entry:
  %first = call i32 @choose(i1 false)
  %second = call i32 @choose(i1 true)
  %first.ok = icmp eq i32 %first, 42
  %second.ok = icmp eq i32 %second, 99
  %ok = and i1 %first.ok, %second.ok
  %status = select i1 %ok, i32 0, i32 1
  ret i32 %status
}
