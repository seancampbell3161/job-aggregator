from unittest.mock import MagicMock

import pytest

from src.tailor.endpoint.storage import LocalFileStorage, S3Storage


def test_local_file_storage_roundtrip(tmp_path):
    s = LocalFileStorage(root=str(tmp_path), base_url="/tailored")
    assert s.exists("j1.pdf") is False
    s.put("j1.pdf", b"%PDF-1.4", "application/pdf")
    assert s.exists("j1.pdf") is True
    assert (tmp_path / "j1.pdf").read_bytes() == b"%PDF-1.4"
    assert s.url("j1.pdf", 900) == "/tailored/j1.pdf"


def test_local_get_roundtrip(tmp_path):
    s = LocalFileStorage(root=str(tmp_path))
    assert s.get("nope.json") is None
    s.put("a.json", b'{"x":1}', "application/json")
    assert s.get("a.json") == b'{"x":1}'


def test_s3_storage_delegates_to_boto3():
    s3 = MagicMock()
    bucket = "my-bucket"
    storage = S3Storage(s3, bucket)

    # exists() -> True when head_object succeeds
    s3.head_object.return_value = {}
    assert storage.exists("j1.pdf") is True
    s3.head_object.assert_called_with(Bucket=bucket, Key="tailored/j1.pdf")

    # exists() -> False when head_object raises
    s3.head_object.side_effect = Exception("NoSuchKey")
    assert storage.exists("j1.pdf") is False
    s3.head_object.side_effect = None

    # put() delegates to put_object with correct args
    storage.put("j1.pdf", b"%PDF", "application/pdf")
    s3.put_object.assert_called_with(
        Bucket=bucket, Key="tailored/j1.pdf", Body=b"%PDF", ContentType="application/pdf"
    )

    # url() calls generate_presigned_url and returns its value
    s3.generate_presigned_url.return_value = "https://signed.example/j1.pdf"
    result = storage.url("j1.pdf", 900)
    s3.generate_presigned_url.assert_called_with(
        "get_object", Params={"Bucket": bucket, "Key": "tailored/j1.pdf"}, ExpiresIn=900
    )
    assert result == "https://signed.example/j1.pdf"

    # get() -> bytes when get_object succeeds
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"%PDF-bytes")}
    assert storage.get("j1.pdf") == b"%PDF-bytes"
    s3.get_object.assert_called_with(Bucket=bucket, Key="tailored/j1.pdf")

    # get() -> None when get_object raises
    s3.get_object.side_effect = Exception("NoSuchKey")
    assert storage.get("j1.pdf") is None
