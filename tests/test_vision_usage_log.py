"""VISION_USAGE log line for OpenAI scoreboard reads."""
import logging

from services import vision_extraction as v


def test_full_usage_logs_exact_line(caplog):
    resp = {"usage": {
        "prompt_tokens": 1200, "completion_tokens": 340, "total_tokens": 1540,
        "completion_tokens_details": {"reasoning_tokens": 64},
        "prompt_tokens_details": {"cached_tokens": 1024},
    }}
    with caplog.at_level(logging.INFO, logger=v.log.name):
        v._log_openai_usage(resp, "gpt-5.4-mini")
    lines = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(lines) == 1
    assert lines[0].getMessage() == (
        "VISION_USAGE model=gpt-5.4-mini prompt=1200 completion=340 "
        "reasoning=64 cached=1024 total=1540"
    )


def test_missing_or_odd_usage_logs_zeros_without_raising(caplog):
    zeros = "VISION_USAGE model=m prompt=0 completion=0 reasoning=0 cached=0 total=0"
    for odd in ({}, {"usage": None}, {"usage": {"completion_tokens_details": None}}):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=v.log.name):
            v._log_openai_usage(odd, "m")
        assert [r.getMessage() for r in caplog.records] == [zeros]
    v._log_openai_usage(None, "m")      # not a dict at all: must not raise
    v._log_openai_usage({"usage": {"prompt_tokens": "x"}}, "m")
