"""Tests for GET /api/usage/{project} — weeks param and backward cache extension."""

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient


@pytest.mark.anyio
async def test_usage_api_weeks_param_limits_response(client: TestClient, db):
    """?weeks=1 returns only rows within the past week; ?weeks=4 returns up to 4 weeks."""
    from datetime import datetime, timezone, timedelta
    from migas.server.tests.conftest import USER_A, SESSION_1, SESSION_2

    project = 'test/api-weeks-limit'
    await db.register(project)
    auth = await db.token(project)

    now = datetime.now(timezone.utc)

    # Crumb inside 1-week window
    await db.crumb(
        project,
        status='C',
        session_id=SESSION_1,
        user_id=USER_A,
        timestamp=now - timedelta(days=3),
    )
    # Crumb outside 1-week window but inside 4-week window
    await db.crumb(
        project,
        status='C',
        session_id=SESSION_2,
        user_id=USER_A,
        timestamp=now - timedelta(days=20),
        ensure_user=False,
    )

    res1 = client.get(f'/api/usage/{project}?weeks=1', headers=auth)
    assert res1.status_code == 200
    dates1 = [r['date'] for r in res1.json()]
    cutoff1 = (now - timedelta(weeks=1)).date().isoformat()
    assert all(d >= cutoff1 for d in dates1), f'weeks=1 returned rows older than {cutoff1}'

    res4 = client.get(f'/api/usage/{project}?weeks=4', headers=auth)
    assert res4.status_code == 200
    dates4 = [r['date'] for r in res4.json()]
    cutoff4 = (now - timedelta(weeks=4)).date().isoformat()
    assert any(d < cutoff1 for d in dates4), 'weeks=4 should include rows older than 1 week'
    assert all(d >= cutoff4 for d in dates4), f'weeks=4 returned rows older than {cutoff4}'


@pytest.mark.anyio
async def test_usage_api_backward_extension_only_queries_gap(client: TestClient, db, monkeypatch):
    """Second request for more weeks only queries the gap, not the already-cached range."""
    from datetime import datetime, timezone, timedelta
    from migas.server.tests.conftest import USER_A, SESSION_1

    project = 'test/api-backward-ext'
    await db.register(project)
    auth = await db.token(project)

    await db.crumb(
        project,
        status='C',
        session_id=SESSION_1,
        user_id=USER_A,
        timestamp=datetime.now(timezone.utc) - timedelta(days=3),
    )

    # Seed the cache with weeks=1
    res1 = client.get(f'/api/usage/{project}?weeks=1', headers=auth)
    assert res1.status_code == 200

    # Intercept get_viz_data calls on the second request (weeks=2)
    from migas.server.api import routes

    calls = []
    original = routes.get_viz_data

    async def tracking_get_viz_data(project_name, start_ts=None, end_ts=None, session=None):
        calls.append({'start_ts': start_ts, 'end_ts': end_ts})
        return await original(project_name, start_ts=start_ts, end_ts=end_ts, session=session)

    monkeypatch.setattr(routes, 'get_viz_data', tracking_get_viz_data)

    res2 = client.get(f'/api/usage/{project}?weeks=2', headers=auth)
    assert res2.status_code == 200

    # The backward extension call must have start_ts approximately 2 weeks ago
    # (not 2000-01-01 — not a full rescan)
    two_weeks_ago = datetime.now(timezone.utc) - timedelta(weeks=2)
    backward_calls = [c for c in calls if c['end_ts'] is not None]
    assert len(backward_calls) >= 1
    earliest_start = min(c['start_ts'] for c in backward_calls)
    assert earliest_start > two_weeks_ago - timedelta(days=1), (
        f'Backward extension scanned too far back: {earliest_start}'
    )


@pytest.mark.anyio
async def test_usage_api_oldest_date_migration(client: TestClient, db):
    """Cache entries without oldest_date are handled gracefully."""
    import json
    import os
    import redis as sync_redis
    from datetime import datetime, timezone, timedelta
    from migas.server.cache import historical_key

    project = 'test/api-migration'
    await db.register(project)
    auth = await db.token(project)

    # Manually write a legacy cache entry (no oldest_date) using sync redis
    # (avoids event-loop mismatch with the async singleton)
    uri = os.getenv('MIGAS_REDIS_URI', 'redis://:@localhost:6379')
    r = sync_redis.from_url(uri, decode_responses=True)
    legacy = {
        'last_date': (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat(),
        'data': [{'date': '2026-04-01', 'version': '1.0.0', 'status': 'C', 'count': 5}],
    }
    r.set(historical_key(project), json.dumps(legacy))
    r.close()

    res = client.get(f'/api/usage/{project}?weeks=1', headers=auth)
    assert res.status_code == 200
    # Should not crash; oldest_date derived from min(data[*].date)
    # Legacy row (2026-04-01) is outside the 1-week window — must not appear
    dates = [r['date'] for r in res.json()]
    assert '2026-04-01' not in dates, 'Legacy row outside weeks=1 window should be excluded'


@pytest.mark.anyio
async def test_usage_api_since_returns_only_older_rows(client: TestClient, db):
    """?since=<date> returns only rows strictly older than <date> — the delta
    the client is missing when it already holds data back to <date>."""
    from datetime import datetime, timezone, timedelta
    from migas.server.tests.conftest import USER_A, SESSION_1, SESSION_2

    project = 'test/api-since-delta'
    await db.register(project)
    auth = await db.token(project)

    now = datetime.now(timezone.utc)

    # One crumb the client already has (~3 days old)
    await db.crumb(
        project,
        status='C',
        session_id=SESSION_1,
        user_id=USER_A,
        timestamp=now - timedelta(days=3),
    )
    # One crumb older than the `since` boundary (~20 days)
    await db.crumb(
        project,
        status='C',
        session_id=SESSION_2,
        user_id=USER_A,
        timestamp=now - timedelta(days=20),
        ensure_user=False,
    )

    # Client has data back to 7 days ago; asks for the gap between weeks=4 and there
    since = (now - timedelta(days=7)).date().isoformat()
    res = client.get(f'/api/usage/{project}?weeks=4&since={since}', headers=auth)
    assert res.status_code == 200
    dates = [r['date'] for r in res.json()]
    assert dates, 'delta should include the 20-day-old row'
    assert all(d < since for d in dates), f'delta contained rows >= {since}: {dates}'


@pytest.mark.anyio
async def test_usage_api_response_cache(client: TestClient, db, monkeypatch):
    """A repeat request will use the cache instead of calling get_viz_data.

    Different (weeks, since) combinations key separately, so they each go to the DB once.
    """
    from datetime import datetime, timezone, timedelta
    from migas.server.tests.conftest import USER_A, SESSION_1
    from migas.server.api import routes

    project = 'test/api-response-cache'
    await db.register(project)
    auth = await db.token(project)

    await db.crumb(
        project,
        status='C',
        session_id=SESSION_1,
        user_id=USER_A,
        timestamp=datetime.now(timezone.utc) - timedelta(days=3),
    )

    calls = []
    original = routes.get_viz_data

    async def tracking_get_viz_data(project_name, start_ts=None, end_ts=None, session=None):
        calls.append({'start_ts': start_ts, 'end_ts': end_ts})
        return await original(project_name, start_ts=start_ts, end_ts=end_ts, session=session)

    monkeypatch.setattr(routes, 'get_viz_data', tracking_get_viz_data)

    res1 = client.get(f'/api/usage/{project}?weeks=1', headers=auth)
    assert res1.status_code == 200
    first_call_count = len(calls)
    assert first_call_count >= 1, 'cold request should hit the DB at least once'

    # Now the cache should be used
    res2 = client.get(f'/api/usage/{project}?weeks=1', headers=auth)
    assert res2.status_code == 200
    assert res2.json() == res1.json()
    assert len(calls) == first_call_count, (
        f'cached request should not call get_viz_data; got {len(calls) - first_call_count} extra'
    )

    # Different cache key (weeks=2) — must go to DB again.
    res3 = client.get(f'/api/usage/{project}?weeks=2', headers=auth)
    assert res3.status_code == 200
    assert len(calls) > first_call_count, 'distinct (weeks) cache key should miss and query'


@pytest.mark.anyio
async def test_usage_export_tsv_returns_raw_crumbs_for_range(client: TestClient, db):
    """TSV export returns joined DB rows, filtered by timestamp range and active version."""
    from datetime import datetime, timezone, timedelta
    from uuid import uuid4

    from migas.server.tests.conftest import USER_A, USER_B, SESSION_1, SESSION_2, SESSION_3

    project = f'test/api-export-{uuid4()}'
    await db.register(project)
    auth = await db.token(project)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    await db.crumb(
        project,
        status='C',
        status_desc='done',
        session_id=SESSION_1,
        user_id=USER_A,
        timestamp=now - timedelta(days=1),
        version='1.0.0',
    )
    await db.crumb(
        project,
        status='F',
        session_id=SESSION_2,
        user_id=USER_B,
        timestamp=now - timedelta(days=1),
        version='2.0.0',
    )
    await db.crumb(
        project,
        status='C',
        session_id=SESSION_3,
        user_id=USER_A,
        timestamp=now - timedelta(days=10),
        version='1.0.0',
        ensure_user=False,
    )

    res = client.get(
        f'/api/usage-export/{project}',
        params={
            'start': (now - timedelta(days=2)).isoformat(),
            'end': (now + timedelta(hours=1)).isoformat(),
            'version': '1.0.0',
        },
        headers=auth,
    )

    assert res.status_code == 200
    assert res.headers['content-type'].startswith('text/tab-separated-values')
    assert f'filename="migas-{project.replace("/", "-")}-' in res.headers['content-disposition']

    rows = list(csv.DictReader(io.StringIO(res.text), delimiter='\t'))
    assert len(rows) == 1
    row = rows[0]
    assert row['project'] == project
    assert row['version'] == '1.0.0'
    assert row['status'] == 'C'
    assert row['status_desc'] == 'done'
    assert row['session_id'] == SESSION_1
    assert row['params'] == ''
    assert row['joined_user_id'] == USER_A
    assert row['platform'] == 'Linux-x86_64'
    assert row['joined_geoloc_idx'] == ''


@pytest.mark.anyio
async def test_usage_export_tsv_returns_all_joinable_offline_telemetry(client: TestClient, db):
    """An unbounded export includes crumb, user, geolocation, and structured params fields."""
    from datetime import datetime, timezone
    from uuid import uuid4

    from sqlalchemy import insert

    from migas.server.connections import gen_session
    from migas.server.export import TELEMETRY_EXPORT_COLUMNS
    from migas.server.models import GeoLoc

    project = f'test/api-export-joined-{uuid4()}'
    user_id = str(uuid4())
    session_id = str(uuid4())
    city = f'Offline-{uuid4()}'
    await db.register(project)
    auth = await db.token(project)

    async with gen_session() as session:
        geoloc_idx = (
            await session.execute(
                insert(GeoLoc)
                .values(
                    asn=64512,
                    asn_org='Offline Telemetry Network',
                    continent_code='NA',
                    country_code='US',
                    state_province_name='New York',
                    city_name=city,
                    lat=40.7128,
                    lon=-74.006,
                )
                .returning(GeoLoc.idx)
            )
        ).scalar_one()

    await db.user(
        user_id,
        user_type='general',
        platform='Linux-aarch64',
        container='apptainer',
        geoloc_idx=geoloc_idx,
    )
    params = {'iam': 'newparam', 'input': {'modality': 'T1w'}, 'flags': ['offline', 'batch']}
    await db.crumb(
        project,
        status='C',
        status_desc='offline complete',
        session_id=session_id,
        user_id=user_id,
        timestamp=datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc),
        version='2.1.0',
        params=params,
        ensure_user=False,
    )

    res = client.get(f'/api/usage-export/{project}', headers=auth)

    assert res.status_code == 200
    assert res.headers['cache-control'] == 'no-store'
    assert res.headers['x-content-type-options'] == 'nosniff'
    assert res.headers['content-disposition'].endswith(
        f'filename="migas-{project.replace("/", "-")}-all.tsv"'
    )
    reader = csv.DictReader(io.StringIO(res.text), delimiter='\t')
    assert reader.fieldnames == TELEMETRY_EXPORT_COLUMNS + [
        'params.flags',
        'params.iam',
        'params.input.modality',
    ]
    rows = list(reader)
    assert len(rows) == 1
    row = rows[0]
    assert row['project'] == project
    assert row['user_id'] == user_id
    assert row['joined_user_id'] == user_id
    assert row['user_idx']
    assert row['user_type'] == 'general'
    assert row['platform'] == 'Linux-aarch64'
    assert row['container'] == 'apptainer'
    assert row['geoloc_idx'] == str(geoloc_idx)
    assert row['joined_geoloc_idx'] == str(geoloc_idx)
    assert row['asn'] == '64512'
    assert row['asn_org'] == 'Offline Telemetry Network'
    assert row['continent_code'] == 'NA'
    assert row['country_code'] == 'US'
    assert row['state_province_name'] == 'New York'
    assert row['city_name'] == city
    assert row['lat'] == '40.7128'
    assert row['lon'] == '-74.006'
    assert json.loads(row['params']) == params
    assert json.loads(row['params.flags']) == ['offline', 'batch']
    assert row['params.iam'] == 'newparam'
    assert row['params.input.modality'] == 'T1w'


@pytest.mark.anyio
async def test_usage_export_tsv_empty_project_returns_header(client: TestClient, db):
    from uuid import uuid4

    from migas.server.export import TELEMETRY_EXPORT_COLUMNS

    project = f'test/api-export-empty-{uuid4()}'
    await db.register(project)
    auth = await db.token(project)

    res = client.get(f'/api/usage-export/{project}', headers=auth)

    assert res.status_code == 200
    reader = csv.DictReader(io.StringIO(res.text), delimiter='\t')
    assert reader.fieldnames == TELEMETRY_EXPORT_COLUMNS
    assert list(reader) == []


@pytest.mark.anyio
async def test_usage_export_tsv_preserves_crumb_without_user(client: TestClient, db):
    from datetime import datetime, timezone
    from uuid import uuid4

    project = f'test/api-export-anonymous-{uuid4()}'
    await db.register(project)
    auth = await db.token(project)
    await db.crumb(
        project,
        status='R',
        session_id=str(uuid4()),
        user_id=None,
        timestamp=datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc),
    )

    res = client.get(f'/api/usage-export/{project}', headers=auth)

    assert res.status_code == 200
    rows = list(csv.DictReader(io.StringIO(res.text), delimiter='\t'))
    assert len(rows) == 1
    assert rows[0]['user_id'] == ''
    assert rows[0]['joined_user_id'] == ''
    assert rows[0]['joined_geoloc_idx'] == ''


@pytest.mark.anyio
async def test_usage_export_tsv_rejects_reversed_range(client: TestClient, db):
    from uuid import uuid4

    project = f'test/api-export-range-{uuid4()}'
    await db.register(project)
    auth = await db.token(project)

    res = client.get(
        f'/api/usage-export/{project}',
        params={'start': '2026-07-21T00:00:00Z', 'end': '2026-07-20T00:00:00Z'},
        headers=auth,
    )

    assert res.status_code == 400
    assert res.json()['detail'] == 'start must be before end.'


@pytest.mark.anyio
async def test_usage_export_tsv_respects_project_access(client: TestClient, db):
    """Scoped tokens cannot export another project's raw crumbs."""
    from datetime import datetime, timezone, timedelta

    allowed_project = 'test/api-export-auth-allowed'
    blocked_project = 'test/api-export-auth-blocked'
    await db.register(allowed_project)
    await db.register(blocked_project)
    auth = await db.token(allowed_project)

    now = datetime.now(timezone.utc)
    res = client.get(
        f'/api/usage-export/{blocked_project}',
        params={'start': (now - timedelta(days=1)).isoformat(), 'end': now.isoformat()},
        headers=auth,
    )

    assert res.status_code == 403


@pytest.mark.anyio
async def test_usage_api_cold_cache_no_epoch_scan(client: TestClient, db, monkeypatch):
    """On a cold cache, no DB query should scan from epoch — all start_ts must be bounded."""
    from datetime import datetime, timezone, timedelta
    from migas.server.tests.conftest import USER_A, SESSION_1
    from migas.server.api import routes

    project = 'test/api-cold-cache-bounded'
    await db.register(project)
    auth = await db.token(project)

    await db.crumb(
        project,
        status='C',
        session_id=SESSION_1,
        user_id=USER_A,
        timestamp=datetime.now(timezone.utc) - timedelta(days=3),
    )

    calls = []
    original = routes.get_viz_data

    async def tracking_get_viz_data(project_name, start_ts=None, end_ts=None, session=None):
        calls.append({'start_ts': start_ts, 'end_ts': end_ts})
        return await original(project_name, start_ts=start_ts, end_ts=end_ts, session=session)

    monkeypatch.setattr(routes, 'get_viz_data', tracking_get_viz_data)

    # Cold cache — no prior request, no Redis seed
    res = client.get(f'/api/usage/{project}?weeks=1', headers=auth)
    assert res.status_code == 200

    one_week_ago = datetime.now(timezone.utc) - timedelta(weeks=1)
    for call in calls:
        if call['start_ts'] is not None:
            assert call['start_ts'] > one_week_ago - timedelta(days=2), (
                f'Cold cache triggered scan from {call["start_ts"]} — should be bounded to ~1 week ago'
            )
