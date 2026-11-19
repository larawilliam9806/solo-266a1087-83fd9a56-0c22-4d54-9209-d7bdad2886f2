"""影像介质 ZIP 入库功能测试。

覆盖：
- 根目录 DICOMDIR 解析（仅 IMAGE 记录、非 IMAGE 忽略、Study/Series/实例数展示）；
- 确认时路径与文件头 UID 核对、缺件/包外路径/不唯一/不可读/压缩语法/UID 不符拒绝；
- 其他序列缺陷不影响所选序列、目录未登记文件不入库；
- 清单含来源 ZIP SHA-256 与介质内路径；
- 同介质同序列只发布一次（含并发确认），失败可重试。
"""
from __future__ import annotations

import io
import re
import threading
import zipfile
from pathlib import Path

import pydicom
import pytest

from samples import generate_samples as gen
from samples.generate_media_zip import build_fileset, _dicom_bytes
from app import media, services


# ---------------------------------------------------------------- 辅助函数
def _build_zip_bytes(sample_dir: Path, mutate=None, *, extras: bool = False) -> bytes:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        fs_dir = Path(tmp) / "fs"
        build_fileset(sample_dir, fs_dir)
        files = {
            p.relative_to(fs_dir).as_posix(): p.read_bytes()
            for p in fs_dir.rglob("*") if p.is_file()
        }
    if extras:
        # DICOMDIR 未登记的游离文件，任何发布都不得带入
        orphan = pydicom.dcmread(str(sample_dir / "ct_001.dcm"))
        files["ORPHAN/unregistered.dcm"] = _dicom_bytes(orphan)
        files["notes.txt"] = b"loose file, must be ignored\n"
    if mutate is not None:
        files = mutate(files)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _upload_media(client, data: bytes, filename: str = "medium.zip"):
    return client.post(
        "/media/upload",
        data={"media_zip": (io.BytesIO(data), filename, "application/zip")},
        content_type="multipart/form-data",
    )


def _sha_from(resp) -> str:
    m = re.search(r"/media/([0-9a-f]{64})", resp.data.decode())
    assert m, resp.data[:300]
    return m.group(1)


def _confirm(client, sha: str, series_uid: str):
    return client.post(
        f"/media/{sha}/confirm",
        data={"series_uid": series_uid},
        follow_redirects=False,
    )


def _download(client, batch_id: str) -> zipfile.ZipFile:
    resp = client.get(f"/batches/{batch_id}/download")
    assert resp.status_code == 200
    return zipfile.ZipFile(io.BytesIO(resp.data))


@pytest.fixture()
def media_zip(sample_files) -> bytes:
    return _build_zip_bytes(sample_files)


# ================================================================ 上传与预览
class TestMediaUploadAndPreview:
    def test_preview_lists_studies_series_and_image_counts(self, client, media_zip):
        resp = _upload_media(client, media_zip)
        assert resp.status_code == 302
        sha = resp.headers["Location"].rstrip("/").split("/")[-1]
        page = client.get(f"/media/{sha}").data.decode()

        # CT 序列（2 条 IMAGE）在列
        assert gen.SERIES_CT_UID in page
        assert gen.STUDY_UID in page
        # 非 IMAGE 叶子（RT/SR）被计数忽略，其所属序列显示 0 实例、不可选
        assert "已忽略非 IMAGE 叶子记录 2 条" in page
        assert "无 IMAGE 记录，不可入库" in page

    def test_only_image_records_are_indexed(self, client, media_zip):
        sha = _sha_from(_upload_media(client, media_zip))
        upload = services.get_media_upload(sha)
        assert upload["image_count"] == 2          # 仅两张 CT
        assert upload["ignored_count"] == 2        # RT STRUCT + SR
        index, _ = media.build_index_from_zip(media_zip)
        image_sops = {img.sop_uid for img in index.iter_images()}
        assert image_sops == {gen.SOP_CT_1, gen.SOP_CT_2}
        assert gen.SOP_RT not in image_sops and gen.SOP_SR not in image_sops

    def test_missing_dicomdir_rejected(self, client, sample_files):
        zb = _build_zip_bytes(
            sample_files,
            lambda f: {k: v for k, v in f.items() if k != "DICOMDIR"},
        )
        resp = _upload_media(client, zb)
        assert resp.status_code == 302
        page = client.get(resp.headers["Location"]).data.decode()
        assert "根目录缺少 DICOMDIR" in page

    def test_bad_zip_rejected(self, client):
        resp = _upload_media(client, b"this is not a zip at all")
        # 失败时回到首页并闪现错误
        assert resp.status_code == 302
        page = client.get(resp.headers["Location"]).data.decode()
        assert "不是有效的 ZIP" in page

    def test_dicomdir_in_subdirectory_not_accepted(self, client, sample_files):
        def move(f):
            f["subdir/DICOMDIR"] = f.pop("DICOMDIR")
            return f
        zb = _build_zip_bytes(sample_files, move)
        resp = _upload_media(client, zb)
        assert resp.status_code == 302
        page = client.get(resp.headers["Location"]).data.decode()
        assert "根目录缺少 DICOMDIR" in page

    def test_same_bytes_uploaded_twice_deduplicated(self, client, media_zip):
        r1 = _upload_media(client, media_zip, "a.zip")
        r2 = _upload_media(client, media_zip, "b.zip")
        assert r1.headers["Location"] == r2.headers["Location"]
        from app import config
        stored = list(config.get_media_dir().rglob("*.zip"))
        assert len(stored) == 1

    def test_unknown_media_returns_404(self, client):
        assert client.get("/media/" + "a" * 64).status_code == 404


# ================================================================ 发布成功路径
class TestPublishHappyPath:
    def test_publish_ct_series_produces_batch_with_manifest(self, client, media_zip):
        sha = _sha_from(_upload_media(client, media_zip))
        resp = _confirm(client, sha, gen.SERIES_CT_UID)
        assert resp.status_code == 302
        batch_id = resp.headers["Location"].rstrip("/").split("/")[-1]

        zf = _download(client, batch_id)
        names = zf.namelist()
        assert len([n for n in names if n.endswith(".dcm")]) == 2
        assert "manifest.csv" in names

        manifest = zf.read("manifest.csv").decode("utf-8-sig").splitlines()
        assert len(manifest) == 3  # 表头 + 2 实例
        for line in manifest[1:]:
            cols = line.split(",")
            assert cols[10] == sha                          # 来源 ZIP SHA-256
            assert cols[11].startswith("PT000000/")        # 介质内路径
            assert cols[7] in (gen.SOP_CT_1, gen.SOP_CT_2)

    def test_unregistered_files_are_never_ingested(self, client, sample_files):
        # 介质中夹带 DICOMDIR 未登记的游离文件，任何发布都不得带入
        zb = _build_zip_bytes(sample_files, extras=True)
        with zipfile.ZipFile(io.BytesIO(zb)) as zf:
            assert "ORPHAN/unregistered.dcm" in zf.namelist()
            assert "notes.txt" in zf.namelist()
        sha = _sha_from(_upload_media(client, zb))
        resp = _confirm(client, sha, gen.SERIES_CT_UID)
        batch_id = resp.headers["Location"].rstrip("/").split("/")[-1]
        zf = _download(client, batch_id)
        assert all("ORPHAN" not in n for n in zf.namelist())
        assert "notes.txt" not in zf.namelist()

    def test_published_instances_are_remapped(self, client, media_zip):
        sha = _sha_from(_upload_media(client, media_zip))
        batch_id = _confirm(client, sha, gen.SERIES_CT_UID).headers[
            "Location"].rstrip("/").split("/")[-1]
        zf = _download(client, batch_id)
        out = pydicom.dcmread(io.BytesIO(zf.read(
            next(n for n in zf.namelist() if n.endswith(".dcm")))))
        assert str(out.StudyInstanceUID) != gen.STUDY_UID
        assert str(out.SeriesInstanceUID) != gen.SERIES_CT_UID
        assert str(out.SOPInstanceUID) not in (gen.SOP_CT_1, gen.SOP_CT_2)
        assert str(out.file_meta.MediaStorageSOPInstanceUID) == str(out.SOPInstanceUID)
        assert str(out.PatientName) == ""

    def test_non_image_series_cannot_be_published(self, client, media_zip):
        sha = _sha_from(_upload_media(client, media_zip))
        resp = client.post(
            f"/media/{sha}/confirm",
            data={"series_uid": gen.SERIES_RT_UID},
            follow_redirects=True,
        )
        assert "没有任何 IMAGE 记录" in resp.data.decode()


# ================================================================ 核对拒绝
class TestVerificationRejections:
    def _sha(self, client, sample_files, mutate):
        return _sha_from(_upload_media(client, _build_zip_bytes(sample_files, mutate)))

    def test_missing_file_is_rejected_and_not_downloadable(self, client, sample_files):
        def mutate(f):
            del f[next(k for k in f if k.endswith("IM000001"))]
            return f
        sha = self._sha(client, sample_files, mutate)
        resp = client.post(
            f"/media/{sha}/confirm",
            data={"series_uid": gen.SERIES_CT_UID},
            follow_redirects=True,
        )
        assert "缺件" in resp.data.decode()
        from app.db import get_db
        conn = get_db()
        try:
            bid = conn.execute(
                "SELECT batch_id FROM series_publications"
            ).fetchone()["batch_id"]
        finally:
            conn.close()
        assert client.get(f"/batches/{bid}/download").status_code == 409

    def test_uid_mismatch_is_rejected(self, client, sample_files):
        def mutate(f):
            target = next(k for k in f if k.endswith("IM000000"))
            ds = pydicom.dcmread(io.BytesIO(f[target]))
            ds.SeriesInstanceUID = "9.9.9.9.9.9"
            f[target] = _dicom_bytes(ds)
            return f
        sha = self._sha(client, sample_files, mutate)
        resp = client.post(
            f"/media/{sha}/confirm",
            data={"series_uid": gen.SERIES_CT_UID},
            follow_redirects=True,
        )
        assert "SeriesInstanceUID" in resp.data.decode() and "不符" in resp.data.decode()

    def test_compressed_transfer_syntax_rejected(self, client, sample_files):
        def mutate(f):
            target = next(k for k in f if k.endswith("IM000000"))
            ds = pydicom.dcmread(io.BytesIO(f[target]))
            del ds.PixelData
            ds.file_meta.TransferSyntaxUID = pydicom.uid.JPEGBaseline8Bit
            f[target] = _dicom_bytes(ds)
            return f
        sha = self._sha(client, sample_files, mutate)
        resp = client.post(
            f"/media/{sha}/confirm",
            data={"series_uid": gen.SERIES_CT_UID},
            follow_redirects=True,
        )
        assert "压缩传输语法" in resp.data.decode()

    def test_unreadable_file_rejected(self, client, sample_files):
        def mutate(f):
            target = next(k for k in f if k.endswith("IM000000"))
            f[target] = b"not a dicom file" * 20
            return f
        sha = self._sha(client, sample_files, mutate)
        resp = client.post(
            f"/media/{sha}/confirm",
            data={"series_uid": gen.SERIES_CT_UID},
            follow_redirects=True,
        )
        assert "无法读取或不是 DICOM" in resp.data.decode()

    def test_path_traversal_rejected(self, client, sample_files):
        pydicom.config.settings.reading_validation_mode = pydicom.config.IGNORE

        def mutate(f):
            dd = pydicom.dcmread(io.BytesIO(f["DICOMDIR"]))
            for rec in dd.DirectoryRecordSequence:
                if getattr(rec, "DirectoryRecordType", "") == "IMAGE":
                    rec.ReferencedFileID = ["..", "..", "etc", "passwd"]
                    break
            f["DICOMDIR"] = _dicom_bytes(dd)
            return f
        try:
            sha = self._sha(client, sample_files, mutate)
            resp = client.post(
                f"/media/{sha}/confirm",
                data={"series_uid": gen.SERIES_CT_UID},
                follow_redirects=True,
            )
            page = resp.data.decode()
            assert "指向介质根目录之外" in page or "非法路径组件" in page
        finally:
            pydicom.config.settings.reading_validation_mode = pydicom.config.RAISE

    def test_duplicate_media_path_rejected(self, client, sample_files):
        def mutate(f):
            dd = pydicom.dcmread(io.BytesIO(f["DICOMDIR"]))
            images = [
                r for r in dd.DirectoryRecordSequence
                if getattr(r, "DirectoryRecordType", "") == "IMAGE"
            ]
            images[1].ReferencedFileID = list(images[0].ReferencedFileID)
            f["DICOMDIR"] = _dicom_bytes(dd)
            return f
        sha = self._sha(client, sample_files, mutate)
        resp = client.post(
            f"/media/{sha}/confirm",
            data={"series_uid": gen.SERIES_CT_UID},
            follow_redirects=True,
        )
        assert "不唯一" in resp.data.decode()

    def test_other_series_defect_does_not_block_selection(self, client, sample_files):
        def mutate(f):
            target = next(k for k in f if k.endswith("SR000000"))
            f[target] = b"broken sr content"
            return f
        sha = self._sha(client, sample_files, mutate)
        resp = _confirm(client, sha, gen.SERIES_CT_UID)
        assert resp.status_code == 302
        batch_id = resp.headers["Location"].rstrip("/").split("/")[-1]
        assert client.get(f"/batches/{batch_id}/download").status_code == 200


# ================================================================ 幂等与重试
class TestIdempotencyAndRetry:
    def test_repeat_confirm_returns_same_batch(self, client, media_zip):
        sha = _sha_from(_upload_media(client, media_zip))
        first = _confirm(client, sha, gen.SERIES_CT_UID).headers[
            "Location"].rstrip("/").split("/")[-1]
        second = _confirm(client, sha, gen.SERIES_CT_UID).headers[
            "Location"].rstrip("/").split("/")[-1]
        assert first == second
        from app.db import get_db
        conn = get_db()
        try:
            count = conn.execute(
                "SELECT COUNT(*) c FROM series_publications "
                "WHERE zip_sha256=? AND series_uid=?",
                (sha, gen.SERIES_CT_UID),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert count == 1

    def test_concurrent_confirms_create_single_batch(self, client, media_zip):
        sha = _sha_from(_upload_media(client, media_zip))
        batch_ids, errors = [], []

        def worker():
            try:
                resp = _confirm(client, sha, gen.SERIES_CT_UID)
                batch_ids.append(resp.headers["Location"].rstrip("/").split("/")[-1])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(set(batch_ids)) == 1
        zf = _download(client, batch_ids[0])
        assert zf.testzip() is None

    def test_concurrent_retries_after_failure_single_batch(self, client, media_zip,
                                                            monkeypatch):
        from app import services as svc

        sha = _sha_from(_upload_media(client, media_zip))
        real = svc.run_batch
        state = {"calls": 0}
        lock = threading.Lock()

        def flaky(batch_id, saved, **kwargs):
            with lock:
                state["calls"] += 1
                if state["calls"] == 1:
                    raise RuntimeError("simulated transient failure")
            return real(batch_id, saved, **kwargs)

        monkeypatch.setattr(svc, "run_batch", flaky)
        first = client.post(
            f"/media/{sha}/confirm",
            data={"series_uid": gen.SERIES_CT_UID},
            follow_redirects=True,
        )
        assert first.status_code == 200
        monkeypatch.undo()

        # 5 个线程同时重试同一 failed 登记：只能产生一个新批次
        batch_ids, errors = [], []

        def worker():
            try:
                resp = _confirm(client, sha, gen.SERIES_CT_UID)
                batch_ids.append(resp.headers["Location"].rstrip("/").split("/")[-1])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        assert len(set(batch_ids)) == 1
        assert _download(client, batch_ids[0]).testzip() is None
        from app.db import get_db
        conn = get_db()
        try:
            pubs = conn.execute(
                "SELECT status, COUNT(*) c FROM series_publications "
                "WHERE zip_sha256=? AND series_uid=? GROUP BY status",
                (sha, gen.SERIES_CT_UID),
            ).fetchall()
        finally:
            conn.close()
        # 登记行只有一行且为 ready
        assert len(pubs) == 1 and pubs[0]["status"] == "ready" and pubs[0]["c"] == 1

    def test_retry_after_transient_failure_succeeds(self, client, media_zip, monkeypatch):
        from app import services as svc

        sha = _sha_from(_upload_media(client, media_zip))
        real = svc.run_batch
        state = {"calls": 0}

        def flaky(batch_id, saved, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("simulated transient failure")
            return real(batch_id, saved, **kwargs)

        monkeypatch.setattr(svc, "run_batch", flaky)
        first = client.post(
            f"/media/{sha}/confirm",
            data={"series_uid": gen.SERIES_CT_UID},
            follow_redirects=True,
        )
        assert first.status_code == 200
        from app.db import get_db
        conn = get_db()
        try:
            pub = conn.execute(
                "SELECT * FROM series_publications WHERE zip_sha256=? AND series_uid=?",
                (sha, gen.SERIES_CT_UID),
            ).fetchone()
        finally:
            conn.close()
        assert pub["status"] == "failed"
        failed_bid = pub["batch_id"]
        assert client.get(f"/batches/{failed_bid}/download").status_code == 409

        monkeypatch.undo()
        second = _confirm(client, sha, gen.SERIES_CT_UID)
        retry_bid = second.headers["Location"].rstrip("/").split("/")[-1]
        assert retry_bid != failed_bid
        assert client.get(f"/batches/{retry_bid}/download").status_code == 200
        conn = get_db()
        try:
            pub2 = conn.execute(
                "SELECT status, batch_id FROM series_publications "
                "WHERE zip_sha256=? AND series_uid=?",
                (sha, gen.SERIES_CT_UID),
            ).fetchone()
        finally:
            conn.close()
        assert pub2["status"] == "ready" and pub2["batch_id"] == retry_bid
        # 失败批次始终不可下载
        assert client.get(f"/batches/{failed_bid}/download").status_code == 409

    def test_different_series_of_same_zip_are_independent(self, client, sample_files):
        # 构造含两个 IMAGE 序列的介质：复制 CT 并改成新 Series UID
        def add_second_series(files):
            target = next(k for k in files if k.endswith("IM000000"))
            ds = pydicom.dcmread(io.BytesIO(files[target]))
            ds.SeriesInstanceUID = "1.2.826.0.1.9999.7777.2099"
            ds.SOPInstanceUID = "1.2.826.0.1.9999.7777.4099"
            ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
            files["PT000000/ST000000/SE000099/IM000000"] = _dicom_bytes(ds)

            dd = pydicom.dcmread(io.BytesIO(files["DICOMDIR"]))
            extra = pydicom.Dataset()
            extra.DirectoryRecordType = "SERIES"
            extra.SeriesInstanceUID = ds.SeriesInstanceUID
            extra.Modality = "CT"
            img = pydicom.Dataset()
            img.DirectoryRecordType = "IMAGE"
            img.ReferencedSOPClassUIDInFile = ds.SOPClassUID
            img.ReferencedSOPInstanceUIDInFile = ds.SOPInstanceUID
            img.ReferencedTransferSyntaxUIDInFile = ds.file_meta.TransferSyntaxUID
            img.ReferencedFileID = ["PT000000", "ST000000", "SE000099", "IM000000"]
            dd.DirectoryRecordSequence.append(extra)
            dd.DirectoryRecordSequence.append(img)
            files["DICOMDIR"] = _dicom_bytes(dd)
            return files

        zb = _build_zip_bytes(sample_files, add_second_series)
        sha = _sha_from(_upload_media(client, zb))
        b1 = _confirm(client, sha, gen.SERIES_CT_UID).headers[
            "Location"].rstrip("/").split("/")[-1]
        b2 = _confirm(client, sha, "1.2.826.0.1.9999.7777.2099").headers[
            "Location"].rstrip("/").split("/")[-1]
        assert b1 != b2
        assert client.get(f"/batches/{b1}/download").status_code == 200
        assert client.get(f"/batches/{b2}/download").status_code == 200
        zf1 = _download(client, b1)
        zf2 = _download(client, b2)
        assert len([n for n in zf1.namelist() if n.endswith(".dcm")]) == 2
        assert len([n for n in zf2.namelist() if n.endswith(".dcm")]) == 1


# ================================================================ 单元：路径安全
class TestPathSafety:
    def test_safe_components_normalized(self):
        path, problem = media._safe_media_path(["PT000001", "ST000001", "IM000000"])
        assert problem is None
        assert path == "PT000001/ST000001/IM000000"

    @pytest.mark.parametrize("components", [
        [],
        ["..", "etc", "passwd"],
        ["a", "../b"],
        ["a/../../b"],
        ["C:", "windows"],
    ])
    def test_unsafe_components_rejected(self, components):
        _, problem = media._safe_media_path(components)
        assert problem
