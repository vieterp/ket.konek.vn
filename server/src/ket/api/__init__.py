"""Tầng HTTP: middleware, dependency, router.

Ranh giới với `kernel`: tầng này biết FastAPI, `kernel` thì **không**. Luật đó
giữ cho worker, CLI và test gọi được cùng một dịch vụ nghiệp vụ mà không phải
dựng một request giả — và nó là thứ khiến `ket.admin` chạy được cùng logic tạo
tài khoản với endpoint HTTP, thay vì có một bản sao thứ hai.
"""
