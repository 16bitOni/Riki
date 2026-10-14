"""Tests for the PII scanner and content tagging."""

from app.privacy import Finding, has_pii, scan, scrub, tag_untrusted


class TestPIIDetection:
    def test_aadhaar_with_spaces(self):
        findings = scan("my aadhaar is 4999 2760 2163")
        assert any(f.kind == "AADHAAR" for f in findings)

    def test_aadhaar_without_spaces(self):
        findings = scan("aadhaar: 499927602163")
        assert any(f.kind == "AADHAAR" for f in findings)

    def test_pan_card(self):
        findings = scan("PAN is ABCDE1234F")
        assert any(f.kind == "PAN" for f in findings)

    def test_credit_card_with_spaces(self):
        # 4111 1111 1111 1111 is a well-known test Visa number (passes Luhn)
        findings = scan("card: 4111 1111 1111 1111")
        assert any(f.kind == "CARD" for f in findings)

    def test_credit_card_without_spaces(self):
        findings = scan("card: 4111111111111111")
        assert any(f.kind == "CARD" for f in findings)

    def test_invalid_card_fails_luhn(self):
        findings = scan("card: 1234567890123456")
        assert not any(f.kind == "CARD" for f in findings)

    def test_indian_phone(self):
        findings = scan("call me at +91 9876543210")
        assert any(f.kind == "PHONE" for f in findings)

    def test_indian_phone_bare(self):
        findings = scan("phone: 9876543210")
        assert any(f.kind == "PHONE" for f in findings)

    def test_email(self):
        findings = scan("mail me at test@example.com")
        assert any(f.kind == "EMAIL" for f in findings)

    def test_upi_id(self):
        findings = scan("pay to user@ybl")
        assert any(f.kind == "UPI" for f in findings)

    def test_ifsc(self):
        findings = scan("IFSC: SBIN0001234")
        assert any(f.kind == "IFSC" for f in findings)

    def test_api_key(self):
        findings = scan("my key is sk-abcdefghijklmnopqrstuvwxyz1234")
        assert any(f.kind == "SECRET" for f in findings)

    def test_aws_key(self):
        findings = scan("AWS key: AKIAIOSFODNN7EXAMPLE")
        assert any(f.kind == "SECRET" for f in findings)

    def test_password_mention(self):
        findings = scan("password is hunter2")
        assert any(f.kind == "PASSWORD" for f in findings)

    def test_clean_text_has_no_pii(self):
        assert not has_pii("what's the weather in Kolkata today?")
        assert not has_pii("remind me to call dad at 7pm")

    def test_normal_numbers_not_flagged(self):
        assert not has_pii("I ran 5k today")
        assert not has_pii("the meeting is at 3pm")
        assert not has_pii("slept 7 hours")


class TestScrubbing:
    def test_scrub_replaces_pii(self):
        result = scrub("PAN is ABCDE1234F and phone is 9876543210")
        assert "ABCDE1234F" not in result
        assert "9876543210" not in result
        assert "[REDACTED:PAN]" in result
        assert "[REDACTED:PHONE]" in result

    def test_scrub_preserves_clean_text(self):
        text = "what's the weather in Kolkata?"
        assert scrub(text) == text

    def test_scrub_handles_multiple_findings(self):
        text = "aadhaar 499927602163, pan ABCDE1234F"
        result = scrub(text)
        assert "[REDACTED:" in result
        assert "499927602163" not in result


class TestUntrustedTagging:
    def test_tag_adds_markers(self):
        result = tag_untrusted("email body here")
        assert "EXTERNAL CONTENT" in result
        assert "email body here" in result

    def test_tag_wraps_content(self):
        result = tag_untrusted("test")
        assert result.startswith("\n⚠")
        assert result.endswith("⚠ END EXTERNAL CONTENT\n")
