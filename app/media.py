"""影像介质 ZIP 的接收、DICOMDIR 解析与确认前核对。

业务规则（见 README“介质入库规则”）：

- 介质根目录必须恰好有一个 ``DICOMDIR``；
- 仅解析目录中的 **IMAGE** 记录，其他叶子记录（SR、RT STRUCT、KEY OBJECT 等）
  不参与预览与入库；
- 仅接受未压缩传输语法的 DICOM；
- 确认时从**预览所用的同一份 ZIP**取文件，逐实例核对目录记录路径与文件头里的
  Study/Series/SOP UID；
- 所选序列的记录缺件、路径指向包外或不唯一、文件无法读取或 UID 不符 ->
  指出问题记录并拒绝发布；其他序列的问题不影响所选序列；
- 目录未登记的文件（包括游离 DICOM）一律不入库。
"""
from __future__ import annotations

import hashlib
import io
import posixpath
import zipfile
from dataclasses import dataclass, field

import pydicom

from .dicom_proc import UNCOMPRESSED_TS

DICOMDIR_NAME = "DICOMDIR"

# 目录记录类型标签
TAG_RECORD_TYPE = pydicom.tag.Tag(0x0004, 0x1430)
TAG_REFERENCED_FILE_ID = pydicom.tag.Tag(0x0004, 0x1500)
TAG_REF_SOP_UID_IN_FILE = pydicom.tag.Tag(0x0004, 0x1511)
TAG_STUDY_UID = pydicom.tag.Tag(0x0020, 0x000D)
TAG_SERIES_UID = pydicom.tag.Tag(0x0020, 0x000E)
TAG_SOP_UID = pydicom.tag.Tag(0x0008, 0x0018)
TAG_MODALITY = pydicom.tag.Tag(0x0008, 0x0060)
TAG_SERIES_NUMBER = pydicom.tag.Tag(0x0020, 0x0011)
TAG_PATIENT_ID = pydicom.tag.Tag(0x0010, 0x0020)
TAG_PATIENT_NAME = pydicom.tag.Tag(0x0010, 0x0010)
TAG_STUDY_DATE = pydicom.tag.Tag(0x0008, 0x0020)
TAG_STUDY_DESCRIPTION = pydicom.tag.Tag(0x0008, 0x1030)
TAG_INSTANCE_NUMBER = pydicom.tag.Tag(0x0020, 0x0013)


class MediaError(Exception):
    """介质本身不合法（无 DICOMDIR、ZIP 损坏等），无法进入预览。"""


class PublicationRejected(Exception):
    """所选序列核对未通过，拒绝发布。message 面向数据员，包含问题记录明细。"""


@dataclass
class ImageRecord:
    """DICOMDIR 中的一条 IMAGE 记录（叶子）。"""

    record_index: int
    study_uid: str
    series_uid: str
    sop_uid: str                       # Referenced SOP Instance UID in File
    media_path: str                    # 介质内 POSIX 路径（由 ReferencedFileID 拼成）
    file_id_components: list[str]
    instance_number: str = ""
    # 确认核对阶段填充
    problems: list[str] = field(default_factory=list)


@dataclass
class SeriesNode:
    series_uid: str
    modality: str = ""
    series_number: str = ""
    images: list[ImageRecord] = field(default_factory=list)


@dataclass
class StudyNode:
    study_uid: str
    study_date: str = ""
    study_description: str = ""
    series_map: dict[str, SeriesNode] = field(default_factory=dict)

    @property
    def series(self) -> list[SeriesNode]:
        return list(self.series_map.values())


@dataclass
class PatientNode:
    patient_id: str
    patient_name: str
    study_map: dict[str, StudyNode] = field(default_factory=dict)

    @property
    def studies(self) -> list[StudyNode]:
        return list(self.study_map.values())


@dataclass
class MediaIndex:
    """DICOMDIR 解析结果：层级树 + 扁平 IMAGE 记录索引。"""

    patients: list[PatientNode] = field(default_factory=list)
    # media_path -> IMAGE 记录（同一 ZIP 内路径必须唯一）
    path_to_image: dict[str, ImageRecord] = field(default_factory=dict)
    # 非 IMAGE 叶子记录数量，仅用于页面提示“已忽略”
    ignored_leaf_count: int = 0
    total_records: int = 0

    def iter_images(self):
        for patient in self.patients:
            for study in patient.studies:
                for series in study.series:
                    for image in series.images:
                        yield image

    def find_series(self, series_uid: str) -> SeriesNode | None:
        for patient in self.patients:
            for study in patient.studies:
                node = study.series_map.get(series_uid)
                if node is not None:
                    return node
        return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_media_path(components: list[str]) -> tuple[str | None, str | None]:
    """把 ReferencedFileID 组件拼成介质内相对路径，并做包外/穿越校验。

    返回 (media_path, problem)；problem 非空时 media_path 为 None。
    """
    if not components:
        return None, "ReferencedFileID 为空"
    cleaned: list[str] = []
    for comp in components:
        # 规范介质组件不应含分隔符；出现分隔符按异常组件处理，避免歧义
        if "/" in comp or "\\" in comp or comp in ("", ".", ".."):
            return None, f"ReferencedFileID 含非法路径组件: {components!r}"
        cleaned.append(comp)
    joined = posixpath.join(*cleaned)
    # 归一化后任何向上逃逸、绝对路径、盘符/UNC 都拒绝
    normalized = posixpath.normpath(joined)
    if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        return None, f"路径指向介质根目录之外: {joined}"
    if len(normalized) >= 2 and normalized[1] == ":":
        return None, f"路径含盘符，疑似包外引用: {joined}"
    if normalized.startswith("\\\\"):
        return None, f"UNC 路径拒绝: {joined}"
    return normalized, None


def _str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, pydicom.multival.MultiValue):
        return "\\".join(str(v) for v in value)
    return str(value)


def parse_dicomdir(data: bytes) -> MediaIndex:
    """解析 DICOMDIR 字节，按 DICOM 标准前序记录布局重建层级树。

    DICOMDIR 的 DirectoryRecordSequence 按 PATIENT -> STUDY -> SERIES -> 叶子
    的深度优先前序排列；用“记录类型 + 栈”重建父子关系，不依赖记录字节偏移，
    因此对经过改写、偏移失效的目录同样稳健。
    """
    # 读取放宽 VR 值校验：恶意/老旧介质可能含非法 CS 等取值，目录结构仍可解析，
    # 路径安全与 UID 核对由本模块自己的规则兜底，并给出面向数据员的中文报错。
    previous_mode = pydicom.config.settings.reading_validation_mode
    pydicom.config.settings.reading_validation_mode = pydicom.config.WARN
    try:
        try:
            # 直接走底层读取：公开 dcmread 会按 SOP Class 自动构造 pydicom 的
            # DicomDir 类，该类用记录字节偏移建树，经过第三方工具改写、偏移失效的
            # DICOMDIR 会直接报错。DirectoryRecordSequence 本身是标准前序排列，
            # 下面用“记录类型 + 栈”重建层级，不依赖任何偏移。
            fp = io.BytesIO(data)
            pydicom.filereader.read_preamble(fp, False)
            file_meta = pydicom.filereader._read_file_meta_info(fp)
            original_class = None
            if file_meta is not None and "MediaStorageSOPClassUID" in file_meta:
                original_class = str(file_meta.MediaStorageSOPClassUID)
            ts = file_meta.get("TransferSyntaxUID") if file_meta is not None else None
            is_implicit = ts is None or str(ts) == "1.2.840.10008.1.2"
            # DICOMDIR 标准传输语法为 Explicit VR Little Endian
            ds_body = pydicom.filereader.read_dataset(
                fp, is_implicit_VR=is_implicit, is_little_endian=True
            )
            dd = pydicom.dataset.FileDataset(
                io.BytesIO(data), ds_body, file_meta=file_meta, preamble=b"\0" * 128
            )
            if original_class is not None:
                dd.file_meta.MediaStorageSOPClassUID = original_class
        except Exception as exc:  # noqa: BLE001
            raise MediaError(f"DICOMDIR 无法解析为 DICOM 文件: {exc}") from exc
    finally:
        pydicom.config.settings.reading_validation_mode = previous_mode

    sop_class = str(getattr(getattr(dd, "file_meta", None), "MediaStorageSOPClassUID", ""))
    if sop_class and sop_class != "1.2.840.10008.1.3.10":
        raise MediaError(f"根目录文件不是 Media Storage Directory（SOP Class={sop_class}）")
    if "DirectoryRecordSequence" not in dd or not dd.DirectoryRecordSequence:
        raise MediaError("DICOMDIR 中没有 Directory Record Sequence")

    index = MediaIndex()
    current_patient: PatientNode | None = None
    current_study: StudyNode | None = None
    current_series: SeriesNode | None = None

    for record_index, record in enumerate(dd.DirectoryRecordSequence):
        index.total_records += 1
        if TAG_RECORD_TYPE not in record:
            # 非标准记录无法定位层级，直接拒绝，避免静默漏数据
            raise MediaError(f"第 {record_index + 1} 条目录记录缺少 Directory Record Type")
        rec_type = str(record[TAG_RECORD_TYPE].value).strip().upper()

        if rec_type == "PATIENT":
            current_patient = PatientNode(
                patient_id=_str(record[TAG_PATIENT_ID].value)
                if TAG_PATIENT_ID in record else "",
                patient_name=_str(record[TAG_PATIENT_NAME].value)
                if TAG_PATIENT_NAME in record else "",
            )
            index.patients.append(current_patient)
            current_study = None
            current_series = None
            continue

        if rec_type == "STUDY":
            if current_patient is None:
                raise MediaError(f"第 {record_index + 1} 条 STUDY 记录缺少上级 PATIENT")
            study_uid = _str(record[TAG_STUDY_UID].value) if TAG_STUDY_UID in record else ""
            if not study_uid:
                raise MediaError(f"第 {record_index + 1} 条 STUDY 记录缺少 Study Instance UID")
            current_study = current_patient.study_map.setdefault(
                study_uid,
                StudyNode(
                    study_uid=study_uid,
                    study_date=_str(record[TAG_STUDY_DATE].value)
                    if TAG_STUDY_DATE in record else "",
                    study_description=_str(record[TAG_STUDY_DESCRIPTION].value)
                    if TAG_STUDY_DESCRIPTION in record else "",
                ),
            )
            current_series = None
            continue

        if rec_type == "SERIES":
            if current_study is None:
                raise MediaError(f"第 {record_index + 1} 条 SERIES 记录缺少上级 STUDY")
            series_uid = _str(record[TAG_SERIES_UID].value) if TAG_SERIES_UID in record else ""
            if not series_uid:
                raise MediaError(f"第 {record_index + 1} 条 SERIES 记录缺少 Series Instance UID")
            current_series = current_study.series_map.setdefault(
                series_uid,
                SeriesNode(
                    series_uid=series_uid,
                    modality=_str(record[TAG_MODALITY].value)
                    if TAG_MODALITY in record else "",
                    series_number=_str(record[TAG_SERIES_NUMBER].value)
                    if TAG_SERIES_NUMBER in record else "",
                ),
            )
            continue

        # 叶子记录：只处理 IMAGE，其余类型计数后忽略
        if rec_type != "IMAGE":
            index.ignored_leaf_count += 1
            continue

        if current_series is None:
            raise MediaError(f"第 {record_index + 1} 条 IMAGE 记录缺少上级 SERIES")
        study_uid = current_study.study_uid  # type: ignore[union-attr]
        series_uid = current_series.series_uid

        sop_uid = (
            _str(record[TAG_REF_SOP_UID_IN_FILE].value)
            if TAG_REF_SOP_UID_IN_FILE in record else ""
        )
        if not sop_uid:
            raise MediaError(
                f"第 {record_index + 1} 条 IMAGE 记录缺少 Referenced SOP Instance UID in File"
            )

        if TAG_REFERENCED_FILE_ID not in record:
            raise MediaError(f"第 {record_index + 1} 条 IMAGE 记录缺少 Referenced File ID")
        components = [str(c) for c in record[TAG_REFERENCED_FILE_ID].value]
        media_path, path_problem = _safe_media_path(components)
        image = ImageRecord(
            record_index=record_index,
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=sop_uid,
            media_path=media_path or "",
            file_id_components=components,
            instance_number=_str(record[TAG_INSTANCE_NUMBER].value)
            if TAG_INSTANCE_NUMBER in record else "",
        )
        if path_problem:
            image.problems.append(path_problem)
        elif media_path in index.path_to_image:
            # 同一介质路径被多条 IMAGE 记录引用 -> 路径不唯一
            image.problems.append(
                f"介质路径 {media_path} 被多条 IMAGE 记录引用，路径不唯一"
            )
            index.path_to_image[media_path].problems.append(
                f"介质路径 {media_path} 被多条 IMAGE 记录引用，路径不唯一"
            )
        else:
            index.path_to_image[media_path] = image
        current_series.images.append(image)

    return index


def open_media_zip(zip_bytes: bytes, *, verify_integrity: bool = False) -> zipfile.ZipFile:
    """把 ZIP 字节作为只读归档打开，损坏时抛 MediaError。

    verify_integrity=True 时额外执行 CRC 全量校验（遍历全部成员，代价与介质
    大小成正比），仅在上传入口调用一次；确认流程复用已落盘的同一份字节，
    只需结构性打开，避免对大介质重复数倍 I/O。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise MediaError(f"上传内容不是有效的 ZIP 文件: {exc}") from exc
    if verify_integrity:
        bad = zf.testzip()
        if bad is not None:
            zf.close()
            raise MediaError(f"ZIP 完整性校验失败，首个损坏成员: {bad}")
    return zf


def locate_dicomdir(zf: zipfile.ZipFile) -> str:
    """在 ZIP 根目录定位唯一的 DICOMDIR（大小写不敏感）。"""
    roots = [
        info.filename for info in zf.infolist()
        if not info.is_dir() and "/" not in info.filename
        and info.filename.upper() == DICOMDIR_NAME
    ]
    if not roots:
        raise MediaError("介质根目录缺少 DICOMDIR")
    if len(roots) > 1:
        raise MediaError(f"介质根目录存在多个 DICOMDIR 文件: {roots}")
    return roots[0]


def build_index_from_zip(
    zip_bytes: bytes, *, verify_integrity: bool = False
) -> tuple[MediaIndex, str]:
    """打开 ZIP、定位并解析根目录 DICOMDIR，返回 (索引, DICOMDIR 成员名)。"""
    zf = open_media_zip(zip_bytes, verify_integrity=verify_integrity)
    try:
        dicomdir_member = locate_dicomdir(zf)
        try:
            data = zf.read(dicomdir_member)
        except Exception as exc:  # noqa: BLE001
            raise MediaError(f"DICOMDIR 无法读取: {exc}") from exc
        return parse_dicomdir(data), dicomdir_member
    finally:
        zf.close()


def verify_series(
    zip_bytes: bytes,
    index: MediaIndex,
    series_uid: str,
) -> list[ImageRecord]:
    """对所选序列执行发布前全量核对，返回带核对结论的 IMAGE 记录列表。

    只核对所选序列；其他序列的缺件/错误与本次发布无关。
    任何一条记录存在问题都抛 PublicationRejected，异常信息逐条列出问题记录。
    """
    series = index.find_series(series_uid)
    if series is None:
        raise PublicationRejected(f"所选序列 {series_uid} 不在 DICOMDIR 中")
    if not series.images:
        raise PublicationRejected(f"所选序列 {series_uid} 没有任何 IMAGE 记录，无法入库")

    zf = open_media_zip(zip_bytes)
    try:
        names = set(zf.namelist())
        for image in series.images:
            label = (
                f"记录 #{image.record_index + 1}"
                f"（SOP {image.sop_uid}，路径 {image.media_path or image.file_id_components}）"
            )
            # 1) 路径本身的问题（包外/不唯一）在解析阶段已登记
            if image.problems:
                continue
            # 2) 缺件：目录登记的路径在 ZIP 中不存在
            if image.media_path not in names:
                image.problems.append(f"{label}: 介质内缺少该文件（缺件）")
                continue
            # 3) 文件无法读取或不是合法 DICOM
            try:
                raw = zf.read(image.media_path)
                ds = pydicom.dcmread(io.BytesIO(raw), force=False)
            except Exception as exc:  # noqa: BLE001
                image.problems.append(f"{label}: 文件无法读取或不是 DICOM（{exc}）")
                continue
            # 4) 仅接受未压缩传输语法
            file_meta = getattr(ds, "file_meta", None)
            if file_meta is None or "TransferSyntaxUID" not in file_meta:
                image.problems.append(f"{label}: 缺少文件元信息或 Transfer Syntax UID")
                continue
            ts_uid = str(file_meta.TransferSyntaxUID)
            if ts_uid not in UNCOMPRESSED_TS:
                image.problems.append(
                    f"{label}: 使用压缩传输语法 {ts_uid}，本服务仅接受未压缩 DICOM"
                )
                continue
            # 5) 文件头三级 UID 与目录记录/上级记录核对
            header_problems: list[str] = []
            for tag, name, expected in (
                (TAG_STUDY_UID, "StudyInstanceUID", image.study_uid),
                (TAG_SERIES_UID, "SeriesInstanceUID", image.series_uid),
                (TAG_SOP_UID, "SOPInstanceUID", image.sop_uid),
            ):
                if tag not in ds or not str(ds[tag].value):
                    header_problems.append(f"文件头缺少 {name}")
                elif str(ds[tag].value) != expected:
                    header_problems.append(
                        f"文件头 {name}={ds[tag].value} 与目录记录 {expected} 不符"
                    )
            # 文件元信息中的 Media Storage SOP Instance UID 同步核对
            media_sop = getattr(file_meta, "MediaStorageSOPInstanceUID", None)
            if media_sop is not None and str(media_sop) != image.sop_uid:
                header_problems.append(
                    f"文件头 MediaStorageSOPInstanceUID={media_sop} "
                    f"与目录记录 {image.sop_uid} 不符"
                )
            if header_problems:
                image.problems.append(f"{label}: " + "；".join(header_problems))

        bad = [img for img in series.images if img.problems]
        if bad:
            lines = ["所选序列核对未通过，拒绝发布，问题记录如下："]
            for img in bad:
                for problem in img.problems:
                    lines.append(f"  - {problem}")
            raise PublicationRejected("\n".join(lines))
        return list(series.images)
    finally:
        zf.close()


def extract_series_files(
    zip_bytes: bytes,
    images: list[ImageRecord],
    dest_dir,
) -> list[tuple[str, object]]:
    """把核对通过的实例从同一份 ZIP 解到独立暂存目录。

    返回 [(介质内路径, 暂存文件路径), ...]，顺序与 IMAGE 记录一致。
    目录未登记的文件不会被解出。
    """
    from pathlib import Path

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=False)
    zf = open_media_zip(zip_bytes)
    saved: list[tuple[str, object]] = []
    try:
        used_names: set[str] = set()
        for order, image in enumerate(images):
            # 用序号前缀避免不同目录下同名文件互相覆盖
            local_name = f"{order:05d}.dcm"
            if local_name in used_names:  # pragma: no cover - 序号天然唯一
                raise MediaError("暂存文件名冲突")
            used_names.add(local_name)
            target = dest_dir / local_name
            target.write_bytes(zf.read(image.media_path))
            saved.append((image.media_path, target))
        return saved
    finally:
        zf.close()
