"""Report rendering: layout, masking, color on/off, summary counts."""
from keypool_guard.classify import FailureClass
from keypool_guard.config import GuardConfig, KeyConfig, ProviderConfig
from keypool_guard.pool import KeyPool
from keypool_guard.prober import ProbeResult
from keypool_guard.report import render_report


def build_pool() -> tuple:
    config = GuardConfig(
        providers=[
            ProviderConfig(
                name="mock",
                base_url="http://x/v1",
                model="m",
                keys=[
                    KeyConfig(value="sk-aaa111222333"),
                    KeyConfig(value="sk-bbb444555666", name="backup"),
                ],
            )
        ]
    )
    pool = KeyPool(config)
    provider = config.providers[0]
    pool.apply(
        provider,
        ProbeResult("backup", FailureClass.INVALID, 401, 5.0, "invalid key (HTTP 401)"),
    )
    return pool, "sk-aaa111222333"


def test_render_contains_status_and_summary():
    pool, raw_key = build_pool()
    text = render_report(pool.snapshot(), colored=False)
    assert "PROVIDER" in text and "STATUS" in text
    assert "healthy" in text
    assert "invalid" in text
    assert "healthy 1/2 keys across 1 provider(s)" in text
    assert "invalid key (HTTP 401)" in text


def test_render_never_contains_raw_keys():
    pool, raw_key = build_pool()
    text = render_report(pool.snapshot(), colored=False)
    assert raw_key not in text
    assert "sk-bbb444555666" not in text
    # masked forms are shown instead
    assert "sk-aaa..." in text
    assert "backup" in text


def test_render_no_ansi_when_disabled():
    pool, _ = build_pool()
    assert "\x1b[" not in render_report(pool.snapshot(), colored=False)


def test_render_ansi_when_enabled():
    pool, _ = build_pool()
    text = render_report(pool.snapshot(), colored=True)
    assert "\x1b[31m" in text  # red for the broken key
    assert "\x1b[32m" in text  # green for the summary's healthy part


def test_all_healthy_summary_is_green_plain():
    config = GuardConfig(
        providers=[
            ProviderConfig(
                name="p",
                base_url="http://x/v1",
                model="m",
                keys=[KeyConfig(value="sk-aaa111222333")],
            )
        ]
    )
    pool = KeyPool(config)
    text = render_report(pool.snapshot(), colored=False)
    assert "healthy 1/1 keys across 1 provider(s)" in text
