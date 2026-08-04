#!/usr/bin/env python
"""
Unit test cho PaastAnalysisService.compute_scores — đối chiếu với 4 case minh hoạ
ở PAAST_Business_Logic_Rules v1.1 mục 10.

Chạy: python tests/test_paast_scoring.py
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

import django
django.setup()

from video_management.services.paast_analysis_service import PaastAnalysisService


def _prefer(primary=0, secondary=0):
    total = primary + secondary
    off = 6 - total
    return (
        [{"status": "primary"}] * primary
        + [{"status": "secondary"}] * secondary
        + [{"status": "off"}] * off
    )


def _six(pass_count):
    return [{"status": "pass"}] * pass_count + [{"status": "miss"}] * (6 - pass_count)


def _stick(pass_count_of_2):
    text = [{"status": "pass"}] * pass_count_of_2 + [{"status": "miss"}] * (2 - pass_count_of_2)
    na = [{"status": "na"}] * 4
    return text + na


CASES = [
    # (name, prefer(primary,secondary), action_pass, acknowledge_pass, stick_pass_of_2, trust_pass, expected_total)
    ("Case A — Content mạnh đều", (2, 0), 4, 3, 1, 2, 60),
    ("Case B — Rất mạnh 4 lớp, thiếu hẳn Stick", (2, 0), 4, 3, 0, 2, 50),
    ("Case C — Prefer chỉ có secondary", (0, 2), 4, 3, 1, 2, 44),
    ("Case D — Content yếu toàn diện", (0, 0), 1, 1, 0, 0, 7),
]


def run():
    failures = []
    for name, (primary, secondary), action_p, ack_p, stick_p, trust_p, expected_total in CASES:
        classification = {
            "prefer": _prefer(primary, secondary),
            "action": _six(action_p),
            "acknowledge": _six(ack_p),
            "stick": _stick(stick_p),
            "trust": _six(trust_p),
        }
        result = PaastAnalysisService.compute_scores(classification)
        actual_total = result["total_score"]
        status_str = "PASS" if actual_total == expected_total else "FAIL"
        if status_str == "FAIL":
            failures.append(name)
        print(f"[{status_str}] {name}: total={actual_total} (expected {expected_total}) "
              f"| prefer={result['prefer']['score']} action={result['action']['score']} "
              f"acknowledge={result['acknowledge']['score']} stick={result['stick']['score']} "
              f"trust={result['trust']['score']}")

    print()
    if failures:
        print(f"FAILED: {len(failures)}/{len(CASES)} case(s) — {failures}")
        sys.exit(1)
    else:
        print(f"OK: tất cả {len(CASES)} case khớp business doc §10.")


if __name__ == "__main__":
    run()
