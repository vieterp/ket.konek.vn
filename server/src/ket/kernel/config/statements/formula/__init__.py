"""Grammar công thức chỉ tiêu BCTC (FR-GLE-043) — parser riêng, **không** `eval`.

```
expr       := term (('+'|'-') term)*
term       := number | rowref | accfn
rowref     := '[' row_code ']'
accfn      := fn '(' acct_range (',' acct_range)* ')'
fn         := DR | CR | BAL | DR_PS | CR_PS | DR_NET | CR_NET
acct_range := '1121' | '11*' | '131..138'
```

So với phác thảo phase-05 (5 hàm), thêm `DR_PS`/`CR_PS` (tổng phát sinh thô một
bên): B02 cần "lũy kế phát sinh Có TK 511" (chỉ tiêu 01) **tách khỏi** "lũy kế
phát sinh Nợ TK 511" (chỉ tiêu 02) — hai hàm net không tách được hai chỉ tiêu
này. Ngữ nghĩa từng hàm khai ở `evaluator.py`.
"""
