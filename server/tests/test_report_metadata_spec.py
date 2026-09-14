"""Spec + scope + loader của report engine (lát 5C) — thuần dữ liệu, không DB.

Ba ranh giới fail-closed được chứng minh ở đây:

* `parse_layout_spec`/`parse_param_set_spec` — JSONB thô sai hình dạng nổ thành
  `ReportSpecInvalidError`, không thành `KeyError` giữa lượt render.
* `compose_scoped_query`/`assert_placeholders_allowed` — không có đường nối
  chuỗi tự do: placeholder ngoài `allowed_params` và identifier sắp xếp lạ đều
  bị chặn.
* `load_builtin_reports` — dữ liệu builtin đóng gói phải tự đủ và đúng hợp
  đồng ngay lúc test, không đợi tới lúc cấp dữ liệu kế toán.
"""

from __future__ import annotations

import pytest

# Nạp registry trước khi đọc manifest: `load_builtin_reports` kiểm
# `required_permission_module` bằng chính sổ đăng ký mã quyền, và sổ ấy chỉ đầy
# khi các module đã import. Thiếu dòng này tệp CHỈ xanh khi chạy cùng tệp khác
# đã import hộ — đúng kiểu phụ thuộc thứ tự làm bài kiểm xanh vì lý do khác lý
# do nó viết ra.
from ket import model_registry as _model_registry  # noqa: F401
from ket.kernel.config.reports.loader import load_builtin_reports
from ket.kernel.config.reports.models import ReportDataset
from ket.kernel.config.reports.scope import (
    assert_placeholders_allowed,
    compose_scoped_query,
    sql_placeholders,
)
from ket.kernel.config.reports.spec import (
    STANDARD_PARAMS,
    parse_layout_spec,
    parse_param_set_spec,
)
from ket.kernel.errors import ReportDatasetInvalidError, ReportSpecInvalidError
from ket.modules.sales.models import REVERSING_KINDS, SalesInvoiceKind

BASE_LAYOUT: dict[str, object] = {
    "columns": [
        {"key": "posting_date", "label": "Ngày", "type": "date"},
        {"key": "description", "label": "Diễn giải", "type": "text"},
        {"key": "debit", "label": "Nợ", "type": "money"},
        {"key": "credit", "label": "Có", "type": "money"},
    ],
    "group_by": [{"key": "account_code", "heading_keys": ["account_code", "account_name"]}],
    "totals": ["debit", "credit"],
    "sort": ["account_code", "posting_date"],
}


class TestLayoutSpec:
    def test_valid_layout_parses(self) -> None:
        spec = parse_layout_spec(dict(BASE_LAYOUT), layout_code="t")
        assert [c.key for c in spec.columns] == ["posting_date", "description", "debit", "credit"]
        assert spec.page.orientation == "portrait"

    def test_duplicate_column_keys_rejected(self) -> None:
        raw = dict(BASE_LAYOUT)
        raw["columns"] = [
            {"key": "debit", "label": "Nợ", "type": "money"},
            {"key": "debit", "label": "Nợ 2", "type": "money"},
        ]
        raw["totals"] = []
        raw["group_by"] = []
        raw["sort"] = ["debit"]
        with pytest.raises(ReportSpecInvalidError):
            parse_layout_spec(raw, layout_code="t")

    def test_total_must_be_displayed_money_column(self) -> None:
        raw = dict(BASE_LAYOUT)
        raw["totals"] = ["description"]
        with pytest.raises(ReportSpecInvalidError):
            parse_layout_spec(raw, layout_code="t")
        raw["totals"] = ["so_tien_khong_hien_thi"]
        with pytest.raises(ReportSpecInvalidError):
            parse_layout_spec(raw, layout_code="t")

    def test_group_keys_must_prefix_sort(self) -> None:
        """Grouping streaming chỉ đúng khi dòng cùng nhóm liền nhau — layout vi
        phạm phải chết lúc parse, không cho ra tổng nhóm sai lặng lẽ."""
        raw = dict(BASE_LAYOUT)
        raw["sort"] = ["posting_date", "account_code"]
        with pytest.raises(ReportSpecInvalidError):
            parse_layout_spec(raw, layout_code="t")

    def test_unknown_field_rejected(self) -> None:
        raw = dict(BASE_LAYOUT)
        raw["colums"] = raw.pop("columns")
        with pytest.raises(ReportSpecInvalidError):
            parse_layout_spec(raw, layout_code="t")

    def test_unknown_column_type_rejected(self) -> None:
        raw = dict(BASE_LAYOUT)
        raw["columns"] = [{"key": "x", "label": "X", "type": "percent"}]
        raw.update(totals=[], group_by=[], sort=["x"])
        with pytest.raises(ReportSpecInvalidError):
            parse_layout_spec(raw, layout_code="t")


class TestParamSetSpec:
    def test_standard_param_cannot_be_redeclared(self) -> None:
        raw = {"params": [{"name": "ledger", "kind": "int", "label": "Sổ"}]}
        with pytest.raises(ReportSpecInvalidError):
            parse_param_set_spec(raw, param_set_code="p")

    def test_duplicate_names_rejected(self) -> None:
        raw = {
            "params": [
                {"name": "x", "kind": "int", "label": "X"},
                {"name": "x", "kind": "text", "label": "X2"},
            ]
        }
        with pytest.raises(ReportSpecInvalidError):
            parse_param_set_spec(raw, param_set_code="p")

    def test_unknown_kind_rejected(self) -> None:
        raw = {"params": [{"name": "x", "kind": "json", "label": "X"}]}
        with pytest.raises(ReportSpecInvalidError):
            parse_param_set_spec(raw, param_set_code="p")


class TestScopeComposition:
    def _dataset(self, sql: str, *, allowed: list[str], branch: bool = True) -> ReportDataset:
        return ReportDataset(
            code="t",
            sql_text=sql,
            allowed_params=allowed,
            supports_branch=branch,
            supports_ledger=True,
        )

    def test_placeholders_extraction_ignores_casts_and_strings(self) -> None:
        sql = "SELECT CAST(:a AS INTEGER), x::text, ':not_a_bind', :b_2 FROM t"
        assert sql_placeholders(sql) == frozenset({"a", "b_2"})

    def test_placeholder_outside_allowed_params_rejected(self) -> None:
        dataset = self._dataset("SELECT :rogue FROM t", allowed=["from_date"])
        with pytest.raises(ReportDatasetInvalidError):
            assert_placeholders_allowed(dataset)

    def test_wrapper_adds_scope_and_order(self) -> None:
        layout = parse_layout_spec(dict(BASE_LAYOUT), layout_code="t")
        dataset = self._dataset("SELECT 1 AS x", allowed=[])
        composed = compose_scoped_query(dataset, layout)
        assert "scoped.branch_id = ANY(:branch_ids)" in composed
        assert "scoped.ledger = :ledger" in composed
        assert composed.endswith("ORDER BY scoped.account_code, scoped.posting_date")

    def test_wrapper_skips_branch_when_unsupported(self) -> None:
        layout = parse_layout_spec(dict(BASE_LAYOUT), layout_code="t")
        dataset = self._dataset("SELECT 1 AS x", allowed=[], branch=False)
        composed = compose_scoped_query(dataset, layout)
        assert "branch_ids" not in composed
        assert "scoped.ledger = :ledger" in composed

    def test_sort_identifier_is_re_validated(self) -> None:
        """Phòng đường gọi không đi qua parser (spec dựng tay trong code)."""
        layout = parse_layout_spec(dict(BASE_LAYOUT), layout_code="t")
        hacked = layout.model_copy(update={"sort": ("account_code; DROP TABLE x",)})
        dataset = self._dataset("SELECT 1 AS x", allowed=[])
        with pytest.raises(ReportDatasetInvalidError):
            compose_scoped_query(dataset, hacked)


class TestBuiltinManifest:
    def test_builtin_reports_load_and_are_self_contained(self) -> None:
        loaded = load_builtin_reports()
        codes = {d.code for d in loaded.manifest.definitions}
        # 8 báo cáo bộ sổ theo đúng mã mẫu thông tư (5D — đóng câu hỏi mở #1
        # của 5C: mã trung lập `SO-CAI` thay bằng `S03b-DN`).
        assert {
            "S03a-DN",
            "S03b-DN",
            "S38-DN",
            "S06-DN",
            "S03a-DNN",
            "S03b-DNN",
            "S19-DNN",
            "F01-DNN",
        } <= codes
        assert "SO-CAI" not in codes
        for entry in loaded.manifest.datasets:
            placeholders = sql_placeholders(loaded.sql_by_dataset[entry.code])
            assert placeholders <= frozenset(entry.allowed_params)

    def test_builtin_dataset_count_stays_under_project_cap(self) -> None:
        """Trần **≤30 dataset phủ ~155 báo cáo** (plan.md / phase-05 §Chiến lược
        quy mô: "Vượt 40 thì dừng và xem lại thiết kế").

        Con số này thay cho trần ≤5 của thời phase 5 — trần đó là cách diễn đạt
        CÙNG một mục tiêu khi mới có 10 báo cáo đầu ("3 dataset × nhiều layout",
        không phải một dataset một báo cáo), và nó hết dùng được ngay khi phân
        hệ đầu tiên đăng ký bộ báo cáo của mình. Điều cần canh là mục tiêu đo
        được của dự án, không phải cái mốc tạm.
        """
        loaded = load_builtin_reports()
        assert len(loaded.manifest.datasets) <= 30

    def test_look_alike_forms_share_one_dataset(self) -> None:
        """Mẫu sổ chỉ khác nhau một tham số phải dùng CHUNG dataset.

        Đây mới là bất biến mà trần đếm ở trên tồn tại để bảo vệ, và nó kiểm
        được thẳng thay vì gián tiếp qua một con số: `S07a-DN`/`S08-DN` là cùng
        sổ chi tiết một tài khoản tiền (khác nhóm TK), `S03a1-DN`/`S03a2-DN` là
        cùng nhật ký chuyên dùng (khác chiều tiền). Tách chúng thành hai dataset
        là nhân đôi một câu SQL — chính thứ `fixed_params` sinh ra để tránh.
        """
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        for left, right, param in (
            ("S07a-DN", "S08-DN", "account_prefix"),
            ("S03a1-DN", "S03a2-DN", "direction"),
        ):
            assert by_code[left].dataset_code == by_code[right].dataset_code
            assert by_code[left].param_set_code == by_code[right].param_set_code
            # Cùng dataset thì phải khác nhau ở ĐÚNG tham số ghim, nếu không hai
            # mã mẫu khác nhau sẽ in ra cùng một tờ giấy.
            assert by_code[left].fixed_params[param] != by_code[right].fixed_params[param]
            assert by_code[left].layout_code != by_code[right].layout_code

    def test_scheme_bound_definitions_declare_known_scheme(self) -> None:
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        assert by_code["S03b-DN"].package_scheme == "TT99"
        assert by_code["F01-DNN"].package_scheme == "TT133"

    def test_builtin_extra_params_are_not_standard(self) -> None:
        loaded = load_builtin_reports()
        for spec in loaded.param_set_specs.values():
            assert not STANDARD_PARAMS.intersection(p.name for p in spec.params)


class TestGatingIsIndependentOfAccountingScheme:
    """Lát 6G-2 (M-7): cổng quyền của một báo cáo không được phụ thuộc THÔNG TƯ.

    `F01-DNN` (TT133) và `S06-DN` (TT99) là cùng một bảng cân đối tài khoản đọc
    cùng `dataset_code`; 6G-1 đóng cổng `general_ledger` cho bộ sổ TT99 và bỏ
    quên bản TT133, nên cùng một dữ liệu mở hay đóng tùy chế độ kế toán khách
    hàng đang chạy — thứ không ai nhìn thấy khi đọc một trong hai dòng.

    Kiểm theo BẤT BIẾN thay vì theo hai mã cụ thể: mọi cặp báo cáo dùng chung
    dataset phải đòi cùng phân hệ quyền, nên mã mẫu TT133 thêm ở phase sau
    không lặp lại được lỗi này.
    """

    def test_the_same_form_in_two_schemes_requires_the_same_permission_module(self) -> None:
        """Hai định nghĩa là CÙNG MỘT biểu mẫu khi chúng khác nhau đúng ở
        `package_scheme` — cùng dataset, cùng layout, cùng bộ tham số, cùng
        tham số ghim.

        Không gộp theo mỗi `dataset_code`: `S07a-DN` và `S08-DN` dùng chung
        dataset `money_account_ledger` nhưng ghim `account_prefix` khác nhau
        (111 ↔ 112) và vì thế là hai phân hệ khác nhau — cổng của chúng ĐÚNG
        khi khác nhau. Chỗ sai là khi cùng một tờ giấy đổi cổng theo thông tư.
        """
        loaded = load_builtin_reports()
        by_form: dict[tuple[str, str, str, tuple[tuple[str, object], ...]], set[str | None]] = {}
        for definition in loaded.manifest.definitions:
            key = (
                definition.dataset_code,
                definition.layout_code,
                definition.param_set_code,
                tuple(sorted(definition.fixed_params.items())),
            )
            by_form.setdefault(key, set()).add(definition.required_permission_module)
        drifting = {key: modules for key, modules in by_form.items() if len(modules) > 1}
        assert drifting == {}, drifting

    def test_the_trial_balance_of_both_schemes_is_gated(self) -> None:
        """Bản neo cho bất biến ở trên: nếu ai đó gỡ cổng khỏi CẢ HAI mã thì
        phép kiểm kia vẫn xanh (hai `None` cũng là "giống nhau")."""
        loaded = load_builtin_reports()
        gates = {
            definition.code: definition.required_permission_module
            for definition in loaded.manifest.definitions
            if definition.dataset_code == "trial_balance"
        }
        assert gates == {"S06-DN": "general_ledger", "F01-DNN": "general_ledger"}

    def test_only_the_reconciliation_report_demands_company_wide_scope(self) -> None:
        """`requires_full_branch_scope` là cổng PHẠM VI (M-4), khác cổng quyền.

        Bật nhầm nó cho một báo cáo một-vế là chặn người dùng hợp lệ khỏi dữ
        liệu họ có quyền đọc, nên danh sách mang cờ được ghim tường minh.

        `chenh-lech-so-quy-so-ke-toan` cũng so hai vế nhưng KHÔNG cần cờ, và đó
        là phép so đáng nhớ: `treasurer_cash_book` mang `branch_id` + RLS như
        `gl_postings`, và dataset lọc chi nhánh **bên trong** — hai vế cùng thu
        hẹp theo nhau, nên phạm vi hẹp cho ra ÍT DÒNG HƠN chứ không cho ra số
        lệch lớn hơn. Đối chiếu ngân hàng khác vì vế sao kê treo trên tài khoản
        (dùng chung, không mang chi nhánh) nên nó KHÔNG thu hẹp theo."""
        loaded = load_builtin_reports()
        demanding = {
            definition.code
            for definition in loaded.manifest.definitions
            if definition.requires_full_branch_scope
        }
        assert demanding == {"doi-chieu-ngan-hang"}


class TestPurchaseAndPayableManifest:
    """Bất biến MỨC METADATA của mười báo cáo lát 7G-1.

    Hành vi số liệu ở `test_purchase_reports.py` (cần PostgreSQL); ở đây chỉ
    những thứ đọc được từ manifest — và đọc được *trước* khi có dữ liệu là đúng
    lúc để bắt chúng.
    """

    _PURCHASE_SUMMARIES = (
        "tong-hop-mua-hang-theo-mat-hang",
        "tong-hop-mua-hang-theo-nha-cung-cap",
        "tong-hop-mua-hang-theo-nhan-vien",
        "tong-hop-mua-hang-theo-cong-trinh",
    )
    _SLICE_CODES = (
        *_PURCHASE_SUMMARIES,
        "so-chi-tiet-mua-hang",
        "so-nhat-ky-mua-hang",
        "tong-hop-cong-no-phai-tra",
        "chi-tiet-cong-no-phai-tra",
        "chi-tiet-cong-no-phai-tra-theo-hoa-don",
        "chi-tiet-tuoi-no-phai-tra",
    )

    def test_all_ten_reports_are_registered_and_gated_by_the_purchase_module(self) -> None:
        """Mười mã báo cáo có mặt và cả mười đòi quyền phân hệ `purchase`.

        Mã quyền báo cáo chung (`reporting.report.view`) một mình không được mở
        dữ liệu mua hàng — đúng bản vá H-1b của 6E-1. Bỏ trống
        `required_permission_module` ở một dòng là mở toang lặng lẽ đúng dòng ấy.
        """
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        for code in self._SLICE_CODES:
            assert code in by_code, code
            assert by_code[code].required_permission_module == "purchase", code
            assert by_code[code].category == "mua-hang", code

    def test_the_four_purchase_summaries_share_one_dataset_with_four_layouts(self) -> None:
        """Bốn chiều gộp = bốn layout trên MỘT dataset.

        Engine gắn layout vào definition và không nhận `group_by` lúc chạy, nên
        "tổng hợp mua hàng theo bốn chiều" của SRS 05 §5 #1 chỉ diễn đạt được
        bằng bốn definition. Điều phải canh là chúng không kéo theo bốn dataset:
        đó sẽ là bốn bản chép của cùng một phép cộng tiền mua.
        """
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        datasets = {by_code[code].dataset_code for code in self._PURCHASE_SUMMARIES}
        layouts = {by_code[code].layout_code for code in self._PURCHASE_SUMMARIES}
        assert datasets == {"purchase_register"}
        assert len(layouts) == len(self._PURCHASE_SUMMARIES)

    def test_the_payable_reports_pin_the_payable_direction(self) -> None:
        """Ba báo cáo công nợ phải trả ghim `direction = 'chi'`.

        Không ghim thì người gọi tự chọn chiều, và một báo cáo mang tên "phải
        trả" sẽ in ra công nợ phải thu khi client gửi tham số khác — cùng lập
        luận với `S03a1-DN`/`S03a2-DN` của 6E-1.
        """
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        for code in (
            "tong-hop-cong-no-phai-tra",
            "chi-tiet-cong-no-phai-tra",
            "chi-tiet-cong-no-phai-tra-theo-hoa-don",
            "chi-tiet-tuoi-no-phai-tra",
        ):
            assert by_code[code].fixed_params == {"direction": "chi"}, code

    def test_the_aging_detail_reuses_the_delivered_aging_dataset(self) -> None:
        """ "Chi tiết công nợ theo tuổi nợ" KHÔNG có dataset riêng.

        Nó là bảng tuổi nợ của 7A xem ở mức chứng từ, nên nó dùng lại
        `ar_ap_aging`. Một dataset thứ hai sẽ là bản chép thứ hai của phép chia
        mốc tuổi nợ — và hai bảng tuổi nợ lệch nhau là thứ không ai đối chiếu ra.
        """
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        assert by_code["chi-tiet-tuoi-no-phai-tra"].dataset_code == "ar_ap_aging"
        assert (
            by_code["chi-tiet-tuoi-no-phai-tra"].dataset_code
            == by_code["tuoi-no-phai-tra"].dataset_code
        )


class TestNoLayoutTotalsAnUnaddableColumn:
    """Chỉ cột VND được cộng tổng — cột nguyên tệ và cột số lượng thì không.

    Cộng USD với EUR ra một con số là con số không có nghĩa, và nó nguy hiểm hơn
    một ô trống vì nó trông như một con số. Cột `quantity` cũng vậy: một vật tư
    mua/bán bằng nhiều đơn vị quy đổi (3B-3) thì tổng số lượng là phép cộng cái
    với thùng.

    Kỷ luật này có từ `ar_ap_aging` (7A) và 7G-1 ghim nó cho **mười layout của
    riêng lát ấy**. Lát 7G-2a nới ra **mọi layout builtin**: bản hẹp chỉ canh
    được những layout đã biết tên, nên nó im lặng đúng vào lúc cần nói — khi một
    lát sau thêm layout mới. Toàn bộ 34 layout hiện có đã đạt, nên nới ra không
    phải một lời hứa cho tương lai mà là một phát biểu đúng ở hiện tại.
    """

    def test_no_builtin_layout_totals_a_foreign_currency_or_quantity_column(self) -> None:
        loaded = load_builtin_reports()
        for layout in loaded.manifest.layouts:
            spec = loaded.layout_specs[layout.code]
            for key in spec.totals:
                assert not key.endswith("_fc"), f"{layout.code}: cộng tổng cột nguyên tệ {key}"
                assert key != "quantity", f"{layout.code}: cộng tổng số lượng đa đơn vị"


_SALES_KINDS = tuple(
    sorted(
        value
        for name, value in vars(SalesInvoiceKind).items()
        if not name.startswith("_") and isinstance(value, int)
    )
)
"""Mọi `kind` của `SalesInvoiceKind` — `kind_label` phải có nhãn cho từng cái.

Đọc từ chính lớp hằng, **không** gõ tay: một tuple gõ tay không lớn lên khi
`SalesInvoiceKind` thêm loại chứng từ, nên bài kiểm "liệt kê đủ" sẽ xanh đúng vào
lúc có một `kind` mới chưa có nhãn — tức phantom cho đúng ca nó nói nó canh."""


class TestSalesManifest:
    """Bất biến MỨC METADATA của chín báo cáo bán hàng lát 7G-2a.

    Hành vi số liệu ở `test_sales_reports.py` (cần PostgreSQL); ở đây chỉ những
    thứ đọc được từ manifest.
    """

    _SALES_SUMMARIES = (
        "tong-hop-ban-hang-theo-mat-hang",
        "tong-hop-ban-hang-theo-khach-hang",
        "tong-hop-ban-hang-theo-nhan-vien",
        "tong-hop-ban-hang-theo-dia-phuong",
        "tong-hop-ban-hang-theo-don-vi",
    )
    _SLICE_CODES = (
        *_SALES_SUMMARIES,
        "so-chi-tiet-ban-hang",
        "so-chi-tiet-ban-hang-theo-quy-cach",
        "so-nhat-ky-ban-hang",
        "doanh-so-ban-hang-theo-thang",
    )

    def test_all_nine_reports_are_registered_and_gated_by_the_sales_module(self) -> None:
        """Chín mã báo cáo có mặt và cả chín đòi quyền phân hệ `sales`.

        Mã quyền báo cáo chung (`reporting.report.view`) một mình không được mở
        dữ liệu bán hàng — đúng bản vá H-1b của 6E-1. Bỏ trống
        `required_permission_module` ở một dòng là mở toang lặng lẽ đúng dòng ấy.
        """
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        for code in self._SLICE_CODES:
            assert code in by_code, code
            assert by_code[code].required_permission_module == "sales", code
            assert by_code[code].category == "ban-hang", code

    def test_the_five_sales_summaries_share_one_dataset_with_five_layouts(self) -> None:
        """Năm chiều gộp = năm layout trên MỘT dataset.

        SRS 06 §5.1 #1 nêu năm chiều (địa phương / đơn vị / khách hàng / mặt hàng
        / nhân viên). Engine gắn layout vào definition và không nhận `group_by`
        lúc chạy, nên năm chiều chỉ diễn đạt được bằng năm definition. Điều phải
        canh là chúng không kéo theo năm dataset: đó sẽ là năm bản chép của cùng
        một phép cộng doanh thu.
        """
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        datasets = {by_code[code].dataset_code for code in self._SALES_SUMMARIES}
        layouts = {by_code[code].layout_code for code in self._SALES_SUMMARIES}
        assert datasets == {"sales_register"}
        assert len(layouts) == len(self._SALES_SUMMARIES)

    def test_every_sales_report_reads_the_one_register_dataset(self) -> None:
        """Cả chín báo cáo đọc `sales_register` — không có dataset thứ hai.

        Sổ chi tiết, sổ nhật ký và doanh số theo tháng là ba cách XEM cùng một
        tập dòng hàng bán; một dataset riêng cho mỗi cách xem là ba chỗ để phép
        cộng doanh thu lệch nhau, và không con số nào trên ba tờ giấy ấy chỉ ra
        chỗ lệch.
        """
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        assert {by_code[code].dataset_code for code in self._SLICE_CODES} == {"sales_register"}

    def test_the_variant_book_groups_by_item_before_variant(self) -> None:
        """Gộp hai bậc: mã hàng trước, quy cách sau.

        `uq_item_variants_item_code` cho phép hai mã hàng dùng chung một mã quy
        cách ("M" của áo và "M" của mũ), nên gộp theo quy cách một bậc sẽ trộn
        doanh thu của hai mặt hàng vào một nhóm mang một cái tên đúng.
        """
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        spec = loaded.layout_specs[by_code["so-chi-tiet-ban-hang-theo-quy-cach"].layout_code]
        assert [group.key for group in spec.group_by] == ["item_code", "variant_code"]

    def test_the_sign_branch_lists_exactly_the_reversing_kinds(self) -> None:
        """Tập đảo dấu của dataset = `REVERSING_KINDS` của `sales.models`, không
        phải một tuple gõ tay trùng hợp đúng.

        Bộ dữ liệu test ghi sổ `kind` 0/1/2/5, nên đổi `IN (2, 3, 6)` thành
        `IN (2, 3)` hay `IN (2, 6)` **vẫn xanh** ở mọi bài kiểm số liệu — và hỏng
        theo kiểu chia đôi: `amount`/`vat_amount` vẫn đúng (dấu đến từ sổ) trong
        khi `quantity`, `goods_amount_fc` và `discount_amount_fc` của một chứng từ
        giảm giá / điều chỉnh giảm ra DƯƠNG, tức số lượng trả lại cộng vào số
        lượng bán. Ghim ở đây vì đó là chỗ duy nhất bắt được mà không phải ghi sổ
        thêm hai chứng từ chỉ để canh một tuple.
        """
        sql = load_builtin_reports().sql_by_dataset["sales_register"]
        expected = ", ".join(str(kind) for kind in sorted(REVERSING_KINDS))
        assert f"si.kind IN ({expected})" in sql

    def test_every_sales_kind_has_its_own_label(self) -> None:
        """`kind_label` liệt kê ĐỦ các `kind` hiện có.

        `ELSE` lộ số thô nên một `kind` mới không bị dán nhãn sai — nhưng nó cũng
        không được dừng ở đó: một `kind` đã tồn tại mà thiếu nhãn thì cả một loại
        chứng từ hiện ra dưới dạng "Loại chứng từ 4" trên báo cáo gửi ra ngoài.
        """
        sql = load_builtin_reports().sql_by_dataset["sales_register"]
        for kind in _SALES_KINDS:
            assert f"WHEN {kind} THEN" in sql, kind

    def test_the_sales_register_declares_every_filter_its_sql_binds(self) -> None:
        """Sáu chiều lọc của dataset đều khai ở param_set.

        Loader đã bắt chiều ngược (tham số khai mà dataset không cho), nhưng
        chiều này — SQL ràng một tham số mà không definition nào cấp — làm lượt
        đọc đổ ở tầng bind, tức lỗi 500 thay vì một ô lọc trống.
        """
        loaded = load_builtin_reports()
        by_code = {d.code: d for d in loaded.manifest.definitions}
        param_set = loaded.param_set_specs[by_code["so-chi-tiet-ban-hang"].param_set_code]
        assert {param.name for param in param_set.params} == {
            "customer_id",
            "item_id",
            "variant_id",
            "kind",
            "salesperson_id",
            "project_id",
        }
