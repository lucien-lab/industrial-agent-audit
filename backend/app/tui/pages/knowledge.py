"""Knowledge 页面：复用阶段 2 的 RAG 检索接口。

关键语义（不得曲解）：

- `hybrid` 的 `score` 是 RRF 融合分数，与 `vector_similarity`（余弦相似度）
  不是同一量纲，**不能**被解释为“相关概率”；
- `equipment_scope=generic_compressor` 只是同类设备的通用资料，
  不能当作 MetroPT-3 的具体参数；
- 每条结果都能回溯到 doc_key + 页码 + chunk_id。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Markdown, Select, Static

from app.tui.client import ApiError
from app.tui.formatting import fmt_ms, fmt_ratio, scope_label, truncate
from app.tui.models import SearchHit, SearchResults
from app.tui.pages.base import BasePage

METHOD_OPTIONS = [("混合 hybrid", "hybrid"), ("向量 vector", "vector"), ("关键词 keyword", "keyword")]


class KnowledgePage(BasePage):
    PAGE_ID = "knowledge"
    PAGE_TITLE = "Knowledge"
    PAGE_SUMMARY = "RAG 知识库检索（vector / keyword / hybrid）"

    BINDINGS = [Binding("ctrl+s", "search", "检索", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self._results: list[SearchHit] = []
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Knowledge · 工业知识库检索", classes="page-header")
            yield Static(
                "混合检索 score 为 RRF 分数，不是概率；通用压缩机资料 ≠ 本设备参数。"
                "选中结果可查看原文片段、来源与页码。",
                classes="page-hint",
            )
            with Horizontal(classes="controls"):
                yield Input(
                    placeholder="例：TP2 和 TP3 分别代表什么？",
                    id="knowledge-query",
                )
                yield Select(METHOD_OPTIONS, value="hybrid", id="knowledge-method", allow_blank=False)
                yield Input(value="5", placeholder="top_k", id="knowledge-topk")
                yield Button("检索", id="knowledge-search", variant="primary")
            yield Static("", id="page-knowledge-message", classes="message")
            with Horizontal():
                yield DataTable(id="knowledge-table", cursor_type="row", classes="split-left")
                with VerticalScroll(classes="split-right"):
                    yield Markdown("_尚未检索。_", id="knowledge-detail")

    def on_mount(self) -> None:
        table = self.query_one("#knowledge-table", DataTable)
        table.add_columns("#", "分数", "文档", "页", "适用性", "Chunk")

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #
    async def action_search(self) -> None:
        await self._search()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "knowledge-search":
            await self._search()

    async def _search(self) -> None:
        if self._busy:
            self.notify_status("上一次检索尚未结束。", "warn")
            return
        query = self.query_one("#knowledge-query", Input).value.strip()
        if not query:
            self.notify_status("请输入检索词。", "warn")
            return
        method = self.query_one("#knowledge-method", Select).value
        method = method if isinstance(method, str) else "hybrid"
        top_k = self._parse_topk(self.query_one("#knowledge-topk", Input).value)
        self._busy = True
        self.notify_status(f"检索中（{method}, top_k={top_k}）…", "dim")
        try:
            result = await self.client.knowledge_search(query, method=method, top_k=top_k)
        except ApiError as exc:
            self._busy = False
            self.notify_status(f"检索失败：{exc}", "error")
            return
        except Exception as exc:  # pragma: no cover
            self._busy = False
            self.notify_status(f"Unexpected error: {type(exc).__name__}", "error")
            return
        self._busy = False
        self._render_results(result)

    @staticmethod
    def _parse_topk(raw: str) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 5
        return max(1, min(20, value))

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _render_results(self, result: SearchResults) -> None:
        self._results = list(result.results)
        table = self.query_one("#knowledge-table", DataTable)
        table.clear()
        for rank, hit in enumerate(result.results, start=1):
            table.add_row(
                str(rank),
                self._score_text(hit),
                truncate(hit.document_title or hit.doc_key, 26),
                f"{hit.page_start}-{hit.page_end}",
                (hit.equipment_scope or "?").replace("_", " "),
                str(hit.chunk_id),
                key=str(hit.chunk_id),
            )
        elapsed = fmt_ms((result.retrieval or {}).get("elapsed_ms"))
        if not result.results:
            self.notify_status(f"没有检索到结果（{result.method}，耗时 {elapsed}）", "warn")
            self.query_one("#knowledge-detail", Markdown).update(
                "### 没有检索结果\n\n"
                f"- 查询：{result.query}\n- 方法：{result.method}\n"
                "- 可能原因：知识库为空、过滤条件过严，或查询词与文档用语差异较大。\n"
            )
        else:
            self.notify_status(
                f"检索完成：{len(result.results)} 条 · 方法 {result.method} · 耗时 {elapsed}", "ok"
            )
            table.focus()
        if result.warnings:
            self.set_message("；".join(result.warnings), "warn")

    @staticmethod
    def _score_text(hit: SearchHit) -> str:
        if hit.rrf_score is not None:
            return f"rrf {hit.rrf_score:.5f}"
        if hit.vector_similarity is not None:
            return f"cos {fmt_ratio(hit.vector_similarity, 4)}"
        if hit.keyword_score is not None:
            return f"kw {fmt_ratio(hit.keyword_score, 3)}"
        return fmt_ratio(hit.score, 5)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "knowledge-table":
            return
        chunk_id = event.row_key.value if event.row_key else None
        hit = next((item for item in self._results if str(item.chunk_id) == str(chunk_id)), None)
        if hit is None:
            return
        self.query_one("#knowledge-detail", Markdown).update(self._detail_markdown(hit))

    def _detail_markdown(self, hit: SearchHit) -> str:
        lines = [
            f"### {hit.document_title or hit.doc_key}",
            "",
            f"- 引用标签：`{hit.citation_label or '—'}`",
            f"- doc_key：`{hit.doc_key}` · 类型 {hit.doc_type or '—'} · 可信级别 {hit.trust_level or '—'}",
            f"- 页码 {hit.page_start}–{hit.page_end} · chunk_id {hit.chunk_id}"
            f" · chunk_index {hit.chunk_index}",
            f"- 适用性：**{scope_label(hit.equipment_scope)}**",
            f"- 分数：{self._score_text(hit)}"
            + (
                f" · 余弦相似度 {fmt_ratio(hit.vector_similarity, 4)}"
                if hit.vector_similarity is not None
                else ""
            )
            + (
                f" · 关键词分 {fmt_ratio(hit.keyword_score, 3)}"
                if hit.keyword_score is not None
                else ""
            ),
        ]
        if hit.section_title:
            lines.append(f"- 小节：{hit.section_title}")
        if hit.source_url:
            lines.append(f"- 来源：{hit.source_url}")
        if hit.equipment_scope == "generic_compressor":
            lines += [
                "",
                "> ⚠ 这是同类设备的**通用资料**，不是 MetroPT-3 的实测参数或厂商规范。",
            ]
        lines += ["", "**原文片段（未改写）**", "```text", hit.content.strip(), "```"]
        return "\n".join(lines)

    async def reset_view(self) -> None:  # type: ignore[override]
        self._results = []
        self.query_one("#knowledge-table", DataTable).clear()
        self.query_one("#knowledge-detail", Markdown).update("_已清空检索视图。_")
        self.notify_status("已清空知识检索视图（索引未改变）", "dim")

    async def load(self, *, force: bool = False) -> None:
        if force:
            await self.action_search()
            return
        try:
            health = await self.client.knowledge_health()
        except ApiError as exc:
            self.notify_status(f"知识库不可用：{exc}", "error")
            return
        self.set_message(
            f"知识库 {health.status} · 文档 {health.documents} · Chunk {health.chunks}",
            "ok" if health.status == "ok" else "warn",
        )


__all__ = ["KnowledgePage", "METHOD_OPTIONS"]
