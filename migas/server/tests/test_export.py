import csv
import io
import json
from datetime import datetime, timezone

from ..export import (
    TELEMETRY_EXPORT_COLUMNS,
    _csv_value,
    _expanded_param_columns,
    _flatten_param_paths,
    _param_value,
    _write_row,
)


def test_structured_values_remain_in_one_tsv_cell():
    value = {'labels': ['control,group', 'tab\tvalue'], 'nested': {'enabled': True}}
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, delimiter='\t', lineterminator='\n')

    line = _write_row(
        buffer,
        writer,
        ['row-1', _csv_value(value), _csv_value(datetime(2026, 7, 20, tzinfo=timezone.utc))],
    )
    fields = next(csv.reader(io.StringIO(line), delimiter='\t'))

    assert fields[0] == 'row-1'
    assert json.loads(fields[1]) == value
    assert fields[2] == '2026-07-20T00:00:00+00:00'


def test_export_column_names_are_unique():
    assert len(TELEMETRY_EXPORT_COLUMNS) == len(set(TELEMETRY_EXPORT_COLUMNS))


def test_expanded_param_columns_preserve_keys_and_avoid_collisions():
    paths = [('iam',), ('input', 'modality'), ('project',), ('params.project',)]
    assert _expanded_param_columns(paths) == [
        ('iam', ('iam',)),
        ('input.modality', ('input', 'modality')),
        ('params.params.project', ('params.project',)),
        ('params.project', ('project',)),
    ]


def test_nested_params_flatten_objects_but_keep_arrays_as_cells():
    params = {'input': {'modality': 'T1w'}, 'flags': ['offline', 'batch']}

    assert _flatten_param_paths(params) == {('input', 'modality'), ('flags',)}
    assert _param_value(params, ('input', 'modality')) == 'T1w'
    assert _param_value(params, ('flags',)) == ['offline', 'batch']
