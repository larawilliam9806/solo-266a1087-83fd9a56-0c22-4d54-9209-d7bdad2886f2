"""Flask 应用工厂与路由。"""
from __future__ import annotations

import os
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import config, media, services
from .db import get_db, init_db, recover_interrupted_batches


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

    init_db()
    recover_interrupted_batches()

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            batches=services.list_batches(),
            recent_media=_list_recent_media(),
        )

    @app.post("/upload")
    def upload():
        files = request.files.getlist("dicom_files")
        if not files or all(not f.filename for f in files):
            flash("请至少选择一个 DICOM 文件", "error")
            return redirect(url_for("index"))
        try:
            batch_id, saved = services.save_uploads(files)
        except Exception as exc:  # noqa: BLE001
            flash(f"上传暂存失败: {exc}", "error")
            return redirect(url_for("index"))
        try:
            services.run_batch(batch_id, saved)
        except services.BatchError as exc:
            flash(str(exc), "error")
            return redirect(url_for("batch_detail", batch_id=batch_id))
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("批次 %s 处理异常", batch_id)
            flash(f"处理失败: {exc}", "error")
            return redirect(url_for("batch_detail", batch_id=batch_id))
        flash(f"批次 {batch_id} 处理完成", "ok")
        return redirect(url_for("batch_detail", batch_id=batch_id))

    # ------------------------------------------------ 影像介质 ZIP
    @app.post("/media/upload")
    def media_upload():
        storage = request.files.get("media_zip")
        if storage is None or not storage.filename:
            flash("请选择一个影像介质 ZIP 文件", "error")
            return redirect(url_for("index"))
        zip_bytes = storage.read()
        try:
            zip_sha256, _index = services.save_media_upload(
                zip_bytes, storage.filename
            )
        except media.MediaError as exc:
            flash(f"介质校验失败: {exc}", "error")
            return redirect(url_for("index"))
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("介质上传处理异常")
            flash(f"介质上传失败: {exc}", "error")
            return redirect(url_for("index"))
        return redirect(url_for("media_preview", zip_sha256=zip_sha256))

    @app.get("/media/<zip_sha256>")
    def media_preview(zip_sha256: str):
        upload = services.get_media_upload(zip_sha256)
        if upload is None:
            abort(404, description="介质不存在或未登记")
        zip_bytes = services.load_media_zip(zip_sha256)
        try:
            index, _ = media.build_index_from_zip(zip_bytes)
        except media.MediaError as exc:
            flash(f"介质目录无法解析: {exc}", "error")
            return redirect(url_for("index"))
        publications = {
            row["series_uid"]: row for row in services.list_publications(zip_sha256)
        }
        return render_template(
            "media.html",
            upload=upload,
            index=index,
            publications=publications,
        )

    @app.post("/media/<zip_sha256>/confirm")
    def media_confirm(zip_sha256: str):
        series_uid = (request.form.get("series_uid") or "").strip()
        if not series_uid:
            flash("请先选择一个序列再确认入库", "error")
            return redirect(url_for("media_preview", zip_sha256=zip_sha256))
        if services.get_media_upload(zip_sha256) is None:
            abort(404, description="介质不存在或未登记")
        try:
            batch_id, outcome = services.publish_series(zip_sha256, series_uid)
        except services.BatchError as exc:
            flash(str(exc), "error")
            return redirect(url_for("media_preview", zip_sha256=zip_sha256))
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("序列发布异常")
            flash(f"发布失败: {exc}", "error")
            return redirect(url_for("media_preview", zip_sha256=zip_sha256))
        if outcome == "existing":
            flash(
                f"该介质的此序列已发布（或正在发布），返回已登记批次 {batch_id}，"
                "同一序列只会产生一个可下载批次",
                "ok",
            )
        else:
            flash(f"序列发布完成，批次 {batch_id}", "ok")
        return redirect(url_for("batch_detail", batch_id=batch_id))

    @app.get("/batches/<batch_id>")
    def batch_detail(batch_id: str):
        batch = services.get_batch(batch_id)
        if batch is None:
            abort(404)
        conn = get_db()
        try:
            files = conn.execute(
                "SELECT * FROM batch_files WHERE batch_id=? ORDER BY orig_name",
                (batch_id,),
            ).fetchall()
        finally:
            conn.close()
        return render_template("batch.html", batch=batch, files=files)

    @app.get("/batches/<batch_id>/download")
    def download(batch_id: str):
        batch = services.get_batch(batch_id)
        if batch is None:
            abort(404)
        if batch["status"] != "ready" or not batch["zip_path"]:
            # 半成品绝不提供下载
            abort(409, description="批次尚未处理成功，无可下载文件包")
        zip_path = Path(batch["zip_path"])
        if not zip_path.exists():
            abort(404, description="文件包缺失")
        return send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"batch_{batch_id}.zip",
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.errorhandler(413)
    def too_large(_err):
        flash("上传内容超过服务端大小限制", "error")
        return redirect(url_for("index"))

    return app


def _list_recent_media(limit: int = 10):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM media_uploads ORDER BY created_at DESC, zip_sha256 DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
