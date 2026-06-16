import os
import pyodbc
import httpx
from datetime import datetime, timedelta
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether, Flowable
)
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from collections import defaultdict

load_dotenv()

# ── Colors ───────────────────────────────────────────────────────────────────
BG          = colors.HexColor("#0E1018")
CARD_BG     = colors.HexColor("#161922")
TABLE_BG    = colors.HexColor("#161922")
TABLE_ALT   = colors.HexColor("#1B1F2C")
HEADER_BG   = colors.HexColor("#11131C")
TENANT_HDR  = colors.HexColor("#1A1D2B")
BORDER      = colors.HexColor("#252836")
TEXT        = colors.HexColor("#E4E6F0")
TEXT_DIM    = colors.HexColor("#7C7F96")
TEXT_MUTED  = colors.HexColor("#484B5E")
GREEN       = colors.HexColor("#4E9A1A")
GREEN_DIM   = colors.HexColor("#1A2E10")
AMBER       = colors.HexColor("#C07A10")
AMBER_DIM   = colors.HexColor("#2E1E08")
RED         = colors.HexColor("#D94040")
RED_DIM     = colors.HexColor("#2E1010")
BLUE        = colors.HexColor("#378ADD")
ACCENT      = colors.HexColor("#4A6FF0")

# ── Tenant header flowable ────────────────────────────────────────────────────
class TenantHeader(Flowable):
    def __init__(self, name, system, n_types, total_archival, archived, failed, sla_pct, accent_color=ACCENT):
        super().__init__()
        self.name           = name
        self.system         = system
        self.n_types        = n_types
        self.total_archival = total_archival
        self.archived       = archived
        self.failed         = failed
        self.sla_pct        = sla_pct
        self.accent_color   = accent_color
        self.height         = 30

    def wrap(self, availWidth, availHeight):
        self._width = availWidth
        return (availWidth, self.height)

    def draw(self):
        c = self.canv
        w, h = self._width, self.height
        c.setFillColor(TENANT_HDR)
        c.rect(0, 0, w, h, fill=1, stroke=0)
        c.setFillColor(self.accent_color)
        c.rect(0, 0, 3, h, fill=1, stroke=0)
        c.setFillColor(TEXT)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(12, h - 12, self.name)
        c.setFillColor(TEXT_DIM)
        c.setFont("Helvetica", 8)
        c.drawString(12, 7, self.system)
        c.setStrokeColor(BORDER)
        c.setLineWidth(0.5)
        c.line(0, 0, w, 0)


class SectionLabel(Flowable):
    def __init__(self, text, dot_color=ACCENT):
        super().__init__()
        self.text      = text
        self.dot_color = dot_color
        self.height    = 20

    def wrap(self, availWidth, availHeight):
        self._width = availWidth
        return (availWidth, self.height)

    def draw(self):
        c = self.canv
        c.setFillColor(self.dot_color)
        c.circle(4, 8, 3, fill=1, stroke=0)
        c.setFillColor(TEXT_DIM)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(12, 5, self.text)


def page_decorator(canv, doc):
    canv.saveState()
    w, h = doc.pagesize
    canv.setFillColor(BG)
    canv.rect(0, 0, w, h, fill=1, stroke=0)
    canv.setFillColor(colors.HexColor("#13151E"))
    canv.rect(0, 0, w, 10*mm, fill=1, stroke=0)
    canv.setFillColor(TEXT_MUTED)
    canv.setFont("Helvetica", 7)
    canv.drawString(15*mm, 3.5*mm, "Archival Pipeline — Daily Report  •  Confidential")
    canv.drawRightString(w - 15*mm, 3.5*mm, f"Page {doc.page}")
    canv.restoreState()

# ── DB connection ────────────────────────────────────────────────────────────
def get_conn(prefix):
    driver = "ODBC Driver 18 for SQL Server" if os.name != "nt" else "SQL Server"
    conn = pyodbc.connect(
        f"DRIVER={{{driver}}};"
        f"SERVER={os.environ[f'{prefix}_DB_SERVER']};"
        f"DATABASE={os.environ[f'{prefix}_DB_NAME']};"
        f"UID={os.environ[f'{prefix}_DB_USER']};"
        f"PWD={os.environ[f'{prefix}_DB_PASSWORD']};"
        f"TrustServerCertificate=yes;"
    )
    conn.autocommit = True
    return conn

# ── Queries ──────────────────────────────────────────────────────────────────
ARCHIVAL_VENDORS_CTE = """
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
WITH ArchivalVendors AS (
    SELECT
        src.TenantId,
        STRING_AGG(v.Name, ', ') AS ArchivalSystems
    FROM (
        SELECT tv.TenantId, tv.VendorCode FROM TenantVendors tv WITH (NOLOCK) WHERE tv.IsDeleted = 0
        UNION
        SELECT vis.TenantId, vis.Code FROM VendorIntegrationSettings vis WITH (NOLOCK) WHERE vis.IsDeleted = 0
    ) src
    INNER JOIN Vendors v WITH (NOLOCK)
        ON v.Code = src.VendorCode AND v.IsDeleted = 0 AND v.Type = 3
    GROUP BY src.TenantId
)
"""

SUMMARY_QUERY = ARCHIVAL_VENDORS_CTE + """
SELECT
    COUNT(DISTINCT av.TenantId)                                                                AS TotalTenants,
    COUNT(b.TenantId)                                                                          AS TotalRecords,
    SUM(CASE WHEN b.IsProcessed = 1 THEN 1 ELSE 0 END)                                        AS Archived,
    SUM(CASE WHEN b.IsProcessed = 0 AND b.SQSRetryCount < 3 THEN 1 ELSE 0 END)               AS InPipeline,
    SUM(CASE WHEN b.IsProcessed = 0 AND b.SQSRetryCount >= 3 THEN 1 ELSE 0 END)               AS FailedDLQ,
    COALESCE(CAST(
        CASE
            WHEN SUM(CASE WHEN b.CreationTime <= DATEADD(HOUR, -24, GETDATE()) THEN 1 ELSE 0 END) = 0 THEN 0
            ELSE 100.0
                * SUM(CASE WHEN b.CreationTime <= DATEADD(HOUR, -24, GETDATE()) AND b.IsProcessed = 0 THEN 1 ELSE 0 END)
                / SUM(CASE WHEN b.CreationTime <= DATEADD(HOUR, -24, GETDATE()) THEN 1 ELSE 0 END)
        END
    AS DECIMAL(5,2)), 0)                                                                       AS AvgSLAMissPct
FROM ArchivalVendors av
INNER JOIN TenantDetail td WITH (NOLOCK) ON td.TenantId = av.TenantId AND td.IsDeleted = 0
INNER JOIN Archival_Logs b WITH (NOLOCK)
    ON b.TenantId = av.TenantId
    AND b.CreationTime >= DATEADD(DAY, -7, GETDATE())
"""

TENANT_QUERY = ARCHIVAL_VENDORS_CTE + """
, Base AS (
    SELECT
        al.TenantId,
        al.TransactionType,
        al.IsProcessed,
        al.SQSRetryCount,
        al.CreationTime,
        al.IsMessageDeleted,
        CASE WHEN al.CreationTime <= DATEADD(HOUR, -24, GETDATE()) THEN 1 ELSE 0 END AS IsDue
    FROM Archival_Logs al WITH (NOLOCK)
    WHERE al.CreationTime >= DATEADD(DAY, -7, GETDATE())
),
LastSuccess AS (
    SELECT TenantId, TransactionType, IsMessageDeleted, MAX(CreationTime) AS LastSuccessfulArchiveTime
    FROM Archival_Logs WITH (NOLOCK)
    WHERE IsProcessed = 1 AND CreationTime >= DATEADD(MONTH, -6, GETDATE())
    GROUP BY TenantId, TransactionType, IsMessageDeleted
),
ChatRecords AS (
    SELECT
        TenantId,
        COUNT(DISTINCT SharedMessageId) AS TotalChatRecords
    FROM AppChatMessages WITH (NOLOCK)
    WHERE CreationTime >= DATEADD(DAY, -7, GETDATE())
    GROUP BY TenantId
),
-- Live feed records: all records created in window regardless of deletion state
-- so a comment that was later deleted still counts as 1 total record
FeedLive AS (
    SELECT
        fd.TenantId,
        CASE
            WHEN fd.FeedReplyID = '00000000-0000-0000-0000-000000000000' THEN 'feed'
            WHEN p.FeedReplyID  = '00000000-0000-0000-0000-000000000000' THEN 'comment'
            WHEN p.Id IS NOT NULL                                         THEN 'reply'
        END AS ContentType,
        COUNT(*) AS TotalRecords
    FROM FeedDetail fd WITH (NOLOCK)
    LEFT JOIN FeedDetail p WITH (NOLOCK)
        ON p.Id = fd.FeedReplyID AND p.TenantId = fd.TenantId
    WHERE fd.CreationTime >= DATEADD(DAY, -7, GETDATE())
    GROUP BY fd.TenantId,
        CASE
            WHEN fd.FeedReplyID = '00000000-0000-0000-0000-000000000000' THEN 'feed'
            WHEN p.FeedReplyID  = '00000000-0000-0000-0000-000000000000' THEN 'comment'
            WHEN p.Id IS NOT NULL                                         THEN 'reply'
        END
),
-- Deleted feed records: records deleted in the window
FeedDeleted AS (
    SELECT
        fd.TenantId,
        CASE
            WHEN fd.FeedReplyID = '00000000-0000-0000-0000-000000000000' THEN 'feed_deleted'
            ELSE 'comment_deleted'
        END AS ContentType,
        COUNT(*) AS TotalRecords
    FROM FeedDetail fd WITH (NOLOCK)
    WHERE fd.IsDeleted = 1
      AND fd.DeletionTime >= DATEADD(DAY, -7, GETDATE())
    GROUP BY fd.TenantId,
        CASE
            WHEN fd.FeedReplyID = '00000000-0000-0000-0000-000000000000' THEN 'feed_deleted'
            ELSE 'comment_deleted'
        END
)
SELECT
    av.TenantId,
    td.BusinessName                                                                            AS Tenant,
    av.ArchivalSystems                                                                         AS [Archival System],
    CASE
        WHEN b.IsMessageDeleted = 1 AND ISNULL(b.TransactionType, 'N/A') = 'feed'              THEN 'feed_deleted'
        WHEN b.IsMessageDeleted = 1 AND ISNULL(b.TransactionType, 'N/A') IN ('comment','reply') THEN 'comment_deleted'
        ELSE ISNULL(b.TransactionType, 'N/A')
    END                                                                                        AS [Type],
    COALESCE(COUNT(b.TenantId), 0)                                                            AS [Total Archival Records],
    CASE
        WHEN b.IsMessageDeleted = 0 AND ISNULL(b.TransactionType, 'N/A') = 'chat'
            THEN COALESCE(MAX(cr.TotalChatRecords), 0)
        WHEN b.IsMessageDeleted = 0 AND ISNULL(b.TransactionType, 'N/A') IN ('feed', 'comment', 'reply')
            THEN COALESCE(MAX(fl.TotalRecords), 0)
        WHEN b.IsMessageDeleted = 1 AND ISNULL(b.TransactionType, 'N/A') IN ('feed', 'comment', 'reply')
            THEN COALESCE(MAX(fd2.TotalRecords), 0)
        ELSE NULL
    END                                                                                        AS [Total Records],
    CASE
        WHEN b.IsMessageDeleted = 0 AND ISNULL(b.TransactionType, 'N/A') = 'chat'
            THEN COALESCE(MAX(cr.TotalChatRecords), 0) - COALESCE(COUNT(b.TenantId), 0)
        WHEN b.IsMessageDeleted = 0 AND ISNULL(b.TransactionType, 'N/A') IN ('feed', 'comment', 'reply')
            THEN COALESCE(MAX(fl.TotalRecords), 0) - COALESCE(COUNT(b.TenantId), 0)
        WHEN b.IsMessageDeleted = 1 AND ISNULL(b.TransactionType, 'N/A') IN ('feed', 'comment', 'reply')
            THEN COALESCE(MAX(fd2.TotalRecords), 0) - COALESCE(COUNT(b.TenantId), 0)
        ELSE NULL
    END                                                                                        AS [Missing Records],
    COALESCE(SUM(CASE WHEN b.IsProcessed = 1 THEN 1 ELSE 0 END), 0)                          AS Archived,
    COALESCE(SUM(CASE WHEN b.IsProcessed = 0 AND b.SQSRetryCount < 3 THEN 1 ELSE 0 END), 0) AS [In Pipeline],
    COALESCE(SUM(CASE WHEN b.IsProcessed = 0 AND b.SQSRetryCount >= 3 THEN 1 ELSE 0 END), 0) AS Failed,
    COALESCE(CAST(
        CASE
            WHEN SUM(b.IsDue) = 0 THEN 0
            ELSE 100.0 * SUM(CASE WHEN b.IsDue = 1 AND b.IsProcessed = 0 THEN 1 ELSE 0 END) / SUM(b.IsDue)
        END
    AS DECIMAL(5,2)), 0)                                                                       AS [SLA Miss %],
    ls.LastSuccessfulArchiveTime                                                               AS [Last Success]
FROM ArchivalVendors av
INNER JOIN TenantDetail td WITH (NOLOCK) ON td.TenantId = av.TenantId AND td.IsDeleted = 0
INNER JOIN Base b ON b.TenantId = av.TenantId
LEFT JOIN LastSuccess ls
    ON ls.TenantId = av.TenantId
    AND ls.TransactionType = b.TransactionType
    AND ls.IsMessageDeleted = b.IsMessageDeleted
LEFT JOIN ChatRecords cr
    ON cr.TenantId = av.TenantId
    AND b.IsMessageDeleted = 0
    AND ISNULL(b.TransactionType, 'N/A') = 'chat'
LEFT JOIN FeedLive fl
    ON fl.TenantId = av.TenantId
    AND b.IsMessageDeleted = 0
    AND fl.ContentType = ISNULL(b.TransactionType, 'N/A')
LEFT JOIN FeedDeleted fd2
    ON fd2.TenantId = av.TenantId
    AND b.IsMessageDeleted = 1
    AND fd2.ContentType = CASE
        WHEN ISNULL(b.TransactionType, 'N/A') = 'feed'                THEN 'feed_deleted'
        WHEN ISNULL(b.TransactionType, 'N/A') IN ('comment', 'reply') THEN 'comment_deleted'
        ELSE NULL
    END
GROUP BY
    av.TenantId,
    td.BusinessName,
    av.ArchivalSystems,
    b.TransactionType,
    b.IsMessageDeleted,
    ls.LastSuccessfulArchiveTime
ORDER BY td.BusinessName, [SLA Miss %] DESC
"""

VENDOR_QUERY = ARCHIVAL_VENDORS_CTE + """
, Base AS (
    SELECT
        al.TenantId, al.TransactionType, al.IsProcessed, al.SQSRetryCount, al.CreationTime,
        al.IsMessageDeleted,
        CASE WHEN al.CreationTime <= DATEADD(HOUR, -24, GETDATE()) THEN 1 ELSE 0 END AS IsDue
    FROM Archival_Logs al WITH (NOLOCK)
    WHERE al.CreationTime >= DATEADD(DAY, -7, GETDATE())
),
LastSuccess AS (
    SELECT al.TenantId, al.TransactionType, al.IsMessageDeleted, MAX(al.CreationTime) AS LastSuccessfulArchiveTime
    FROM Archival_Logs al WITH (NOLOCK)
    WHERE al.IsProcessed = 1 AND al.CreationTime >= DATEADD(MONTH, -6, GETDATE())
    GROUP BY al.TenantId, al.TransactionType, al.IsMessageDeleted
)
SELECT
    av.ArchivalSystems,
    CASE
        WHEN b.IsMessageDeleted = 1 AND ISNULL(b.TransactionType, 'N/A') = 'feed'              THEN 'feed_deleted'
        WHEN b.IsMessageDeleted = 1 AND ISNULL(b.TransactionType, 'N/A') IN ('comment','reply') THEN 'comment_deleted'
        ELSE ISNULL(b.TransactionType, 'N/A')
    END                                                                                          AS [Type],
    COALESCE(COUNT(b.TenantId), 0)                                                              AS [Total Records],
    COALESCE(SUM(CASE WHEN b.IsProcessed = 1 THEN 1 ELSE 0 END), 0)                            AS Archived,
    COALESCE(SUM(CASE WHEN b.IsProcessed = 0 AND b.SQSRetryCount < 3 THEN 1 ELSE 0 END), 0)   AS [In Pipeline],
    COALESCE(SUM(CASE WHEN b.IsProcessed = 0 AND b.SQSRetryCount >= 3 THEN 1 ELSE 0 END), 0)  AS Failed,
    COALESCE(CAST(
        CASE
            WHEN SUM(b.IsDue) = 0 THEN 0
            ELSE 100.0 * SUM(CASE WHEN b.IsDue = 1 AND b.IsProcessed = 0 THEN 1 ELSE 0 END) / SUM(b.IsDue)
        END
    AS DECIMAL(5,2)), 0)                                                                         AS [SLA Miss %],
    MAX(ls.LastSuccessfulArchiveTime)                                                            AS [Last Success]
FROM ArchivalVendors av
INNER JOIN TenantDetail td WITH (NOLOCK) ON td.TenantId = av.TenantId AND td.IsDeleted = 0
INNER JOIN Base b ON b.TenantId = av.TenantId
LEFT JOIN LastSuccess ls
    ON ls.TenantId = av.TenantId
    AND ls.TransactionType = b.TransactionType
    AND ls.IsMessageDeleted = b.IsMessageDeleted
GROUP BY av.ArchivalSystems, b.TransactionType, b.IsMessageDeleted
ORDER BY av.ArchivalSystems, [SLA Miss %] DESC
"""

TYPE_QUERY = ARCHIVAL_VENDORS_CTE + """
, Base AS (
    SELECT
        al.TenantId, al.TransactionType, al.IsProcessed, al.SQSRetryCount, al.CreationTime,
        al.IsMessageDeleted,
        CASE WHEN al.CreationTime <= DATEADD(HOUR, -24, GETDATE()) THEN 1 ELSE 0 END AS IsDue
    FROM Archival_Logs al WITH (NOLOCK)
    WHERE al.CreationTime >= DATEADD(DAY, -7, GETDATE())
),
LastSuccess AS (
    SELECT al.TransactionType, al.IsMessageDeleted, MAX(al.CreationTime) AS LastSuccessfulArchiveTime
    FROM Archival_Logs al WITH (NOLOCK)
    WHERE al.IsProcessed = 1 AND al.CreationTime >= DATEADD(MONTH, -6, GETDATE())
    GROUP BY al.TransactionType, al.IsMessageDeleted
)
SELECT
    CASE
        WHEN b.IsMessageDeleted = 1 AND ISNULL(b.TransactionType, 'N/A') = 'feed'              THEN 'feed_deleted'
        WHEN b.IsMessageDeleted = 1 AND ISNULL(b.TransactionType, 'N/A') IN ('comment','reply') THEN 'comment_deleted'
        ELSE ISNULL(b.TransactionType, 'N/A')
    END                                                                                          AS [Type],
    COALESCE(COUNT(b.TenantId), 0)                                                              AS [Total Records],
    COALESCE(SUM(CASE WHEN b.IsProcessed = 1 THEN 1 ELSE 0 END), 0)                            AS Archived,
    COALESCE(SUM(CASE WHEN b.IsProcessed = 0 AND b.SQSRetryCount < 3 THEN 1 ELSE 0 END), 0)   AS [In Pipeline],
    COALESCE(SUM(CASE WHEN b.IsProcessed = 0 AND b.SQSRetryCount >= 3 THEN 1 ELSE 0 END), 0)  AS Failed,
    COALESCE(CAST(
        CASE
            WHEN SUM(b.IsDue) = 0 THEN 0
            ELSE 100.0 * SUM(CASE WHEN b.IsDue = 1 AND b.IsProcessed = 0 THEN 1 ELSE 0 END) / SUM(b.IsDue)
        END
    AS DECIMAL(5,2)), 0)                                                                         AS [SLA Miss %],
    ls.LastSuccessfulArchiveTime                                                                 AS [Last Success]
FROM ArchivalVendors av
INNER JOIN TenantDetail td WITH (NOLOCK) ON td.TenantId = av.TenantId AND td.IsDeleted = 0
INNER JOIN Base b ON b.TenantId = av.TenantId
LEFT JOIN LastSuccess ls
    ON ls.TransactionType = b.TransactionType
    AND ls.IsMessageDeleted = b.IsMessageDeleted
GROUP BY b.TransactionType, b.IsMessageDeleted, ls.LastSuccessfulArchiveTime
ORDER BY [SLA Miss %] DESC
"""

# ── Helpers ──────────────────────────────────────────────────────────────────
def sla_color(pct):
    if pct == 0:
        return GREEN
    elif pct < 10:
        return AMBER
    else:
        return RED

def sla_bg(pct):
    if pct == 0:
        return GREEN_DIM
    elif pct < 10:
        return AMBER_DIM
    else:
        return RED_DIM

def fmt_last_success(dt):
    if dt is None:
        return "Never"
    if isinstance(dt, datetime):
        pass
    elif isinstance(dt, str):
        try:
            dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S.%f")
            except ValueError:
                dt = dt[:19]
                try:
                    dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return str(dt)
    else:
        return str(dt)
    now = datetime.now()
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        return f"{seconds // 60}m ago"
    elif seconds < 86400:
        return f"{seconds // 3600}h ago"
    else:
        return f"{seconds // 86400}d ago"

def fmt_num(n):
    if n is None:
        return "0"
    return f"{int(n):,}"

def make_sla_cell(sla):
    fg = sla_color(sla)
    bg = sla_bg(sla)
    return Table(
        [[Paragraph(f"{sla:.1f}%", ParagraphStyle(
            "sla", fontSize=9, fontName="Helvetica-Bold",
            textColor=fg, alignment=TA_CENTER))]],
        style=TableStyle([
            ("BOX",           (0,0), (-1,-1), 0.5, fg),
            ("BACKGROUND",    (0,0), (-1,-1), bg),
            ("TOPPADDING",    (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
            ("RIGHTPADDING",  (0,0), (-1,-1), 8),
        ])
    )

def make_missing_cell(missing):
    if missing is None:
        return Paragraph("—", ParagraphStyle("na", fontSize=9, fontName="Helvetica",
                                              textColor=TEXT_MUTED, alignment=TA_CENTER))
    fg = GREEN if missing == 0 else RED
    bg = GREEN_DIM if missing == 0 else RED_DIM
    return Table(
        [[Paragraph(fmt_num(missing), ParagraphStyle(
            "miss", fontSize=9, fontName="Helvetica-Bold",
            textColor=fg, alignment=TA_CENTER))]],
        style=TableStyle([
            ("BOX",           (0,0), (-1,-1), 0.5, fg),
            ("BACKGROUND",    (0,0), (-1,-1), bg),
            ("TOPPADDING",    (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
            ("RIGHTPADDING",  (0,0), (-1,-1), 8),
        ])
    )

def make_breakdown_table(header_style, cell_style, num_style, col_headers, col_widths, rows_data):
    table_data = [[Paragraph(h, header_style) for h in col_headers]]
    for r in rows_data:
        failed = r["failed"]
        dlq_color = RED if failed > 0 else TEXT_DIM
        dlq_font  = "Helvetica-Bold" if failed > 0 else "Helvetica"
        dlq_style = ParagraphStyle("dlq", fontSize=9, fontName=dlq_font,
                                   textColor=dlq_color, alignment=TA_RIGHT)
        total_records = r.get("total_records")
        total_cell = Paragraph(
            fmt_num(total_records) if total_records is not None else "—",
            num_style if total_records is not None else ParagraphStyle("na2", fontSize=9,
                fontName="Helvetica", textColor=TEXT_MUTED, alignment=TA_RIGHT)
        )
        table_data.append([
            Paragraph(r["tx_type"], cell_style),
            total_cell,
            Paragraph(fmt_num(r["archival_logs"]), num_style),
            make_missing_cell(r.get("missing")),
            Paragraph(fmt_num(r["archived"]),    num_style),
            Paragraph(fmt_num(r["in_pipeline"]), num_style),
            Paragraph(fmt_num(failed),           dlq_style),
            make_sla_cell(r["sla"]),
            Paragraph(r["last_succ"], ParagraphStyle("ls", fontSize=9,
                      fontName="Helvetica", textColor=TEXT_DIM)),
        ])
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0),  HEADER_BG),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [TABLE_BG, TABLE_ALT]),
        ("LINEBELOW",     (0,0), (-1,-1), 0.5, BORDER),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("RIGHTPADDING",  (0,0), (-1,-1), 8),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("BOX",           (0,0), (-1,-1), 0.5, BORDER),
        ("ALIGN",         (2,0), (6,-1),  "RIGHT"),
    ]))
    return t

# ── Report ───────────────────────────────────────────────────────────────────
def generate_report(prefix):
    conn = get_conn(prefix)
    cursor = conn.cursor()

    cursor.execute(SUMMARY_QUERY)
    summary = cursor.fetchone()

    cursor.execute(TENANT_QUERY)
    tenant_rows = cursor.fetchall()

    cursor.execute(VENDOR_QUERY)
    vendor_rows = cursor.fetchall()

    cursor.execute(TYPE_QUERY)
    type_rows = cursor.fetchall()

    conn.close()

    tenants_map = defaultdict(list)
    tenant_meta = {}
    for row in tenant_rows:
        tid = row[0]
        tenants_map[tid].append(row)
        if tid not in tenant_meta:
            tenant_meta[tid] = {"name": row[1], "system": row[2]}

    vendors_map = defaultdict(list)
    for row in vendor_rows:
        vendors_map[row[0]].append(row)

    type_map = defaultdict(list)
    for row in type_rows:
        type_map[str(row[0] or "N/A")].append(row)

    # Fleet-level totals summed from tenant breakdown rows
    total_source_records  = sum(int(r[5]) for rows in tenants_map.values() for r in rows if r[5] is not None)
    total_archival_logs   = sum(int(r[4]) for rows in tenants_map.values() for r in rows if r[4] is not None)
    total_missing         = sum(int(r[6]) for rows in tenants_map.values() for r in rows if r[6] is not None and int(r[6]) > 0)

    now         = datetime.now()
    period_from = (now - timedelta(days=7)).strftime("%b %d, %Y %I:%M %p")
    period_to   = now.strftime("%b %d, %Y %I:%M %p")
    period_str  = f"{period_from}  →  {period_to}"
    output_file = f"archival-report-{prefix.lower()}-{now.strftime('%Y-%m-%d')}.pdf"

    doc = SimpleDocTemplate(
        output_file,
        pagesize=landscape(A3),
        leftMargin=8*mm,
        rightMargin=8*mm,
        topMargin=12*mm,
        bottomMargin=12*mm,
    )

    title_style  = ParagraphStyle("title", fontSize=22, fontName="Helvetica-Bold",
                                  textColor=TEXT, spaceAfter=8, leading=28)
    period_style = ParagraphStyle("period", fontSize=10, fontName="Helvetica",
                                  textColor=TEXT_DIM, spaceAfter=14, leading=14)
    cell_style   = ParagraphStyle("cell", fontSize=8, fontName="Helvetica",
                                  textColor=TEXT, leading=11)
    num_style    = ParagraphStyle("num", fontSize=8, fontName="Helvetica",
                                  textColor=TEXT, leading=11, alignment=TA_RIGHT)
    header_style = ParagraphStyle("th", fontSize=7, fontName="Helvetica-Bold",
                                  textColor=TEXT_DIM)

    # Main (full-width) table: Type col widened from 30mm to 38mm; Last Success trimmed by 8mm to compensate
    col_widths  = [38*mm, 30*mm, 30*mm, 26*mm, 26*mm, 26*mm, 22*mm, 26*mm, 20*mm]
    col_headers = ["Type", "Source Records", "Archival Logs", "Missing", "Archived", "In Pipeline", "Failed", "SLA Miss", "Last Success"]

    story = []

    story.append(Paragraph(f"Archival Pipeline — Daily Report ({prefix.capitalize()})", title_style))
    story.append(Paragraph(period_str, period_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=6))

    story.append(SectionLabel("FLEET SUMMARY", dot_color=ACCENT))
    story.append(Spacer(1, 6))

    sla_pct = float(summary.AvgSLAMissPct or 0)

    def stat_card(label, value, sub, value_color=TEXT, top_color=BORDER):
        return Table(
            [
                [Table([[""]], colWidths=[50*mm], rowHeights=[3],
                       style=TableStyle([("BACKGROUND",(0,0),(-1,-1), top_color),
                                         ("TOPPADDING",(0,0),(-1,-1),0),
                                         ("BOTTOMPADDING",(0,0),(-1,-1),0),
                                         ("LEFTPADDING",(0,0),(-1,-1),0),
                                         ("RIGHTPADDING",(0,0),(-1,-1),0)]))],
                [Paragraph(label, ParagraphStyle("cl", fontSize=8, textColor=TEXT_DIM, fontName="Helvetica"))],
                [Paragraph(value, ParagraphStyle("cv", fontSize=20, fontName="Helvetica-Bold", textColor=value_color, leading=24))],
                [Paragraph(sub,   ParagraphStyle("cs", fontSize=7,  textColor=TEXT_MUTED, fontName="Helvetica"))],
            ],
            colWidths=[55*mm],
            style=TableStyle([
                ("BOX",           (0,0), (-1,-1), 0.5, BORDER),
                ("BACKGROUND",    (0,0), (-1,-1), CARD_BG),
                ("TOPPADDING",    (0,1), (-1,-1), 8),
                ("BOTTOMPADDING", (0,0), (-1,-1), 8),
                ("LEFTPADDING",   (0,0), (-1,-1), 12),
                ("RIGHTPADDING",  (0,0), (-1,-1), 12),
                ("TOPPADDING",    (0,0), (-1,0),  0),
                ("BOTTOMPADDING", (0,0), (-1,0),  0),
                ("LEFTPADDING",   (0,0), (-1,0),  0),
                ("RIGHTPADDING",  (0,0), (-1,0),  0),
            ])
        )

    cards = Table([[
        stat_card("Total tenants",   fmt_num(summary.TotalTenants), "active this period",                        TEXT,               ACCENT),
        stat_card("Avg SLA miss",    f"{sla_pct:.1f}%",            "records due >24h not yet archived",         sla_color(sla_pct), sla_color(sla_pct)),
        # Records card — three labeled rows, custom layout
        Table(
            [
                [Table([[""]], colWidths=[50*mm], rowHeights=[3],
                       style=TableStyle([("BACKGROUND",(0,0),(-1,-1), TEXT_DIM),
                                         ("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0),
                                         ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0)]))],
                [Paragraph("Records", ParagraphStyle("rcl", fontSize=8, textColor=TEXT_DIM, fontName="Helvetica"))],
                [Paragraph(f"Total: {fmt_num(total_source_records)}",
                           ParagraphStyle("rv1", fontSize=10, fontName="Helvetica-Bold", textColor=TEXT, leading=13))],
                [Paragraph(f"Archival Logs: {fmt_num(total_archival_logs)}",
                           ParagraphStyle("rv2", fontSize=10, fontName="Helvetica-Bold", textColor=TEXT, leading=13))],
                [Paragraph(f"Missing: {fmt_num(total_missing)}",
                           ParagraphStyle("rv3", fontSize=10, fontName="Helvetica-Bold",
                                          textColor=RED if total_missing > 0 else GREEN, leading=13))],
                [Paragraph("source data vs archival logs",
                           ParagraphStyle("rcs", fontSize=7, textColor=TEXT_MUTED, fontName="Helvetica"))],
            ],
            colWidths=[55*mm],
            style=TableStyle([
                ("BOX",           (0,0), (-1,-1), 0.5, BORDER),
                ("BACKGROUND",    (0,0), (-1,-1), CARD_BG),
                ("TOPPADDING",    (0,1), (-1,-1), 5),
                ("BOTTOMPADDING", (0,0), (-1,-1), 5),
                ("LEFTPADDING",   (0,0), (-1,-1), 12),
                ("RIGHTPADDING",  (0,0), (-1,-1), 12),
                ("TOPPADDING",    (0,0), (-1,0),  0),
                ("BOTTOMPADDING", (0,0), (-1,0),  0),
                ("LEFTPADDING",   (0,0), (-1,0),  0),
                ("RIGHTPADDING",  (0,0), (-1,0),  0),
            ])
        ),
        stat_card("Archived",        fmt_num(summary.Archived),    "processed ok",                              GREEN,              GREEN),
        stat_card("In pipeline",     fmt_num(summary.InPipeline),  "staged + queued + in-progress",             AMBER,              AMBER),
        stat_card("Failed (DLQ)",    fmt_num(summary.FailedDLQ),   "retry >= 3",                                RED,                RED),
    ]],
    colWidths=[58*mm] * 6,
    style=TableStyle([
        ("LEFTPADDING",  (0,0), (-1,-1), 3),
        ("RIGHTPADDING", (0,0), (-1,-1), 3),
        ("VALIGN",       (0,0), (-1,-1), "TOP"),
    ]))

    story.append(cards)
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=4))

    story.append(SectionLabel("TENANT BREAKDOWN", dot_color=BLUE))
    story.append(Spacer(1, 6))

    col_w = (doc.width / 2) - 3*mm
    tenant_blocks = []
    for tid, trows in tenants_map.items():
        meta = tenant_meta[tid]
        rows_data = [{
            "tx_type":       str(r[3] or "N/A"),
            "archival_logs": int(r[4] or 0),
            "total_records": int(r[5]) if r[5] is not None else None,
            "missing":       int(r[6]) if r[6] is not None else None,
            "archived":      int(r[7] or 0),
            "in_pipeline":   int(r[8] or 0),
            "failed":        int(r[9] or 0),
            "sla":           float(r[10] or 0),
            "last_succ":     fmt_last_success(r[11]),
        } for r in trows]

        total_archival = sum(r["archival_logs"] for r in rows_data)
        total_archived = sum(r["archived"] for r in rows_data)
        total_failed   = sum(r["failed"] for r in rows_data)
        worst_sla      = max(r["sla"] for r in rows_data)
        accent         = GREEN if worst_sla == 0 else (AMBER if worst_sla < 10 else RED)

        # Tenant breakdown table: Type col widened from 26mm to 34mm; Last Success trimmed by 8mm to compensate
        narrow_col_widths = [34*mm, 22*mm, 22*mm, 20*mm, 20*mm, 20*mm, 17*mm, 20*mm, 24*mm]

        hdr = TenantHeader(
            name           = meta["name"],
            system         = meta["system"],
            n_types        = len(rows_data),
            total_archival = total_archival,
            archived       = total_archived,
            failed         = total_failed,
            sla_pct        = worst_sla,
            accent_color   = accent,
        )
        archival_table = make_breakdown_table(header_style, cell_style, num_style, col_headers, narrow_col_widths, rows_data)

        combined = Table(
            [[hdr], [archival_table]],
            colWidths=[col_w],
            style=TableStyle([
                ("TOPPADDING",    (0,0), (-1,-1), 0),
                ("BOTTOMPADDING", (0,0), (-1,-1), 0),
                ("LEFTPADDING",   (0,0), (-1,-1), 0),
                ("RIGHTPADDING",  (0,0), (-1,-1), 0),
                ("VALIGN",        (0,0), (-1,-1), "TOP"),
            ])
        )
        tenant_blocks.append(combined)

    col_w = (doc.width / 2) - 3*mm
    for i in range(0, len(tenant_blocks), 2):
        left_block  = tenant_blocks[i]
        right_block = tenant_blocks[i + 1] if i + 1 < len(tenant_blocks) else Spacer(col_w, 1)
        pair = Table(
            [[left_block, right_block]],
            colWidths=[col_w, col_w],
            style=TableStyle([
                ("VALIGN",        (0,0), (-1,-1), "TOP"),
                ("TOPPADDING",    (0,0), (-1,-1), 0),
                ("BOTTOMPADDING", (0,0), (-1,-1), 0),
                ("LEFTPADDING",   (0,0), (-1,-1), 0),
                ("RIGHTPADDING",  (0,0), (-1,-1), 0),
                ("LEFTPADDING",   (1,0), (1,-1),  5),
            ])
        )
        story.append(pair)
        story.append(Spacer(1, 6))

    doc.build(story, onFirstPage=page_decorator, onLaterPages=page_decorator)
    print(f"Report saved: {output_file}")

    return {
        "summary":      summary,
        "tenants_map":  tenants_map,
        "tenant_meta":  tenant_meta,
        "vendors_map":  vendors_map,
        "type_map":     type_map,
        "period_from":  period_from,
        "period_to":    period_to,
        "output_file":  output_file,
        "env_name":     prefix.capitalize(),
    }

# ── Slack ─────────────────────────────────────────────────────────────────────
def sla_emoji(pct):
    if pct == 0:
        return "🟢"
    elif pct < 10:
        return "🟡"
    else:
        return "🔴"

def send_slack_file(png_path, data):
    file_size = os.path.getsize(png_path)

    slack_token   = os.environ.get("SLACK_BOT_TOKEN")
    webhook_url   = os.environ.get("SLACK_WEBHOOK_URL")
    slack_channel = os.environ.get("SLACK_CHANNEL")

    period_from = data["period_from"]
    period_to   = data["period_to"]
    summary     = data["summary"]
    sla_pct     = float(summary.AvgSLAMissPct or 0)
    env_name    = data.get("env_name", "")
    env_label   = f" — *{env_name}*" if env_name else ""

    if slack_token and slack_channel:
        filename = os.path.basename(png_path)

        resp1 = httpx.post(
            "https://slack.com/api/files.getUploadURLExternal",
            headers={"Authorization": f"Bearer {slack_token}"},
            data={"filename": filename, "length": file_size},
        )
        r1 = resp1.json()
        if not r1.get("ok"):
            print(f"Slack upload URL error: {r1.get('error')}")
            return

        upload_url = r1["upload_url"]
        file_id    = r1["file_id"]

        with open(png_path, "rb") as f:
            resp2 = httpx.post(upload_url, content=f.read(),
                               headers={"Content-Type": "image/png"})
        if resp2.status_code != 200:
            print(f"Slack upload error: {resp2.status_code} {resp2.text}")
            return

        resp3 = httpx.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers={"Authorization": f"Bearer {slack_token}"},
            json={
                "files":           [{"id": file_id, "title": f"Archival Pipeline — Daily Report{env_label}  |  {period_from} → {period_to}"}],
                "channel_id":      slack_channel,
                "initial_comment": (
                    f"*📊 Archival Pipeline — Daily Report{env_label}*\n"
                    f"{period_from}  →  {period_to}\n"
                    f"{sla_emoji(sla_pct)} Avg SLA Miss: *{sla_pct:.1f}%*  "
                    f"·  ✅ Archived: *{fmt_num(summary.Archived)}*  "
                    f"·  ⏳ In Pipeline: *{fmt_num(summary.InPipeline)}*  "
                    f"·  🚨 Failed (DLQ): *{fmt_num(summary.FailedDLQ)}*"
                ),
            },
        )
        r3 = resp3.json()
        if r3.get("ok"):
            print("Slack file uploaded!")
        else:
            print(f"Slack file upload error: {r3.get('error')}")

    elif webhook_url:
        resp = httpx.post(webhook_url, json={
            "text": (
                f"*📊 Archival Pipeline — Daily Report{env_label}*\n"
                f"{period_from}  →  {period_to}\n"
                f"{sla_emoji(sla_pct)} Avg SLA Miss: *{sla_pct:.1f}%*  "
                f"·  ✅ Archived: *{fmt_num(summary.Archived)}*  "
                f"·  ⏳ In Pipeline: *{fmt_num(summary.InPipeline)}*  "
                f"·  🚨 Failed (DLQ): *{fmt_num(summary.FailedDLQ)}*\n"
                f"_Report PNG saved locally: {png_path}_"
            )
        })
        if resp.status_code == 200:
            print("Slack summary sent via webhook (PNG not uploaded — add SLACK_BOT_TOKEN + SLACK_CHANNEL to .env for inline image)")
        else:
            print(f"Slack webhook error: {resp.status_code} {resp.text}")
    else:
        print("No Slack credentials found — set SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN + SLACK_CHANNEL in .env")

def pdf_to_png(pdf_path):
    from pdf2image import convert_from_path
    from PIL import Image

    print("Converting PDF to PNG...")
    pages = convert_from_path(pdf_path, dpi=120)

    if len(pages) == 1:
        png_path = pdf_path.replace(".pdf", ".png")
        pages[0].save(png_path, "PNG")
    else:
        total_height = sum(p.height for p in pages)
        max_width    = max(p.width for p in pages)
        combined     = Image.new("RGB", (max_width, total_height), (14, 16, 24))
        y = 0
        for page in pages:
            combined.paste(page, (0, y))
            y += page.height
        png_path = pdf_path.replace(".pdf", ".png")
        combined.save(png_path, "PNG")

    print(f"PNG saved: {png_path}")
    return png_path

if __name__ == "__main__":
    prefix = os.environ.get("ENVIRONMENT", "").upper()
    if prefix not in ("FYNANCIAL", "SANCTUARY"):
        raise ValueError(f"ENVIRONMENT must be FYNANCIAL or SANCTUARY, got: '{prefix}'")
    print(f"\nRunning report for {prefix}...\n")
    data = generate_report(prefix)
    png  = pdf_to_png(data["output_file"])
    send_slack_file(png, data)