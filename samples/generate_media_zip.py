"""合成影像介质 ZIP 生成器（含根目录 DICOMDIR）。

在 samples/generate_samples.py 的 4 个 DICOM 实例基础上，用 pydicom FileSet
生成标准 PATIENT/STUDY/SERIES/(IMAGE|RT STRUCTURE SET|SR DOCUMENT) 目录记录，
再连同游离文件、目录外文件一起打成介质 ZIP，供介质入库功能与测试使用。

运行：python samples/generate_media_zip.py [输出 zip 路径]
脚本与输出目录分离；输出目录会被清空重建。
"""
from __future__ import annotations

import io
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydicom import dcmread  # noqa: E402
from pydicom.dataset import Dataset  # noqa: E402
from pydicom.fileset import FileSet, RecordNode  # noqa: E402

from samples import generate_samples as gen  # noqa: E402


def _record_chain(ds, leaf_record_type: str) -> RecordNode:
    """构造 PATIENT -> STUDY -> SERIES -> 叶子 的 RecordNode 链。"""
    patient = Dataset()
    patient.DirectoryRecordType = "PATIENT"
    patient.PatientID = ds.PatientID
    patient.PatientName = ds.PatientName

    study = Dataset()
    study.DirectoryRecordType = "STUDY"
    study.StudyInstanceUID = ds.StudyInstanceUID
    study.StudyDate = getattr(ds, "StudyDate", "20261118")
    study.StudyDescription = getattr(ds, "StudyDescription", "")

    series = Dataset()
    series.DirectoryRecordType = "SERIES"
    series.SeriesInstanceUID = ds.SeriesInstanceUID
    series.Modality = getattr(ds, "Modality", "")
    series.SeriesNumber = getattr(ds, "SeriesNumber", 1)

    leaf = Dataset()
    leaf.DirectoryRecordType = leaf_record_type
    leaf.ReferencedSOPClassUIDInFile = ds.SOPClassUID
    leaf.ReferencedSOPInstanceUIDInFile = ds.SOPInstanceUID
    leaf.ReferencedTransferSyntaxUIDInFile = ds.file_meta.TransferSyntaxUID
    leaf.InstanceNumber = getattr(ds, "InstanceNumber", 1)

    leaf_node = RecordNode(leaf)
    series_node = RecordNode(series)
    study_node = RecordNode(study)
    patient_node = RecordNode(patient)
    leaf_node.parent = series_node
    series_node.parent = study_node
    study_node.parent = patient_node
    return leaf_node


def _leaf_type(ds) -> str:
    name = ds.SOPClassUID.name
    if name.startswith("CT Image"):
        return "IMAGE"
    if "RT Structure" in name:
        return "RT STRUCTURE SET"
    return "SR DOCUMENT"


def build_fileset(sample_dir: Path, work_dir: Path) -> FileSet:
    """把样例 DICOM 加进 FileSet 并写出 DICOMDIR 文件集到 work_dir。"""
    fs = FileSet()
    for path in sorted(sample_dir.glob("*.dcm")):
        ds = dcmread(str(path))
        fs.add_custom(ds, _record_chain(ds, _leaf_type(ds)))
    fs.write(work_dir)
    return fs


def make_media_zip(sample_dir: Path, out_zip: Path, *, extra: bool = True) -> Path:
    """打包标准介质 ZIP。

    extra=True 时额外放入：
      - ORPHAN/ 目录下一个 DICOMDIR 未登记的游离 DICOM（不得入库）；
      - notes.txt 非 DICOM 文件（不得入库）。
    """
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        fs_dir = Path(tmp) / "fs"
        build_fileset(sample_dir, fs_dir)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(fs_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(fs_dir).as_posix())
            if extra:
                orphan = dcmread(str(sample_dir / "ct_001.dcm"))
                zf.writestr(
                    "ORPHAN/unregistered.dcm",
                    _dicom_bytes(orphan),
                )
                zf.writestr("notes.txt", "unregistered loose file, must be ignored\n")
        out_zip.write_bytes(buf.getvalue())
    return out_zip


def _dicom_bytes(ds) -> bytes:
    buf = io.BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


def main(out_zip: Path | None = None) -> Path:
    sample_dir = ROOT / "samples" / "data"
    if not sample_dir.exists():
        gen.main(sample_dir)
    if out_zip is None:
        out_zip = ROOT / "samples" / "media" / "sample_medium.zip"
    if out_zip.exists():
        out_zip.unlink()
    make_media_zip(sample_dir, out_zip)
    print(f"已生成合成影像介质 ZIP: {out_zip}")
    return out_zip


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    main(target)
