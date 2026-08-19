import asyncio

from bucket_manager import BucketManager


def run(awaitable):
    return asyncio.run(awaitable)


def test_meaning_metadata_round_trip(tmp_path):
    manager = BucketManager({"buckets_dir": str(tmp_path)})

    bucket_id = run(
        manager.create(
            content="我们在雨后散步。",
            domain=["关系"],
            meaning="  因为那一刻我觉得很安心。  ",
        )
    )

    bucket = run(manager.get(bucket_id))
    assert bucket["metadata"]["meaning"] == ["因为那一刻我觉得很安心。"]

    assert run(manager.update(bucket_id, meaning_append="我后来仍会想起雨声。"))
    assert run(manager.update(bucket_id, meaning_append="我后来仍会想起雨声。"))
    bucket = run(manager.get(bucket_id))
    assert bucket["metadata"]["meaning"] == [
        "因为那一刻我觉得很安心。",
        "我后来仍会想起雨声。",
    ]

    assert run(manager.update(bucket_id, meaning=["这份意义被重新整理过。", ""]))
    bucket = run(manager.get(bucket_id))
    assert bucket["metadata"]["meaning"] == ["这份意义被重新整理过。"]

    assert run(manager.update(bucket_id, meaning=[]))
    bucket = run(manager.get(bucket_id))
    assert "meaning" not in bucket["metadata"]
