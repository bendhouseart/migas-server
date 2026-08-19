"""Streaming, lossless telemetry exports."""

import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import func, select

from .connections import gen_session
from .models import Crumb, GeoLoc, User


# Crumb columns retain their established names for compatibility. Joined fields
# are prefixed where names would otherwise be ambiguous.
TELEMETRY_EXPORT_COLUMNS = [
    'idx',
    'project',
    'version',
    'language',
    'language_version',
    'timestamp',
    'session_id',
    'user_id',
    'status',
    'status_desc',
    'error_type',
    'error_desc',
    'is_ci',
    'params',
    'user_idx',
    'joined_user_id',
    'user_type',
    'platform',
    'container',
    'geoloc_idx',
    'joined_geoloc_idx',
    'asn',
    'asn_org',
    'continent_code',
    'country_code',
    'state_province_name',
    'city_name',
    'lat',
    'lon',
]


def _csv_value(value: Any) -> Any:
    """Convert structured values without flattening or losing information."""
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _write_row(buffer: io.StringIO, writer: Any, values: list[Any]) -> str:
    buffer.seek(0)
    buffer.truncate(0)
    writer.writerow(values)
    return buffer.getvalue()


ParamPath = tuple[str, ...]


def _flatten_param_paths(value: Any, prefix: ParamPath = ()) -> set[ParamPath]:
    """Return leaf paths; arrays remain leaf values for one TSV cell."""
    if not isinstance(value, dict):
        return {prefix} if prefix else set()
    if not value:
        return {prefix} if prefix else set()

    paths: set[ParamPath] = set()
    for key, child in value.items():
        paths.update(_flatten_param_paths(child, (*prefix, key)))
    return paths


def _expanded_param_columns(paths: list[ParamPath]) -> list[tuple[str, ParamPath]]:
    """Map JSON leaf paths to unique TSV headers while keeping names readable."""
    used = set(TELEMETRY_EXPORT_COLUMNS)
    headers_by_path: dict[ParamPath, str] = {}

    # Reserve aliases for paths that collide with the fixed telemetry schema
    # before processing ordinary keys. This guarantees, for example, that a
    # parameter named ``project`` is always exported as ``params.project``.
    unique_paths = set(paths)
    ordered_paths = sorted(unique_paths, key=lambda path: ('.'.join(path) not in used, path))
    for path in ordered_paths:
        path_name = '.'.join(path)
        header = f'params.{path_name}' if path_name in used else path_name
        while header in used:
            header = f'params.{header}'
        used.add(header)
        headers_by_path[path] = header

    return [(headers_by_path[path], path) for path in sorted(headers_by_path)]


def _param_value(params: Any, path: ParamPath) -> Any:
    value = params
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _apply_export_filters(
    query,
    project: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    version: str | None = None,
):
    query = query.where(Crumb.project == project)
    if start is not None:
        query = query.where(Crumb.timestamp >= start)
    if end is not None:
        query = query.where(Crumb.timestamp <= end)
    if version is not None:
        query = query.where(Crumb.version == version)
    return query


def _telemetry_export_query(
    project: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    version: str | None = None,
):
    """Build the stable, flattened crumb/user/geolocation export query."""
    query = _apply_export_filters(
        select(
            Crumb.idx.label('idx'),
            Crumb.project.label('project'),
            Crumb.version.label('version'),
            Crumb.language.label('language'),
            Crumb.language_version.label('language_version'),
            Crumb.timestamp.label('timestamp'),
            Crumb.session_id.label('session_id'),
            Crumb.user_id.label('user_id'),
            Crumb.status.label('status'),
            Crumb.status_desc.label('status_desc'),
            Crumb.error_type.label('error_type'),
            Crumb.error_desc.label('error_desc'),
            Crumb.is_ci.label('is_ci'),
            Crumb.params.label('params'),
            User.idx.label('user_idx'),
            User.user_id.label('joined_user_id'),
            User.user_type.label('user_type'),
            User.platform.label('platform'),
            User.container.label('container'),
            User.geoloc_idx.label('geoloc_idx'),
            GeoLoc.idx.label('joined_geoloc_idx'),
            GeoLoc.asn.label('asn'),
            GeoLoc.asn_org.label('asn_org'),
            GeoLoc.continent_code.label('continent_code'),
            GeoLoc.country_code.label('country_code'),
            GeoLoc.state_province_name.label('state_province_name'),
            GeoLoc.city_name.label('city_name'),
            GeoLoc.lat.label('lat'),
            GeoLoc.lon.label('lon'),
        )
        .select_from(Crumb)
        .outerjoin(User, Crumb.user_id == User.user_id)
        .outerjoin(GeoLoc, User.geoloc_idx == GeoLoc.idx)
        .order_by(Crumb.timestamp.asc(), Crumb.idx.asc()),
        project,
        start=start,
        end=end,
        version=version,
    )
    return query.execution_options(yield_per=5000, stream_results=True)


async def _parameter_paths(
    session,
    project: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    version: str | None = None,
) -> list[ParamPath]:
    """Discover JSON leaf paths in the export range without retaining its rows."""
    query = _apply_export_filters(
        select(Crumb.params).where(func.jsonb_typeof(Crumb.params) == 'object'),
        project,
        start=start,
        end=end,
        version=version,
    ).execution_options(yield_per=5000, stream_results=True)
    paths: set[ParamPath] = set()
    result = await session.stream_scalars(query)
    async for params in result:
        paths.update(_flatten_param_paths(params))
    return sorted(paths)


async def stream_telemetry_tsv(
    project: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    version: str | None = None,
) -> AsyncIterator[str]:
    """Stream joined telemetry as TSV, one database row at a time."""
    async with gen_session() as session:
        param_columns = _expanded_param_columns(
            await _parameter_paths(session, project, start=start, end=end, version=version)
        )
        columns = TELEMETRY_EXPORT_COLUMNS + [header for header, _path in param_columns]
        buffer = io.StringIO(newline='')
        writer = csv.writer(buffer, delimiter='\t', lineterminator='\n', quoting=csv.QUOTE_MINIMAL)
        yield _write_row(buffer, writer, columns)

        query = _telemetry_export_query(project, start=start, end=end, version=version)
        result = await session.stream(query)
        async for row in result:
            params = row._mapping['params']
            expanded_values = [
                _csv_value(_param_value(params, path)) for _header, path in param_columns
            ]
            yield _write_row(
                buffer,
                writer,
                [_csv_value(row._mapping[column]) for column in TELEMETRY_EXPORT_COLUMNS]
                + expanded_values,
            )
