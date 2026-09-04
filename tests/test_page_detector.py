from __future__ import annotations
import io
import time
from PIL import Image
import pytest
import page_detector as pd

@pytest.mark.parametrize("analyzed,flagged,failed,status,score", [
    (0,0,1,"failed",None),
    (0,0,5,"failed",None),
    (1,0,0,"success",100.0),
    (1,0,1,"partial_success",100.0),
    (5,2,2,"partial_success",60.0),
])
def test_page_result_summary(analyzed, flagged, failed, status, score):
    result = pd.page_result_summary(analyzed, flagged, failed)
    assert result["scan_status"] == status
    assert result["page_score"] == score
    if failed and analyzed:
        assert result["confidence"] == "reduced"


def test_zero_success_never_perfect():
    assert pd.page_result_summary(0,0,1)["page_score"] is None


def test_bounded_downloader_deduplicates_urls(monkeypatch):
    calls = []
    def fake(url, referer=None, timeout=20):
        calls.append(url); return url.encode()
    monkeypatch.setattr(pd, "_download_image", fake)
    items = [{"image_url":"https://example.com/a"},{"image_url":"https://example.com/a"},{"image_url":"https://example.com/b"}]
    results, errors = pd._download_images_bounded(items, referer="https://example.com/page", max_workers=3)
    assert not errors
    assert set(results) == {"https://example.com/a","https://example.com/b"}
    assert sorted(calls) == ["https://example.com/a","https://example.com/b"]


def test_bounded_downloader_captures_failure(monkeypatch):
    def fake(url, referer=None, timeout=20):
        if url.endswith("bad"): raise TimeoutError("timeout")
        return b"ok"
    monkeypatch.setattr(pd, "_download_image", fake)
    results, errors = pd._download_images_bounded([{"image_url":"https://example.com/good"},{"image_url":"https://example.com/bad"}], referer=None)
    assert "https://example.com/good" in results
    assert "https://example.com/bad" in errors


def test_bounded_downloader_is_concurrent(monkeypatch):
    def fake(url, referer=None, timeout=20):
        time.sleep(.05); return b"ok"
    monkeypatch.setattr(pd, "_download_image", fake)
    items=[{"image_url":f"https://example.com/{i}"} for i in range(6)]
    start=time.perf_counter(); pd._download_images_bounded(items, referer=None, max_workers=6); elapsed=time.perf_counter()-start
    assert elapsed < .20


def test_download_image_rejects_tiny_icon(monkeypatch):
    image = Image.new("RGB", (50,50), "white"); buf=io.BytesIO(); image.save(buf,format="PNG")
    class R:
        content=buf.getvalue();
        def raise_for_status(self): pass
    monkeypatch.setattr(pd, "safe_get", lambda *a, **k: R())
    with pytest.raises(ValueError, match="too small"):
        pd._download_image("https://example.com/tiny.png")


def test_scan_page_failed_when_all_downloads_fail(monkeypatch):
    scraped={"items":[{"name":"P","image_url":"https://example.com/p.png","product_url":"https://example.com/p"}],"final_url":"https://example.com/cat","page_context":"","dom_verified_count":1,"jsonld_discovered_count":0}
    monkeypatch.setattr(pd,"scrape_product_candidates",lambda *a,**k:scraped)
    monkeypatch.setattr(pd,"filter_product_candidates",lambda items,**k:(items,[],None))
    monkeypatch.setattr(pd,"_download_images_bounded",lambda *a,**k:({}, {"https://example.com/p.png":"timeout"}))
    result=pd.scan_page("https://example.com/cat",max_items=1)
    assert result["analyzed_count"] == 0
    assert result["failed_count"] == 1
    assert result["scan_status"] == "failed"
    assert result["page_score"] is None
