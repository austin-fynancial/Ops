"""
report.py

Combined integration status report for one environment. Per tenant it shows:
  - CRM connection statuses          (SQL Server:  <ENV>_SQL_DB_*)
  - Performance vendor statuses      (PostgreSQL:  <ENV>_CONNECTOR_DB_*)

Tenants are matched on  TenantDetail.TenantId (SQL Server) = tenant_mdm.tenant_id (Postgres).
Connector tenants with no active TenantDetail match are flagged in the report.

Usage:
    python reports/report.py FYNANCIAL                     # query both DBs, write HTML
    python reports/report.py                               # uses ENVIRONMENT from .env
    python reports/report.py FYNANCIAL --slack             # also post summary + upload file
    python reports/report.py FYNANCIAL --slack --no-file   # summary only
    python reports/report.py --demo                        # fake data, no DB/VPN needed
    python reports/report.py --print-sql performance       # print a query to paste into Beekeeper
    python reports/report.py --print-sql crm               # ...or into SSMS
    python reports/report.py --print-sql bulk              # performance data freshness (Postgres)
    python reports/report.py --print-sql streaming         # CRM streaming health (SQL Server)

Needs utils/db_connector.py (imported as utils.db_connector).
Run from the project root, where .env lives.
For --slack: uv add slack_sdk, and set SLACK_BOT_TOKEN and SLACK_CHANNEL
(use the channel ID, e.g. C0123456789, since file uploads need an ID).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make `utils.db_connector` importable when this file is run directly
# (python reports/report.py) as well as with -m (python -m reports.report).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Test tenants are excluded everywhere: in the CRM SQL (below) and from the Postgres rows.
EXCLUDED_TENANT_IDS = (52, 67, 60, 49, 65, 9)
TEST_TENANTS = {str(t) for t in EXCLUDED_TENANT_IDS}

# The four health areas every tenant has, in display order: (key, title). Only "connection" has
# data so far; the others show "Not set up" on the list and as empty sections on the tenant page.
AREAS = (
    ("connection", "Connection Health"),
    ("bulk", "Performance Data Health"),
    ("streaming", "CRM Health"),
    ("archival", "Archival Health"),
)
# Connection Health is made of two groups of integrations: (key, title).
CONNECTION_GROUPS = (("perf", "Performance"), ("crm", "CRM"), ("arch", "Archival"))
GROUP_KEYS = tuple(key for key, _ in CONNECTION_GROUPS)

# CRM Health (streaming) settings. They feed the CRM streaming query and the text on the page.
STREAM_WINDOW_DAYS = 7     # how far back to look
STREAM_SLA_HOURS = 24      # an item older than this should already have streamed
STREAM_FAIL_PCT = 10       # % of due items missed before a tenant is FAILING
STREAM_MIN_VOLUME = 10     # with fewer due items than this, a miss is DEGRADED, not FAILING

# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------
# CRM status per tenant + CRM (SQL Server). Mirrors the app's Connect/Disconnect logic.
CRM_SQL = """
WITH CRMEnabled AS (
    SELECT DISTINCT vi.TenantId, vi.Code
    FROM VendorIntegrationSettings vi WITH (NOLOCK)
    WHERE vi.IsDeleted = 0
      AND vi.Code IN ('WB', 'SF', 'PRA', 'SFV2', 'RT',
                      -- archival vendors: enabled for the tenant, but the app has no connection to check
                      'SM', 'GR', 'GRV2', 'TL', 'MW', 'MRW', 'GB')
      -- the vendor must also be active for the tenant in TenantVendors
      AND EXISTS (
          SELECT 1
          FROM TenantVendors tv WITH (NOLOCK)
          WHERE tv.TenantId = vi.TenantId
            AND tv.VendorCode = vi.Code
            AND tv.IsDeleted = 'false'
      )
),
JWT AS (
    -- JWT auth: SFV2/PRA count as connected if AuthType = 'JWTToken' and an OrganizationId setting exists
    SELECT DISTINCT vi.TenantId, vi.Code
    FROM VendorIntegrationSettings vi WITH (NOLOCK)
    INNER JOIN VendorSettingsMasters vsm WITH (NOLOCK)
        ON vsm.Id = vi.VendorSettingsMastersID AND vsm.IsDeleted = 0
    WHERE vi.IsDeleted = 0
      AND vi.Code IN ('SFV2', 'PRA')
      AND vsm.Name = 'AuthType'
      AND vi.[Value] = 'JWTToken'
      AND EXISTS (
          SELECT 1
          FROM VendorIntegrationSettings vi2 WITH (NOLOCK)
          INNER JOIN VendorSettingsMasters vsm2 WITH (NOLOCK)
              ON vsm2.Id = vi2.VendorSettingsMastersID AND vsm2.IsDeleted = 0
          WHERE vi2.IsDeleted = 0
            AND vi2.TenantId = vi.TenantId
            AND vi2.Code = vi.Code
            AND vsm2.Name = 'OrganizationId'
      )
),
Conn AS (
    SELECT
        vc.TenantId,
        vc.VendorCode,
        COUNT(*) AS LiveRows,
        SUM(CASE WHEN ISNULL(vc.AccessToken, '') <> '' THEN 1 ELSE 0 END) AS RowsWithToken,
        SUM(CASE WHEN ISNULL(vc.AccessToken, '') <> ''
                  AND vc.RefreshTokenExpireTime > GETUTCDATE() THEN 1 ELSE 0 END) AS ValidRows,
        -- a token row counts as valid if it has no expiry date set, or the expiry is still in the future (used for Redtail)
        SUM(CASE WHEN ISNULL(vc.AccessToken, '') <> ''
                  AND (vc.RefreshTokenExpireTime IS NULL OR vc.RefreshTokenExpireTime > GETUTCDATE()) THEN 1 ELSE 0 END) AS ValidOrUnsetRows,
        MAX(vc.RefreshTokenExpireTime) AS LatestRefreshExpiry
    FROM VendorConnections vc WITH (NOLOCK)
    WHERE vc.IsDeleted = 0
      AND vc.VendorCode IN ('WB', 'PRA', 'SFV2', 'RT')
    GROUP BY vc.TenantId, vc.VendorCode
)
SELECT
    td.TenantId,
    LTRIM(RTRIM(td.BusinessName)) AS BusinessName,
    ce.Code AS VendorCode,
    v.Name AS VendorName,
    COUNT(CASE WHEN ce.Code NOT IN ('SM', 'GR', 'GRV2', 'TL', 'MW', 'MRW', 'GB') THEN 1 END) OVER (PARTITION BY ce.TenantId) AS CRMsEnabled,
    CASE WHEN ce.Code IN ('SM', 'GR', 'GRV2', 'TL', 'MW', 'MRW', 'GB') THEN 'archival' ELSE 'crm' END AS Kind,
    CASE
        WHEN ce.Code IN ('SM', 'GR', 'GRV2', 'TL', 'MW', 'MRW', 'GB') THEN 'Archival - enabled, not verified'
        WHEN ce.Code = 'SF' THEN 'Legacy - UI always shows Connected'
        WHEN j.TenantId IS NOT NULL THEN 'Connected (JWT)'
        -- Redtail: the app only checks that a token exists, but this report also checks the refresh token expiry
        WHEN ce.Code = 'RT' AND c.ValidOrUnsetRows > 0 THEN 'Connected'
        WHEN ce.Code IN ('WB', 'SFV2', 'PRA') AND c.ValidRows > 0 THEN 'Connected'
        WHEN c.TenantId IS NULL THEN 'Not connected - no connection row'
        WHEN c.LatestRefreshExpiry <= GETUTCDATE() THEN 'Not connected - refresh token expired'
        ELSE 'Not connected - no access token'
    END AS Status,
    c.LatestRefreshExpiry
FROM CRMEnabled ce
INNER JOIN TenantDetail td WITH (NOLOCK)
    ON td.TenantId = ce.TenantId AND td.IsDeleted = 0
INNER JOIN Vendors v WITH (NOLOCK)
    ON v.Code = ce.Code AND v.IsDeleted = 0
LEFT JOIN JWT j
    ON j.TenantId = ce.TenantId AND j.Code = ce.Code
LEFT JOIN Conn c
    ON c.TenantId = ce.TenantId AND c.VendorCode = ce.Code
WHERE td.TenantId NOT IN (__EXCLUDED_TENANTS__)
ORDER BY
    CASE WHEN c.TenantId IS NULL THEN 0 ELSE 1 END,
    c.LatestRefreshExpiry,
    td.TenantId;
"""

CRM_SQL = CRM_SQL.replace("__EXCLUDED_TENANTS__", ", ".join(str(t) for t in EXCLUDED_TENANT_IDS))

# Performance vendor status per tenant + vendor (PostgreSQL).
# Output tenant_id is tenant_mdm.tenant_id, i.e. TenantDetail.TenantId in SQL Server.
PERFORMANCE_SQL = """
-- One row per tenant + vendor that is MAPPED to the tenant.
-- Connected = at least one ADMIN user has a connected token for that vendor.
-- Anything else is Disconnected, including mapped vendors with no token at all.

WITH mapped AS (
    -- Which vendors each tenant is set up for (DISTINCT guards against duplicate mapping rows)
    SELECT DISTINCT tvm.tenant_id, tvm.vendor_id
    FROM tenant_vendor_mapping tvm
    WHERE tvm.is_active = 'true'   -- only active mappings; inactive ones are old duplicates
),
latest_tokens AS (
    -- Newest active, non-empty token per tenant + vendor + user (same logic as before)
    SELECT DISTINCT ON (t.tenant_id, t.vendor_id, t.user_id)
        t.tenant_id,
        t.vendor_id,
        t.user_id,
        t.is_connected
    FROM token_mgm t
    WHERE t.is_active = 'true'
      AND t.access_token != ''
    ORDER BY t.tenant_id, t.vendor_id, t.user_id, t.created_at DESC
),
admin_status AS (
    -- Roll the admin tokens up to one row per tenant + vendor
    SELECT
        lt.tenant_id,
        lt.vendor_id,
        COUNT(*)                                                                    AS admin_token_count,
        COUNT(*) FILTER (WHERE lt.is_connected)                                     AS connected_admins,
        STRING_AGG(u.email, ', ' ORDER BY u.email) FILTER (WHERE lt.is_connected)   AS connected_by
    FROM latest_tokens lt
    JOIN user_master u ON u.user_id::text = lt.user_id::text
    JOIN role_master r ON r.id = u.role_id
    WHERE r.role_name = 'ADMIN'
      AND LOWER(COALESCE(u.email, '')) NOT IN ('finology@yopmail.com', 'barryco2007@gmail.com')
    GROUP BY lt.tenant_id, lt.vendor_id
)
SELECT
    tm.tenant_id                                     AS tenant_id,    -- same value as TenantDetail.TenantId in SQL Server
    tm.tenancy_name                                  AS tenant_name,
    v.vendor_name,
    v.vendor_key,                                    -- = TenantVendors.VendorCode in SQL Server
    CASE
        WHEN COALESCE(a.connected_admins, 0) > 0     THEN 'Connected'
        WHEN COALESCE(a.admin_token_count, 0) > 0    THEN 'Disconnected - admin token not connected'
        ELSE                                              'Disconnected - no admin token'
    END                                              AS status,
    COALESCE(a.connected_admins, 0)                  AS connected_admins,
    a.connected_by
FROM mapped m
JOIN tenant_mdm tm ON tm.id = m.tenant_id
JOIN vendor_mdm v  ON v.id  = m.vendor_id
LEFT JOIN admin_status a
       ON a.tenant_id = m.tenant_id
      AND a.vendor_id = m.vendor_id
WHERE v.vendor_name IN ('Addepar', 'BlackDiamond', 'Orion', 'Emoney', 'Tamarac')
ORDER BY
    CASE WHEN COALESCE(a.connected_admins, 0) > 0 THEN 1 ELSE 0 END,   -- disconnected first
    tm.tenancy_name,
    v.vendor_name;
"""

# Every active tenant. The report lists all of them (even with no CRM or performance vendor),
# and connector tenants are matched against this list.
TENANT_NAMES_SQL = """
SELECT td.TenantId, LTRIM(RTRIM(td.BusinessName)) AS BusinessName
FROM TenantDetail td WITH (NOLOCK)
WHERE td.IsDeleted = 0
  AND td.TenantId NOT IN (__EXCLUDED_TENANTS__)
"""
TENANT_NAMES_SQL = TENANT_NAMES_SQL.replace("__EXCLUDED_TENANTS__", ", ".join(str(t) for t in EXCLUDED_TENANT_IDS))


# Which vendors are active for each tenant. TenantVendors.VendorCode is the same value as
# vendor_mdm.vendor_key in Postgres, which is how performance vendors are matched to it.
ACTIVE_TENANT_VENDORS_SQL = """
SELECT tv.TenantId, tv.VendorCode
FROM TenantVendors tv WITH (NOLOCK)
WHERE tv.IsDeleted = 'false'
"""

# Bulk data freshness per monitored tenant + vendor + table (PostgreSQL connector database).
BULK_FRESHNESS_SQL = """
-- Performance data freshness (PostgreSQL, connector database): one row per monitored tenant + vendor + table.
-- Run this against the CONNECTOR database, not the SQL Server one.
--
-- Monitored for now: Orion and BlackDiamond, minus the test tenants (see "scope").
-- Only active mappings count (tenant_vendor_mapping.is_active = 'true').
-- Skipped tables:
--   Orion        fact_transaction (pulled at runtime, no longer stored), dim_client (Orion uses households)
--   BlackDiamond dim_account_group, dim_account_group_account
-- Extra for Orion: its three performance reports in fact_vendor_client_reports (TWR, PerformanceSummary,
--   AssetAllocation), matched on the mapping's tenant_id and report_type, using updated_at.
--
-- Loads run every day, so the expected load date is simply:
--   today (New York) once the 10:30 cutoff has passed, otherwise yesterday.
--
-- Status per table:
--   UP TO DATE  the expected day's load has landed
--   LATE        it is one day behind
--   STALE       it is two or more days behind
--   NO DATA     the table has no active rows for this tenant + vendor
--
-- Assumes connector_updated_at is timestamptz. If it is a plain timestamp stored in UTC, use the
-- alternative expression in the "dated" section.

WITH scope AS (
    -- The tenant + vendor pairs being monitored. Add vendor names or tenant ids here to change it.
    -- tm.tenant_id is the app's TenantId (the same value as TenantDetail.TenantId in SQL Server).
    SELECT tvm.id, tm.tenant_id, tm.tenancy_name, v.vendor_name, v.vendor_key,
           tvm.tenant_id AS connector_tenant_id   -- what fact_vendor_client_reports.tenant_id matches
    FROM tenant_vendor_mapping tvm
    JOIN vendor_mdm v  ON v.id  = tvm.vendor_id
    JOIN tenant_mdm tm ON tm.id = tvm.tenant_id
    WHERE tvm.is_active = 'true'   -- only active mappings; inactive ones are old duplicates
      AND v.vendor_name IN ('Orion', 'BlackDiamond')
      AND tm.tenant_id NOT IN (__EXCLUDED_TENANTS__)   -- the test tenants (EXCLUDED_TENANT_IDS)
),
cut AS (
    SELECT CASE WHEN CAST(n.ny_now AS time) >= TIME '10:30'
                THEN CAST(n.ny_now AS date)
                ELSE CAST(n.ny_now AS date) - 1
           END AS expected_day
    FROM (SELECT now() AT TIME ZONE 'America/New_York' AS ny_now) n
),
table_freshness AS (
    -- Each branch starts from the scope, so a table with no rows still shows up (as NO DATA).
    SELECT s.id AS tenant_vendor, 'dim_household' AS table_name,
           MAX(x.connector_updated_at)    AS last_updated,
           BOOL_OR(x.connector_is_active) AS has_active
    FROM scope s
    LEFT JOIN dim_household x ON x.tenant_vendor = s.id
    GROUP BY s.id
    UNION ALL

    SELECT s.id AS tenant_vendor, 'dim_client' AS table_name,
           MAX(x.connector_updated_at)    AS last_updated,
           BOOL_OR(x.connector_is_active) AS has_active
    FROM scope s
    LEFT JOIN dim_client x ON x.tenant_vendor = s.id
    WHERE s.vendor_name <> 'Orion'   -- Orion tenants use households, so dim_client has no data to check
    GROUP BY s.id
    UNION ALL

    SELECT s.id AS tenant_vendor, 'dim_account' AS table_name,
           MAX(x.connector_updated_at)    AS last_updated,
           BOOL_OR(x.connector_is_active) AS has_active
    FROM scope s
    LEFT JOIN dim_account x ON x.tenant_vendor = s.id
    GROUP BY s.id
    UNION ALL

    SELECT s.id AS tenant_vendor, 'dim_security' AS table_name,
           MAX(x.connector_updated_at)    AS last_updated,
           BOOL_OR(x.connector_is_active) AS has_active
    FROM scope s
    LEFT JOIN dim_security x ON x.tenant_vendor = s.id
    GROUP BY s.id
    UNION ALL

    SELECT s.id AS tenant_vendor, 'fact_holding' AS table_name,
           MAX(x.connector_updated_at)    AS last_updated,
           BOOL_OR(x.connector_is_active) AS has_active
    FROM scope s
    LEFT JOIN fact_holding x ON x.tenant_vendor = s.id
    GROUP BY s.id
    UNION ALL

    SELECT s.id AS tenant_vendor, 'fact_transaction' AS table_name,
           MAX(x.connector_updated_at)    AS last_updated,
           BOOL_OR(x.connector_is_active) AS has_active
    FROM scope s
    LEFT JOIN fact_transaction x ON x.tenant_vendor = s.id
    WHERE s.vendor_name <> 'Orion'   -- Orion transactions are pulled at runtime, not stored, so there is nothing to check
    GROUP BY s.id
    UNION ALL

    SELECT s.id AS tenant_vendor, 'dim_account_ownership' AS table_name,
           MAX(x.connector_updated_at)    AS last_updated,
           BOOL_OR(x.connector_is_active) AS has_active
    FROM scope s
    LEFT JOIN dim_account_ownership x ON x.tenant_vendor = s.id
    GROUP BY s.id
    UNION ALL

    SELECT s.id AS tenant_vendor, 'dim_account_group' AS table_name,
           MAX(x.connector_updated_at)    AS last_updated,
           BOOL_OR(x.connector_is_active) AS has_active
    FROM scope s
    LEFT JOIN dim_account_group x ON x.tenant_vendor = s.id
    WHERE s.vendor_name <> 'BlackDiamond'   -- not monitored for BlackDiamond
    GROUP BY s.id
    UNION ALL

    SELECT s.id AS tenant_vendor, 'dim_account_group_account' AS table_name,
           MAX(x.connector_updated_at)    AS last_updated,
           BOOL_OR(x.connector_is_active) AS has_active
    FROM scope s
    LEFT JOIN dim_account_group_account x ON x.tenant_vendor = s.id
    WHERE s.vendor_name <> 'BlackDiamond'   -- not monitored for BlackDiamond
    GROUP BY s.id

    UNION ALL

    -- Orion's performance cards live in fact_vendor_client_reports, keyed by the connector tenant id and
    -- report_type (not by tenant_vendor). Freshness is the latest updated_at for each report type.
    SELECT s.id AS tenant_vendor, 'fact_vendor_client_reports (TWR)' AS table_name,
           MAX(r.updated_at)      AS last_updated,
           COUNT(r.tenant_id) > 0 AS has_active
    FROM scope s
    LEFT JOIN fact_vendor_client_reports r
           ON r.tenant_id = s.connector_tenant_id AND r.report_type = 'TWR'
    WHERE s.vendor_name = 'Orion'
    GROUP BY s.id

    UNION ALL

    SELECT s.id AS tenant_vendor, 'fact_vendor_client_reports (PerformanceSummary)' AS table_name,
           MAX(r.updated_at)      AS last_updated,
           COUNT(r.tenant_id) > 0 AS has_active
    FROM scope s
    LEFT JOIN fact_vendor_client_reports r
           ON r.tenant_id = s.connector_tenant_id AND r.report_type = 'PerformanceSummary'
    WHERE s.vendor_name = 'Orion'
    GROUP BY s.id

    UNION ALL

    SELECT s.id AS tenant_vendor, 'fact_vendor_client_reports (AssetAllocation)' AS table_name,
           MAX(r.updated_at)      AS last_updated,
           COUNT(r.tenant_id) > 0 AS has_active
    FROM scope s
    LEFT JOIN fact_vendor_client_reports r
           ON r.tenant_id = s.connector_tenant_id AND r.report_type = 'AssetAllocation'
    WHERE s.vendor_name = 'Orion'
    GROUP BY s.id
),
dated AS (
    SELECT tf.*,
           -- the calendar day the data landed, in New York time
           CAST(tf.last_updated AT TIME ZONE 'America/New_York' AS date) AS last_day
           -- plain timestamp stored in UTC instead:
           -- CAST(tf.last_updated AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York' AS date) AS last_day
    FROM table_freshness tf
)

SELECT
    s.tenant_id                          AS tenant_id,
    s.tenancy_name                       AS tenant_name,
    s.vendor_name,
    s.vendor_key,
    d.table_name,
    d.last_updated,
    CASE
        WHEN d.last_updated IS NULL OR NOT COALESCE(d.has_active, false) THEN 'NO DATA'
        WHEN d.last_day >= c.expected_day                                THEN 'UP TO DATE'
        WHEN d.last_day >= c.expected_day - 1                            THEN 'LATE'
        ELSE                                                                  'STALE'
    END                                  AS data_freshness,
    CASE
        WHEN d.last_updated IS NULL OR NOT COALESCE(d.has_active, false) THEN NULL
        ELSE GREATEST(c.expected_day - d.last_day, 0)
    END                                  AS days_behind
FROM dated d
JOIN scope s ON s.id = d.tenant_vendor
CROSS JOIN cut c
ORDER BY
    s.tenancy_name,
    d.table_name;
"""
BULK_FRESHNESS_SQL = BULK_FRESHNESS_SQL.replace("__EXCLUDED_TENANTS__", ", ".join(str(t) for t in EXCLUDED_TENANT_IDS))

# CRM streaming health per tenant (SQL Server): do chats and feed posts reach the CRM activity stream?
CRM_STREAMING_SQL = """
-- CRM streaming health, one row per tenant (SQL Server). Single SELECT, no session settings.
--
-- The window, SLA and thresholds are set by the STREAM_* constants in report.py and shown in the cfg block:
--   window_start  how far back to look
--   sla_cutoff    an item older than this should already have streamed
--   fail_pct      % of due items missed before a tenant is FAILING
--   min_volume    with fewer due items than this, a miss is DEGRADED instead of FAILING
-- Times are UTC (the same clock as the CRM connection query). If CreationTime is stored in server
-- local time, swap GETUTCDATE() for GETDATE() in cfg.
--
-- CrmStreaming:
--   NOT CONFIGURED  no CRM vendor, or both push-to-activity-stream flags are off
--   NO ACTIVITY     configured, but nothing eligible and no stream rows in the window
--   FAILING         >= fail_pct of due stream rows are still unprocessed, or >= fail_pct of due chats/feeds
--                   never streamed (only once there are at least min_volume due items)
--   DEGRADED        any smaller miss: a stuck row, a row out of retries, or a chat/feed that never streamed
--   HEALTHY         otherwise

WITH cfg AS (
    SELECT DATEADD(DAY, -__WINDOW_DAYS__, GETUTCDATE()) AS window_start,
           DATEADD(HOUR, -__SLA_HOURS__, GETUTCDATE()) AS sla_cutoff,
           __FAIL_PCT__ AS fail_pct,
           __MIN_VOLUME__ AS min_volume
),
CrmTenants AS (
    -- tenants with a CRM vendor (type 5), from either TenantVendors or VendorIntegrationSettings
    SELECT DISTINCT src.TenantId
    FROM (
        SELECT tv.TenantId, tv.VendorCode FROM TenantVendors tv WITH (NOLOCK) WHERE tv.IsDeleted = 0
        UNION
        SELECT vis.TenantId, vis.Code FROM VendorIntegrationSettings vis WITH (NOLOCK) WHERE vis.IsDeleted = 0
    ) src
    INNER JOIN Vendors v WITH (NOLOCK)
        ON v.Code = src.VendorCode AND v.IsDeleted = 0 AND v.Type = 5
),
PushFlags AS (
    SELECT
        TenantId,
        MAX(CASE WHEN Name = 'App.PushToActivityStream.Chat' AND Value = 'true' THEN 1 ELSE 0 END) AS PushChat,
        MAX(CASE WHEN Name = 'App.PushToActivityStream.Post' AND Value = 'true' THEN 1 ELSE 0 END) AS PushPost
    FROM AbpFeatures WITH (NOLOCK)
    WHERE Name IN ('App.PushToActivityStream.Chat', 'App.PushToActivityStream.Post')
    GROUP BY TenantId
),
ChatRows AS (
    -- every chat message row that should stream: a CRM user is the sender or the receiver,
    -- and whether that row has a stream record
    SELECT
        cm.TenantId,
        cm.SharedMessageId,
        cm.CreationTime,
        CASE WHEN EXISTS (
                SELECT 1 FROM CrmActivityStreams cas WITH (NOLOCK)
                WHERE cas.TransactionId   = CAST(cm.Id AS NVARCHAR(100))
                  AND cas.TenantId        = cm.TenantId
                  AND cas.IsDeleted       = 0
                  AND cas.TransactionType = 'chat'
            ) THEN 1 ELSE 0 END AS Streamed
    FROM AppChatMessages cm WITH (NOLOCK)
    CROSS JOIN cfg
    WHERE cm.Side = 2
      AND cm.CreationTime >= cfg.window_start
      AND (
          EXISTS (
              SELECT 1 FROM CRMUsers crmu WITH (NOLOCK)
              WHERE crmu.AppId = cm.UserId AND crmu.TenantId = cm.TenantId AND crmu.IsDeleted = 0
          )
          OR EXISTS (
              SELECT 1 FROM CRMUsers crmu WITH (NOLOCK)
              WHERE crmu.AppId = cm.TargetUserId AND crmu.TenantId = cm.TenantId AND crmu.IsDeleted = 0
          )
      )
),
ChatItems AS (
    -- one row per shared chat message; it counts as streamed if any of its rows has a stream record
    SELECT
        TenantId,
        SharedMessageId,
        MIN(CreationTime) AS FirstCreated,
        MAX(Streamed)     AS Streamed
    FROM ChatRows
    GROUP BY TenantId, SharedMessageId
),
ChatStats AS (
    SELECT
        ci.TenantId,
        COUNT(*)                                                                                   AS ChatEligible,
        SUM(ci.Streamed)                                                                           AS ChatStreamed,
        SUM(CASE WHEN ci.FirstCreated <= cfg.sla_cutoff THEN 1 ELSE 0 END)                         AS ChatDue,
        SUM(CASE WHEN ci.Streamed = 0 AND ci.FirstCreated <= cfg.sla_cutoff THEN 1 ELSE 0 END)     AS ChatMissing
    FROM ChatItems ci
    CROSS JOIN cfg
    GROUP BY ci.TenantId
),
FeedItems AS (
    -- one row per feed post for a CRM user (ForUserId), and whether it has a stream record
    SELECT
        fd.TenantId,
        fd.CreationTime,
        CASE WHEN EXISTS (
                SELECT 1 FROM CrmActivityStreams cas WITH (NOLOCK)
                WHERE cas.TransactionId = CAST(fd.Id AS NVARCHAR(100))
                  AND cas.TenantId      = fd.TenantId
                  AND cas.IsDeleted     = 0
            ) THEN 1 ELSE 0 END AS Streamed
    FROM FeedDetail fd WITH (NOLOCK)
    CROSS JOIN cfg
    WHERE fd.IsDeleted = 0
      AND fd.CreationTime >= cfg.window_start
      AND EXISTS (
          SELECT 1 FROM CRMUsers crmu WITH (NOLOCK)
          WHERE crmu.AppId = fd.ForUserId AND crmu.TenantId = fd.TenantId AND crmu.IsDeleted = 0
      )
),
FeedStats AS (
    SELECT
        fi.TenantId,
        COUNT(*)                                                                                   AS FeedEligible,
        SUM(fi.Streamed)                                                                           AS FeedStreamed,
        SUM(CASE WHEN fi.CreationTime <= cfg.sla_cutoff THEN 1 ELSE 0 END)                         AS FeedDue,
        SUM(CASE WHEN fi.Streamed = 0 AND fi.CreationTime <= cfg.sla_cutoff THEN 1 ELSE 0 END)     AS FeedMissing
    FROM FeedItems fi
    CROSS JOIN cfg
    GROUP BY fi.TenantId
),
StreamStats AS (
    -- the stream records themselves: how many are processed, out of retries, or stuck past the SLA
    SELECT
        cas.TenantId,
        COUNT(*)                                                                                   AS StreamRows,
        SUM(CASE WHEN cas.IsProcessed = 1 THEN 1 ELSE 0 END)                                       AS Processed,
        SUM(CASE WHEN cas.IsProcessed = 0 AND cas.TryAttempts >= 3 THEN 1 ELSE 0 END)              AS Failed,
        SUM(CASE WHEN cas.CreationTime <= cfg.sla_cutoff THEN 1 ELSE 0 END)                        AS StreamDue,
        SUM(CASE WHEN cas.IsProcessed = 0 AND cas.CreationTime <= cfg.sla_cutoff THEN 1 ELSE 0 END) AS Stuck,
        MIN(CASE WHEN cas.IsProcessed = 0 THEN cas.CreationTime END)                               AS OldestUnprocessed,
        MAX(cas.CreationTime)                                                                      AS LastStreamCreated
    FROM CrmActivityStreams cas WITH (NOLOCK)
    CROSS JOIN cfg
    WHERE cas.IsDeleted = 0
      AND cas.CreationTime >= cfg.window_start
    GROUP BY cas.TenantId
),
TenantStats AS (
    SELECT
        td.TenantId,
        LTRIM(RTRIM(td.BusinessName))                         AS BusinessName,
        CASE WHEN ct.TenantId IS NOT NULL THEN 1 ELSE 0 END   AS HasCRM,
        COALESCE(pf.PushChat, 0)                              AS PushChat,
        COALESCE(pf.PushPost, 0)                              AS PushPost,
        COALESCE(cs.ChatEligible, 0)                          AS ChatEligible,
        COALESCE(cs.ChatStreamed, 0)                          AS ChatStreamed,
        COALESCE(cs.ChatDue, 0)                               AS ChatDue,
        COALESCE(cs.ChatMissing, 0)                           AS ChatMissing,
        COALESCE(fs.FeedEligible, 0)                          AS FeedEligible,
        COALESCE(fs.FeedStreamed, 0)                          AS FeedStreamed,
        COALESCE(fs.FeedDue, 0)                               AS FeedDue,
        COALESCE(fs.FeedMissing, 0)                           AS FeedMissing,
        COALESCE(ss.StreamRows, 0)                            AS StreamRows,
        COALESCE(ss.Processed, 0)                             AS Processed,
        COALESCE(ss.Failed, 0)                                AS Failed,
        COALESCE(ss.StreamDue, 0)                             AS StreamDue,
        COALESCE(ss.Stuck, 0)                                 AS Stuck,
        ss.OldestUnprocessed,
        ss.LastStreamCreated
    FROM TenantDetail td WITH (NOLOCK)
    LEFT JOIN CrmTenants ct  ON ct.TenantId = td.TenantId
    LEFT JOIN PushFlags pf   ON pf.TenantId = td.TenantId
    LEFT JOIN ChatStats cs   ON cs.TenantId = td.TenantId
    LEFT JOIN FeedStats fs   ON fs.TenantId = td.TenantId
    LEFT JOIN StreamStats ss ON ss.TenantId = td.TenantId
    WHERE td.IsDeleted = 0
      AND td.TenantId NOT IN (__EXCLUDED_TENANTS__)
),
Scored AS (
    -- only count chats / feeds for the push flags that are switched on
    SELECT
        t.*,
        CASE WHEN t.PushChat = 1 THEN t.ChatMissing ELSE 0 END
          + CASE WHEN t.PushPost = 1 THEN t.FeedMissing ELSE 0 END AS MissingTotal,
        CASE WHEN t.PushChat = 1 THEN t.ChatDue ELSE 0 END
          + CASE WHEN t.PushPost = 1 THEN t.FeedDue ELSE 0 END     AS EligibleDue
    FROM TenantStats t
),
Rated AS (
    SELECT
        s.*,
        CASE
            WHEN s.HasCRM = 0 OR (s.PushChat = 0 AND s.PushPost = 0)                       THEN 'NOT CONFIGURED'
            WHEN s.ChatEligible + s.FeedEligible + s.StreamRows = 0                        THEN 'NO ACTIVITY'
            WHEN (s.StreamDue    >= cfg.min_volume AND 100.0 * s.Stuck        / NULLIF(s.StreamDue, 0)    >= cfg.fail_pct)
              OR (s.EligibleDue  >= cfg.min_volume AND 100.0 * s.MissingTotal / NULLIF(s.EligibleDue, 0) >= cfg.fail_pct)
                                                                                           THEN 'FAILING'
            WHEN s.Failed > 0 OR s.Stuck > 0 OR s.MissingTotal > 0                         THEN 'DEGRADED'
            ELSE                                                                                'HEALTHY'
        END AS CrmStreaming
    FROM Scored s
    CROSS JOIN cfg
)
SELECT
    r.TenantId,
    r.BusinessName                                                                       AS Tenant,
    r.CrmStreaming,
    r.HasCRM,
    r.PushChat,
    r.PushPost,
    r.ChatEligible,
    r.ChatStreamed,
    r.ChatMissing,
    r.FeedEligible,
    r.FeedStreamed,
    r.FeedMissing,
    r.StreamRows,
    r.Processed,
    r.Failed,
    r.Stuck,
    COALESCE(CAST(100.0 * r.Stuck        / NULLIF(r.StreamDue, 0)    AS DECIMAL(5,2)), 0) AS SlaMissPct,
    COALESCE(CAST(100.0 * r.MissingTotal / NULLIF(r.EligibleDue, 0)  AS DECIMAL(5,2)), 0) AS MissingPct,
    r.OldestUnprocessed,
    r.LastStreamCreated
FROM Rated r
ORDER BY
    CASE r.CrmStreaming
        WHEN 'FAILING' THEN 1 WHEN 'DEGRADED' THEN 2 WHEN 'NO ACTIVITY' THEN 3
        WHEN 'HEALTHY' THEN 4 ELSE 5
    END,
    r.BusinessName;
"""
CRM_STREAMING_SQL = (
    CRM_STREAMING_SQL.replace("__EXCLUDED_TENANTS__", ", ".join(str(t) for t in EXCLUDED_TENANT_IDS))
    .replace("__WINDOW_DAYS__", str(STREAM_WINDOW_DAYS))
    .replace("__SLA_HOURS__", str(STREAM_SLA_HOURS))
    .replace("__FAIL_PCT__", str(STREAM_FAIL_PCT))
    .replace("__MIN_VOLUME__", str(STREAM_MIN_VOLUME))
)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def fetch_crm(env: str) -> tuple[list[dict], dict[str, str]]:
    """CRM status rows + {TenantId: BusinessName} for every active tenant (SQL Server)."""
    from utils.db_connector import DBConnector

    db = DBConnector(env, "SQL")
    print(f"[{env}/SQL] running CRM status query ...")
    crm_rows = db.fetch_all(CRM_SQL)
    names = {
        str(r["TenantId"]): (r["BusinessName"] or "").strip()
        for r in db.fetch_all(TENANT_NAMES_SQL)
    }
    return crm_rows, names


def fetch_tenant_vendors(env: str) -> list[dict]:
    """Which vendors are active for each tenant (SQL Server TenantVendors)."""
    from utils.db_connector import DBConnector

    db = DBConnector(env, "SQL")
    print(f"[{env}/SQL] reading TenantVendors ...")
    return db.fetch_all(ACTIVE_TENANT_VENDORS_SQL)


def fetch_bulk(env: str) -> list[dict]:
    """Performance data freshness per monitored tenant + vendor + table (PostgreSQL connector database)."""
    from utils.db_connector import DBConnector

    db = DBConnector(env, "CONNECTOR")
    print(f"[{env}/CONNECTOR] running performance data freshness query ...")
    return db.fetch_all(BULK_FRESHNESS_SQL)


def fetch_stream(env: str) -> list[dict]:
    """CRM streaming health per tenant (SQL Server)."""
    from utils.db_connector import DBConnector

    db = DBConnector(env, "SQL")
    print(f"[{env}/SQL] running CRM streaming health query ...")
    return db.fetch_all(CRM_STREAMING_SQL)


def fetch_performance(env: str) -> list[dict]:
    """Performance vendor status rows (PostgreSQL), from connector_vendor_status.sql."""
    from utils.db_connector import DBConnector

    db = DBConnector(env, "CONNECTOR")
    print(f"[{env}/CONNECTOR] running performance vendor status query ...")
    return db.fetch_all(PERFORMANCE_SQL)


# --------------------------------------------------------------------------
# CRM row shaping (SQL Server rows -> compact records)
# --------------------------------------------------------------------------
def classify(status: str) -> str:
    if status.startswith("Connected"):
        return "connected"
    if status.startswith(("Legacy", "Archival")):
        return "legacy"
    return "disconnected"


def prepare_crm(rows: list[dict]) -> list[dict]:
    """Turn raw DB rows into the compact records the HTML report uses."""
    out = []
    for r in rows:
        status = r["Status"]
        state = classify(status)
        reason = status.split(" - ", 1)[1] if state == "disconnected" and " - " in status else ""
        exp = r.get("LatestRefreshExpiry")
        if exp is not None:
            # DB values are naive UTC; mark them as UTC for the browser
            exp = exp.isoformat() + ("Z" if exp.tzinfo is None else "")
        out.append(
            {
                "tenantId": str(r["TenantId"]),
                "tenant": (r["BusinessName"] or f"Tenant {r['TenantId']}").strip(),
                "code": r["VendorCode"],
                "crm": r["VendorName"],
                "kind": r.get("Kind") or "crm",
                "enabled": int(r["CRMsEnabled"]),
                "status": status,
                "state": state,
                "reason": reason,
                "expiry": exp,
            }
        )
    return out


def demo_crm_rows() -> list[dict]:
    """Fake rows so the report design can be previewed without a DB or VPN."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def d(days):
        return now + timedelta(days=days)

    s = "Not connected - "
    spec = [
        (101, "Northwind Wealth Advisors", "SFV2", "Salesforce", 1, "Connected", d(42)),
        (102, "Harbor & Pine Financial", "WB", "Wealthbox", 1, "Connected", d(12)),
        (103, "Summit Ridge Partners", "RT", "Redtail", 1, s + "refresh token expired", d(-200)),
        (104, "Bluebird Capital Group", "SFV2", "Salesforce", 2, s + "refresh token expired", d(-165)),
        (104, "Bluebird Capital Group", "RT", "Redtail", 2, "Connected", d(25)),
        (105, "Cedar Lane Advisory", "PRA", "Practifi", 1, "Connected (JWT)", None),
        (106, "Oakmont Family Office", "WB", "Wealthbox", 1, s + "no connection row", None),
        (107, "Lighthouse Retirement", "SF", "Salesforce (Legacy)", 1, "Legacy - UI always shows Connected", None),
        (108, "Granite Peak Investments", "PRA", "Practifi", 1, s + "refresh token expired", d(-9)),
        (109, "Willow Creek Wealth", "SFV2", "Salesforce", 1, "Connected", d(88)),
        (110, "Silverline Advisors", "RT", "Redtail", 1, s + "no access token", None),
        (111, "Maple & Stone Financial", "WB", "Wealthbox", 1, "Connected", d(5)),
        (101, "Northwind Wealth Advisors", "SM", "Smarsh", 1, "Archival - enabled, not verified", None),
        (105, "Cedar Lane Advisory", "GRV2", "Global Relay V2", 1, "Archival - enabled, not verified", None),
        (142, "Brightwater Financial", "TL", "Theta Lake", 0, "Archival - enabled, not verified", None),
    ]
    return [
        {
            "TenantId": t, "BusinessName": n, "VendorCode": c, "VendorName": v,
            "CRMsEnabled": e, "Status": st, "LatestRefreshExpiry": x,
            "Kind": "archival" if c in ("SM", "GR", "GRV2", "TL", "MW", "MRW", "GB") else "crm",
        }
        for t, n, c, v, e, st, x in spec
    ]


# --------------------------------------------------------------------------
# Shaping and merging
# --------------------------------------------------------------------------
def prepare_performance(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        status = r["status"] or ""
        state = "connected" if status.startswith("Connected") else "disconnected"
        reason = status.split(" - ", 1)[1] if state == "disconnected" and " - " in status else ""
        tid = r["tenant_id"]
        out.append(
            {
                "tenantId": None if tid is None else str(tid),
                "tenant": (r["tenant_name"] or "").strip(),
                "name": r["vendor_name"],
                "key": (r.get("vendor_key") or "").strip(),
                "state": state,
                "reason": reason,
                "status": status,
                "by": r.get("connected_by") or "",
                "admins": int(r.get("connected_admins") or 0),
            }
        )
    return out


def apply_tenant_vendors(
    perf: list[dict], app_names: dict[str, str], active_rows: list[dict]
) -> tuple[list[dict], list[str], list[str]]:
    """Keep a performance row only if TenantVendors has an active row for that tenant + vendor.

    Matched on vendor_mdm.vendor_key = TenantVendors.VendorCode (case-insensitive). Rows are kept,
    not dropped, when the check can't be evaluated: tenants missing from the app database (they are
    flagged in the report instead) and rows with no vendor_key.
    Returns (kept_rows, notes, info). Notes go in the report banner and the console; info is console only.
    """
    if not active_rows:  # almost certainly a problem (permissions, wrong database), not "no vendors anywhere"
        return list(perf), ["TenantVendors returned no active rows, so the performance vendor check was skipped."], []

    active = {(str(r["TenantId"]), str(r["VendorCode"]).strip().lower()) for r in active_rows}

    kept: list[dict] = []
    dropped, evaluated, passed = Counter(), Counter(), Counter()
    key_of: dict[str, str] = {}
    no_key = 0
    for r in perf:
        tid, name, key = r["tenantId"], r["name"], (r.get("key") or "").strip().lower()
        if tid is None or tid not in app_names:
            kept.append(r)
            continue
        if not key:
            no_key += 1
            kept.append(r)
            continue
        key_of.setdefault(name, key)
        evaluated[name] += 1
        if (tid, key) in active:
            kept.append(r)
            passed[name] += 1
        else:
            dropped[name] += 1

    notes: list[str] = []
    info: list[str] = []
    if no_key:
        notes.append(f"TenantVendors check skipped for {no_key} performance row(s) with no vendor_key.")
    if dropped:
        info.append(
            f"TenantVendors check left out {sum(dropped.values())} performance row(s) with no active tenant + vendor row: "
            + ", ".join(f"{n} {c}" for n, c in sorted(dropped.items()))
        )
    for n in sorted(evaluated):
        if not passed[n]:
            notes.append(
                f"Every {n} row ({evaluated[n]}) was left out because TenantVendors has no active row with "
                f"VendorCode '{key_of[n]}'. If that looks wrong, check that vendor_key matches TenantVendors.VendorCode."
            )
    return kept, notes, info


# Friendly names for the bulk data tables and report cards. The query still returns the raw names;
# a table that is not listed here simply shows under its raw name.
BULK_LABELS = {
    "dim_household": "Households",
    "dim_client": "Clients",
    "dim_account": "Accounts",
    "dim_security": "Securities",
    "fact_holding": "Holdings",
    "fact_transaction": "Transactions",
    "dim_account_ownership": "Account Ownerships",
    "dim_account_group": "Account Groups",
    "dim_account_group_account": "Account Group Accounts",
    "fact_vendor_client_reports (TWR)": "TWR Card",
    "fact_vendor_client_reports (PerformanceSummary)": "Performance Summary Card",
    "fact_vendor_client_reports (AssetAllocation)": "Asset Allocation Card",
}


# Bulk data comes in two kinds, shown as separate sections inside Bulk Data Health when a vendor has both
# (today only Orion does). Anything not listed here counts as portfolio data.
BULK_SECTIONS = {
    "fact_vendor_client_reports (TWR)": "Card data",
    "fact_vendor_client_reports (PerformanceSummary)": "Card data",
    "fact_vendor_client_reports (AssetAllocation)": "Card data",
}
BULK_DEFAULT_SECTION = "Portfolio data"


def prepare_bulk(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        last = r.get("last_updated")
        if last is not None:
            last = last.isoformat() + ("Z" if last.tzinfo is None else "")
        tid = r["tenant_id"]
        out.append(
            {
                "tenantId": None if tid is None else str(tid),
                "tenant": (r["tenant_name"] or "").strip(),
                "vendor": r["vendor_name"],
                "table": r["table_name"],
                "label": BULK_LABELS.get(r["table_name"], r["table_name"]),
                "section": BULK_SECTIONS.get(r["table_name"], BULK_DEFAULT_SECTION),
                "status": r["data_freshness"],
                "last": last,
                "behind": r.get("days_behind"),
            }
        )
    return out


def bulk_area(t: dict) -> dict:
    """Roll a tenant's per-table freshness up to one Performance Data Health status.

    Failing: any table is STALE. Degraded: any table is LATE, or some (not all) tables have NO DATA.
    No data: no table has any rows. Healthy: every table is UP TO DATE.
    """
    rows = t["bulk"]
    if not rows:
        return {"state": "notset", "issues": 0}
    n = Counter(r["status"] for r in rows)
    total = len(rows)
    base = {"total": total, "fresh": n["UP TO DATE"], "late": n["LATE"], "stale": n["STALE"], "nodata": n["NO DATA"]}
    if n["NO DATA"] == total:
        return {**base, "state": "nodata", "label": "No data", "issues": 0, "summary": f"No data in {total} table{'s' if total != 1 else ''}"}
    issues = n["LATE"] + n["STALE"] + n["NO DATA"]
    state = "failing" if n["STALE"] else ("degraded" if issues else "healthy")
    return {**base, "state": state, "issues": issues, "summary": f"{n['UP TO DATE']} of {total} up to date"}


def _iso_utc(value):
    if value is None:
        return None
    return value.isoformat() + ("Z" if value.tzinfo is None else "")


def prepare_stream(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append(
            {
                "tenantId": str(r["TenantId"]),
                "tenant": (r["Tenant"] or "").strip(),
                "status": r["CrmStreaming"],
                "hasCrm": bool(r.get("HasCRM", 1)),
                "pushChat": bool(r["PushChat"]),
                "pushPost": bool(r["PushPost"]),
                "chatEligible": int(r["ChatEligible"]),
                "chatStreamed": int(r["ChatStreamed"]),
                "chatMissing": int(r["ChatMissing"]),
                "feedEligible": int(r["FeedEligible"]),
                "feedStreamed": int(r["FeedStreamed"]),
                "feedMissing": int(r["FeedMissing"]),
                "streamRows": int(r["StreamRows"]),
                "processed": int(r["Processed"]),
                "failed": int(r["Failed"]),
                "stuck": int(r["Stuck"]),
                "slaMissPct": float(r["SlaMissPct"] or 0),
                "missingPct": float(r["MissingPct"] or 0),
                "oldest": _iso_utc(r.get("OldestUnprocessed")),
                "last": _iso_utc(r.get("LastStreamCreated")),
            }
        )
    return out


def streaming_area(t: dict) -> dict:
    """Roll a tenant's CRM streaming numbers up to one CRM Health status.

    NOT CONFIGURED shows as not set up and NO ACTIVITY as neutral "No activity": neither counts as unhealthy.
    """
    s = t["stream"]
    if not s or s["status"] == "NOT CONFIGURED":
        return {"state": "notset", "issues": 0}
    if s["status"] == "NO ACTIVITY":
        return {"state": "nodata", "label": "No activity", "issues": 0, "summary": f"No activity in {STREAM_WINDOW_DAYS} days"}
    state = {"FAILING": "failing", "DEGRADED": "degraded", "HEALTHY": "healthy"}.get(s["status"])
    if state is None:
        return {"state": "notset", "issues": 0}
    chat, post = s["pushChat"], s["pushPost"]
    eligible = (s["chatEligible"] if chat else 0) + (s["feedEligible"] if post else 0)
    streamed = (s["chatStreamed"] if chat else 0) + (s["feedStreamed"] if post else 0)
    missing = (s["chatMissing"] if chat else 0) + (s["feedMissing"] if post else 0)
    if state == "healthy":
        summary = f"{streamed} of {eligible} streamed" if eligible else f"{s['processed']} of {s['streamRows']} delivered"
        return {"state": state, "issues": 0, "summary": summary}
    bits = []
    if s["stuck"]:
        bits.append(f"{s['stuck']} stuck")
    if s["failed"]:
        bits.append(f"{s['failed']} failed")
    if missing:
        bits.append(f"{missing} not streamed")
    issues = s["stuck"] + missing + max(s["failed"] - s["stuck"], 0)
    return {"state": state, "issues": issues, "summary": " \u00B7 ".join(bits)}


def connection_area(t: dict) -> dict:
    """Roll a tenant's CRM and performance integrations up to one Connection Health status."""
    items = [i for key in GROUP_KEYS for i in t[key]]
    if not items:
        return {"state": "notset", "issues": 0, "total": 0}
    bad = sum(i["state"] == "disconnected" for i in items)
    good = sum(i["state"] == "connected" for i in items)
    base = {
        "issues": bad, "connected": good, "checked": good + bad,
        "legacy": sum(i["state"] == "legacy" for i in items), "total": len(items),
    }
    if bad:
        return {**base, "state": "failing", "summary": f"{good} of {good + bad} connected"}
    if good:
        return {**base, "state": "healthy", "summary": f"{good} of {good + bad} connected"}
    return {**base, "state": "nodata", "summary": "Not verified"}


def build_tenants(
    crm: list[dict], perf: list[dict], app_names: dict[str, str], bulk: list[dict] | None = None,
    stream: list[dict] | None = None,
) -> list[dict]:
    """Merge both sources into one record per tenant, worst first."""
    tenants: dict[str, dict] = {}

    def entry(tid: str | None, name: str, in_app: bool) -> dict:
        key = tid if tid is not None else f"?{name}"
        if key not in tenants:
            tenants[key] = {
                "tenantId": tid if tid is not None else "\u2014",
                "tenant": name or (f"Tenant {tid}" if tid else "Unknown tenant"),
                "inApp": in_app,
                "crm": [],
                "perf": [],
                "arch": [],
                "bulk": [],
                "stream": None,
            }
        return tenants[key]

    for r in crm:  # CRM rows come from TenantDetail, so these tenants are in the app DB
        if r["tenantId"] in TEST_TENANTS:
            continue
        t = entry(r["tenantId"], app_names.get(r["tenantId"]) or r["tenant"], True)
        t["arch" if r.get("kind") == "archival" else "crm"].append(
            {
                "name": r["crm"], "code": r["code"], "state": r["state"],
                "reason": r["reason"], "status": r["status"], "expiry": r["expiry"],
            }
        )

    for r in perf:
        tid = r["tenantId"]
        if tid in TEST_TENANTS:
            continue
        in_app = tid is not None and tid in app_names
        name = app_names[tid] if in_app else r["tenant"]
        t = entry(tid, name, in_app)
        t["perf"].append(
            {
                "name": r["name"], "state": r["state"], "reason": r["reason"],
                "status": r["status"], "by": r["by"], "admins": r["admins"],
            }
        )

    for r in bulk or []:
        tid = r["tenantId"]
        if tid in TEST_TENANTS:
            continue
        in_app = tid is not None and tid in app_names
        name = app_names[tid] if in_app else r["tenant"]
        t = entry(tid, name, in_app)
        t["bulk"].append({k: r[k] for k in ("vendor", "table", "label", "section", "status", "last", "behind")})

    for r in stream or []:  # these rows come from TenantDetail, so the tenants are in the app DB
        tid = r["tenantId"]
        if tid in TEST_TENANTS:
            continue
        t = entry(tid, app_names.get(tid) or r["tenant"], True)
        t["stream"] = {k: v for k, v in r.items() if k not in ("tenantId", "tenant")}

    # Every active tenant is listed, even one with no CRM or performance vendor set up
    for tid, name in app_names.items():
        if tid in TEST_TENANTS or tid in tenants:
            continue
        entry(tid, name, True)

    for t in tenants.values():
        # One status per health area. Only Connection Health has data so far; the rest are "notset".
        t["areas"] = {key: {"state": "notset", "issues": 0} for key, _ in AREAS}
        t["areas"]["connection"] = connection_area(t)
        t["areas"]["bulk"] = bulk_area(t)
        t["areas"]["streaming"] = streaming_area(t)
        states = [a["state"] for a in t["areas"].values()]
        t["issues"] = sum(a.get("issues", 0) for a in t["areas"].values())
        if any(st in ("failing", "degraded") for st in states):
            t["overall"] = "attention"
        elif "healthy" in states:
            t["overall"] = "healthy"
        elif "nodata" in states:
            t["overall"] = "neutral"
        else:
            t["overall"] = "none"

    rank = {"attention": 0, "healthy": 1, "neutral": 2, "none": 3}
    return sorted(tenants.values(), key=lambda t: (-t["issues"], rank[t["overall"]], t["tenant"].lower()))


def summarize(tenants: list[dict]) -> dict:
    crm = Counter(i["state"] for t in tenants for i in t["crm"])
    perf = Counter(i["state"] for t in tenants for i in t["perf"])
    return {
        "tenants": len(tenants),
        "healthy": sum(t["overall"] == "healthy" for t in tenants),
        "attention": sum(t["overall"] == "attention" for t in tenants),
        "none": sum(t["overall"] == "none" for t in tenants),
        "crm_connected": crm["connected"],
        "crm_disconnected": crm["disconnected"],
        "perf_connected": perf["connected"],
        "perf_disconnected": perf["disconnected"],
        "unmatched": sum(not t["inApp"] for t in tenants),
        "bulk_attention": sum(t["areas"]["bulk"]["state"] in ("failing", "degraded") for t in tenants),
        "stream_attention": sum(t["areas"]["streaming"]["state"] in ("failing", "degraded") for t in tenants),
    }


# --------------------------------------------------------------------------
# Demo data (no database needed)
# --------------------------------------------------------------------------
def demo_data() -> tuple[list[dict], list[dict], dict[str, str], list[dict], list[dict], list[dict]]:
    crm_rows = demo_crm_rows()
    app_names = {str(r["TenantId"]): r["BusinessName"] for r in crm_rows}
    app_names["120"] = "Redwood Partners"  # has performance vendors but no CRM
    # active tenants with no CRM or performance vendor at all
    app_names.update({"140": "Quiet Harbor Advisors", "141": "Pine Street Capital", "142": "Brightwater Financial"})

    ok, no_admin, no_conn = "Connected", "Disconnected - no admin token", "Disconnected - admin token not connected"
    spec = [
        (101, "Northwind Wealth Advisors", "Orion", ok, 1, "ops@northwind.example"),
        (101, "Northwind Wealth Advisors", "Addepar", ok, 2, "a@northwind.example, b@northwind.example"),
        (102, "Harbor & Pine Financial", "BlackDiamond", no_admin, 0, None),
        (102, "Harbor & Pine Financial", "Orion", ok, 1, "admin@harborpine.example"),
        (104, "Bluebird Capital Group", "Orion", no_conn, 0, None),
        (106, "Oakmont Family Office", "Tamarac", ok, 1, "admin@oakmont.example"),
        (109, "Willow Creek Wealth", "Emoney", no_admin, 0, None),
        (111, "Maple & Stone Financial", "Addepar", ok, 1, "admin@maplestone.example"),
        (120, "Redwood Partners", "Orion", ok, 1, "admin@redwood.example"),
        (120, "Redwood Partners", "Emoney", no_admin, 0, None),
        (130, "Orphan Test Co", "Addepar", ok, 1, "admin@orphan.example"),  # not in the app DB
    ]
    perf_rows = [
        {
            "tenant_id": t, "tenant_name": n, "vendor_name": v, "vendor_key": v.lower(), "status": s,
            "connected_admins": a, "connected_by": by,
        }
        for t, n, v, s, a, by in spec
    ]

    # TenantVendors.VendorCode differs in case from vendor_key on purpose (matching ignores case),
    # and Willow Creek's Emoney is mapped in the connector DB but inactive in TenantVendors.
    inactive = {(109, "Emoney")}
    active_rows = [
        {"TenantId": r["tenant_id"], "VendorCode": r["vendor_key"].upper()}
        for r in perf_rows if (r["tenant_id"], r["vendor_name"]) not in inactive
    ]

    # Bulk data freshness: Orion tenants have 10 items (7 tables + 3 performance reports), BlackDiamond 7.
    orion_tables = ["dim_household", "dim_account", "dim_security", "fact_holding", "dim_account_ownership",
                    "dim_account_group", "dim_account_group_account",
                    "fact_vendor_client_reports (TWR)", "fact_vendor_client_reports (PerformanceSummary)",
                    "fact_vendor_client_reports (AssetAllocation)"]
    bd_tables = ["dim_household", "dim_client", "dim_account", "dim_security", "fact_holding", "fact_transaction",
                 "dim_account_ownership"]
    now_utc = datetime.now(timezone.utc)

    def bulk(tid, name, vendor, key, tables, overrides=None, default="UP TO DATE"):
        hours = {"UP TO DATE": 3, "LATE": 30, "STALE": 80, "NO DATA": None}
        behind = {"UP TO DATE": 0, "LATE": 1, "STALE": 3, "NO DATA": None}
        out = []
        for tb in tables:
            st = (overrides or {}).get(tb, default)
            out.append({
                "tenant_id": tid, "tenant_name": name, "vendor_name": vendor, "vendor_key": key, "table_name": tb,
                "last_updated": None if hours[st] is None else now_utc - timedelta(hours=hours[st]),
                "data_freshness": st, "days_behind": behind[st],
            })
        return out

    bulk_rows = (
        bulk(101, "Northwind Wealth Advisors", "Orion", "orion", orion_tables)
        + bulk(102, "Harbor & Pine Financial", "BlackDiamond", "blackdiamond", bd_tables, {"dim_account": "LATE", "fact_holding": "STALE"})
        + bulk(102, "Harbor & Pine Financial", "Orion", "orion", orion_tables)
        + bulk(104, "Bluebird Capital Group", "Orion", "orion", orion_tables, {"dim_security": "NO DATA", "fact_vendor_client_reports (AssetAllocation)": "LATE"})
        + bulk(120, "Redwood Partners", "Orion", "orion", orion_tables, default="NO DATA")
    )

    # CRM streaming health, shaped like the query's output. Tenants not listed here are "not set up".
    def stream(tid, name, status, chat=True, post=True, c=(0, 0, 0), f=(0, 0, 0), rows=(0, 0, 0, 0),
               sla=0.0, miss=0.0, oldest_h=None, last_h=None, has_crm=True):
        n = datetime.now(timezone.utc).replace(tzinfo=None)
        return {
            "TenantId": tid, "Tenant": name, "CrmStreaming": status, "HasCRM": int(has_crm),
            "PushChat": int(chat), "PushPost": int(post),
            "ChatEligible": c[0], "ChatStreamed": c[1], "ChatMissing": c[2],
            "FeedEligible": f[0], "FeedStreamed": f[1], "FeedMissing": f[2],
            "StreamRows": rows[0], "Processed": rows[1], "Failed": rows[2], "Stuck": rows[3],
            "SlaMissPct": sla, "MissingPct": miss,
            "OldestUnprocessed": None if oldest_h is None else n - timedelta(hours=oldest_h),
            "LastStreamCreated": None if last_h is None else n - timedelta(hours=last_h),
        }

    stream_rows = [
        stream(101, "Northwind Wealth Advisors", "HEALTHY", c=(14, 14, 0), f=(6, 6, 0), rows=(20, 20, 0, 0), last_h=2),
        stream(102, "Harbor & Pine Financial", "DEGRADED", c=(12, 11, 1), f=(5, 5, 0), rows=(17, 16, 0, 1),
               sla=7.14, miss=5.88, oldest_h=40, last_h=1),
        stream(103, "Summit Ridge Partners", "FAILING", c=(18, 13, 5), f=(4, 4, 0), rows=(34, 26, 3, 8),
               sla=26.67, miss=22.73, oldest_h=95, last_h=3),
        stream(104, "Bluebird Capital Group", "NO ACTIVITY"),
        stream(105, "Cedar Lane Advisory", "NOT CONFIGURED", chat=False, post=False),
        stream(106, "Oakmont Family Office", "HEALTHY", post=False, c=(9, 9, 0), f=(3, 0, 0), rows=(9, 9, 0, 0), last_h=5),
        stream(108, "Granite Peak Investments", "HEALTHY", c=(10, 6, 0), f=(2, 2, 0), rows=(8, 8, 0, 0), last_h=1),
    ]
    return crm_rows, perf_rows, app_names, active_rows, bulk_rows, stream_rows


# --------------------------------------------------------------------------
# HTML report (self-contained: inline CSS + JS, no network requests)
# --------------------------------------------------------------------------
TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Tenant Health</title>
<style>
  :root {
    color-scheme: light dark;
    --bg:#f4f6f8; --surface:#ffffff; --text:#101828; --muted:#475467; --faint:#667085; --line:#e4e7ec; --hover:#f7f8fb; --soft:#eaecf0; --accent:#1d4ed8;
    --bad:#b42318; --bad-soft:#fee4e2; --ok:#067647; --ok-soft:#d1fadf; --warn:#93370d; --warn-soft:#fef0c7;
    --shadow:0 1px 2px rgba(16,24,40,.04), 0 10px 28px rgba(16,24,40,.08);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#0b1020; --surface:#121a2e; --text:#e8ebf4; --muted:#aab4c8; --faint:#8794ad; --line:#222c47; --hover:#18223b; --soft:#1c2540; --accent:#93a4ff;
      --bad:#fca5a5; --bad-soft:rgba(248,113,113,.16); --ok:#6ee7a0; --ok-soft:rgba(74,222,128,.14); --warn:#fcd34d; --warn-soft:rgba(251,191,36,.14);
      --shadow:0 10px 30px rgba(0,0,0,.35);
    }
  }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; background:var(--bg); color:var(--text); font-variant-numeric:tabular-nums;
         font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  button, input, select { font:inherit; color:inherit; }
  a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
  :focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  [hidden] { display:none !important; }
  .wrap { max-width:1280px; margin:0 auto; padding:26px 24px 64px; }

  /* ---------- list page ---------- */
  .top { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:6px 16px; margin-bottom:6px; }
  .brand { display:flex; align-items:center; gap:10px; }
  h1 { margin:0; font-size:20px; font-weight:700; letter-spacing:-.01em; }
  .env { padding:2px 10px; border-radius:99px; font-size:11px; font-weight:700; letter-spacing:.08em; color:var(--accent); background:color-mix(in srgb, var(--accent) 14%, transparent); }
  .updated { color:var(--muted); font-size:13px; } .updated .stale { color:var(--warn); font-weight:600; }

  .toolbar { position:sticky; top:0; z-index:20; padding:10px 0 8px; background:color-mix(in srgb, var(--bg) 88%, transparent); backdrop-filter:saturate(1.4) blur(10px); }
  .toolrow { display:flex; flex-wrap:wrap; align-items:center; gap:8px; }
  .toolrow2 { justify-content:space-between; margin-top:10px; }
  .search { position:relative; flex:1 1 260px; max-width:420px; }
  .search input { width:100%; padding:9px 40px 9px 36px; border:1px solid var(--line); border-radius:10px; background:var(--surface); }
  .search svg { position:absolute; left:11px; top:50%; transform:translateY(-50%); color:var(--faint); }
  .search kbd { position:absolute; right:9px; top:50%; transform:translateY(-50%); padding:0 6px; border:1px solid var(--line); border-radius:5px; color:var(--faint); font:11px ui-monospace, SFMono-Regular, Menlo, monospace; }
  select, .btn { padding:9px 12px; border:1px solid var(--line); border-radius:10px; background:var(--surface); cursor:pointer; max-width:230px; }
  .btn { display:inline-flex; align-items:center; gap:6px; } .btn:hover, select:hover { background:var(--hover); }
  .btn .n { padding:0 7px; border-radius:99px; font-size:11px; font-weight:700; color:var(--warn); background:var(--warn-soft); }
  .seg { display:flex; flex-wrap:wrap; gap:6px; }
  .chip { padding:5px 12px; border:1px solid var(--line); border-radius:99px; background:var(--surface); color:var(--muted); font-size:13px; cursor:pointer; }
  .chip:hover { background:var(--hover); } .chip.active { color:#fff; background:var(--accent); border-color:var(--accent); }
  .count { color:var(--muted); font-size:13px; }
  .notes-panel { margin:6px 0 4px; padding:12px 16px; border-radius:12px; font-size:13px; color:var(--warn); background:var(--warn-soft); }
  .notes-panel p { margin:0; } .notes-panel p + p { margin-top:8px; }

  .tablewrap { margin-top:12px; overflow-x:auto; border:1px solid var(--line); border-radius:16px; background:var(--surface); }
  table.tbl { width:100%; min-width:900px; border-collapse:collapse; }
  .tbl th { padding:0; text-align:left; white-space:nowrap; border-bottom:1px solid var(--line); color:var(--muted); font-size:11.5px; font-weight:600; letter-spacing:.05em; text-transform:uppercase; }
  .tbl th > span, .tbl th > button { display:block; padding:12px 16px; }
  .th-btn { width:100%; border:0; background:none; text-align:left; font:inherit; letter-spacing:inherit; text-transform:inherit; color:inherit; cursor:pointer; }
  .th-btn:hover { color:var(--text); }
  .th-btn.sorted::after { content:" \25B2"; font-size:9px; } .th-btn.sorted.desc::after { content:" \25BC"; }
  .tbl td { padding:12px 16px; vertical-align:top; border-bottom:1px solid var(--line); }
  .tbl tr.row { cursor:pointer; } .tbl tr.row:hover td { background:var(--hover); } .tbl tr:last-child td { border-bottom:0; }
  .tname { font-weight:600; } .tid { color:var(--muted); font-size:12px; }
  .cell-sub { margin-top:4px; color:var(--muted); font-size:12.5px; }
  .notset { color:var(--faint); font-size:13px; }
  .badge { margin-left:8px; padding:1px 7px; border-radius:6px; font-size:11px; font-weight:600; color:var(--warn); background:var(--warn-soft); white-space:nowrap; vertical-align:1px; }
  .empty { padding:48px 20px; text-align:center; color:var(--muted); } .empty h3 { margin:0 0 4px; color:var(--text); font-size:17px; }
  .more { display:block; margin:20px auto 0; }

  .pill { display:inline-flex; flex:none; align-items:center; gap:6px; padding:4px 10px 4px 8px; border-radius:999px; font-size:12px; font-weight:600; white-space:nowrap; color:var(--muted); background:var(--soft); }
  .pill.bad { color:var(--bad); background:var(--bad-soft); } .pill.ok { color:var(--ok); background:var(--ok-soft); }
  .pill.warn { color:var(--warn); background:var(--warn-soft); } .pill.mute { color:var(--muted); background:var(--soft); }

  details.howto { margin-top:30px; color:var(--muted); font-size:12.5px; }
  details.howto summary { cursor:pointer; font-weight:600; }
  details.howto ul { margin:8px 0 0; padding-left:18px; line-height:1.75; }

  /* ---------- tenant page ---------- */
  .crumbs { display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin-bottom:16px; color:var(--muted); }
  .crumbs .sep { margin:0 8px; color:var(--faint); } .crumbs .here { color:var(--text); font-weight:500; }
  .card { background:var(--surface); border:1px solid var(--line); border-radius:16px; }
  .head { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:14px 24px; padding:22px 24px; margin-bottom:20px; }
  .head h2 { margin:0; font-size:28px; line-height:1.2; font-weight:700; letter-spacing:-.02em; }
  .meta { margin:6px 0 0; color:var(--muted); }
  .head-actions { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(min(100%, 520px), 1fr)); gap:20px; align-items:stretch; }
  .sec { display:flex; flex-direction:column; gap:18px; padding:22px 24px; }
  .sec-head { display:flex; align-items:center; justify-content:space-between; gap:12px; }
  .sec h3.title { margin:0; font-size:18px; font-weight:650; letter-spacing:-.01em; }
  .sub { margin:4px 0 0; color:var(--faint); font-size:13px; }
  .ph { border-style:dashed; background:transparent; min-height:220px; }
  .big { font-size:38px; line-height:1.05; font-weight:700; letter-spacing:-.02em; } .big.bad { color:var(--bad); } .big.ok { color:var(--ok); }
  .cap { margin-top:6px; color:var(--muted); }
  .group { margin:0 0 8px; color:var(--faint); font-size:12px; font-weight:600; letter-spacing:.06em; text-transform:uppercase; }
  .rows { list-style:none; margin:0; padding:0; border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  .rows li { display:flex; align-items:center; gap:12px; padding:12px 14px; } .rows li + li { border-top:1px solid var(--line); }
  .av { flex:none; display:grid; place-items:center; width:34px; height:34px; border-radius:10px; color:#fff; font-size:12px; font-weight:700; letter-spacing:.02em; }
  .body { flex:1; min-width:0; } .name { font-weight:600; }
  .tag { margin-left:8px; padding:1px 7px; border-radius:6px; background:var(--soft); color:var(--muted); font-size:11px; font-weight:600; letter-spacing:.04em; text-transform:uppercase; }
  .d { margin-top:2px; color:var(--muted); font-size:13px; } .d.bad { color:var(--bad); }

  .mono { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:13px; font-weight:500; }
  .okfold { margin-top:10px; border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  .okfold summary { padding:11px 14px; cursor:pointer; color:var(--muted); font-size:13px; font-weight:600; }
  .okfold[open] summary { border-bottom:1px solid var(--line); }
  .okfold .rows { border:0; border-radius:0; }
  .subgroup { margin-top:14px; }
  .flags { display:flex; flex-wrap:wrap; gap:8px; }
  .flag { display:inline-flex; align-items:center; padding:4px 11px; border-radius:99px; font-size:12.5px; font-weight:600; color:var(--muted); background:var(--soft); }
  .flag.on { color:var(--ok); background:var(--ok-soft); }
  .panelrow { display:grid; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); gap:12px; }
  .panel { padding:12px 14px; border:1px solid var(--line); border-radius:12px; }
  .panel.off { opacity:.65; }
  .panel h4 { margin:0 0 6px; color:var(--faint); font-size:12px; font-weight:600; letter-spacing:.07em; text-transform:uppercase; }
  .pbig { font-size:22px; font-weight:700; letter-spacing:-.01em; } .pbig .pl { color:var(--muted); font-size:13px; font-weight:500; }
  .pbar { display:flex; height:8px; margin:8px 0 7px; border-radius:99px; overflow:hidden; background:var(--soft); }
  .pbar i { display:block; height:100%; } .pbar .g { background:var(--ok); } .pbar .y { background:var(--faint); opacity:.5; } .pbar .r { background:var(--bad); }
  .facts { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:14px 18px; }
  .fact .fl { color:var(--faint); font-size:12px; } .fact .fv { margin-top:1px; font-size:14.5px; font-weight:600; } .fact .fv.bad { color:var(--bad); }
  .alertbox { padding:12px 14px; border-radius:12px; font-size:13px; } .alertbox ul { margin:0; padding-left:18px; }
  .alertbox.bad { color:var(--bad); background:var(--bad-soft); } .alertbox.warn { color:var(--warn); background:var(--warn-soft); }
  .subhead { display:flex; align-items:baseline; justify-content:space-between; gap:10px; margin:0 0 8px; font-size:13px; font-weight:600; }
  .subhead span:last-child { color:var(--muted); font-size:12.5px; font-weight:500; }
  @media (max-width:560px) { .search { max-width:none; } select { flex:1 1 140px; max-width:none; } }
  @media (prefers-reduced-motion: reduce) { * { animation:none !important; transition:none !important; } }
</style>
</head>
<body>
<div class="wrap">

  <!-- ================= Tenants (list) ================= -->
  <div id="listView">
    <header class="top">
      <div class="brand"><h1>Tenant Health</h1><span class="env" id="env"></span></div>
      <div class="updated" id="updated"></div>
    </header>

    <div class="toolbar">
      <div class="toolrow">
        <label class="search" aria-label="Search">
          <span id="searchicon"></span>
          <input id="q" type="search" placeholder="Search tenants, integrations, reasons" autocomplete="off">
          <kbd>/</kbd>
        </label>
        <select id="vendor" aria-label="Filter by integration"></select>
        <select id="sort" aria-label="Sort tenants"></select>
        <button class="btn" id="export" type="button"></button>
        <button class="btn" id="notesbtn" type="button" hidden></button>
      </div>
      <div class="toolrow toolrow2">
        <div class="seg" id="seg"></div>
        <div class="count" id="count"></div>
      </div>
    </div>
    <div class="notes-panel" id="notes" hidden></div>

    <div id="list"></div>

    <details class="howto">
      <summary>How to read this</summary>
      <ul>
        <li>Every active tenant in TenantDetail is listed, including tenants with nothing set up. Excluded tenants are left out.</li>
        <li>Each tenant has four health areas. Connection Health, Performance Data Health and CRM Health have data so far; Archival Health shows "Not set up" until it is added.</li>
        <li>Performance Data Health checks the daily loads of Orion and BlackDiamond tables, plus Orion's TWR, Performance Summary and Asset Allocation reports (fact_vendor_client_reports). Loads run every day, so today's load is expected by 10:30 AM ET (yesterday's before then). Up to date = it landed, Late = one day behind, Stale = two or more days behind, No data = no active rows. A tenant with some empty tables shows Degraded; one with every table empty shows No data.</li>
        <li>Click a tenant to open its page. A tenant's link in the address bar can be shared, and the browser's back button returns to this list.</li>
        <li>Tenants are matched on TenantDetail.TenantId (SQL Server) = tenant_mdm.tenant_id (Postgres).</li>
        <li>CRM status approximates the app's Connect/Disconnect logic at the tenant level; the app checks per user and has a few fail-open cases.</li>
        <li>A CRM or performance vendor is only listed when it is also active for the tenant (TenantVendors.IsDeleted = false). Performance vendors are matched on vendor_key = TenantVendors.VendorCode.</li>
        <li>Performance vendors are Connected when at least one ADMIN user has a connected token for a vendor that is mapped to the tenant.</li>
        <li>Redtail's refresh token expiry is checked here even though the app only checks that a token exists. A Redtail token with no expiry date set counts as connected. Legacy Salesforce is never checked, so it shows as Not checked.</li>
        <li id="streamhelp"></li>
        <li>Archival vendors (Smarsh, Global Relay, Global Relay V2, Theta Lake, Message Watcher, Mirrorweb, GreenBoard) are listed when enabled for the tenant. The app shows them as Connected without any connection behind it, so they show as Enabled and never affect the status.</li>
        <li id="excluded"></li>
        <li>Shortcuts: <b>/</b> search, <b>Esc</b> back to the list or clear search.</li>
      </ul>
    </details>
  </div>

  <!-- ================= One tenant ================= -->
  <div id="detailView" hidden></div>

</div>

<script>
const DATA = __DATA__;
const META = __META__;
const AREAS = META.areas;      /* [{key, title}]: the four health areas, in display order */
const GROUPS = META.groups;    /* [{key, title}]: the groups inside Connection Health */

const STREAM = META.streaming || { windowDays: 7, slaHours: 24, failPct: 10, minVolume: 10 };
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : "");
const plural = (n, one, many) => n + " " + (n === 1 ? one : many);
const view = { q: "", vendor: "all", status: "all", sort: "attention", shown: 50 };
let listScroll = 0;
DATA.forEach((t, i) => { t._i = i; });

const svg = (d, s, w) => '<svg viewBox="0 0 24 24" width="' + (s || 16) + '" height="' + (s || 16) + '" fill="none" stroke="currentColor" stroke-width="' + (w || 2.2) +
  '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + "</svg>";
const PATH = {
  check: '<path d="M5 12.5l4.2 4.2L19 7"/>', x: '<path d="M6 6l12 12M18 6L6 18"/>', minus: '<path d="M6 12h12"/>',
  alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4"/><path d="M12 17h.01"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/>', download: '<path d="M12 4v11"/><path d="M7.5 11l4.5 4.5 4.5-4.5"/><path d="M5 19h14"/>',
  bell: '<path d="M12 7v6"/><path d="M12 17h.01"/>',
};
const STATE = {
  failing: { label: "Failing", cls: "bad", icon: svg(PATH.x, 13, 2.4) },
  degraded: { label: "Degraded", cls: "warn", icon: svg(PATH.alert, 13, 2.4) },
  healthy: { label: "Healthy", cls: "ok", icon: svg(PATH.check, 13, 2.4) },
  nodata: { label: "Not checked", cls: "mute", icon: svg(PATH.minus, 13, 2.4) },
};
const pill = (state, label) => { const s = STATE[state]; return '<span class="pill ' + s.cls + '">' + s.icon + esc(label || s.label) + "</span>"; };
const ITEM_STATE = { connected: ["healthy", "Connected"], disconnected: ["failing", "Disconnected"], legacy: ["nodata", "Not checked"] };
const itemPill = (i, group) => { if (group === "arch") return pill("nodata", "Enabled"); const m = ITEM_STATE[i.state] || ["nodata", i.state]; return pill(m[0], m[1]); };
const overallPill = (t) => t.overall === "attention" ? pill("failing", "Needs attention \u00B7 " + t.issues)
  : (t.overall === "healthy" ? pill("healthy") : (t.overall === "none" ? '<span class="pill mute">No integrations</span>' : pill("nodata")));
const notInApp = (t) => (t.inApp ? "" : '<span class="badge" title="No active TenantDetail match for this tenant_id">not in app DB</span>');

/* ---------- data helpers ---------- */
const connItems = (t) => GROUPS.reduce((a, g) => a.concat((t[g.key] || []).map((i) => Object.assign({ group: g.key }, i))), []);
const areaOf = (t, key) => (t.areas && t.areas[key]) || { state: "notset" };
const reasonOf = (i) => (i.reason ? cap(i.reason) : "Disconnected");
const fmtDate = (iso) => new Date(iso).toLocaleDateString([], { dateStyle: "medium" });
const RANK = { attention: 0, healthy: 1, neutral: 2, none: 3 };
const AREA_RANK = { failing: 0, degraded: 1, nodata: 2, healthy: 3, notset: 4 };
const builtAreas = AREAS.filter((a) => DATA.some((t) => areaOf(t, a.key).state !== "notset"));

function relative(iso) {
  if (!iso) return null;
  const days = Math.round((Date.parse(iso) - Date.now()) / 86400000);
  if (days < 0) return "expired " + (-days) + "d ago";
  if (days === 0) return "expires today";
  return "expires in " + days + "d";
}
function ago(iso) {
  const m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return m + "m ago";
  const h = Math.round(m / 60);
  return h < 48 ? h + "h ago" : Math.round(h / 24) + "d ago";
}
function initials(name) {
  const words = String(name).replace(/\(.*?\)/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").split(/[^A-Za-z0-9]+/).filter(Boolean);
  if (!words.length) return "?";
  return (words.length === 1 ? words[0].slice(0, 2) : words[0][0] + words[1][0]).toUpperCase();
}
function hue(name) { let h = 0; for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
const avatar = (name) => '<span class="av" style="background:hsl(' + hue(name) + ' 48% 38%)">' + esc(initials(name)) + "</span>";

/* ---------- list: filtering and sorting ---------- */
const vkey = (i) => i.group + ":" + i.name;
function matchesBase(t) {
  if (view.vendor !== "all" && !connItems(t).some((i) => vkey(i) === view.vendor)) return false;
  const q = view.q.trim().toLowerCase();
  if (!q) return true;
  const hay = [t.tenant, t.tenantId].concat(connItems(t).map((i) => i.name + " " + (i.reason || "") + " " + (i.status || ""))).concat((t.bulk || []).map((r) => r.vendor + " " + (r.label || "") + " " + r.table + " " + r.status)).concat(t.stream ? [t.stream.status + " crm streaming"] : []).join(" ").toLowerCase();
  return hay.indexOf(q) > -1;
}
/* With an integration selected, the status filters describe that integration, not the whole tenant. */
function effStatus(t) {
  if (view.vendor === "all") return t.overall;
  const it = connItems(t).filter((i) => vkey(i) === view.vendor);
  if (it.some((i) => i.state === "disconnected")) return "attention";
  if (it.some((i) => i.state === "connected")) return "healthy";
  return "neutral";
}
function cmp(a, b) {
  const name = a.tenant.localeCompare(b.tenant, undefined, { sensitivity: "base" });
  const s = view.sort;
  if (s === "name") return name;
  if (s === "name-desc") return -name;
  if (s === "id") {
    const x = parseFloat(a.tenantId), y = parseFloat(b.tenantId), nx = isNaN(x), ny = isNaN(y);
    if (nx !== ny) return nx ? 1 : -1;
    return (nx ? String(a.tenantId).localeCompare(String(b.tenantId)) : x - y) || name;
  }
  if (s.indexOf("area:") === 0) {
    const p = s.split(":");
    const d = AREA_RANK[areaOf(a, p[1]).state] - AREA_RANK[areaOf(b, p[1]).state];
    return (p[2] === "rev" ? -d : d) || name;
  }
  return RANK[effStatus(a)] - RANK[effStatus(b)] || b.issues - a.issues || name;
}

/* ---------- list: rendering ---------- */
function streamProblems(t) {
  const s = t.stream;
  if (!s) return "";
  const bits = [];
  if (s.stuck) bits.push(s.stuck + " stream record" + (s.stuck === 1 ? "" : "s") + " unprocessed for over " + STREAM.slaHours + "h");
  if (s.failed) bits.push(s.failed + " out of retries");
  const missing = (s.pushChat ? s.chatMissing : 0) + (s.pushPost ? s.feedMissing : 0);
  if (missing) bits.push(missing + " chat/feed item" + (missing === 1 ? "" : "s") + " never streamed");
  return bits.join("; ");
}
function areaCell(t, a) {
  const s = areaOf(t, a.key);
  if (s.state === "notset") return '<td><span class="notset">Not set up</span></td>';
  let problems = "";
  if (a.key === "connection") problems = connItems(t).filter((i) => i.state === "disconnected").map((i) => i.name + ": " + reasonOf(i)).join("; ");
  else if (a.key === "bulk") problems = (t.bulk || []).filter((r) => r.status !== "UP TO DATE").map((r) => r.vendor + " " + (r.label || r.table) + ": " + r.status.toLowerCase() + (r.behind ? " (" + r.behind + "d behind)" : "")).join("; ");
  else if (a.key === "streaming") problems = streamProblems(t);
  return '<td' + (problems ? ' title="' + esc(problems) + '"' : "") + ">" + pill(s.state, s.label) + (s.summary ? '<div class="cell-sub">' + esc(s.summary) + "</div>" : "") + "</td>";
}
const rowHtml = (t) => '<tr class="row" tabindex="0" data-i="' + t._i + '"><td><div class="tname">' + esc(t.tenant) + notInApp(t) +
  '</div><div class="tid">ID ' + esc(t.tenantId) + "</div></td>" + AREAS.map((a) => areaCell(t, a)).join("") + "</tr>";

function headerCell(label, key) {
  if (!key) return "<th><span>" + esc(label) + "</span></th>";
  const on = view.sort === key || view.sort === key + ":rev" || (key === "name" && view.sort === "name-desc");
  const desc = view.sort === key + ":rev" || view.sort === "name-desc";
  return '<th aria-sort="' + (on ? (desc ? "descending" : "ascending") : "none") + '"><button type="button" class="th-btn' + (on ? " sorted" : "") + (desc ? " desc" : "") +
    '" data-sort="' + esc(key) + '">' + esc(label) + "</button></th>";
}

function renderList() {
  const base = DATA.filter(matchesBase);
  const counts = { all: base.length, attention: 0, healthy: 0, none: 0 };
  base.forEach((t) => { const s = effStatus(t); if (counts[s] != null) counts[s]++; });
  const segs = [["all", "All"], ["attention", "Needs attention"], ["healthy", "Healthy"]];
  if (view.vendor === "all") segs.push(["none", "No integrations"]);
  if (view.status === "none" && view.vendor !== "all") view.status = "all";
  $("#seg").innerHTML = segs.map((x) => '<button class="chip' + (view.status === x[0] ? " active" : "") + '" data-status="' + x[0] + '">' + x[1] + " (" + counts[x[0]] + ")</button>").join("");

  const rows = base.filter((t) => view.status === "all" || effStatus(t) === view.status).sort(cmp);
  $("#count").textContent = rows.length === DATA.length ? plural(DATA.length, "tenant", "tenants") : rows.length + " of " + plural(DATA.length, "tenant", "tenants");
  $("#vendor").value = view.vendor;
  $("#sort").value = Array.from($("#sort").options).some((o) => o.value === view.sort) ? view.sort : "";

  if (!rows.length) {
    $("#list").innerHTML = '<div class="tablewrap"><div class="empty"><h3>' + (DATA.length ? "No tenants match" : "No tenants found") + "</h3>" +
      (DATA.length ? "Try clearing the search or filters." : "No active tenants were returned for this environment.") + "</div></div>";
    return;
  }
  const head = headerCell("Tenant", "name") + AREAS.map((a) => headerCell(a.title, builtAreas.indexOf(a) > -1 ? "area:" + a.key : "")).join("");
  const shown = rows.slice(0, view.shown);
  $("#list").innerHTML = '<div class="tablewrap"><table class="tbl"><thead><tr>' + head + "</tr></thead><tbody>" + shown.map(rowHtml).join("") + "</tbody></table></div>" +
    (rows.length > shown.length ? '<button class="btn more" data-more="1">Show ' + Math.min(50, rows.length - shown.length) + " more (" + (rows.length - shown.length) + " left)</button>" : "");
}

/* ---------- tenant page ---------- */
function integrationRow(i, group) {
  const lines = [];
  if (i.state === "disconnected") lines.push('<div class="d bad">' + esc(reasonOf(i)) + "</div>");
  if (i.expiry && !/JWT/.test(i.status || "")) {
    const past = Date.parse(i.expiry) < Date.now();
    const days = Math.abs(Math.round((Date.parse(i.expiry) - Date.now()) / 86400000));
    lines.push('<div class="d">Token expiry: ' + esc(fmtDate(i.expiry)) + " (" + (past ? (days ? days + "d ago" : "today") : (days ? "in " + days + "d" : "today")) + ")</div>");
  }
  if (group === "arch") lines.push('<div class="d">Enabled for this tenant. The app shows it as Connected but has no connection to check.</div>');
  else if (i.state === "legacy") lines.push('<div class="d">The app never checks this connection, so its status is unknown.</div>');
  if (i.state === "connected" && /JWT/.test(i.status || "")) lines.push('<div class="d">Connected through JWT authentication.</div>');
  if (i.by) lines.push('<div class="d">Connected by ' + esc(i.by) + " (" + plural(i.admins || 0, "admin", "admins") + ")</div>");
  return "<li>" + avatar(i.name) + '<div class="body"><div class="name">' + esc(i.name) + (i.code ? '<span class="tag">' + esc(i.code) + "</span>" : "") + "</div>" + lines.join("") + "</div>" + itemPill(i, group) + "</li>";
}
function connectionCard(t, title) {
  const a = areaOf(t, "connection");
  const head = '<div><div class="sec-head"><h3 class="title" id="h-connection">' + esc(title) + "</h3>" + (a.state === "notset" ? "" : pill(a.state)) + "</div>" +
    '<p class="sub">Checked ' + esc(ago(META.generated)) + (a.total ? " \u00B7 " + plural(a.total, "integration", "integrations") + " set up" : "") + "</p></div>";
  if (a.state === "notset") return '<section class="card sec" aria-labelledby="h-connection">' + head + '<div class="d">No integrations are set up for this tenant.</div></section>';
  const big = a.checked ? '<div class="big ' + (a.state === "failing" ? "bad" : a.state === "healthy" ? "ok" : "") + '">' + a.connected + " of " + a.checked + '</div><div class="cap">integrations connected' +
    (a.legacy ? " \u00B7 " + a.legacy + " not checked" : "") + "</div>" : '<div class="big">Not checked</div><div class="cap">The app does not check these connections.</div>';
  const groups = GROUPS.map((g) => { const items = t[g.key] || [];
    return '<div><h4 class="group">' + esc(g.title) + "</h4>" + (items.length ? '<ul class="rows">' + items.map((i) => integrationRow(i, g.key)).join("") + "</ul>" : '<div class="d">Not set up for this tenant.</div>') + "</div>"; }).join("");
  return '<section class="card sec" aria-labelledby="h-connection">' + head + "<div>" + big + "</div>" + groups + "</section>";
}
const BULK_RANK = { "STALE": 0, "LATE": 1, "NO DATA": 2, "UP TO DATE": 3 };
const BULK_PILL = { "UP TO DATE": ["healthy", "Up to date"], "LATE": ["degraded", "Late"], "STALE": ["failing", "Stale"], "NO DATA": ["nodata", "No data"] };
const etTime = (iso) => new Date(iso).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) + " ET";
function bulkRow(r) {
  const p = BULK_PILL[r.status] || ["nodata", r.status];
  const line = r.last ? "Last updated " + etTime(r.last) + " (" + ago(r.last) + ")" + (r.behind ? " \u00B7 " + r.behind + "d behind" : "") : "No active rows for this tenant";
  return '<li><div class="body"><div class="name" title="' + esc(r.table) + '">' + esc(r.label || r.table) + '</div><div class="d' + (r.status === "STALE" ? " bad" : "") + '">' + esc(line) + "</div></div>" + pill(p[0], p[1]) + "</li>";
}
function bulkCard(t, title) {
  const a = areaOf(t, "bulk"), rows = t.bulk || [];
  const vendors = Array.from(new Set(rows.map((r) => r.vendor)));
  const head = '<div><div class="sec-head"><h3 class="title" id="h-bulk">' + esc(title) + "</h3>" + (a.state === "notset" ? "" : pill(a.state, a.label)) + "</div>" +
    '<p class="sub">Checked ' + esc(ago(META.generated)) + (rows.length ? " \u00B7 " + plural(rows.length, "table", "tables") + " across " + plural(vendors.length, "vendor", "vendors") : "") + "</p></div>";
  if (a.state === "notset") return '<section class="card sec" aria-labelledby="h-bulk">' + head + '<div class="d">Performance data is not monitored for this tenant (no Orion or BlackDiamond mapping).</div></section>';
  const extras = [["stale", "stale"], ["late", "late"], ["nodata", "no data"]].filter((x) => a[x[0]]).map((x) => a[x[0]] + " " + x[1]).join(" \u00B7 ");
  const big = a.state === "nodata" ? '<div class="big">No data</div><div class="cap">No rows in any of ' + plural(a.total, "table", "tables") + "</div>"
    : '<div class="big ' + (a.state === "failing" ? "bad" : a.state === "healthy" ? "ok" : "") + '">' + a.fresh + " of " + a.total + '</div><div class="cap">tables up to date' + (extras ? " \u00B7 " + extras : "") + "</div>";
  const block = (list) => {   // problems first, healthy ones folded away
    const bad = list.filter((r) => r.status !== "UP TO DATE"), ok = list.filter((r) => r.status === "UP TO DATE");
    return (bad.length ? '<ul class="rows">' + bad.map(bulkRow).join("") + "</ul>" : "") +
      (ok.length ? '<details class="okfold"' + (a.issues ? "" : " open") + "><summary>" + ok.length + ' up to date</summary><ul class="rows">' + ok.map(bulkRow).join("") + "</ul></details>" : "");
  };
  const ORDER = { "Portfolio data": 0, "Card data": 1 };
  const groups = vendors.map((v) => {
    const mine = rows.filter((r) => r.vendor === v).sort((x, y) => BULK_RANK[x.status] - BULK_RANK[y.status] || (x.label || x.table).localeCompare(y.label || y.table));
    const kinds = Array.from(new Set(mine.map((r) => r.section || ""))).sort((x, y) => (x in ORDER ? ORDER[x] : 9) - (y in ORDER ? ORDER[y] : 9));
    const body = kinds.length > 1
      ? kinds.map((k) => { const part = mine.filter((r) => (r.section || "") === k), fresh = part.filter((r) => r.status === "UP TO DATE").length;
          return '<div class="subgroup"><h5 class="subhead"><span>' + esc(k) + "</span><span>" + fresh + " of " + part.length + " up to date</span></h5>" + block(part) + "</div>"; }).join("")
      : block(mine);
    return '<div><h4 class="group">' + esc(v) + " \u00B7 " + plural(mine.length, "table", "tables") + "</h4>" + body + "</div>";
  }).join("");
  return '<section class="card sec" aria-labelledby="h-bulk">' + head + "<div>" + big + "</div>" + groups + "</section>";
}
const flagChip = (label, on) => '<span class="flag' + (on ? " on" : "") + '">' + esc(label) + ": " + (on ? "On" : "Off") + "</span>";
const pctText = (n) => Number(n).toFixed(1) + "%";
function streamPanel(label, on, eligible, streamed, missing) {
  if (!on) return '<div class="panel off"><h4>' + esc(label) + '</h4><div class="d">Push to the activity stream is off, so these are not monitored.</div></div>';
  if (!eligible) return '<div class="panel"><h4>' + esc(label) + '</h4><div class="pbig">0</div><div class="d">Nothing eligible in the last ' + STREAM.windowDays + " days.</div></div>";
  const pending = Math.max(eligible - streamed - missing, 0);
  const w = (n) => (100 * n / eligible).toFixed(2);
  return '<div class="panel"><h4>' + esc(label) + '</h4><div class="pbig">' + streamed + " of " + eligible + ' <span class="pl">streamed</span></div>' +
    '<div class="pbar"><i class="g" style="width:' + w(streamed) + '%"></i><i class="y" style="width:' + w(pending) + '%"></i><i class="r" style="width:' + w(missing) + '%"></i></div>' +
    '<div class="d' + (missing ? " bad" : "") + '">' + (missing ? missing + " never streamed (older than " + STREAM.slaHours + "h)" : "None missing") + "</div>" +
    (pending ? '<div class="d">' + pending + " still inside the " + STREAM.slaHours + "h window</div>" : "") + "</div>";
}
const fact = (label, value, bad) => '<div class="fact"><div class="fl">' + esc(label) + '</div><div class="fv' + (bad ? " bad" : "") + '">' + value + "</div></div>";
const whenText = (iso) => (iso ? esc(etTime(iso)) + " (" + esc(ago(iso)) + ")" : "None");
function streamCard(t, title) {
  const a = areaOf(t, "streaming"), s = t.stream;
  const head = '<div><div class="sec-head"><h3 class="title" id="h-streaming">' + esc(title) + "</h3>" + (a.state === "notset" ? "" : pill(a.state, a.label)) + "</div>" +
    '<p class="sub">Checked ' + esc(ago(META.generated)) + " \u00B7 last " + STREAM.windowDays + " days</p></div>";
  const flags = s ? '<div class="flags">' + flagChip("Chat push", s.pushChat) + flagChip("Post push", s.pushPost) + "</div>" : "";
  const wrap = (body) => '<section class="card sec" aria-labelledby="h-streaming">' + head + body + "</section>";
  if (a.state === "notset") {
    const why = !s ? "CRM streaming is not set up for this tenant." : !s.hasCrm ? "No CRM vendor is set up for this tenant." : "Push to the activity stream is off for both chats and posts.";
    return wrap(flags + '<div class="d">' + esc(why) + "</div>");
  }
  if (a.state === "nodata") return wrap('<div><div class="big">No activity</div><div class="cap">Nothing to stream and no stream records in the last ' + STREAM.windowDays + " days. This is not counted as a problem.</div></div>" + flags);
  const eligible = (s.pushChat ? s.chatEligible : 0) + (s.pushPost ? s.feedEligible : 0);
  const streamed = (s.pushChat ? s.chatStreamed : 0) + (s.pushPost ? s.feedStreamed : 0);
  const missing = (s.pushChat ? s.chatMissing : 0) + (s.pushPost ? s.feedMissing : 0);
  const reasons = [];
  if (s.stuck) reasons.push(plural(s.stuck, "stream record is", "stream records are") + " still unprocessed after " + STREAM.slaHours + "h (" + pctText(s.slaMissPct) + " of those due)");
  if (s.failed) reasons.push(plural(s.failed, "record has", "records have") + " run out of retries");
  if (missing) reasons.push(plural(missing, "chat or feed item", "chat and feed items") + " never reached the stream (" + pctText(s.missingPct) + " of those due)");
  const alert = reasons.length ? '<div class="alertbox ' + (a.state === "failing" ? "bad" : "warn") + '"><ul>' + reasons.map((r) => "<li>" + esc(r) + "</li>").join("") + "</ul></div>" : "";
  const cls = a.state === "failing" ? "bad" : a.state === "healthy" ? "ok" : "";
  const big = eligible ? '<div class="big ' + cls + '">' + streamed + " of " + eligible + '</div><div class="cap">chats and feed posts streamed</div>'
    : '<div class="big ' + cls + '">' + s.processed + " of " + s.streamRows + '</div><div class="cap">stream records delivered</div>';
  const stale = s.oldest && Date.now() - Date.parse(s.oldest) > STREAM.slaHours * 3600000;
  const facts = '<div><h4 class="group">Stream records</h4><div class="facts">' +
    fact("Records", s.streamRows) + fact("Processed", s.processed) +
    fact("Out of retries", s.failed, s.failed > 0) + fact("Stuck over " + STREAM.slaHours + "h", s.stuck, s.stuck > 0) +
    fact("SLA miss", pctText(s.slaMissPct), s.slaMissPct > 0) + fact("Never streamed", pctText(s.missingPct), s.missingPct > 0) +
    fact("Oldest unprocessed", whenText(s.oldest), stale) + fact("Last stream record", whenText(s.last)) + "</div></div>";
  return wrap(alert + "<div>" + big + "</div>" + flags + '<div class="panelrow">' +
    streamPanel("Chats", s.pushChat, s.chatEligible, s.chatStreamed, s.chatMissing) + streamPanel("Feeds", s.pushPost, s.feedEligible, s.feedStreamed, s.feedMissing) + "</div>" + facts);
}
const placeholder = (a) => '<section class="card sec ph" aria-labelledby="h-' + esc(a.key) + '"><h3 class="title" id="h-' + esc(a.key) + '">' + esc(a.title) + "</h3></section>";

function summaryText(t) {
  const conn = connItems(t).map((i) => i.name + ": " + (i.state === "disconnected" ? "disconnected (" + reasonOf(i) + (i.expiry ? ", " + relative(i.expiry) : "") + ")" : i.state === "legacy" ? (i.group === "arch" ? "enabled, not verified" : "not checked") : "connected"));
  const bulk = (t.bulk || []).filter((r) => r.status !== "UP TO DATE").map((r) => r.vendor + " " + (r.label || r.table) + ": " + r.status.toLowerCase() + (r.behind ? " (" + r.behind + "d behind)" : ""));
  const st = t.stream, live = st && areaOf(t, "streaming").state !== "notset" && areaOf(t, "streaming").state !== "nodata";
  const streamLines = live ? [
    "Chats: " + (st.pushChat ? st.chatStreamed + " of " + st.chatEligible + " streamed, " + st.chatMissing + " never streamed" : "push off"),
    "Feeds: " + (st.pushPost ? st.feedStreamed + " of " + st.feedEligible + " streamed, " + st.feedMissing + " never streamed" : "push off"),
    "Stream records: " + st.processed + " of " + st.streamRows + " processed, " + st.failed + " out of retries, " + st.stuck + " stuck over " + STREAM.slaHours + "h"] : [];
  return [t.tenant + " (ID " + t.tenantId + ")",
    "Status: " + (t.overall === "attention" ? "needs attention (" + t.issues + ")" : t.overall === "none" ? "no integrations configured" : t.overall)]
    .concat(AREAS.map((a) => { const s = areaOf(t, a.key);
      return a.title + ": " + (s.state === "notset" ? "not set up" : (s.label || STATE[s.state].label).toLowerCase() + (s.summary ? " (" + s.summary + ")" : "")) +
        (a.key === "connection" && conn.length ? "\n  " + conn.join("\n  ") : "") + (a.key === "bulk" && bulk.length ? "\n  " + bulk.join("\n  ") : "") +
        (a.key === "streaming" && streamLines.length ? "\n  " + streamLines.join("\n  ") : ""); }))
    .concat(["Source: Tenant Health report (" + META.env + "), generated " + new Date(META.generated).toLocaleString()]).join("\n");
}
function copyText(text) {
  const fallback = () => { const a = document.createElement("textarea"); a.value = text; a.style.position = "fixed"; a.style.opacity = "0";
    document.body.appendChild(a); a.select(); try { document.execCommand("copy"); } catch (e) { /* ignore */ } a.remove(); return Promise.resolve(); };
  return navigator.clipboard && window.isSecureContext ? navigator.clipboard.writeText(text).catch(fallback) : fallback();
}

function showDetail(i) {
  const t = DATA[i];
  if (!$("#listView").hidden) listScroll = window.scrollY;
  $("#listView").hidden = true;
  const d = $("#detailView");
  d.hidden = false;
  d.dataset.i = i;
  d.innerHTML = '<nav class="crumbs" aria-label="Breadcrumb"><a href="#" id="back">Tenants</a><span class="sep">/</span><span class="here">' + esc(t.tenant) + "</span></nav>" +
    '<header class="card head"><div><h2>' + esc(t.tenant) + notInApp(t) + '</h2><p class="meta">ID ' + esc(t.tenantId) + " \u00B7 " + esc(META.env) + " \u00B7 Last checked " + esc(ago(META.generated)) +
    '</p></div><div class="head-actions">' + overallPill(t) + '<button class="btn" id="dcopy" type="button">Copy summary</button></div></header>' +
    '<div class="grid">' + AREAS.map((a) => (a.key === "connection" ? connectionCard(t, a.title) : a.key === "bulk" ? bulkCard(t, a.title) : a.key === "streaming" ? streamCard(t, a.title) : placeholder(a))).join("") + "</div>";
  document.title = t.tenant + " \u00B7 Tenant Health";
  window.scrollTo(0, 0);
}
function showList() {
  const was = !$("#detailView").hidden;
  $("#detailView").hidden = true;
  $("#listView").hidden = false;
  document.title = "Tenant Health";
  renderList();
  if (was) window.scrollTo(0, listScroll);
}
const hashFor = (t) => (t.tenantId && t.tenantId !== "\u2014" ? "#t-" + encodeURIComponent(t.tenantId) : "#i-" + t._i);
function route() {
  let idx = -1, m = location.hash.match(/^#t-(.+)$/);
  if (m) idx = DATA.findIndex((t) => t.tenantId === decodeURIComponent(m[1]));
  else if ((m = location.hash.match(/^#i-(\d+)$/))) idx = Number(m[1]) < DATA.length ? Number(m[1]) : -1;
  if (idx > -1) showDetail(idx); else showList();
}

/* ---------- header, notes, filters ---------- */
function paintUpdated() {
  const stale = Date.now() - Date.parse(META.generated) > 26 * 3600 * 1000;
  const when = new Date(META.generated).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  $("#updated").innerHTML = '<span class="' + (stale ? "stale" : "") + '" title="' + esc(when) + '">Updated ' + esc(ago(META.generated)) + "</span>" +
    (stale ? ' <span class="stale">\u00B7 this report may be out of date</span>' : "") + " \u00B7 " + esc(when);
}
function setup() {
  $("#env").textContent = META.env;
  $("#excluded").textContent = "Excluded tenants: " + META.excluded.join(", ") + ".";
  $("#streamhelp").textContent = "CRM Health checks that chats and feed posts for CRM users reach the CRM activity stream over the last " + STREAM.windowDays +
    " days. An item gets " + STREAM.slaHours + " hours to stream before it counts as missing. Failing means " + STREAM.failPct + "% or more of the due stream records are still unprocessed, or " + STREAM.failPct +
    "% or more of the due chats and feeds never streamed (once there are at least " + STREAM.minVolume + " due items). Any smaller miss shows Degraded. Tenants with CRM push turned off show Not set up, and tenants with nothing to stream show No activity. Neither counts as a problem.";
  $("#searchicon").innerHTML = svg(PATH.search, 16);
  $("#export").innerHTML = svg(PATH.download, 16) + "Export CSV";
  paintUpdated();
  setInterval(paintUpdated, 60000);

  const opts = (g) => Array.from(new Set(DATA.reduce((a, t) => a.concat((t[g.key] || []).map((i) => i.name)), []))).sort()
    .map((v) => '<option value="' + g.key + ":" + esc(v) + '">' + esc(v) + "</option>").join("");
  $("#vendor").innerHTML = '<option value="all">All integrations</option>' + GROUPS.map((g) => { const o = opts(g); return o ? '<optgroup label="' + esc(g.title) + '">' + o + "</optgroup>" : ""; }).join("");
  $("#sort").innerHTML = '<option value="attention">Needs attention first</option><option value="name">Name (A to Z)</option><option value="name-desc">Name (Z to A)</option><option value="id">Tenant ID</option>' +
    builtAreas.map((a) => '<option value="area:' + esc(a.key) + '">' + esc(a.title) + " (worst first)</option>").join("") + '<option value="" hidden>Custom</option>';

  const parts = [];
  const u = META.unmatched || [];
  if (u.length) {
    const names = u.slice(0, 6).map((x) => esc(x.tenant) + " (" + esc(x.tenantId) + ")").join(", ");
    parts.push("<strong>" + u.length + (u.length === 1 ? " connector tenant has" : " connector tenants have") + " no active match in the app database (TenantDetail):</strong> " + names +
      (u.length > 6 ? ", and " + (u.length - 6) + " more" : "") + ". If this looks wrong, check that tenant_mdm.tenant_id lines up with TenantDetail.TenantId.");
  }
  (META.notes || []).forEach((n) => parts.push(esc(n)));
  if (parts.length) {
    $("#notes").innerHTML = parts.map((p) => "<p>" + p + "</p>").join("");
    $("#notesbtn").innerHTML = svg(PATH.bell, 16) + 'Notes <span class="n">' + parts.length + "</span>";
    $("#notesbtn").hidden = false;
  }
}

/* ---------- CSV export (respects the current search and filters) ---------- */
function exportCsv() {
  const safe = (v) => (/^[=+\-@\t\r]/.test(String(v)) ? "'" + v : v);
  const areaTitle = (k) => (AREAS.find((a) => a.key === k) || { title: k }).title;
  const rows = [["tenant_id", "tenant", "area", "group", "item", "state", "status", "reason", "refresh_token_expiry", "connected_by", "last_updated", "days_behind", "source_table", "section"]];
  const titleOf = {}; GROUPS.forEach((g) => { titleOf[g.key] = g.title; });
  DATA.filter(matchesBase).filter((t) => view.status === "all" || effStatus(t) === view.status).forEach((t) => {
    const items = connItems(t), bulk = t.bulk || [];
    if (!items.length && !bulk.length) rows.push([t.tenantId, t.tenant, areaTitle("connection"), "", "", "none", "No integrations configured", "", "", "", "", "", "", ""]);
    items.forEach((i) => rows.push([t.tenantId, t.tenant, areaTitle("connection"), titleOf[i.group], i.name, i.state, i.status || "", i.reason || "", i.expiry || "", i.by || "", "", "", "", ""]));
    bulk.forEach((r) => rows.push([t.tenantId, t.tenant, areaTitle("bulk"), r.vendor, r.label || r.table, r.status.toLowerCase().replace(/ /g, "_"), "", "", "", "", r.last || "", r.behind == null ? "" : r.behind, r.table, r.section || ""]));
    const sa = areaOf(t, "streaming");
    if (sa.state !== "notset") rows.push([t.tenantId, t.tenant, areaTitle("streaming"), "", "CRM streaming", (t.stream ? t.stream.status : "").toLowerCase().replace(/ /g, "_"), sa.summary || "", streamProblems(t), "", "", t.stream && t.stream.last ? t.stream.last : "", "", "", ""]);
  });
  const csv = "\ufeff" + rows.map((r) => r.map((v) => '"' + String(safe(v)).replace(/"/g, '""') + '"').join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = "tenant-health-" + META.env.toLowerCase() + "-" + String(META.generated).slice(0, 10) + ".csv";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

/* ---------- events ---------- */
function refilter() { view.shown = 50; renderList(); }
$("#q").addEventListener("input", (e) => { view.q = e.target.value; refilter(); });
$("#vendor").addEventListener("change", (e) => { view.vendor = e.target.value; refilter(); });
$("#sort").addEventListener("change", (e) => { if (e.target.value) { view.sort = e.target.value; refilter(); } });
$("#export").addEventListener("click", exportCsv);
$("#notesbtn").addEventListener("click", () => { $("#notes").hidden = !$("#notes").hidden; });
$("#seg").addEventListener("click", (e) => { const b = e.target.closest("[data-status]"); if (b) { view.status = b.dataset.status; refilter(); } });
$("#list").addEventListener("click", (e) => {
  if (e.target.closest("[data-more]")) { view.shown += 50; renderList(); return; }
  const th = e.target.closest("[data-sort]");
  if (th) {
    const k = th.dataset.sort;
    if (k === "name") view.sort = view.sort === "name" ? "name-desc" : "name";
    else view.sort = view.sort === k ? k + ":rev" : k;
    refilter();
    return;
  }
  const r = e.target.closest("tr[data-i]");
  if (r) location.hash = hashFor(DATA[Number(r.dataset.i)]);
});
$("#list").addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches("tr[data-i]")) { e.preventDefault(); location.hash = hashFor(DATA[Number(e.target.dataset.i)]); }
});
$("#detailView").addEventListener("click", (e) => {
  const c = e.target.closest("#dcopy");
  if (c) copyText(summaryText(DATA[Number($("#detailView").dataset.i)])).then(() => { c.textContent = "Copied"; setTimeout(() => { c.textContent = "Copy summary"; }, 1500); });
});
document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey && !$("#listView").hidden) { e.preventDefault(); $("#q").focus(); $("#q").select(); }
  else if (e.key === "Escape") {
    if (!$("#detailView").hidden) location.hash = "";
    else if (document.activeElement === $("#q") && $("#q").value) { $("#q").value = ""; view.q = ""; refilter(); }
  }
});
window.addEventListener("hashchange", route);

setup();
route();
</script>
</body>
</html>
"""


def build_html(tenants: list[dict], env: str, generated: datetime, notes: list[str] | None = None) -> str:
    unmatched = [{"tenant": t["tenant"], "tenantId": t["tenantId"]} for t in tenants if not t["inApp"]]
    # "</" is escaped so data can never close the <script> tag early
    payload = json.dumps(tenants, ensure_ascii=False).replace("</", "<\\/")
    meta = json.dumps(
        {"env": env, "generated": generated.isoformat(), "unmatched": unmatched,
         "excluded": sorted(EXCLUDED_TENANT_IDS), "notes": notes or [],
         "areas": [{"key": key, "title": title} for key, title in AREAS],
         "groups": [{"key": key, "title": title} for key, title in CONNECTION_GROUPS],
         "streaming": {"windowDays": STREAM_WINDOW_DAYS, "slaHours": STREAM_SLA_HOURS,
                       "failPct": STREAM_FAIL_PCT, "minVolume": STREAM_MIN_VOLUME}}, ensure_ascii=False
    ).replace("</", "<\\/")
    # one pass: data can never collide with a placeholder
    head, rest = TEMPLATE.split("__DATA__", 1)
    middle, tail = rest.split("__META__", 1)
    return head + payload + middle + meta + tail


# --------------------------------------------------------------------------
# Slack (optional)
# --------------------------------------------------------------------------
def tenant_line(t: dict) -> str:
    parts = []
    bad = [
        f"{i['name']} ({i.get('reason') or 'disconnected'})"
        for key in GROUP_KEYS for i in t[key] if i["state"] == "disconnected"
    ]
    if bad:
        parts.append("Connection: " + ", ".join(bad))
    b = t["areas"]["bulk"]
    if b["state"] in ("failing", "degraded"):
        bits = [f"{b[k]} {label}" for k, label in (("stale", "stale"), ("late", "late"), ("nodata", "no data")) if b.get(k)]
        parts.append("Performance data: " + ", ".join(bits))
    st = t["areas"]["streaming"]
    if st["state"] in ("failing", "degraded"):
        parts.append("CRM streaming: " + st["summary"])
    return f"\u2022 *{t['tenant']}*: " + "; ".join(parts)


def post_to_slack(tenants: list[dict], summary: dict, env: str, html_path: Path | None) -> None:
    from slack_sdk import WebClient  # imported here so --demo needs no Slack deps

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    channel = os.environ["SLACK_CHANNEL"]

    bad = [t for t in tenants if t["overall"] == "attention"]
    icon = ":white_check_mark:" if not bad else ":warning:"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Integration Status: {env}"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Tenants needing attention*\n{summary['attention']} of {summary['tenants']}"},
                {"type": "mrkdwn", "text": f"*Healthy tenants*\n{summary['healthy']}"},
                {"type": "mrkdwn", "text": f"*CRM disconnected*\n{summary['crm_disconnected']} of {summary['crm_connected'] + summary['crm_disconnected']}"},
                {"type": "mrkdwn", "text": f"*Performance disconnected*\n{summary['perf_disconnected']} of {summary['perf_connected'] + summary['perf_disconnected']}"},
                {"type": "mrkdwn", "text": f"*Performance data not fresh*\n{summary['bulk_attention']} tenants"},
                {"type": "mrkdwn", "text": f"*CRM streaming not healthy*\n{summary['stream_attention']} tenants"},
            ],
        },
    ]
    if bad:
        lines, used = [], 0
        for t in bad:
            line = tenant_line(t)
            if used + len(line) > 2700 or len(lines) >= 15:
                lines.append(f"...and {len(bad) - len(lines)} more tenants")
                break
            lines.append(line)
            used += len(line) + 1
        blocks += [{"type": "divider"}, {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
    notes = []
    if summary["none"]:
        notes.append(f":information_source: {summary['none']} tenant(s) have no CRM or performance vendor set up.")
    if summary["unmatched"]:
        notes.append(f":grey_question: {summary['unmatched']} connector tenant(s) have no active match in the app database.")
    if notes:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " ".join(notes)}]})

    client.chat_postMessage(
        channel=channel,
        text=f"{icon} Integration status {env}: {summary['attention']} tenant(s) need attention",
        blocks=blocks,
    )
    if html_path is not None:
        client.files_upload_v2(
            channel=channel,
            file=str(html_path),
            title=f"Integration status report ({env})",
            initial_comment="Download and open in a browser for the interactive report.",
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Combined CRM + performance vendor status report")
    ap.add_argument("env", nargs="?", help="Environment (e.g. FYNANCIAL). Defaults to ENVIRONMENT.")
    ap.add_argument("--out", help="Output HTML path (default: status-report-<env>-<date>.html)")
    ap.add_argument("--slack", action="store_true", help="Post a summary to Slack")
    ap.add_argument("--no-file", action="store_true", help="With --slack: post the summary only, no file upload")
    ap.add_argument("--demo", action="store_true", help="Use fake data (no databases needed)")
    ap.add_argument("--print-sql", choices=["crm", "performance", "tenants", "bulk", "streaming"],
                    help="Print a query to paste into Beekeeper / SSMS, then exit")
    args = ap.parse_args()

    if args.print_sql:
        print({"crm": CRM_SQL, "performance": PERFORMANCE_SQL, "tenants": TENANT_NAMES_SQL, "bulk": BULK_FRESHNESS_SQL, "streaming": CRM_STREAMING_SQL}[args.print_sql].strip())
        return 0

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    stream_notes: list[str] = []
    if args.demo:
        env = "DEMO"
        crm_raw, perf_raw, app_names, active_rows, bulk_raw, stream_raw = demo_data()
    else:
        env = (args.env or os.environ.get("ENVIRONMENT") or "").strip().upper()
        if not env:
            print("No environment given and ENVIRONMENT is not set.")
            return 1
        crm_raw, app_names = fetch_crm(env)
        active_rows = fetch_tenant_vendors(env)
        perf_raw = fetch_performance(env)
        bulk_raw = fetch_bulk(env)
        try:
            stream_raw = fetch_stream(env)
        except Exception as exc:  # noqa: BLE001 - the rest of the report is still useful without this area
            stream_raw = []
            stream_notes.append(f"CRM Health could not be loaded, so it shows as not set up: {type(exc).__name__}: {str(exc)[:300]}")
            print(f"[{env}] WARNING: {stream_notes[-1]}")

    perf_rows, tv_notes, tv_info = apply_tenant_vendors(
        prepare_performance(perf_raw), app_names, active_rows
    )
    for line in tv_info + tv_notes:
        print(f"[{env}] {line}")
    tenants = build_tenants(prepare_crm(crm_raw), perf_rows, app_names, prepare_bulk(bulk_raw), prepare_stream(stream_raw))
    summary = summarize(tenants)
    now = datetime.now(timezone.utc)

    out = Path(args.out or f"status-report-{env.lower()}-{now:%Y%m%d}.html")
    out.write_text(build_html(tenants, env, now, tv_notes + stream_notes), encoding="utf-8")
    print(
        f"[{env}] {summary['tenants']} tenants ({summary['none']} with no integrations): {summary['attention']} need attention, {summary['healthy']} healthy | "
        f"CRM {summary['crm_disconnected']} disconnected, performance {summary['perf_disconnected']} disconnected, "
        f"performance data {summary['bulk_attention']} not fresh, CRM streaming {summary['stream_attention']} need attention -> {out}"
    )

    if summary["unmatched"]:
        names = ", ".join(f"{t['tenant']} ({t['tenantId']})" for t in tenants if not t["inApp"])
        print(f"[{env}] WARNING: {summary['unmatched']} connector tenant(s) have no active TenantDetail match: {names[:300]}")
        perf_tenants = {t["tenantId"] for t in tenants if t["perf"]}
        if perf_tenants and summary["unmatched"] / len(perf_tenants) > 0.5:
            print(
                f"[{env}] Most connector tenants didn't match. Check that tenant_vendor_mapping.tenant_id points "
                "at tenant_mdm.id and that tenant_mdm.tenant_id equals TenantDetail.TenantId."
            )

    if args.slack:
        post_to_slack(tenants, summary, env, None if args.no_file else out)
        print(f"[{env}] posted to Slack")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - CLI entry point
        print(f"Report failed: {type(exc).__name__}: {exc}")
        sys.exit(1)