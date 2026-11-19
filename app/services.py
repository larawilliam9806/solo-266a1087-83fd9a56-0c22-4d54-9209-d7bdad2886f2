"""批次编排：暂存 -> 计划 -> 映射入库 -> 对象落盘 -> 原子打包 -> 状态流转。"""
from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import uuid
import zipfile
from pathlib import Path

from . import config, dicom_proc, media
from .db import get_db, immediate_tx


class BatchError(Exception):
    pass


def save_uploads(files) -> tuple[str, list[tuple[str, Path]]]:
    """把上传流保存到独立暂存目录（每个批次一个新空目录）。"""
    batch_id = uuid.uuid4().hex
    staging = config.get_upload_tmp_dir() / batch_id
    staging.mkdir(parents=True, exist_ok=False)
    saved: list[tuple[str, Path]] = []
    try:
        for storage in files:
            if not storage or not storage.filename:
                continue
            # 仅取文件名部分，防止路径穿越
            safe_name = Path(storage.filename).name
            target = staging / f"{len(saved):05d}_{safe_name}"
            storage.save(str(target))
            saved.append((storage.filename, target))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return batch_id, saved


def _cleanup_staging(batch_id: str) -> None:
    shutil.rmtree(config.get_upload_tmp_dir() / batch_id, ignore_errors=True)


def _mark_failed(batch_id: str, error: str) -> None:
    conn = get_db()
    try:
        with immediate_tx(conn):
            conn.execute(
                "UPDATE batches SET status='failed', error=?, "
                "finished_at=datetime('now') WHERE id=?",
                (error, batch_id),
            )
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def _build_manifest(batch_id: str) -> bytes:
    """生成 CSV 映射清单，随 zip 交付供数据湖关联。"""
    conn = get_db()
    try:
        batch = conn.execute(
            "SELECT source_zip_sha256 FROM batches WHERE id=?", (batch_id,)
        ).fetchone()
        source_zip_sha256 = batch["source_zip_sha256"] if batch else ""
        files = conn.execute(
            "SELECT * FROM batch_files WHERE batch_id=? ORDER BY orig_name",
            (batch_id,),
        ).fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "original_filename", "original_patient_id", "new_patient_id",
            "original_study_uid", "new_study_uid",
            "original_series_uid", "new_series_uid",
            "original_sop_uid", "new_sop_uid", "sha256",
            "source_zip_sha256", "media_path",
        ])
        for f in files:
            inst = conn.execute(
                "SELECT new_sop_uid, sha256 FROM instances WHERE sop_uid=?",
                (f["sop_uid"],),
            ).fetchone()
            study_new = conn.execute(
                "SELECT new_uid FROM uid_mappings WHERE role='study' AND original_uid=?",
                (f["original_study_uid"],),
            ).fetchone()
            series_new = conn.execute(
                "SELECT new_uid FROM uid_mappings WHERE role='series' AND original_uid=?",
                (f["original_series_uid"],),
            ).fetchone()
            pid_new = conn.execute(
                "SELECT new_patient_id FROM patient_mappings WHERE original_patient_id=?",
                (f["original_patient_id"],),
            ).fetchone()
            writer.writerow([
                f["orig_name"],
                f["original_patient_id"],
                pid_new["new_patient_id"] if pid_new else "",
                f["original_study_uid"],
                study_new["new_uid"] if study_new else "",
                f["original_series_uid"],
                series_new["new_uid"] if series_new else "",
                f["sop_uid"],
                inst["new_sop_uid"],
                inst["sha256"],
                source_zip_sha256,
                f["media_path"],
            ])
        # UTF-8 BOM，方便 Excel 直接打开中文
        return buf.getvalue().encode("utf-8-sig")
    finally:
        conn.close()


def _build_zip(batch_id: str, plan) -> Path:
    """把对象存储中的处理结果与清单打成 zip，先写临时文件再原子 rename。"""
    batches_dir = config.get_batches_dir()
    final_zip = batches_dir / f"{batch_id}.zip"
    tmp_zip = batches_dir / f".{batch_id}.zip.tmp"
    conn = get_db()
    try:
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for pf in plan.files:
                inst = conn.execute(
                    "SELECT object_path, new_sop_uid FROM instances WHERE sop_uid=?",
                    (pf.sop_uid,),
                ).fetchone()
                abs_path = config.get_objects_dir() / inst["object_path"]
                if not abs_path.exists():
                    raise BatchError(f"处理结果缺失: {abs_path}")
                zf.write(abs_path, arcname=f"{inst['new_sop_uid']}.dcm")
            zf.writestr("manifest.csv", _build_manifest(batch_id))
        with open(tmp_zip, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp_zip, final_zip)
        return final_zip
    except Exception:
        Path(tmp_zip).unlink(missing_ok=True)
        raise
    finally:
        conn.close()


def run_batch(
    batch_id: str,
    saved: list[tuple[str, Path]],
    source_zip_sha256: str = "",
    source_series_uid: str = "",
    media_paths: dict[str, str] | None = None,
):
    """完整执行一个批次。任何异常 -> failed，绝不出现可下载的半批。

    介质来源参数（普通文件上传时留空）：
    - source_zip_sha256: 来源介质 ZIP 的 SHA-256，写入批次与清单；
    - source_series_uid: 本次发布所选自序列 UID；
    - media_paths: 暂存文件名/原始名 -> 介质内路径 的映射，写入 batch_files。
    """
    media_paths = media_paths or {}
    # 先登记批次行，保证计划阶段（含整批拒绝）的失败也可在详情页追溯。
    # 介质发布流程已在同一事务里预建 processing 批次行，这里用 INSERT OR IGNORE 兼容。
    conn = get_db()
    try:
        with immediate_tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO batches(id, status, original_count, "
                "source_zip_sha256, source_series_uid) "
                "VALUES(?, 'processing', ?, ?, ?)",
                (batch_id, len(saved), source_zip_sha256, source_series_uid),
            )
            # 普通上传路径下为新插入行；介质路径下补齐实例计数
            conn.execute(
                "UPDATE batches SET original_count=? WHERE id=?",
                (len(saved), batch_id),
            )
    finally:
        conn.close()
    try:
        plan = dicom_proc.plan_batch(saved)
        for pf in plan.files:
            # 介质批次：orig_name 使用介质内路径，直接据此回填
            pf.media_path = media_paths.get(pf.orig_name, "")
        dicom_proc.persist_mappings(plan)
        dicom_proc.process_plan(plan, batch_id)
        zip_path = _build_zip(batch_id, plan)
        conn = get_db()
        try:
            with immediate_tx(conn):
                conn.execute(
                    "UPDATE batches SET status='ready', zip_path=?, "
                    "finished_at=datetime('now') WHERE id=?",
                    (str(zip_path), batch_id),
                )
        finally:
            conn.close()
        return batch_id
    except dicom_proc.BatchRejected as exc:
        _mark_failed(batch_id, f"整批拒绝: {exc}")
        raise BatchError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        _mark_failed(batch_id, f"处理失败: {exc}")
        raise
    finally:
        _cleanup_staging(batch_id)


def get_batch(batch_id: str) -> sqlite3.Row | None:
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM batches WHERE id=?", (batch_id,)
        ).fetchone()
    finally:
        conn.close()


def list_batches(limit: int = 50) -> list[sqlite3.Row]:
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM batches ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()


# ============================================================
# 影像介质 ZIP：上传登记 -> 预览 -> 选序列发布（幂等）
# ============================================================

def _media_zip_relpath(zip_sha256: str) -> str:
    # 两级分片，文件名仅含十六进制字符，天然路径安全
    return str(Path(zip_sha256[:2]) / zip_sha256[2:4] / f"{zip_sha256}.zip")


def save_media_upload(zip_bytes: bytes, original_filename: str) -> tuple[str, media.MediaIndex]:
    """校验并登记一次介质上传，返回 (zip_sha256, 目录索引)。

    ZIP 按 SHA-256 内容寻址落盘：同一份介质重复上传只保存一份。
    介质本身不合法（非 ZIP、缺 DICOMDIR、目录损坏）时抛 MediaError，不留登记。
    """
    index, dicomdir_member = media.build_index_from_zip(
        zip_bytes, verify_integrity=True
    )
    zip_sha256 = media.sha256_bytes(zip_bytes)

    media_dir = config.get_media_dir()
    relpath = _media_zip_relpath(zip_sha256)
    final_path = media_dir / relpath
    if not final_path.exists():
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = media_dir / f".{os.getpid()}-{uuid.uuid4().hex}.zip.tmp"
        tmp_path.write_bytes(zip_bytes)
        with open(tmp_path, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp_path, final_path)

    image_count = sum(1 for _ in index.iter_images())
    conn = get_db()
    try:
        with immediate_tx(conn):
            conn.execute(
                "INSERT INTO media_uploads(zip_sha256, zip_path, filename, "
                "dicomdir_member, image_count, ignored_count) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(zip_sha256) DO UPDATE SET filename=excluded.filename",
                (zip_sha256, relpath, Path(original_filename).name,
                 dicomdir_member, image_count, index.ignored_leaf_count),
            )
    finally:
        conn.close()
    return zip_sha256, index


def get_media_upload(zip_sha256: str) -> sqlite3.Row | None:
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM media_uploads WHERE zip_sha256=?", (zip_sha256,)
        ).fetchone()
    finally:
        conn.close()


def load_media_zip(zip_sha256: str) -> bytes:
    """从对象存储读取已登记介质的 ZIP 字节（确认必须复用预览时的同一份）。"""
    row = get_media_upload(zip_sha256)
    if row is None:
        raise BatchError(f"介质 {zip_sha256} 未登记或已过期，请重新上传")
    path = config.get_media_dir() / row["zip_path"]
    if not path.exists():
        raise BatchError(f"介质文件缺失: {path}")
    return path.read_bytes()


def get_publication(zip_sha256: str, series_uid: str) -> sqlite3.Row | None:
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM series_publications WHERE zip_sha256=? AND series_uid=?",
            (zip_sha256, series_uid),
        ).fetchone()
    finally:
        conn.close()


def list_publications(zip_sha256: str) -> list[sqlite3.Row]:
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM series_publications WHERE zip_sha256=? "
            "ORDER BY created_at",
            (zip_sha256,),
        ).fetchall()
    finally:
        conn.close()


def _set_publication_failed(
    zip_sha256: str, series_uid: str, batch_id: str, error: str
) -> None:
    conn = get_db()
    try:
        with immediate_tx(conn):
            conn.execute(
                "UPDATE series_publications SET status='failed', error=?, "
                "updated_at=datetime('now') WHERE zip_sha256=? AND series_uid=?",
                (error, zip_sha256, series_uid),
            )
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def publish_series(zip_sha256: str, series_uid: str) -> tuple[str, str]:
    """发布介质中的一个序列，返回 (batch_id, outcome)。

    outcome:
      - ``existing``  该 (介质, 序列) 已发布过或正在发布，返回已登记批次（幂等）；
      - ``created``   本次新建并执行了发布。

    并发确认由 series_publications 的主键 + IMMEDIATE 事务串行化：只有一个请求
    能插入登记行，其余请求拿到同一批次，保证同一份 ZIP 的同一序列只发布一次。
    失败（含核对拒绝）后登记行保留为 failed，允许重试，重试成功复用同一登记行。
    """
    upload = get_media_upload(zip_sha256)
    if upload is None:
        raise BatchError("介质未登记，请重新上传 ZIP")

    # 幂等快路径：已有非 failed 登记（ready/processing）直接返回其批次；
    # failed 则落到下面的事务重试（事务内会再次核对，覆盖并发窗口）。
    existing = get_publication(zip_sha256, series_uid)
    if existing is not None and existing["status"] != "failed":
        return existing["batch_id"], "existing"

    conn = get_db()
    try:
        with immediate_tx(conn):
            # 事务内再读一次，覆盖“快路径之后、事务之前”被其他并发请求改写的窗口
            current = conn.execute(
                "SELECT batch_id, status FROM series_publications "
                "WHERE zip_sha256=? AND series_uid=?",
                (zip_sha256, series_uid),
            ).fetchone()
            if current is not None and current["status"] != "failed":
                # 已有 ready/processing 登记（并发请求抢先），幂等返回其批次
                return current["batch_id"], "existing"

            batch_id = uuid.uuid4().hex
            # 批次行与发布登记在同一 IMMEDIATE 事务内建立，满足外键并串行化并发确认
            conn.execute(
                "INSERT INTO batches(id, status, original_count, "
                "source_zip_sha256, source_series_uid) "
                "VALUES(?, 'processing', 0, ?, ?)",
                (batch_id, zip_sha256, series_uid),
            )
            if current is None:
                conn.execute(
                    "INSERT INTO series_publications"
                    "(zip_sha256, series_uid, batch_id, status) "
                    "VALUES(?,?,?, 'processing')",
                    (zip_sha256, series_uid, batch_id),
                )
            else:
                # failed 重试：复用同一登记行，挂接新批次行
                conn.execute(
                    "UPDATE series_publications SET batch_id=?, status='processing', "
                    "error=NULL, updated_at=datetime('now') "
                    "WHERE zip_sha256=? AND series_uid=?",
                    (batch_id, zip_sha256, series_uid),
                )
    finally:
        conn.close()

    # 所有核对与处理都使用预览时落盘的同一份 ZIP
    zip_bytes = load_media_zip(zip_sha256)
    actual_sha = media.sha256_bytes(zip_bytes)
    if actual_sha != zip_sha256:
        err = f"介质内容与登记哈希不符（期望 {zip_sha256}，实际 {actual_sha}）"
        _mark_failed(batch_id, err)
        _set_publication_failed(zip_sha256, series_uid, batch_id, err)
        raise BatchError(err)

    index, _ = media.build_index_from_zip(zip_bytes)
    staging = config.get_upload_tmp_dir() / batch_id
    try:
        try:
            images = media.verify_series(zip_bytes, index, series_uid)
        except media.PublicationRejected as exc:
            _mark_failed(batch_id, f"发布拒绝: {exc}")
            _set_publication_failed(zip_sha256, series_uid, batch_id, str(exc))
            raise BatchError(str(exc)) from exc

        saved_tuples = media.extract_series_files(zip_bytes, images, staging)
        # orig_name 统一用介质内路径：既是可读来源名，也是清单里的 media_path
        saved = [(media_path, tmp_path) for media_path, tmp_path in saved_tuples]
        media_paths = {media_path: media_path for media_path, _ in saved_tuples}
        try:
            run_batch(
                batch_id, saved,
                source_zip_sha256=zip_sha256,
                source_series_uid=series_uid,
                media_paths=media_paths,
            )
        except Exception as exc:  # noqa: BLE001 - run_batch 已把批次置 failed
            _set_publication_failed(zip_sha256, series_uid, batch_id, str(exc))
            raise

        conn = get_db()
        try:
            with immediate_tx(conn):
                conn.execute(
                    "UPDATE series_publications SET status='ready', error=NULL, "
                    "updated_at=datetime('now') WHERE zip_sha256=? AND series_uid=?",
                    (zip_sha256, series_uid),
                )
        finally:
            conn.close()
        return batch_id, "created"
    finally:
        shutil.rmtree(staging, ignore_errors=True)
