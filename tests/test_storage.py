import json

from app import storage


def test_update_json_merges_and_removes(tmp_path):
    path = tmp_path / "a.json"
    storage.write_json_atomic(path, {"a": 1, "b": 2})

    assert storage.update_json(path, {"b": 3, "c": 4}, remove=("a",)) is True
    assert json.loads(path.read_text()) == {"b": 3, "c": 4}


def test_update_json_skips_unchanged_files(tmp_path):
    path = tmp_path / "a.json"
    storage.update_json(path, {"a": 1})
    mtime = path.stat().st_mtime_ns

    assert storage.update_json(path, {"a": 1}) is False
    assert path.stat().st_mtime_ns == mtime  # flash storage is spared


def test_update_json_replaces_corrupt_file(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{broken")

    assert storage.update_json(path, {"a": 1}) is True
    assert json.loads(path.read_text()) == {"a": 1}


def test_atomic_write_leaves_no_temp_files_behind(tmp_path):
    path = tmp_path / "invoice.pdf"
    storage.write_bytes_atomic(path, b"%PDF-1.4")

    assert path.read_bytes() == b"%PDF-1.4"
    assert [p.name for p in tmp_path.iterdir()] == ["invoice.pdf"]


def test_read_json_handles_missing_and_non_dict_content(tmp_path):
    assert storage.read_json(tmp_path / "missing.json") == {}

    (tmp_path / "list.json").write_text("[1, 2]")
    assert storage.read_json(tmp_path / "list.json") == {}
