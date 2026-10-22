# 知识库来源清单（阶段 2）

本文件记录**实际入库**的 6 份文档。所有文件均已下载并核验 SHA-256（脚本：`scripts/fetch_knowledge_docs.py`，
清单文件：`knowledge/sources.json`）。文件本体不入 Git（`knowledge/raw/*.pdf` 已忽略），
可用脚本按 URL 与哈希复现。

**重要**：只有 `metropt3_direct` 的两份文档直接描述本项目的 MetroPT-3 空气生产单元（APU）。
其余 4 份 `generic_compressor` 是美国能源部（DOE）的通用压缩空气系统资料，只能作为原理与排查思路参考，
**不能**把其中的压力值、效率数字、型号参数或维护周期当作 MetroPT-3 的实际配置。

## 1. 文档总表

| doc_key | 文档 | 类型 | 适用性 | 页数 | 许可 | 可信度 |
| --- | --- | --- | --- | ---: | --- | --- |
| `metropt3-data-description` | MetroPT-3 Dataset — Data Description | 数据集说明 | `metropt3_direct` | 3 | CC BY 4.0 | 一手/官方 |
| `metropt-dataset-paper` | The MetroPT dataset for predictive maintenance (Scientific Data 9:764) | 研究论文 | `metropt3_direct` | 8 | CC BY 4.0 | 一手/官方 |
| `doe-compressed-air-sourcebook` | Improving Compressed Air System Performance: A Sourcebook for Industry (3rd ed., DOE/EE-1340) | 技术手册 | `generic_compressor` | 114 | 美国政府作品（公有领域） | 权威二手 |
| `doe-tip-sheet-3-minimize-leaks` | Minimize Compressed Air Leaks (Tip Sheet #3) | 维护指南 | `generic_compressor` | 2 | 美国政府作品（公有领域） | 权威二手 |
| `doe-tip-sheet-6-preventive-maintenance` | Preventive Maintenance Strategies for Compressed Air Systems (Tip Sheet #6) | 维护指南 | `generic_compressor` | 2 | 美国政府作品（公有领域） | 权威二手 |
| `doe-tip-sheet-8-stabilizing-pressure` | Stabilizing System Pressure (Tip Sheet #8) | 维护指南 | `generic_compressor` | 2 | 美国政府作品（公有领域） | 权威二手 |

合计 **131 页**。解析状态全部为 `parsed`（无扫描页、无密码保护）。

## 2. 来源、哈希与元数据

### 2.1 MetroPT-3 Data Description（直接关联）

- 标题：MetroPT-3 Dataset — Data Description
- 来源页：https://archive.ics.uci.edu/dataset/791/metropt+3+dataset
- 本地路径：`data/raw/metropt3/Data Description_Metro.pdf`（阶段 1A 已下载并登记，本阶段复用，未重复下载）
- SHA-256：`b00fac0e8899854078309bef4adaa480d82ecf14dc81c5097c3646973e824127`
- 字节数：81,208
- 版本：UCI dataset 791（2022，v1）
- 语言：英文
- 厂商/型号：Metro（数据）/ INESC TEC；MetroPT-3 APU
- 许可：CC BY 4.0（DOI 10.24432/C5VW3R）
- 适用性说明：传感器字段定义、采样说明与 4 次 Air leak 故障区间以本文件为准。

### 2.2 The MetroPT dataset for predictive maintenance（直接关联）

- 标题：The MetroPT dataset for predictive maintenance，Scientific Data 9:764 (2022)
- DOI：https://doi.org/10.1038/s41597-022-01877-3
- 下载地址：https://www.nature.com/articles/s41597-022-01877-3.pdf
- 本地路径：`knowledge/raw/metropt-dataset-paper.pdf`
- SHA-256：`58bddfc92979229c8a3e79de1608e6ab5a46c6d39b698097ee739e4b6ae07cb6`
- 字节数：1,732,495
- 语言：英文
- 厂商/型号：MetroPT-3 APU（INESC TEC / University of Porto）
- 许可：CC BY 4.0
- 适用性说明：描述 APU 结构、采集链路、FMEA/FMECA 选型依据和故障案例；不含厂商维修阈值。

### 2.3 DOE Compressed Air Sourcebook（通用）

- 标题：Improving Compressed Air System Performance: A Sourcebook for Industry（Third Edition）
- 来源页：https://www.energy.gov/eere/amo/articles/compressed-air-sourcebook-industry
- 下载地址：https://www.energy.gov/sites/default/files/2016/03/f30/Improving%20Compressed%20Air%20Sourcebook%20version%203.pdf
- 本地路径：`knowledge/raw/doe-compressed-air-sourcebook.pdf`
- SHA-256：`3280284235b8daef10f7d9e6a21aada90d7b804b805cc9ef842903aeca009c22`
- 字节数：2,740,595
- 版本：DOE/EE-1340（2016）
- 语言：英文
- 厂商：U.S. DOE Advanced Manufacturing Office / Compressed Air Challenge
- 型号：通用（无具体型号）
- 许可：美国政府作品，通常不受版权保护（17 U.S.C. §105）
- 适用性说明：系统构成、压缩机类型、控制策略、储气、干燥、泄漏与维护的通用参考。

### 2.4–2.6 DOE 压缩空气 Tip Sheets（通用）

| doc_key | SHA-256 | 字节数 | 下载地址 |
| --- | --- | ---: | --- |
| `doe-tip-sheet-3-minimize-leaks` | `2d5c173c054d0fb1187a0944a5815f39afa44bde6d392ad55c9667ff2aa20017` | 270,691 | https://www.energy.gov/sites/prod/files/2014/05/f16/compressed_air3.pdf |
| `doe-tip-sheet-6-preventive-maintenance` | `f331b8fdf80471834bd9c7cc235d995f2315f995bd1306051b193441cb794c47` | 251,120 | https://www.energy.gov/sites/prod/files/2014/05/f16/compressed_air6.pdf |
| `doe-tip-sheet-8-stabilizing-pressure` | `7c2e025951ac54b45aa0883f242890dde50b49abed98605a085840195bcf029a` | 249,217 | https://www.energy.gov/sites/prod/files/2014/05/f16/compressed_air8.pdf |

三份 Tip Sheet 均为 2004 年 8 月版、2 页、英文、美国政府作品。适用性均为 `generic_compressor`。

## 3. 为什么没有收录更多文档

- 第一版目标是**可追溯、可评估**，不是文档数量。6 份文档覆盖了本阶段评估集的全部问题类型：
  传感器字段、数据集背景、压缩机原理、维护与压力稳定、标识符、型号不匹配与未知问题。
- 没有收录任何厂商型号手册（如 Atlas Copco、Kaeser）：未找到可确认许可且可稳定下载的官方文件。
  因此涉及这些型号的问题**必须拒答**，而不是拿通用资料冒充。
- 没有收录扫描版 PDF：本阶段不实现 OCR，收录无法可靠提取文本的文档会制造“解析成功”的假象。

## 4. 复用与再分发

- `knowledge/sources.json` 是唯一来源清单（脚本读取它下载/核验）；
- 重新获取：`python scripts/fetch_knowledge_docs.py`（已存在且哈希一致则跳过下载）；
- 只核验：`python scripts/fetch_knowledge_docs.py --verify-only`；
- 哈希不匹配会以退出码 2 失败，不会静默接受被替换的文件。
