# DICOM 影像批处理服务（匿名化 / 重标识）

影像数据管理员将导出的 DICOM 整理为供研究数据湖关联使用的副本。本服务提供两条入库路径：

1. **影像介质 ZIP 入库**：上传含根目录 `DICOMDIR` 的介质 ZIP，按目录列出
   Study / Series / 各序列实例数，数据员选择一个序列确认入库；
2. **直接文件批次上传**：网页批量上传未压缩 DICOM，统一换号处理。

两条路径都复用同一套确定性换号与整批 zip 下载能力。

## 介质入库规则（DICOMDIR 工作流）

### 预览

- 介质 ZIP 根目录必须**恰好有一个** `DICOMDIR`（大小写不敏感），否则拒绝上传；
- 只解析目录中的 **IMAGE 记录**，按 PATIENT → STUDY → SERIES → IMAGE 层级列出
  Study、Series 与每个序列的 IMAGE 实例数；SR、RT STRUCT、KEY OBJECT 等
  非 IMAGE 叶子记录计数后忽略，不参与入库；
- 目录解析不依赖记录字节偏移，第三方工具改写后偏移失效的 DICOMDIR 仍可读取。

### 确认与核对

确认时从**预览所用的同一份 ZIP**（按 SHA-256 内容寻址落盘）取文件，逐条核对：

1. 目录记录的 `ReferencedFileID` 路径必须留在介质根目录内（拒绝 `..` 穿越、
   绝对路径、盘符/UNC、含分隔符的非法组件）；
2. 同一介质路径只能被一条 IMAGE 记录引用（路径不唯一拒绝）；
3. 路径指向的文件必须真实存在于 ZIP 中（缺件拒绝）；
4. 文件必须可读取并能解析为 DICOM，且使用**未压缩传输语法**
   （Implicit/Explicit VR LE、Explicit VR BE；压缩语法拒绝）；
5. 文件头 `StudyInstanceUID` / `SeriesInstanceUID` / `SOPInstanceUID`
   （含文件元信息 `MediaStorageSOPInstanceUID`）必须与目录记录/上级记录一致。

任何一条不通过都会**指出问题记录（记录序号、SOP UID、路径与原因）并拒绝发布**，
失败批次不提供下载。核对范围仅限所选序列：**其他序列缺件不影响所选序列**；
DICOMDIR 未登记的文件（包括游离 DICOM）一律不入库。

### 幂等与重试

- `series_publications` 以 `(ZIP SHA-256, Series UID)` 为主键，配合 IMMEDIATE
  事务串行化并发确认：**同一份 ZIP 的同一序列只发布一次，即使同时点击确认，
  也只有一个可下载批次**，重复确认直接返回已登记批次；
- 发布失败（含核对拒绝、处理异常、服务重启中断）后登记行保留为 `failed`，
  可对同一介质重新确认重试，重试产生新批次号，成功后登记行转为 `ready`；
  历史失败批次始终不可下载；
- 注意：修复后的 ZIP 字节不同 → SHA-256 不同，会作为一份新介质登记，
  与损坏介质的失败记录相互独立。

### 清单

发布批次的 `manifest.csv` 在原有换号映射基础上增加两列：

| 列 | 含义 |
| --- | --- |
| `source_zip_sha256` | 来源介质 ZIP 的 SHA-256（直接文件上传批次为空） |
| `media_path` | 该实例在介质 ZIP 内的路径（即 DICOMDIR ReferencedFileID） |

## 直接文件上传的处理规则

对每个上传的 DICOM 文件：

| 项目 | 处理方式 |
| --- | --- |
| `PatientName`（含嵌套 PN） | **清空**（保留元素，值置空） |
| `PatientID`（含嵌套） | **替换**为确定性派生的 12 位数字新 ID（`P` + 12 位） |
| `StudyInstanceUID` / `SeriesInstanceUID` / `SOPInstanceUID` | **统一换号** |
| 上述三类 UID 的**全部嵌套引用**（序列内、文件元信息内） | 同步换号 |
| 文件元信息 `MediaStorageSOPInstanceUID` | 与新 `SOPInstanceUID` 同步 |
| SOP Class UID、Transfer Syntax UID、Implementation Class UID | **保持原样** |
| `FrameOfReferenceUID` 等其他非身份 UID | **保持原样** |
| 像素数据（PixelData） | **逐字节保持原样** |
| 传输语法 | 仅接受未压缩（Implicit/Explicit VR LE、Explicit VR BE），压缩语法整批拒绝 |

### UID 换号如何保证一致

- 新 UID 由 `(角色, 原 UID)` 通过 SHA-256 **确定性派生**，结构为
  `<机构前缀>.0.<31 位十进制>`，满足 DICOM UID 规范（仅数字与点、每段无前导零、总长 ≤ 64）。
- 因为是纯函数派生，**同一原标识在任何批次、任何时间、任何并发 worker/进程下都得到同一新标识**；
  “引用先上传、目标实例后上传”也天然一致，不依赖写入顺序。
- 派生结果同时持久化到 SQLite（`uid_mappings` / `patient_mappings`），作为审计与数据湖关联依据。
- 身份 UID 的角色优先级高于标签提示：例如 `ReferencedStudySequence` 中承载 Study UID 的
  `ReferencedSOPInstanceUID` 会按 study 角色而非 sop 角色换号（见合成样例 `sr_001.dcm`）。

## 数据完整性与可靠性保证

1. **同字节复用**：同一 `SOPInstanceUID` 且原文件 SHA-256 相同，直接复用既有处理结果。
2. **字节冲突整批拒绝**：同一 `SOPInstanceUID` 但字节不同，整个批次标记 `failed`，
   不产生任何可下载产物。并发首次提交的竞态由 IMMEDIATE 事务内的哈希比对兜底。
3. **无半批可下载**：批次只有在全部文件处理、入库、zip 打包并原子 rename 成功后才置为 `ready`；
   zip 先写临时文件再 `os.replace`，失败/中断的批次下载返回 409。
4. **重启恢复**：进程启动时将遗留的 `processing` 批次标记 `failed`；已成功入库的实例与映射
   **保留不丢**，重新上传即可复用且标识不变。
5. **重试不变号**：确定性派生 + 映射表只插入不更新（`INSERT OR IGNORE`），重试绝不改变标识。
6. **先落盘后入库**：对象文件 `fsync` + 原子 rename 后才写数据库记录。

## 目录结构

```
app/
  config.py        环境变量配置（数据目录、UID 前缀、上传大小、SQLite 超时）
  db.py            SQLite 连接、WAL、建表、增量迁移、中断恢复
  identifiers.py   确定性 UID / PatientID 派生
  dicom_proc.py    DICOM 校验、标识收集、映射、匿名化重标识、对象落盘
  media.py         介质 ZIP 校验、DICOMDIR 解析（仅 IMAGE）、发布前路径/UID 核对
  services.py      批次编排、介质发布幂等控制、zip 原子打包、CSV 清单、状态机
  web.py           Flask 路由与应用工厂
  wsgi.py          gunicorn 入口
  templates/       上传页、介质预览页、批次详情页
samples/
  generate_samples.py     合成 DICOM 样例生成器（含跨文件引用）
  generate_media_zip.py   合成影像介质 ZIP 生成器（含 DICOMDIR、游离文件）
  data/                   生成的 4 个样例 DICOM
tests/             pytest 测试（介质核对/幂等/并发、多进程并发与中断恢复）
Dockerfile
docker-compose.yml
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

健康检查：`GET /healthz`。

数据持久化在命名卷 `dicom_data`（容器内 `/data`），包含：

- `dicom.db`（SQLite，WAL 模式）
- `objects/`（内容寻址的处理后 DICOM）
- `media/`（按 SHA-256 分片存放的上传介质 ZIP）
- `tmp/`（上传暂存，按批次隔离，结束即清理）
- `batches/`（最终 zip，原子写入）

## 本地开发（无 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 生成含跨文件引用的合成样例
python samples/generate_samples.py

# 生成含 DICOMDIR 的合成影像介质 ZIP（含游离文件，用于手工验证介质流程）
python samples/generate_media_zip.py

# 运行测试（含介质核对/幂等/并发与多进程并发）
pytest -q

# 本地启动
DICOM_DATA_DIR=./data python -m flask --app app.wsgi:app run --debug
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DICOM_DATA_DIR` | `/data` | 数据根目录 |
| `DICOM_UID_PREFIX` | `1.2.826.0.1.3680043.10.9999`（IANA 测试弧） | 派生 UID 的组织前缀，**生产必须替换**为机构合法前缀，≤30 字符 |
| `MAX_CONTENT_LENGTH` | `2147483648`（2GB） | 单请求上传上限（字节） |
| `SQLITE_BUSY_TIMEOUT_MS` | `30000` | SQLite 写锁等待 |
| `FLASK_SECRET_KEY` | 开发占位 | 会话密钥，生产必须替换 |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 介质/文件上传表单、最近介质与批次列表 |
| POST | `/media/upload` | 上传影像介质 ZIP，校验通过后重定向到介质预览页 |
| GET | `/media/<zip_sha256>` | 介质预览：Study/Series/IMAGE 实例数与发布状态 |
| POST | `/media/<zip_sha256>/confirm` | 确认所选序列入库（幂等；失败可重试） |
| POST | `/upload` | multipart 多文件上传并同步处理，重定向到批次详情 |
| GET | `/batches/<id>` | 批次状态、错误信息、文件清单 |
| GET | `/batches/<id>/download` | 下载整批 zip（仅 `ready` 可用，否则 409） |
| GET | `/healthz` | 健康检查 |

下载的 zip 内含处理后的 DICOM（以新 SOP UID 命名）与 `manifest.csv`，
清单列出每个文件的原/新 PatientID、三级 UID 映射、原文件 SHA-256、
来源介质 ZIP 的 SHA-256 与实例在介质内的路径，供数据湖关联。

## 合成样例说明

`samples/generate_samples.py` 生成 4 个未压缩 Explicit VR Little Endian 文件：

- `ct_001.dcm` / `ct_002.dcm`：两张 64×64 带像素的 CT，共享 Study/Series；
- `rs_001.dcm`：RT Structure Set，经 `ReferencedFrameOfReferenceSequence`
  → `RTReferencedStudySequence` → `ContourImageSequence` 跨文件引用两张 CT 的 SOP UID；
- `sr_001.dcm`：Enhanced SR，含 `ReferencedStudySequence`、`ReferencedSeriesSequence`、
  `CurrentRequestedProcedureEvidenceSequence` 三级嵌套引用。

`samples/generate_media_zip.py` 在上述实例基础上生成标准介质 ZIP
（默认 `samples/media/sample_medium.zip`）：

- 根目录 `DICOMDIR`，含 PATIENT/STUDY/SERIES 与 IMAGE（两张 CT）、
  RT STRUCTURE SET、SR DOCUMENT 叶子记录，可直接验证“仅 IMAGE 入库”；
- 额外夹带 `ORPHAN/unregistered.dcm` 与 `notes.txt` 两个 DICOMDIR 未登记文件，
  用于验证“目录未登记的文件不入库”。
