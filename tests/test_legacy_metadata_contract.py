import json

from bucket_manager import BucketManager


async def test_load_bucket_normalizes_legacy_datetime_metadata(tmp_path):
    manager = BucketManager({"buckets_dir": str(tmp_path / "vault")})
    bucket_path = tmp_path / "legacy.md"
    bucket_path.write_text(
        """---
id: legacy-datetime
type: dynamic
created: 2024-01-01T12:34:56
updated_at: 2024-01-02
last_active: 2024-01-03T04:05:06
tags:
  - legacy
nested:
  happened_at: 2024-01-04T07:08:09
  list:
    - 2024-01-05
---

legacy body
""",
        encoding="utf-8",
    )

    bucket = manager._load_bucket(str(bucket_path))

    metadata = bucket["metadata"]
    assert metadata["created"] == "2024-01-01T12:34:56"
    assert metadata["updated_at"] == "2024-01-02"
    assert metadata["last_active"] == "2024-01-03T04:05:06"
    assert metadata["nested"]["happened_at"] == "2024-01-04T07:08:09"
    assert metadata["nested"]["list"] == ["2024-01-05"]
    json.dumps(metadata, ensure_ascii=False)
