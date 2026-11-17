"""HTTP-level tests: real uploads through the Flask app, incl. bad input. Run: pytest -q"""
import io, time
from pathlib import Path
import pytest
from app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def client(): return create_app().test_client()


def wait(client, url, timeout=120):
    jid = url.rsplit("/", 1)[-1]; t = time.time()
    while time.time() - t < timeout:
        m = client.get(f"/api/jobs/{jid}").get_json()
        if m["status"] in ("done", "error"): return m
        time.sleep(0.5)
    raise AssertionError("timeout")


def test_cs1_sample_and_download(client):
    r = client.post("/cs1", data={"sample": "1"}); m = wait(client, r.headers["Location"])
    assert m["status"] == "done"
    csv = client.get(f"/jobs/{m['id']}/download/shipments.csv").data.decode("utf-8-sig")
    assert csv.count("\n") >= 17 and "MULTIPLE_PARCELS" in csv and "OK" in csv


def test_cs1_empty_and_garbage(client):
    assert client.post("/cs1", data={}).status_code == 400
    r = client.post("/cs1", data={"files": (io.BytesIO(b"hello"), "x.jpg")}, content_type="multipart/form-data")
    m = wait(client, r.headers["Location"]); csv = client.get(f"/jobs/{m['id']}/download/shipments.csv").data.decode("utf-8-sig")
    assert "IMAGE_CORRUPT" in csv
    r = client.post("/cs1", data={"files": (io.BytesIO(b"nope"), "x.zip")}, content_type="multipart/form-data")
    assert r.status_code == 400 and b"valid zip" in r.data


def test_cs2_sample(client):
    r = client.post("/cs2", data={"sample": "1"}); m = wait(client, r.headers["Location"])
    assert m["status"] == "done" and "verification_report.xlsx" in m["files"]


def test_cs2_bad_inputs(client):
    assert client.post("/cs2", data={}).status_code == 400
    d = {"wagesheet": (io.BytesIO(b"junk"), "w.xlsx"), "pf_ecr": (io.BytesIO(b"%PDF-broken"), "p.pdf")}
    m = wait(client, client.post("/cs2", data=d, content_type="multipart/form-data").headers["Location"])
    assert m["status"] == "error" and m["message"]


def test_cs3_sample_and_bad(client):
    m = wait(client, client.post("/cs3", data={"sample": "1"}).headers["Location"])
    assert m["status"] == "done" and any(f.startswith("digest_") for f in m["files"])
    r = client.post("/cs3", data={"data": (io.BytesIO(b"a,b\n1,2\n"), "bad.csv")}, content_type="multipart/form-data")
    m = wait(client, r.headers["Location"]); assert m["status"] == "error" and "header" in m["message"].lower()
    assert client.post("/cs3", data={}).status_code == 400
