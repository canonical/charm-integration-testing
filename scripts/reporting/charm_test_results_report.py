#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Generate interactive charm test reports from Test Observer PostgreSQL data."""

from __future__ import annotations

import argparse
import csv
import html
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal, Mapping, Sequence

if TYPE_CHECKING:
    import plotly.graph_objects as go

ExecutionScope = Literal["total", "latest"]

INTEGRATION_PROJECT = "canonical/charm-integration-testing"
INFRA_ISSUE_KEYS = frozenset({"52", "165", "461", "764"})
CATEGORY_ORDER = ("Charm Issue", "UNTRIAGED", "Pipeline Issue", "Infra Issue", "Obsolete")
PLOTLY_CDN_URL = "https://cdn.plot.ly/plotly-3.7.0.min.js"


@dataclass(frozen=True)
class DateRange:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Issue:
    id: int
    source: str
    project: str
    key: str
    title: str

    @property
    def label(self) -> str:
        return f"{self.source}: {self.project} #{self.key}"


@dataclass(frozen=True)
class FailedResult:
    id: int
    archived: bool
    issues: tuple[Issue, ...]


@dataclass(frozen=True)
class BarStat:
    test_case: str
    passed: int
    failed: int

    @property
    def total(self) -> int:
        return self.passed + self.failed

    @property
    def success_percentage(self) -> float:
        return 100 * self.passed / self.total if self.total else 0.0


@dataclass(frozen=True)
class SankeyLink:
    source: str
    target: str
    link_type: str
    weight: float
    result_count: int
    attachment_count: int = 0
    issue: Issue | None = None


ELIGIBLE_RESULTS_CTE = """
WITH filtered_results AS MATERIALIZED (
    SELECT
        tr.id AS test_result_id,
        tr.status,
        tr.test_case_id,
        tr.test_execution_id
    FROM public.test_result AS tr
    WHERE tr.status IN ('PASSED', 'FAILED')
      AND tr.created_at >= %(start)s
      AND tr.created_at < %(end)s
),
candidate_executions AS MATERIALIZED (
    SELECT
        te.id AS test_execution_id,
        te.test_plan_id,
        te.environment_id,
        a.name AS artefact_name,
        a.track,
        a.branch,
        a.archived,
        ab.architecture,
        ab.revision
    FROM (
        SELECT DISTINCT test_execution_id
        FROM filtered_results
    ) AS result_executions
    JOIN public.test_execution AS te ON te.id = result_executions.test_execution_id
    JOIN public.artefact_build AS ab ON ab.id = te.artefact_build_id
    JOIN public.artefact AS a ON a.id = ab.artefact_id
    WHERE a.family = 'charm'::public.familyname
            AND (%(include_archived)s OR NOT a.archived)
),
ranked_executions AS (
    SELECT
        test_execution_id,
        ROW_NUMBER() OVER (
            PARTITION BY artefact_name, test_plan_id, track, branch, environment_id, architecture
            ORDER BY revision DESC, test_execution_id DESC
        ) AS selection_rank
    FROM candidate_executions
    WHERE revision IS NOT NULL
),
selected_executions AS (
    SELECT ce.*
    FROM candidate_executions AS ce
    WHERE %(scope)s = 'total'

    UNION ALL

    SELECT ce.*
    FROM candidate_executions AS ce
    JOIN ranked_executions AS re ON re.test_execution_id = ce.test_execution_id
    WHERE %(scope)s = 'latest'
      AND re.selection_rank = 1
),
selected_results AS (
    SELECT
        fr.test_result_id,
        fr.status,
        fr.test_case_id,
        fr.test_execution_id,
        se.test_plan_id,
        se.environment_id,
        se.artefact_name,
        se.track,
        se.branch,
        se.archived,
        se.architecture,
        se.revision
    FROM filtered_results AS fr
    JOIN selected_executions AS se ON se.test_execution_id = fr.test_execution_id
)
"""

REPORT_QUERY = ELIGIBLE_RESULTS_CTE + """
SELECT
    sr.test_result_id,
    sr.status::text AS status,
    sr.archived,
    tc.name AS test_case,
    i.id AS issue_id,
    i.source::text AS issue_source,
    i.project AS issue_project,
    i.key AS issue_key,
    i.title AS issue_title
FROM selected_results AS sr
JOIN public.test_case AS tc ON tc.id = sr.test_case_id
LEFT JOIN public.issue_test_result_attachment AS attachment
    ON attachment.test_result_id = sr.test_result_id
   AND sr.status = 'FAILED'
LEFT JOIN public.issue AS i ON i.id = attachment.issue_id
ORDER BY sr.test_result_id, i.id
"""


def parse_date(value: str) -> datetime:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid date '{value}'; expected YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError(f"invalid date '{value}'; expected YYYY-MM-DD")
    return datetime.combine(parsed, time.min, tzinfo=UTC)


def resolve_date_range(start: datetime | None, end: datetime | None, now: datetime | None = None) -> DateRange:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    effective_end = end or datetime.combine(current.date(), time.min, tzinfo=UTC)
    effective_start = start or effective_end - timedelta(days=30)
    if effective_start >= effective_end:
        raise ValueError("start must be earlier than end")
    return DateRange(effective_start, effective_end)


def db_timestamp(value: datetime) -> datetime:
    """Convert UTC-aware CLI values to the schema's timezone-naive UTC timestamps."""
    return value.astimezone(UTC).replace(tzinfo=None)


def query_parameters(
    date_range: DateRange,
    scope: ExecutionScope,
    top_test_cases: int,
    include_archived: bool = False,
) -> dict[str, Any]:
    return {
        "start": db_timestamp(date_range.start),
        "end": db_timestamp(date_range.end),
        "scope": scope,
        "top_test_cases": top_test_cases,
        "include_archived": include_archived,
    }


def fetch_report_data(
    database_url: str,
    date_range: DateRange,
    scope: ExecutionScope,
    top_test_cases: int,
    include_archived: bool = False,
) -> tuple[list[BarStat], list[FailedResult], tuple[int, int]]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        raise RuntimeError("psycopg is required; install it (e.g. pip install 'psycopg[binary]')") from error

    parameters = query_parameters(date_range, scope, top_test_cases, include_archived)
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL jit = off")
        with connection.cursor() as cursor:
            cursor.execute(REPORT_QUERY, parameters)
            return aggregate_report_rows(cursor.fetchall(), top_test_cases)


def aggregate_report_rows(
    rows: Iterable[Mapping[str, Any]], top_test_cases: int
) -> tuple[list[BarStat], list[FailedResult], tuple[int, int]]:
    rows = list(rows)
    seen_results: set[int] = set()
    test_case_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    passed_count = 0
    failed_count = 0
    failure_rows: list[Mapping[str, Any]] = []

    for row in rows:
        result_id = int(row["test_result_id"])
        status = str(row["status"])
        if status == "FAILED":
            failure_rows.append(row)
        if result_id in seen_results:
            continue
        seen_results.add(result_id)
        counts = test_case_counts[str(row["test_case"])]
        if status == "PASSED":
            counts[0] += 1
            passed_count += 1
        elif status == "FAILED":
            counts[1] += 1
            failed_count += 1

    bar_stats = sorted(
        (BarStat(test_case, counts[0], counts[1]) for test_case, counts in test_case_counts.items()),
        key=lambda stat: (-stat.total, stat.test_case),
    )[:top_test_cases]
    return bar_stats, group_failed_results(failure_rows), (passed_count, failed_count)


def group_failed_results(rows: Iterable[Mapping[str, Any]]) -> list[FailedResult]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        result_id = int(row["test_result_id"])
        result = grouped.setdefault(result_id, {"archived": bool(row["archived"]), "issues": {}})
        if row["issue_id"] is None:
            continue
        issue = Issue(
            id=int(row["issue_id"]),
            source=str(row["issue_source"]),
            project=str(row["issue_project"]),
            key=str(row["issue_key"]),
            title=str(row["issue_title"]),
        )
        result["issues"][issue.id] = issue
    return [
        FailedResult(result_id, values["archived"], tuple(values["issues"].values()))
        for result_id, values in sorted(grouped.items())
    ]


def classify_failure(result: FailedResult) -> str:
    if result.archived:
        return "Obsolete"
    integration_issues = [issue for issue in result.issues if issue.project == INTEGRATION_PROJECT]
    if any(issue.key in INFRA_ISSUE_KEYS for issue in integration_issues):
        return "Infra Issue"
    if integration_issues:
        return "Pipeline Issue"
    if result.issues:
        return "Charm Issue"
    return "UNTRIAGED"


def build_sankey_links(
    passed_count: int,
    failed_results: Sequence[FailedResult],
) -> list[SankeyLink]:
    links = [
        SankeyLink("Charm Test Results", "PASSED", "status", float(passed_count), passed_count),
        SankeyLink("Charm Test Results", "FAILED", "status", float(len(failed_results)), len(failed_results)),
    ]
    categorized: dict[str, list[FailedResult]] = defaultdict(list)
    for result in failed_results:
        categorized[classify_failure(result)].append(result)

    for category in CATEGORY_ORDER:
        results = categorized[category]
        links.append(SankeyLink("FAILED", category, "category", float(len(results)), len(results)))

    issue_weights: dict[int, float] = defaultdict(float)
    issue_result_ids: dict[int, set[int]] = defaultdict(set)
    issues: dict[int, Issue] = {}
    for result in categorized["Charm Issue"]:
        weight = 1.0 / len(result.issues)
        for issue in result.issues:
            issues[issue.id] = issue
            issue_weights[issue.id] += weight
            issue_result_ids[issue.id].add(result.id)

    for issue_id, issue in sorted(issues.items(), key=lambda item: item[1].label):
        result_ids = issue_result_ids[issue_id]
        links.append(
            SankeyLink(
                "Charm Issue",
                issue.label,
                "issue",
                issue_weights[issue_id],
                len(result_ids),
                len(result_ids),
                issue,
            )
        )
    return links


def build_bar_figure(stats: Sequence[BarStat]) -> go.Figure:
    try:
        import plotly.graph_objects as go
    except ImportError as error:
        raise RuntimeError("plotly is required; install requirements.txt") from error

    figure = go.Figure()
    labels = [stat.test_case for stat in stats]
    custom_data = [[stat.success_percentage, stat.total] for stat in stats]
    figure.add_bar(
        name="PASSED",
        y=labels,
        x=[stat.passed for stat in stats],
        orientation="h",
        marker_color="#00a66c",
        customdata=custom_data,
        hovertemplate="%{y}<br>PASSED: %{x}<br>Total: %{customdata[1]}<br>Success: %{customdata[0]:.1f}%<extra></extra>",
    )
    figure.add_bar(
        name="FAILED",
        y=labels,
        x=[stat.failed for stat in stats],
        orientation="h",
        marker_color="#d9383a",
        customdata=custom_data,
        hovertemplate="%{y}<br>FAILED: %{x}<br>Total: %{customdata[1]}<br>Success: %{customdata[0]:.1f}%<extra></extra>",
    )
    figure.update_layout(
        barmode="stack",
        height=max(380, 38 * len(stats) + 130),
        margin={"l": 250, "r": 40, "t": 40, "b": 70},
        paper_bgcolor="#ffffff",
        plot_bgcolor="#f8fafc",
        font={"family": "Ubuntu, sans-serif", "color": "#334155"},
        legend={"orientation": "h", "x": 0.5, "xanchor": "center", "y": 1.08},
        xaxis={"title": "Number of executed runs", "gridcolor": "#e2e8f0"},
        yaxis={"title": "Test case", "autorange": "reversed"},
    )
    return figure


def build_sankey_figure(links: Sequence[SankeyLink]) -> go.Figure:
    try:
        import plotly.graph_objects as go
    except ImportError as error:
        raise RuntimeError("plotly is required; install requirements.txt") from error

    labels = list(dict.fromkeys([link.source for link in links] + [link.target for link in links]))
    indexes = {label: index for index, label in enumerate(labels)}
    node_colors = {
        "Charm Test Results": "#2085a6",
        "PASSED": "#00a66c",
        "FAILED": "#d9383a",
        "Charm Issue": "#d97706",
        "UNTRIAGED": "#f59e0b",
        "Pipeline Issue": "#3b82f6",
        "Infra Issue": "#7c3aed",
        "Obsolete": "#94a3b8",
    }
    figure = go.Figure(
        go.Sankey(
            arrangement="freeform",
            node={
                "label": labels,
                "color": [node_colors.get(label, "#9ec1d9") for label in labels],
                "pad": 18,
                "thickness": 16,
                "line": {"color": "#ffffff", "width": 0.5},
            },
            link={
                "source": [indexes[link.source] for link in links],
                "target": [indexes[link.target] for link in links],
                "value": [link.weight for link in links],
                "customdata": [[link.result_count, link.attachment_count] for link in links],
                "hovertemplate": "%{source.label} → %{target.label}<br>Flow: %{value:.2f}<br>Results: %{customdata[0]}<br>Attachments: %{customdata[1]}<extra></extra>",
            },
        )
    )
    figure.update_layout(
        height=620,
        margin={"l": 30, "r": 30, "t": 35, "b": 30},
        paper_bgcolor="#ffffff",
        font={"family": "Ubuntu, sans-serif", "size": 11, "color": "#334155"},
    )
    return figure


def render_report(
    stats: Sequence[BarStat],
    links: Sequence[SankeyLink],
    date_range: DateRange,
    scope: ExecutionScope,
) -> str:
    try:
        import plotly.io as pio
    except ImportError as error:
        raise RuntimeError("plotly is required; install requirements.txt") from error

    chart_config = {"responsive": True, "displaylogo": False}
    if stats:
        bar_html = pio.to_html(
            build_bar_figure(stats), full_html=False, include_plotlyjs=False, config=chart_config
        )
    else:
        bar_html = '<p class="empty">No PASSED or FAILED charm results match this selection.</p>'
    has_results = any(link.link_type == "status" and link.weight > 0 for link in links)
    if has_results:
        sankey_html = pio.to_html(
            build_sankey_figure(links), full_html=False, include_plotlyjs=False, config=chart_config
        )
    else:
        sankey_html = '<p class="empty">No charm result flow is available for this selection.</p>'

    range_label = f"{date_range.start.date().isoformat()} to {date_range.end.date().isoformat()}"
    generated = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Charm Test Report</title>
    <script src="{PLOTLY_CDN_URL}"></script>
  <style>
    :root {{ --ink: #172033; --muted: #5f6b7a; --line: #d8dee7; --paper: #ffffff; --canvas: #eef2f5; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: radial-gradient(circle at top left, #ffffff 0, #eef2f5 52%, #e5ebef 100%); color: var(--ink); font-family: Ubuntu, "Noto Sans", sans-serif; }}
    main {{ width: min(1400px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 56px; }}
    header {{ display: flex; align-items: end; justify-content: space-between; gap: 24px; padding: 0 4px 22px; border-bottom: 1px solid var(--line); }}
    h1 {{ margin: 0; font-size: 32px; letter-spacing: 0; }}
    .meta {{ color: var(--muted); font-size: 13px; text-align: right; line-height: 1.6; }}
    .scope {{ color: #ffffff; background: #2085a6; padding: 3px 8px; border-radius: 3px; font-weight: 700; text-transform: uppercase; }}
    section {{ margin-top: 28px; padding: 24px; background: var(--paper); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 8px 24px rgba(23, 32, 51, 0.06); overflow: hidden; }}
    h2 {{ margin: 0 0 18px; font-size: 19px; }}
    .chart {{ min-width: 0; overflow-x: auto; }}
    .empty {{ margin: 0; padding: 64px 20px; text-align: center; color: var(--muted); background: #f8fafc; }}
    @media (max-width: 720px) {{ main {{ width: min(100% - 16px, 1400px); padding-top: 18px; }} header {{ align-items: start; flex-direction: column; }} .meta {{ text-align: left; }} section {{ padding: 14px; }} h1 {{ font-size: 25px; }} }}
    @media print {{ body {{ background: #fff; }} main {{ width: 100%; }} section {{ box-shadow: none; break-inside: avoid; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div><h1>Charm Test Report</h1></div>
    <div class="meta"><span class="scope">{html.escape(scope)}</span><br>{html.escape(range_label)}<br>Generated {html.escape(generated)}</div>
  </header>
  <section aria-labelledby="bar-title"><h2 id="bar-title">Test execution results by test case</h2><div class="chart">{bar_html}</div></section>
  <section aria-labelledby="flow-title"><h2 id="flow-title">Charm test result flow</h2><div class="chart">{sankey_html}</div></section>
</main>
</body>
</html>
"""


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def atomic_write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as temporary:
        writer = csv.DictWriter(temporary, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def write_outputs(
    output_path: Path,
    report_html: str,
    stats: Sequence[BarStat],
    links: Sequence[SankeyLink],
) -> tuple[Path, Path, Path]:
    bar_path = output_path.with_name(f"{output_path.stem}-bar-stats.csv")
    sankey_path = output_path.with_name(f"{output_path.stem}-sankey-links.csv")
    atomic_write_text(output_path, report_html)
    atomic_write_csv(
        bar_path,
        ("test_case", "passed", "failed", "total", "success_percentage"),
        (
            {
                "test_case": stat.test_case,
                "passed": stat.passed,
                "failed": stat.failed,
                "total": stat.total,
                "success_percentage": f"{stat.success_percentage:.2f}",
            }
            for stat in stats
        ),
    )
    atomic_write_csv(
        sankey_path,
        (
            "source",
            "target",
            "link_type",
            "flow_weight",
            "distinct_result_count",
            "attachment_count",
            "issue_source",
            "issue_project",
            "issue_key",
            "issue_title",
        ),
        (
            {
                "source": link.source,
                "target": link.target,
                "link_type": link.link_type,
                "flow_weight": f"{link.weight:.6f}",
                "distinct_result_count": link.result_count,
                "attachment_count": link.attachment_count,
                "issue_source": link.issue.source if link.issue else "",
                "issue_project": link.issue.project if link.issue else "",
                "issue_key": link.issue.key if link.issue else "",
                "issue_title": link.issue.title if link.issue else "",
            }
            for link in links
        ),
    )
    return output_path, bar_path, sankey_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_date, metavar="YYYY-MM-DD", help="start date (inclusive)")
    parser.add_argument("--end", type=parse_date, metavar="YYYY-MM-DD", help="end date (exclusive)")
    parser.add_argument(
        "--execution-scope",
        choices=("total", "latest"),
        default="total",
        help="aggregate all executions or latest revision/execution per group",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="include results from archived charm artifacts",
    )
    parser.add_argument("--top-test-cases", type=int, default=20, help="maximum test cases in the bar chart")
    parser.add_argument("--output", type=Path, default=Path("report.html"), help="output HTML path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.top_test_cases < 1:
        parser.error("--top-test-cases must be at least 1")
    try:
        date_range = resolve_date_range(arguments.start, arguments.end)
    except ValueError as error:
        parser.error(str(error))
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL must be set as environment variable")

    scope: ExecutionScope = arguments.execution_scope
    stats, failed_results, (passed_count, _failed_count) = fetch_report_data(
        database_url,
        date_range,
        scope,
        arguments.top_test_cases,
        arguments.include_archived,
    )
    links = build_sankey_links(passed_count, failed_results)
    report_html = render_report(stats, links, date_range, scope)
    paths = write_outputs(arguments.output, report_html, stats, links)
    print("Generated " + ", ".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
