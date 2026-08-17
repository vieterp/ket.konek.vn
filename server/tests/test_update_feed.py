"""Nguồn cập nhật client mà app server tự phục vụ (bước 18, LD-05).

Không cần PostgreSQL: kho cập nhật là thư mục trên đĩa, và cả hai endpoint đều
ẩn danh nên không chạm tầng danh tính.

Ba nhóm bất biến, mỗi nhóm hỏng theo một kiểu khác nhau và **cả ba đều im lặng**
nếu không có test:

1. Khuôn manifest do Tauri định. Sai tên trường → updater bỏ qua bản cập nhật
   mà không báo lỗi gì, nên máy trạm cứ chạy bản cũ mãi.
2. `204` là câu trả lời cho MỌI lối "không có gì để giao". Trả lỗi thay vào đó
   là dội một lỗi lên mọi máy trạm mỗi lần chúng kiểm.
3. Chỉ tệp có tên trong danh mục mới tải được. Đây là đường ẩn danh duy nhất
   chạm hệ thống tệp.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ket.kernel.updates import catalog
from ket.kernel.updates.catalog import CatalogUnreadableError
from ket.main import create_app
from ket.settings import Settings

PACKAGE_NAME = "Ket_0.9.0_aarch64.app.tar.gz"


def _settings(updates_dir: Path | None) -> Settings:
    return Settings(
        verify_schema_on_startup=False,
        verify_postgres_version_on_startup=False,
        updates_dir=updates_dir,
    )


def _client(updates_dir: Path | None) -> TestClient:
    return TestClient(create_app(_settings(updates_dir)))


@pytest.fixture
def stocked_dir(tmp_path: Path) -> Path:
    """Kho có đúng một bản 0.9.0 cho macOS/aarch64, đẩy lên bằng chính lệnh thật."""
    source = tmp_path / "build" / PACKAGE_NAME
    source.parent.mkdir(parents=True)
    source.write_bytes(b"gia-bo-goi-cap-nhat")
    source.with_name(source.name + ".sig").write_text("chu-ky-gia\n", "utf-8")

    updates_dir = tmp_path / "updates"
    catalog.publish(
        updates_dir,
        package=source,
        version="0.9.0",
        target="darwin",
        arch="aarch64",
        pub_date="2026-08-17T03:00:00Z",
        notes="Lưới nhập liệu",
    )
    return updates_dir


class TestCheckForUpdate:
    def test_offers_newer_build_in_tauri_manifest_shape(self, stocked_dir: Path) -> None:
        with _client(stocked_dir) as client:
            response = client.get("/updates/darwin/aarch64/0.8.0")

        assert response.status_code == 200
        body = response.json()
        # Tên trường là hợp đồng của Tauri, không phải lựa chọn của ta.
        assert set(body) == {"version", "pub_date", "url", "signature", "notes"}
        assert body["version"] == "0.9.0"
        assert body["signature"] == "chu-ky-gia"

    def test_manifest_url_points_back_at_the_host_the_client_used(self, stocked_dir: Path) -> None:
        with _client(stocked_dir) as client:
            response = client.get(
                "/updates/darwin/aarch64/0.8.0", headers={"Host": "ketserver.noi-bo:5443"}
            )

        # Máy trạm tìm tới server bằng địa chỉ nào thì tải gói bằng đúng địa chỉ
        # ấy. Ghim một host trong cấu hình là đúng thứ làm bản cài LAN hỏng:
        # server tự khai `localhost` rồi mọi máy trạm đi tải từ chính nó.
        assert response.json()["url"].startswith("http://ketserver.noi-bo:5443/updates/download/")

    def test_same_version_is_not_an_update(self, stocked_dir: Path) -> None:
        with _client(stocked_dir) as client:
            assert client.get("/updates/darwin/aarch64/0.9.0").status_code == 204

    def test_newer_client_is_not_offered_a_downgrade(self, stocked_dir: Path) -> None:
        with _client(stocked_dir) as client:
            assert client.get("/updates/darwin/aarch64/0.10.0").status_code == 204

    def test_other_platform_gets_nothing(self, stocked_dir: Path) -> None:
        with _client(stocked_dir) as client:
            assert client.get("/updates/windows/x86_64/0.8.0").status_code == 204
            assert client.get("/updates/darwin/x86_64/0.8.0").status_code == 204

    def test_install_without_a_configured_store_still_answers(self) -> None:
        # Bản cài chưa bật đường tự cập nhật vẫn phải chạy bình thường: `204`,
        # không phải `404`/`500`. Một lỗi ở đây là tiếng ồn trên mọi máy trạm.
        with _client(None) as client:
            assert client.get("/updates/darwin/aarch64/0.8.0").status_code == 204

    def test_unparsable_client_version_is_not_guessed(self, stocked_dir: Path) -> None:
        with _client(stocked_dir) as client:
            assert client.get("/updates/darwin/aarch64/khong-phai-so").status_code == 204

    def test_needs_no_session(self, stocked_dir: Path) -> None:
        """Updater chạy ngoài phiên đăng nhập — và đúng lúc cần nhất thì client
        quá cũ tới mức không đăng nhập ghi được gì. Bắt xác thực ở đây là khóa
        đường thoát duy nhất của một máy trạm đã hỏng."""
        with _client(stocked_dir) as client:
            response = client.get("/updates/darwin/aarch64/0.8.0")

        assert response.status_code == 200
        assert "authorization" not in {key.lower() for key in response.request.headers}


class TestDownload:
    def test_serves_the_package_named_in_the_catalog(self, stocked_dir: Path) -> None:
        with _client(stocked_dir) as client:
            response = client.get(f"/updates/download/darwin/aarch64/{PACKAGE_NAME}")

        assert response.status_code == 200
        assert response.content == b"gia-bo-goi-cap-nhat"

    def test_a_real_file_in_the_store_but_not_in_the_catalog_is_unreachable(
        self, stocked_dir: Path
    ) -> None:
        """**Phép thử thật của danh sách trắng.**

        Bản đầu của bài test này xin tệp `.sig` và tưởng đã chứng minh được điều
        đó — nhưng `publish` không bao giờ chép `.sig` vào kho, nên `404` ở đó
        chỉ nói "tệp không tồn tại". Gỡ hẳn whitelist ra thì toàn bộ bộ test vẫn
        xanh (đo được trong review).

        Ở đây tệp **có thật**, nằm đúng thư mục mà endpoint sẽ đọc, và tên nó
        được xin đúng như tên trên đĩa. Chỉ có một lý do để nó không tải được:
        nó không có trong danh mục.
        """
        intruder = stocked_dir / "darwin" / "aarch64" / "bi-mat.env"
        intruder.write_text("KET_DATABASE_PASSWORD=that-su-bi-mat", "utf-8")

        with _client(stocked_dir) as client:
            response = client.get("/updates/download/darwin/aarch64/bi-mat.env")

        assert response.status_code == 404
        assert b"that-su-bi-mat" not in response.content

    def test_same_file_name_on_two_platforms_does_not_cross_over(self, tmp_path: Path) -> None:
        """Hai nền tảng cùng tên gói phải là hai tệp khác nhau.

        `tauri build` đặt tên theo `productName` + phiên bản, nên `Ket_0.9.0.tar.gz`
        trên cả macOS lẫn Linux là chuyện bình thường. Tra theo mỗi tên thì máy
        này tải về binary của máy kia — nó tải xong, chữ ký khớp (cùng khóa ký),
        rồi cài một thứ không chạy được.
        """
        updates_dir = tmp_path / "kho"
        for target, payload in (("darwin", b"ban-macos"), ("linux", b"ban-linux")):
            source = tmp_path / target / "Ket_0.9.0.tar.gz"
            source.parent.mkdir(parents=True)
            source.write_bytes(payload)
            source.with_name(source.name + ".sig").write_text("k", "utf-8")
            catalog.publish(
                updates_dir,
                package=source,
                version="0.9.0",
                target=target,
                arch="x86_64",
                pub_date="2026-08-17T05:00:00Z",
            )

        with _client(updates_dir) as client:
            assert (
                client.get("/updates/download/darwin/x86_64/Ket_0.9.0.tar.gz").content
                == b"ban-macos"
            )
            assert (
                client.get("/updates/download/linux/x86_64/Ket_0.9.0.tar.gz").content
                == b"ban-linux"
            )

    @pytest.mark.parametrize(
        "attempt",
        [
            "../index.json",
            "..%2Findex.json",
            "....//index.json",
            "/etc/passwd",
            "Ket_0.9.0_aarch64.app.tar.gz.sig",
        ],
    )
    def test_only_catalogued_names_are_reachable(self, stocked_dir: Path, attempt: str) -> None:
        """Danh sách trắng, không lọc chuỗi.

        Kể cả `.sig` — nó nằm cạnh gói trong kho nhưng KHÔNG có trong danh mục,
        nên nó cũng không tải được. Đó là phép thử tốt nhất cho "chỉ tên trong
        danh mục": một tệp có thật, ngay đúng thư mục ấy, vẫn không với tới được.

        Khẳng định là **không tệp nào rời máy chủ**, chứ không phải một mã lỗi
        cụ thể: đường có `..` mang ba đoạn nên nó khớp vào route *kiểm cập nhật*
        (`/{target}/{arch}/{current_version}`) và nhận `204`. Khác mã, cùng kết
        quả — mà kết quả mới là thứ đáng khóa lại.
        """
        with _client(stocked_dir) as client:
            response = client.get(f"/updates/download/darwin/aarch64/{attempt}")

        assert response.status_code != 200
        assert b"gia-bo-goi-cap-nhat" not in response.content
        assert b"chu-ky-gia" not in response.content

    def test_catalogued_but_deleted_from_disk_is_a_clean_404(self, stocked_dir: Path) -> None:
        (stocked_dir / "darwin" / "aarch64" / PACKAGE_NAME).unlink()

        with _client(stocked_dir) as client:
            response = client.get(f"/updates/download/darwin/aarch64/{PACKAGE_NAME}")

        assert response.status_code == 404
        assert response.json()["error_code"] == "system.update_package_not_found"


class TestCatalog:
    def test_publishing_twice_replaces_instead_of_duplicating(self, stocked_dir: Path) -> None:
        source = stocked_dir.parent / "build" / PACKAGE_NAME
        catalog.publish(
            stocked_dir,
            package=source,
            version="0.9.0",
            target="darwin",
            arch="aarch64",
            pub_date="2026-08-17T04:00:00Z",
            notes="dựng lại",
        )

        releases = catalog.load_releases(stocked_dir)
        assert len(releases) == 1
        assert releases[0].notes == "dựng lại"

    def test_missing_signature_is_refused_before_anything_is_copied(self, tmp_path: Path) -> None:
        package = tmp_path / "Ket_0.9.0_x64-setup.exe"
        package.write_bytes(b"x")
        updates_dir = tmp_path / "kho"

        with pytest.raises(ValueError, match="chữ ký"):
            catalog.publish(
                updates_dir,
                package=package,
                version="0.9.0",
                target="windows",
                arch="x86_64",
                pub_date="2026-08-17T04:00:00Z",
            )

        # Thiếu chữ ký thì mọi máy trạm sẽ từ chối gói. Copy nó lên rồi mới phát
        # hiện là để lại rác trong kho mà không ai dọn.
        assert not updates_dir.exists()

    @pytest.mark.parametrize(
        ("version", "target", "arch"),
        [("0.9", "darwin", "aarch64"), ("0.9.0", "macos", "aarch64"), ("0.9.0", "darwin", "arm64")],
    )
    def test_typos_are_refused_at_publish_time(
        self, tmp_path: Path, version: str, target: str, arch: str
    ) -> None:
        """Gõ sai `--target` phải hỏng ở lệnh publish, không phải im lặng ở máy
        trạm — `macos` và `arm64` là hai cách gõ sai rất tự nhiên."""
        package = tmp_path / "goi.tar.gz"
        package.write_bytes(b"x")
        package.with_name(package.name + ".sig").write_text("k", "utf-8")

        with pytest.raises(ValueError):
            catalog.publish(
                tmp_path / "kho",
                package=package,
                version=version,
                target=target,
                arch=arch,
                pub_date="2026-08-17T04:00:00Z",
            )

    def test_hand_edited_traversal_in_the_catalog_is_refused(self, stocked_dir: Path) -> None:
        """`index.json` là một tệp trên đĩa và người ta sửa tay được."""
        _corrupt(stocked_dir, file_name="../../../etc/passwd")

        with pytest.raises(CatalogUnreadableError, match="tên tệp không hợp lệ"):
            catalog.load_releases(stocked_dir)

    def test_catalog_from_a_future_server_is_refused(self, stocked_dir: Path) -> None:
        path = stocked_dir / catalog.CATALOG_FILE_NAME
        payload = json.loads(path.read_text("utf-8"))
        payload["schema"] = catalog.CATALOG_SCHEMA + 1
        path.write_text(json.dumps(payload), "utf-8")

        with pytest.raises(CatalogUnreadableError, match="schema"):
            catalog.load_releases(stocked_dir)


def _corrupt(updates_dir: Path, **overrides: object) -> None:
    path = updates_dir / catalog.CATALOG_FILE_NAME
    payload = json.loads(path.read_text("utf-8"))
    payload["releases"][0].update(overrides)
    path.write_text(json.dumps(payload), "utf-8")


class TestBrokenCatalogDoesNotBecomeAnAccountingIncident:
    """Danh mục hỏng là sự cố **cập nhật**, không được thành sự cố **kế toán**.

    Bản cài vẫn ghi sổ được bình thường khi kho cập nhật hỏng, nên hướng hỏng
    đúng là `204`/`404` + một dòng log cho người quản trị — chứ không phải `500`
    dội lên mọi máy trạm ở mỗi lượt kiểm. Trước bản sửa này, cả bảy dạng hỏng
    dưới đây đều cho `500` (đo được trên server thật trong review).
    """

    @staticmethod
    def _break(updates_dir: Path, raw: str) -> None:
        (updates_dir / catalog.CATALOG_FILE_NAME).write_text(raw, "utf-8")

    @pytest.mark.parametrize(
        "raw",
        [
            "{ khong phai json",
            "[]",
            '{"schema": 1, "releases": "khong-phai-danh-sach"}',
            '{"schema": 1, "releases": [{"version": "0.9.0"}]}',
            '{"schema": 99, "releases": []}',
            '{"schema": 1, "releases": [42]}',
        ],
    )
    def test_check_answers_204(self, stocked_dir: Path, raw: str) -> None:
        self._break(stocked_dir, raw)

        with _client(stocked_dir) as client:
            assert client.get("/updates/darwin/aarch64/0.8.0").status_code == 204

    def test_download_answers_404(self, stocked_dir: Path) -> None:
        self._break(stocked_dir, "{ khong phai json")

        with _client(stocked_dir) as client:
            response = client.get(f"/updates/download/darwin/aarch64/{PACKAGE_NAME}")

        assert response.status_code == 404

    def test_bumping_the_catalog_schema_is_an_upgrade_not_an_outage(
        self, stocked_dir: Path
    ) -> None:
        """Nâng `CATALOG_SCHEMA` 1→2 ở một bản server sau này phải là một bước
        nâng cấp, không phải một sự cố: server cũ đọc danh mục mới thì ngừng
        chào bản cập nhật, chứ không ngừng phục vụ."""
        self._break(stocked_dir, '{"schema": 2, "releases": []}')

        with _client(stocked_dir) as client:
            assert client.get("/updates/darwin/aarch64/0.8.0").status_code == 204
            assert client.get("/health").status_code == 200
