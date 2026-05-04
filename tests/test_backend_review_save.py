import base64

from backend.main import SaveReviewRequest, review_save


def test_review_save_download_filename_uses_account_number_without_stamped_suffix(monkeypatch):
    monkeypatch.setattr("backend.main.stamp_reviewed_pdf_bytes", lambda **kwargs: b"%PDF stamped")

    request = SaveReviewRequest(
        file_name="uploaded_rendition.pdf",
        file_base64=base64.b64encode(b"%PDF original").decode("ascii"),
        result={},
        final_record={"account_number": "P12345"},
    )

    response = review_save(request)

    assert response.headers["Content-Disposition"] == 'attachment; filename="P12345.pdf"'
